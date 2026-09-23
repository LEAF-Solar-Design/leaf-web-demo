"""Studio ports of the plugin's rooftop-chain engines: string flip, string swap, the frame-group
operations (create, rename, list, select, delete), the export-settings prompt, the string-data
export and the string rebuild.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s51):

  BranchCmd.cs:18570-18769            FlipString: the start and end markers trade positions
                                      (:18726-18727), the record's from and to handles trade
                                      (:18745-18747), the panel order reverses (:18750-18753)
  BranchCmd.cs:10954-11049            StringSwap: two picks, the same string twice refused (:10974),
  BranchCmd.cs:11340-11423            each pick must be a string (IsAValidString, :11443-11458);
                                      SwapStrings exchanges the label's WHOLE integer record between
                                      the two labels (:11386-11389), the label text (:11371-11374)
                                      and the circuit on the string record (:11391-11406)
  LeafSolarDesign.Core/PanelGrouping.cs:187-300
                                      FrameGroupOps: FindByName, DeleteByName, ExistsByName,
                                      RenameByName and its five outcomes
  LeafSolarDesign.Core/PanelGrouping.cs:72-128
                                      ByFrameGroupNameSelector.Select
  LeafSolarDesign.Core/FrameGroup.cs  the FrameGroup record (Name, FrameHandles, ColorIndex,
                                      LastModifiedTicks)
  Commands.cs:4953-5085               LEAFFRAMEGROUPCREATE: trimmed name, duplicate refusal, the
                                      picked panel-group handles in pick order, ColorIndex 0
  LeafFrameGroupListCommand.cs:40-89  LEAFFRAMEGROUPLIST: the count and each group's frame count
  LeafSelectByFrameGroupCommand.cs:43-154
                                      LEAFSELECTBYFRAMEGROUP: handles resolved, stale ones skipped
  LeafFrameGroupRenameCommand.cs:40-134, LeafFrameGroupDeleteCommand.cs:40-108
  Pvcase/LeafExportSupportCommands.cs:31-43, :160-181, :348-374
                                      the export settings: ten fields, their declared defaults,
                                      the prompt order, PromptDouble and PromptString
  StringHomeRunCmd.cs:153-338         GetStringData and GetStringPanelGroupData: each string's two
                                      ends located in a panel group, the groups sorted by name
  LeafSolarDesign.Core/StringData.cs  the StringData.json shape and the "0.00" coordinate format
  BranchCmd.cs:18255-18354, :18464-18557
                                      STRINGREBUILD: every string vertex re-associated with the
                                      nearest panel centre strictly inside the tolerance, through
                                      the same bucket index, and the rebuilt count

Pure functions over plain data. No CAD host, no I/O, no network. The neutral shapes:

  string       {"handle": str, "panels": [panel handle, ...] (stored order, first = start end),
                "label": {field name: int, ...}, and optionally "circuit": str,
                "label_text": str, "start": marker, "end": marker, "vertices": [[x, y], ...]}
  marker       {"handle": str, "at": [x, y]} (the end marker block and its insertion point)
  panel group  {"handle": str, "name": str, and optionally "panels": [panel handle, ...] and
                "outlines": [[[x, y], ...], ...] (its outline polygons, drawing units); the
                committed rooftop intake carries {handle, name} only
  frame group  {"Name": str, "FrameHandles": [str, ...], "ColorIndex": int,
                "LastModifiedTicks": int}, the plugin's own field names (it is a drawing setting)
  export settings  the ten neutral names of EXPORT_SETTINGS_FIELDS

Every input is bounded and every malformed input fails closed with RooftopInputError (a
ValueError); a bound breach raises RooftopBoundsError. Every pass is linear in its input; the
rebuild's nearest-panel search is the plugin's own 3 x 3 bucket probe, never a scan of all panels.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
import copy
import json
import math
import re
import time

# ------------------------------------------------------------------ bounds --

MAX_STRINGS = 50_000
MAX_PANELS_PER_STRING = 5_000
MAX_PANELS = 500_000
MAX_PANEL_GROUPS = 20_000
MAX_OUTLINE_VERTICES = 100_000
MAX_FRAME_GROUPS = 10_000
MAX_FRAMES_PER_GROUP = 100_000
MAX_LABEL_FIELDS = 64
MAX_NAME_CHARS = 1_024
MAX_VERTICES_PER_STRING = 10_000
MAX_COORDINATE = 1e12   # the "0.00" formatter's decimal context holds 28 digits

# BranchCmd.cs:18508, :18525: the rebuild's bucket size and search tolerance (drawing units).
REBUILD_TOLERANCE = 1.0

# FrameGroupOps.RenameResult (PanelGrouping.cs:246-258).
RENAMED = "Renamed"
NOT_FOUND = "NotFound"
NEW_NAME_ALREADY_EXISTS = "NewNameAlreadyExists"
INVALID_NEW_NAME = "InvalidNewName"
NO_OP_SAME_NAME = "NoOpSameName"

# LeafExportSettings (LeafExportSupportCommands.cs:31-43): the ten fields in prompt order
# (:160-179), neutral names, and the plugin's declared defaults.
EXPORT_SETTINGS_FIELDS = ("module_width", "module_height", "module_x_spacing", "module_y_spacing",
                          "orientation", "tilt", "azimuth", "maintenance_margin", "manufacturer",
                          "product")
EXPORT_SETTINGS_DEFAULTS = {
    "module_width": 0.992,        # ModuleWidthM, :33
    "module_height": 1.640,       # ModuleHeightM, :34
    "module_x_spacing": 0.02,     # ModuleXSpacingM, :35
    "module_y_spacing": 0.02,     # ModuleYSpacingM, :36
    "orientation": 0,             # Orientation, :37 (0 = Landscape, 1 = Portrait)
    "tilt": 15.0,                 # DefaultTiltDeg, :38
    "azimuth": 0.0,               # DefaultAzimuthDeg, :39
    "maintenance_margin": 2.0,    # MaintenanceMarginM, :40
    "manufacturer": "generic",    # ModuleManufacturer, :41
    "product": "generic",         # ModuleName, :42
}
# PromptDouble's AllowNegative per field (:160-175): only tilt and azimuth pass allowNeg: true.
_EXPORT_NEGATIVE_ALLOWED = frozenset({"tilt", "azimuth"})
_EXPORT_TEXT_FIELDS = frozenset({"manufacturer", "product"})

# DateTime ticks at the Unix epoch (100 ns units since 0001-01-01).
_DOTNET_EPOCH_TICKS = 621_355_968_000_000_000

_HANDLE_RE = re.compile(r"[0-9A-Fa-f]{1,16}")
_LABEL_FIELD_RE = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*")
_RESERVED_ROW_FIELDS = frozenset({"id", "type", "quantity", "unit", "string", "kind"})


class RooftopInputError(ValueError):
    """A malformed input: the engine refuses rather than guesses."""


class RooftopBoundsError(RooftopInputError):
    """An input past a declared bound."""


# ------------------------------------------------------------- primitives --

def _num(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RooftopInputError(f"{what} must be a finite number")
    return float(value)


def _xy(value, what):
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise RooftopInputError(f"{what} must be a point of 2 or 3 numbers")
    return (_num(value[0], what), _num(value[1], what))


def _bounded_list(value, limit, what):
    if not isinstance(value, list):
        raise RooftopInputError(f"{what} must be a list")
    if len(value) > limit:
        raise RooftopBoundsError(f"{what} holds {len(value)} items; the bound is {limit}")
    return value


def _text(value, what):
    if not isinstance(value, str):
        raise RooftopInputError(f"{what} must be text")
    if len(value) > MAX_NAME_CHARS:
        raise RooftopBoundsError(f"{what} exceeds {MAX_NAME_CHARS} characters")
    return value


def neutral_handle(value, what="handle"):
    """Rule 8: a drawing handle upper-case with leading zeros stripped ("0" if empty)."""
    if not isinstance(value, str) or not _HANDLE_RE.fullmatch(value):
        raise RooftopInputError(f"{what} {value!r} is not a drawing handle")
    return value.upper().lstrip("0") or "0"


def handle_order(handle):
    """Ascending integer value of a hex handle (G4/G9 neutral-id order)."""
    return int(handle, 16)


def dotnet_utc_ticks(now=None):
    """DateTime.UtcNow.Ticks for `now` (Unix seconds; the clock when None)."""
    seconds = time.time() if now is None else _num(now, "time")
    return _DOTNET_EPOCH_TICKS + int(round(seconds * 10_000_000))


def _is_blank(text):
    """string.IsNullOrWhiteSpace for the text a prompt returns."""
    return text is None or not str(text).strip()


def _net_trim(text):
    return text.strip()


# ---------------------------------------------------------------- strings --

def validate_label(label, what="label"):
    """A label's integer record: {neutral field name: int}. Fails closed on any other shape."""
    if not isinstance(label, dict):
        raise RooftopInputError(f"{what} must be an object of integer fields")
    if len(label) > MAX_LABEL_FIELDS:
        raise RooftopBoundsError(f"{what} holds more than {MAX_LABEL_FIELDS} fields")
    for name, value in label.items():
        if not isinstance(name, str) or not _LABEL_FIELD_RE.fullmatch(name) or name in _RESERVED_ROW_FIELDS:
            raise RooftopInputError(f"{what} field {name!r} is not a neutral name")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RooftopInputError(f"{what} field {name!r} must be an integer")
    return label


def _marker(value, what):
    if not isinstance(value, dict) or set(value) != {"handle", "at"}:
        raise RooftopInputError(f"{what} must be {{handle, at}}")
    return {"handle": neutral_handle(value["handle"], what), "at": list(_xy(value["at"], what))}


_STRING_KEYS = {"handle", "panels", "label"}
_STRING_OPTIONAL = {"circuit", "label_text", "start", "end", "vertices"}


def validate_string(string, what="string"):
    """One neutral string (see the module docstring); returns a normalized deep copy."""
    if not isinstance(string, dict):
        raise RooftopInputError(f"{what} must be an object")
    keys = set(string)
    if not _STRING_KEYS <= keys or keys - _STRING_KEYS - _STRING_OPTIONAL:
        raise RooftopInputError(f"{what} keys must be {sorted(_STRING_KEYS)} plus any of {sorted(_STRING_OPTIONAL)}")
    out = {"handle": neutral_handle(string["handle"], f"{what} handle"),
           "panels": [neutral_handle(p, f"{what} panel")
                      for p in _bounded_list(string["panels"], MAX_PANELS_PER_STRING, f"{what} panels")],
           "label": dict(validate_label(string["label"], f"{what} label"))}
    for key in ("circuit", "label_text"):
        if key in string:
            out[key] = _text(string[key], f"{what} {key}")
    for key in ("start", "end"):
        if key in string:
            out[key] = _marker(string[key], f"{what} {key} marker")
    if "vertices" in string:
        out["vertices"] = [list(_xy(v, f"{what} vertex"))
                           for v in _bounded_list(string["vertices"], MAX_VERTICES_PER_STRING, f"{what} vertices")]
    return out


def string_flip(string):
    """FlipString (BranchCmd.cs:18570-18769) on one string; returns the flipped copy.

    The start marker takes the end marker's position and the end marker the start's
    (:18726-18727), then the record's from and to handles trade (:18745-18747), so the record's
    start end is now the old end marker at the old start position; the panel order reverses
    (:18750-18753), so the new first panel is the old last one. Label and circuit are untouched."""
    s = validate_string(string)
    if "start" in s and "end" in s:
        start_marker, end_marker = s["start"], s["end"]
        start_pos, end_pos = start_marker["at"], end_marker["at"]
        start_marker["at"], end_marker["at"] = end_pos, start_pos
        s["start"], s["end"] = end_marker, start_marker
    elif "start" in s or "end" in s:
        raise RooftopInputError("a string carries both of its end markers or neither")
    s["panels"].reverse()
    return s


def string_swap(first, second):
    """StringSwap then SwapStrings (BranchCmd.cs:10954-11049, :11340-11423) on two strings;
    returns the two swapped copies (first, second).

    Refuses the same string picked twice (:10974). Exchanges the labels' WHOLE integer records
    (:11386-11389: each tag text takes the other's record), the label text (:11371-11374) and the
    circuit on each string record that carries one (:11391-11406). Panels, markers and handles
    stay where they are. Colours trade too (:11365-11368) and are not neutral state."""
    a = validate_string(first, "first string")
    b = validate_string(second, "second string")
    if a["handle"] == b["handle"]:
        raise RooftopInputError("picked the same string twice")
    a["label"], b["label"] = b["label"], a["label"]
    if "label_text" in a or "label_text" in b:
        if "label_text" not in a or "label_text" not in b:
            raise RooftopInputError("both labels carry their text or neither does")
        a["label_text"], b["label_text"] = b["label_text"], a["label_text"]
    if "circuit" in a and "circuit" in b:
        a["circuit"], b["circuit"] = b["circuit"], a["circuit"]
    elif "circuit" in a or "circuit" in b:
        raise RooftopInputError("both string records carry their circuit or neither does")
    return a, b


# ----------------------------------------------------------- frame groups --

def validate_frame_groups(groups):
    """The FrameGroups setting as stored (FrameGroup.cs); None (absent) is its default []."""
    if groups is None:
        return []
    _bounded_list(groups, MAX_FRAME_GROUPS, "FrameGroups")
    out = []
    for g in groups:
        if g is None:
            out.append(None)   # the plugin skips a null entry everywhere (PanelGrouping.cs:200)
            continue
        if not isinstance(g, dict) or not {"Name", "FrameHandles"} <= set(g) or \
                set(g) - {"Name", "FrameHandles", "ColorIndex", "LastModifiedTicks"}:
            raise RooftopInputError("a FrameGroup must carry Name and FrameHandles (and ColorIndex, "
                                    "LastModifiedTicks)")
        name = g["Name"]
        if name is not None:
            _text(name, "FrameGroup name")
        handles = g["FrameHandles"]
        if handles is not None:
            for h in _bounded_list(handles, MAX_FRAMES_PER_GROUP, "FrameGroup handles"):
                _text(h, "FrameGroup handle")
        for key in ("ColorIndex", "LastModifiedTicks"):
            value = g.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RooftopInputError(f"FrameGroup {key} must be an integer")
        out.append({"Name": name, "FrameHandles": None if handles is None else list(handles),
                    "ColorIndex": g.get("ColorIndex", 0), "LastModifiedTicks": g.get("LastModifiedTicks", 0)})
    return out


def _same_name(a, b):
    """string.Equals(a, b, OrdinalIgnoreCase)."""
    return a is not None and b is not None and a.upper() == b.upper() and len(a) == len(b)


def find_by_name(groups, name):
    """FrameGroupOps.FindByName (PanelGrouping.cs:193-205): the index of the first group whose
    name matches case-insensitively, or None."""
    if groups is None or _is_blank(name):
        return None
    for i, g in enumerate(groups):
        if g is None:
            continue
        if _same_name(g["Name"], name):
            return i
    return None


def exists_by_name(groups, name):
    """FrameGroupOps.ExistsByName (PanelGrouping.cs:236-238)."""
    return find_by_name(groups, name) is not None


def delete_by_name(groups, name):
    """FrameGroupOps.DeleteByName (PanelGrouping.cs:213-229): (the list without the first match,
    the removed group) or (the list unchanged, None)."""
    groups = validate_frame_groups(groups)
    if _is_blank(name):
        return groups, None
    i = find_by_name(groups, name)
    if i is None:
        return groups, None
    removed = groups.pop(i)
    return groups, removed


def rename_by_name(groups, old_name, new_name, now_ticks):
    """FrameGroupOps.RenameByName (PanelGrouping.cs:266-299): (outcome, groups). Only RENAMED
    changes the list: the target's Name becomes new_name trimmed and LastModifiedTicks now."""
    groups = validate_frame_groups(groups)
    if _is_blank(new_name):
        return INVALID_NEW_NAME, groups
    if _is_blank(old_name):
        return NOT_FOUND, groups
    if _same_name(old_name, new_name):
        return (NO_OP_SAME_NAME if find_by_name(groups, old_name) is not None else NOT_FOUND), groups
    if find_by_name(groups, new_name) is not None:
        return NEW_NAME_ALREADY_EXISTS, groups
    i = find_by_name(groups, old_name)
    if i is None:
        return NOT_FOUND, groups
    groups[i]["Name"] = _net_trim(new_name)
    groups[i]["LastModifiedTicks"] = int(now_ticks)
    return RENAMED, groups


def frame_group_create(groups, name_answer, picked, panel_group_handles, now_ticks):
    """LEAFFRAMEGROUPCREATE (Commands.cs:4953-5085): (outcome, groups).

    outcome is "cancelled" (a blank name, :4982-4987, or nothing selectable picked, :5017-5022,
    :5038-5043), "duplicate" (:4989-4996) or "created". The selection filter admits only
    panel-group blocks (:5003-5016), so a pick that is not one of `panel_group_handles` is not
    selected; a selection set holds each entity once, in pick order. The new group is appended
    with the trimmed name, the picked handles upper-case, ColorIndex 0 and LastModifiedTicks now."""
    groups = validate_frame_groups(groups)
    if _is_blank(name_answer):
        return "cancelled", groups
    new_name = _net_trim(_text(name_answer, "FrameGroup name"))
    if exists_by_name(groups, new_name):
        return "duplicate", groups
    selectable = {neutral_handle(h, "panel group handle") for h in panel_group_handles}
    seen, handles = set(), []
    for h in _bounded_list(list(picked), MAX_FRAMES_PER_GROUP, "picked handles"):
        n = neutral_handle(h, "picked handle")
        if n in selectable and n not in seen:
            seen.add(n)
            handles.append(n)
    if not handles:
        return "cancelled", groups
    groups.append({"Name": new_name, "FrameHandles": handles, "ColorIndex": 0,
                   "LastModifiedTicks": int(now_ticks)})
    return "created", groups


def frame_group_list(groups):
    """LEAFFRAMEGROUPLIST (LeafFrameGroupListCommand.cs:40-89): the printed values in listed
    order, {"count", "groups": [{"name", "frames"}]}; a null group lists as "(unnamed)" with 0
    frames. The modified time it prints is the host's clock and is not returned."""
    groups = validate_frame_groups(groups)
    listed = []
    for g in groups:
        name = "(unnamed)" if g is None or g["Name"] is None else g["Name"]
        frames = 0 if g is None or g["FrameHandles"] is None else len(g["FrameHandles"])
        listed.append({"name": name, "frames": frames})
    return {"count": len(groups), "groups": listed}


def frame_group_select(groups, name_answer, existing_handles):
    """LEAFSELECTBYFRAMEGROUP (LeafSelectByFrameGroupCommand.cs:43-154) with
    ByFrameGroupNameSelector.Select (PanelGrouping.cs:109-127).

    Returns {"status", "handles", "stale"}: status "no-groups" (:52-60), "cancelled" (:72-78),
    "missing" (:83-91), "empty" (:92-99), "stale" (every handle unresolved, :129-138) or
    "selected". `handles` is the implied selection (the resolved handles in listed order);
    a listed handle resolves when it parses as hex and names an entity in `existing_handles`
    (:109-125)."""
    groups = validate_frame_groups(groups)
    if not groups:
        return {"status": "no-groups", "handles": [], "stale": 0}
    if _is_blank(name_answer):
        return {"status": "cancelled", "handles": [], "stale": 0}
    wanted = _net_trim(name_answer)
    i = find_by_name(groups, wanted)
    if i is None:
        return {"status": "missing", "handles": [], "stale": 0}
    listed = groups[i]["FrameHandles"] or []
    if not listed:
        return {"status": "empty", "handles": [], "stale": 0}
    existing = {neutral_handle(h, "drawing handle") for h in existing_handles}
    resolved, stale = [], 0
    for h in listed:
        if not isinstance(h, str) or not _HANDLE_RE.fullmatch(h) or neutral_handle(h) not in existing:
            stale += 1
            continue
        resolved.append(neutral_handle(h))
    if not resolved:
        return {"status": "stale", "handles": [], "stale": stale}
    return {"status": "selected", "handles": resolved, "stale": stale}


# -------------------------------------------------------- export settings --

def load_export_settings(stored):
    """LeafExportSettingsStore.Read (LeafExportSupportCommands.cs:50-63): absent reads as the
    declared defaults; a stored record gives every field it holds."""
    out = dict(EXPORT_SETTINGS_DEFAULTS)
    if stored is None:
        return out
    if not isinstance(stored, dict) or set(stored) - set(EXPORT_SETTINGS_FIELDS):
        raise RooftopInputError(f"export settings take only {list(EXPORT_SETTINGS_FIELDS)}")
    for key, value in stored.items():
        if key in _EXPORT_TEXT_FIELDS:
            out[key] = _text(value, key)
        elif key == "orientation":
            if isinstance(value, bool) or not isinstance(value, int):
                raise RooftopInputError("orientation must be an integer")
            out[key] = value
        else:
            out[key] = _num(value, key)
    return out


def _prompt_double(answer, current, what, allow_negative):
    """PromptDouble (:348-361): Enter (an empty answer) keeps the current value; zero is allowed;
    a negative answer is refused where AllowNegative is false (the prompt re-asks). Parsed with
    the invariant culture."""
    if answer is None or answer == "":
        return current
    if not isinstance(answer, str) or not re.fullmatch(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*", answer):
        raise RooftopInputError(f"{what} answer {answer!r} is not a number")
    value = float(answer)
    if not math.isfinite(value):
        raise RooftopInputError(f"{what} answer {answer!r} is not finite")
    if value < 0 and not allow_negative:
        raise RooftopInputError(f"{what} does not accept a negative value")
    return value


def export_settings_prompt(current, answers):
    """The export-settings command's ten prompts (LeafExportSupportCommands.cs:160-181) over the
    current settings: returns the settings it saves. `answers` holds one text per prompt in
    order; an empty text takes the prompt's default (the current value). Orientation is
    (int) of the answered double, then 1 stays 1 and anything else is 0 (:168-169)."""
    settings = load_export_settings(current)
    if not isinstance(answers, (list, tuple)) or len(answers) != len(EXPORT_SETTINGS_FIELDS):
        raise RooftopInputError(f"the export settings take exactly {len(EXPORT_SETTINGS_FIELDS)} answers")
    out = dict(settings)
    for field, answer in zip(EXPORT_SETTINGS_FIELDS, answers):
        if field in _EXPORT_TEXT_FIELDS:
            # PromptString (:363-374): an empty result keeps the default.
            text = _text(answer if answer is not None else "", field)
            out[field] = settings[field] if text == "" else text
        elif field == "orientation":
            value = _prompt_double(answer, float(settings[field]), field, False)
            truncated = int(value)   # C# (int) truncates toward zero
            out[field] = 1 if truncated == 1 else 0
        else:
            out[field] = _prompt_double(answer, settings[field], field, field in _EXPORT_NEGATIVE_ALLOWED)
    return out


# ------------------------------------------------------------ string data --

def format_coordinate(value):
    """C# value.ToString("0.00") (StringData.cs:62): the double is first taken to 15 significant
    digits (the custom-format precision), then rounded half away from zero to two decimals.
    A value that rounds to zero prints without a sign."""
    v = _num(value, "coordinate")
    if abs(v) >= MAX_COORDINATE:
        raise RooftopBoundsError(f"coordinate {v!r} is past {MAX_COORDINATE:g} drawing units")
    d = Decimal(v)
    if d != 0:
        exponent = d.adjusted() - 14
        d = d.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_HALF_EVEN)
    q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return f"{q:.2f}"


def _culture_name_key(name):
    """Name.CompareTo(other) under the en-US culture (StringHomeRunCmd.cs:262) for the ASCII
    names panel groups carry: hyphens and apostrophes carry no weight, letters compare
    case-insensitively after punctuation, spaces and digits, and a lower-case letter sorts
    before its upper-case form only as the final tie-break."""
    primary, tertiary = [], []
    for ch in name:
        if ch in "-'":
            continue
        if ch.isalpha():
            primary.append((2, ch.casefold()))
            tertiary.append(0 if ch.islower() else 1)
        elif ch.isdigit():
            primary.append((1, ch))
            tertiary.append(0)
        else:
            primary.append((0, ch))
            tertiary.append(0)
    return (tuple(primary), tuple(tertiary), name)


def _point_inside(x, y, polygon):
    """Even-odd containment of (x, y) in a closed outline polygon."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def validate_panel_groups(panel_groups):
    """[{handle, name, panels?, outlines?}] in drawing order; returns normalized copies."""
    _bounded_list(panel_groups, MAX_PANEL_GROUPS, "panel groups")
    out, seen = [], set()
    for g in panel_groups:
        if not isinstance(g, dict) or not {"handle", "name"} <= set(g) or \
                set(g) - {"handle", "name", "panels", "outlines"}:
            raise RooftopInputError("a panel group is {handle, name} plus optional panels and outlines")
        handle = neutral_handle(g["handle"], "panel group handle")
        if handle in seen:
            raise RooftopInputError(f"panel group {handle} appears twice")
        seen.add(handle)
        item = {"handle": handle, "name": _text(g["name"], "panel group name")}
        if "panels" in g:
            item["panels"] = [neutral_handle(p, "panel group panel")
                              for p in _bounded_list(g["panels"], MAX_PANELS, "panel group panels")]
        if "outlines" in g:
            outlines = []
            for poly in _bounded_list(g["outlines"], MAX_OUTLINE_VERTICES, "panel group outlines"):
                pts = [_xy(v, "outline vertex") for v in _bounded_list(poly, MAX_OUTLINE_VERTICES, "outline")]
                if len(pts) < 3:
                    raise RooftopInputError("an outline needs at least 3 vertices")
                outlines.append(pts)
            item["outlines"] = outlines
        out.append(item)
    return out


def _group_locator(panel_groups):
    """The group an end lies in: by outline containment when the groups carry outlines (the
    plugin's GetStringPanelGroupData, StringHomeRunCmd.cs:313-338, first outline that holds the
    marker), else by the panel at that end belonging to the group's panels.

    A group that carries neither is a group with no outline polyline: the plugin's dictionary
    (:186-195) holds no outline for it, so no end lies in it and every string misses it (:243-246).
    The committed rooftop intake records groups as {handle, name} only, so there every group is
    listed with no strings."""
    with_outlines = [g for g in panel_groups if "outlines" in g]
    if with_outlines:
        def by_point(point, panel):
            for g in with_outlines:
                if any(_point_inside(point[0], point[1], poly) for poly in g["outlines"]):
                    return g["handle"]
            return None
        return by_point
    owner = {}
    for g in panel_groups:
        for p in g.get("panels", ()):
            owner.setdefault(p, g["handle"])

    def by_panel(point, panel):
        return owner.get(panel)
    return by_panel


def string_data(panel_groups, selected_strings):
    """GetStringData (StringHomeRunCmd.cs:153-296): the text of StringData.json, or None when
    the command writes no file.

    Every panel group gets a group entry {handle, name, strings}; a selected string joins the
    group both of its ends lie in (a string whose ends lie in two groups, or in none, joins no
    group, :220-246). A string without both end markers raises inside the plugin's loop, which
    leaves the group list empty (:213-216, :264-267), so no file is written. The groups are sorted
    by name (:262); strings keep the selection order. The serializer writes indented JSON with
    two-space indents, CRLF line ends and no trailing newline (:277-287)."""
    groups = validate_panel_groups(panel_groups)
    strings = [validate_string(s, "selected string") for s in selected_strings]
    if not strings:
        return None   # mStringList.Count == 0: nothing is written (:171)
    if any("start" not in s or "end" not in s for s in strings):
        return None
    locate = _group_locator(groups)
    entries = {g["handle"]: {"handle": g["handle"], "name": g["name"], "strings": []} for g in groups}
    for s in strings:
        start_group = locate(s["start"]["at"], s["panels"][0] if s["panels"] else None)
        end_group = locate(s["end"]["at"], s["panels"][-1] if s["panels"] else None)
        if start_group is not None and start_group == end_group:
            entries[start_group]["strings"].append({
                "handle": s["handle"],
                "startPoint": {"handle": s["start"]["handle"],
                               "coordinate": f"{format_coordinate(s['start']['at'][0])},"
                                             f"{format_coordinate(s['start']['at'][1])}"},
                "endPoint": {"handle": s["end"]["handle"],
                             "coordinate": f"{format_coordinate(s['end']['at'][0])},"
                                           f"{format_coordinate(s['end']['at'][1])}"}})
    ordered = sorted(entries.values(), key=lambda g: _culture_name_key(g["name"]))
    if not ordered:
        return None   # "No Groups found" (:297-299)
    text = json.dumps({"groups": ordered}, indent=2, ensure_ascii=False)
    return text.replace("\n", "\r\n")


# ---------------------------------------------------------- string rebuild --

def _bucket(v, cell):
    return math.floor(v / cell)


def build_panel_index(panel_points, tolerance=REBUILD_TOLERANCE):
    """BuildPanelSearchIndex (BranchCmd.cs:18325-18354): buckets of cell max(1, tolerance), each
    panel in insertion order. panel_points: [(handle, [x, y]), ...] in the drawing's panel order."""
    cell = max(1.0, _num(tolerance, "tolerance"))
    buckets = {}
    for handle, pt in _bounded_list(list(panel_points), MAX_PANELS, "panel points"):
        x, y = _xy(pt, "panel centre")
        buckets.setdefault((_bucket(x, cell), _bucket(y, cell)), []).append((neutral_handle(handle), x, y))
    return {"cell": cell, "buckets": buckets}


def find_panel_near_point(point, index, tolerance=REBUILD_TOLERANCE):
    """FindPanelNearPoint (BranchCmd.cs:18288-18323): the nearest panel centre strictly inside
    the tolerance over the 3 x 3 neighbouring buckets; the first one found wins a tie."""
    if not index["buckets"]:
        return None
    cell = index["cell"]
    x, y = _xy(point, "string vertex")
    bx, by = _bucket(x, cell), _bucket(y, cell)
    best, best_d = None, float("inf")
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for handle, px, py in index["buckets"].get((bx + dx, by + dy), ()):
                d = math.hypot(px - x, py - y)
                if d < tolerance and d < best_d:
                    best, best_d = handle, d
    return best


def string_rebuild(strings, panel_points=None, tolerance=REBUILD_TOLERANCE):
    """STRINGREBUILD with ALL (BranchCmd.cs:18464-18557): (rebuilt strings, rebuilt count).

    A string that carries its polyline vertices gets the panel nearest each vertex, in vertex
    order (:18521-18531); a vertex with no panel inside the tolerance adds nothing. A Studio
    string with no vertices IS its panel association (there is no separate polyline to re-read),
    so its panels stand. Every string counts once (:18539)."""
    rebuilt = [validate_string(s) for s in _bounded_list(list(strings), MAX_STRINGS, "strings")]
    if any("vertices" in s for s in rebuilt):
        if panel_points is None:
            raise RooftopInputError("a rebuild over string vertices needs the panel centres")
        index = build_panel_index(panel_points, tolerance)
        for s in rebuilt:
            if "vertices" in s:
                panels = []
                for v in s["vertices"]:
                    found = find_panel_near_point(v, index, tolerance)
                    if found is not None:
                        panels.append(found)
                s["panels"] = panels
    return rebuilt, len(rebuilt)


# ----------------------------------------------------------------- settings --

def canonical_setting_text(value):
    """G20: a nested setting compares as its canonical JSON text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def frame_groups_without_clock(groups):
    """G27: every FrameGroup's LastModifiedTicks replaced by 0 (wall clock, not state)."""
    out = copy.deepcopy(validate_frame_groups(groups))
    for g in out:
        if g is not None:
            g["LastModifiedTicks"] = 0
    return out
