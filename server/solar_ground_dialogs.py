"""Studio's dialog-batch engines (contract G30): literal ports of the plugin's shading-object
placement, project-area manager and pile-template manager, driven the way the G30 scenario
drives the dialogs.

  e1  LEAFSHADINGOBJECT   the Shading objects form accepted untouched (kind Tree), then the
                          tree centre: canopy circle, restriction ring and label
  e4  LEAFAREAS           "Add Area", then OK: the drawing's project-area record as saved
  e6  LEAFPILETEMPLATES   "+", then OK: the pile-template store file as written, byte for byte

Sources (Branch2025), cited per function:
  Pvcase/ShadingObject.cs                        ShadingObjectParams and its defaults
  Pvcase/ShadingObjectsForm.cs                   LoadFields, CommitAndClose, ParseD
  Pvcase/LeafShadingObjectCommand.cs             PlaceTree, AddCircle, AddLabel
  LeafSolarDesign.Core/Constants/LayerNames.cs   the shading layers
  PvArea.cs, PvAreaStore.cs                      the area record, its envelope and JSON
  Pvcase/PvAreaManagerForm.cs                    RefreshGrid, CommitGrid, AddArea, SaveAndClose
  LeafSolarDesign.Core/PileTemplate.cs           PileTemplate, PileStation, their defaults
  LeafSolarDesign.Core/PileTemplateStore.cs      ReadFromDisk, Normalize, SaveAll, Persist
  Pvcase/PileTemplateManagerForm.cs              LoadSelected, SaveCurrent, AddTemplate, SaveAndClose

Both JSON writers are Newtonsoft.Json's JsonConvert.SerializeObject (compact for the area
record, Formatting.Indented for the template store): properties in declaration order, a double
as .NET's shortest round-trip text with ".0" ensured, two-space indentation with CRLF line
ends, no BOM (File.WriteAllText) and no trailing newline. The store's READ is Newtonsoft's too,
including its populate rule: a list property that already holds a value (RevealBucketBoundariesM
starts as the five default buckets, PileTemplate.cs:92-93) is APPENDED to, never replaced, so
every load of the store grows each template's bucket list by five. The captured before and after
stores show exactly that, and this port reproduces it.

Fails closed: every input is validated, and a shape the plugin's code would treat in a way this
port does not reproduce (a Newtonsoft type coercion, a lenient JSON syntax, a shading kind other
than Tree, PVcase area import) is refused with DialogsInputError, never guessed. All work is
linear in the input; inputs are bounded (MAX_* below).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import math
import re

MAX_STORE_CHARS = 8 * 1024 * 1024    # a store or area record larger than this is refused
MAX_TEMPLATES = 10_000
MAX_LIST_ITEMS = 1_000_000
MAX_AREAS = 10_000
MAX_FORM_MAGNITUDE = 1e15            # "0.##" formatting is ported for |v| below this only
NEWLINE = "\r\n"                     # Environment.NewLine on the plugin host (Windows)


class DialogsInputError(ValueError):
    """A named refusal: the input is outside what the plugin accepts or this port reproduces."""


def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DialogsInputError(f"{what} must be a finite number")
    return float(value)


def _is_blank(text):
    """string.IsNullOrWhiteSpace."""
    return text is None or not text.strip()


def _fold(text):
    """StringComparer.OrdinalIgnoreCase's key: each character upper-cased on its own (a
    character whose upper case is longer than one character keeps itself)."""
    return "".join(ch.upper() if len(ch.upper()) == 1 else ch for ch in text)


def _fold_order(text):
    """OrdinalIgnoreCase ordering: folded UTF-16 code units compared in order."""
    return _fold(text).encode("utf-16-be")


# ------------------------------------------------------- .NET number text --

def format_fixed(value, decimals):
    """.NET `double.ToString("F<decimals>")` (.NET Core 3.0 and later): the exact binary value
    rounded half away from zero to `decimals` places."""
    v = _finite(value, "value")
    try:
        return f"{Decimal(v).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP):f}"
    except InvalidOperation:
        raise DialogsInputError(f"{v!r} is outside the F{decimals} range this port reproduces") from None


def format_custom_decimals(value, max_decimals):
    """.NET `double.ToString("0.##..")` with `max_decimals` optional places: the custom-format
    path takes 15 significant digits first, rounds that digit string half away from zero to
    `max_decimals` places, then drops trailing zeros and a bare point ("-0" for a negative value
    that rounds to zero, as .NET Core 3.0 and later write it)."""
    v = _finite(value, "value")
    if abs(v) >= MAX_FORM_MAGNITUDE:
        raise DialogsInputError(f"{v!r} is outside the custom-format range this port reproduces")
    q = Decimal(format(v, ".14e")).quantize(Decimal(1).scaleb(-max_decimals), rounding=ROUND_HALF_UP)
    text = f"{q:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


_NET_FLOAT = re.compile(r"[\t\n\v\f\r ]*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)[\t\n\v\f\r ]*")
_NET_SYMBOL = re.compile(r"\s*[+-]?(?:infinity|nan|∞)\s*", re.IGNORECASE)


def parse_double(text):
    """`double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture)`: the parsed
    value, or None where TryParse returns false. The symbol spellings (Infinity, NaN) are
    refused: every text this port parses is its own formatted number."""
    if text is None:
        return None
    if not isinstance(text, str):
        raise DialogsInputError("a parsed field must be text")
    m = _NET_FLOAT.fullmatch(text)
    if m:
        return float(m.group(1))
    if _NET_SYMBOL.fullmatch(text):
        raise DialogsInputError(f"{text!r}: the .NET symbol spellings are not ported")
    return None


# ---------------------------------------------------------- Newtonsoft text --

def newtonsoft_double(value):
    """JsonConvert.ToString(double): .NET's shortest round-trip "R" text (scientific when the
    decimal point sits more than 15 digits right of the first digit or more than 3 zeros left
    of it, "E" with a sign and at least two exponent digits), then ".0" appended when the text
    has no '.', 'E' or 'e' (EnsureDecimalPlace)."""
    v = _finite(value, "number")
    if v == 0.0:
        return "-0.0" if math.copysign(1.0, v) < 0 else "0.0"
    tup = Decimal(repr(abs(v))).as_tuple()
    scale = len(tup.digits) + tup.exponent          # value = 0.<digits> x 10^scale
    digits = "".join(str(d) for d in tup.digits).lstrip("0").rstrip("0")
    if scale > 15 or scale < -3:
        exp = scale - 1
        text = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        text += "E" + ("+" if exp >= 0 else "-") + f"{abs(exp):02d}"
    elif scale <= 0:
        text = "0." + "0" * (-scale) + digits
    elif len(digits) <= scale:
        text = digits + "0" * (scale - len(digits))
    else:
        text = digits[:scale] + "." + digits[scale:]
    text = ("-" if v < 0 else "") + text
    return text if any(c in text for c in ".Ee") else text + ".0"


_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r",
                  "\t": "\\t"}


def newtonsoft_string(text):
    """StringEscapeHandling.Default: the short escapes, other control characters and U+0085,
    U+2028, U+2029 as lower-case \\uXXXX, everything else verbatim."""
    out = ['"']
    for ch in text:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif ord(ch) < 0x20 or ch in "\u0085  ":
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def newtonsoft_json(value, indented=False):
    """JsonConvert.SerializeObject of plain values (dicts keep their insertion order, which is
    the declaring class's property order), compact or Formatting.Indented."""
    out = []
    _write_json(value, out, indented, 0)
    return "".join(out)


def _write_json(value, out, indented, depth):
    if value is None:
        out.append("null")
    elif value is True or value is False:
        out.append("true" if value else "false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(newtonsoft_double(value))
    elif isinstance(value, str):
        out.append(newtonsoft_string(value))
    elif isinstance(value, (list, dict)):
        is_dict = isinstance(value, dict)
        opener, closer = ("{", "}") if is_dict else ("[", "]")
        if not value:
            out.append(opener + closer)
            return
        out.append(opener)
        items = value.items() if is_dict else ((None, v) for v in value)
        for i, (key, item) in enumerate(items):
            if i:
                out.append(",")
            if indented:
                out.append(NEWLINE + "  " * (depth + 1))
            if is_dict:
                out.append(newtonsoft_string(key) + (": " if indented else ":"))
            _write_json(item, out, indented, depth + 1)
        if indented:
            out.append(NEWLINE + "  " * depth)
        out.append(closer)
    else:
        raise DialogsInputError(f"{type(value).__name__} is not a JSON value")


class _Obj(list):
    """A JSON object read as its ordered (key, value) pairs, duplicates kept."""


def _refuse_constant(name):
    raise DialogsInputError(f"the JSON constant {name} is not ported")


def _parse_json(text, what):
    """Strict JSON; Newtonsoft's lenient syntax (comments, single quotes, trailing commas) is
    refused rather than guessed."""
    if not isinstance(text, str):
        raise DialogsInputError(f"{what} must be text")
    if len(text) > MAX_STORE_CHARS:
        raise DialogsInputError(f"{what} exceeds {MAX_STORE_CHARS} characters")
    if text.startswith("﻿"):                     # File.ReadAllText drops a UTF-8 BOM
        text = text[1:]
    try:
        return json.loads(text, object_pairs_hook=_Obj, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, DialogsInputError):
            raise
        raise DialogsInputError(f"{what} is not JSON this port reads: {exc}") from None


def _match_property(key, names):
    """Newtonsoft's GetClosestMatchProperty: an exact name first, then OrdinalIgnoreCase."""
    if key in names:
        return key
    folded = _fold(key)
    return next((n for n in names if _fold(n) == folded), None)


def _read_scalar(kind, value, what):
    """A Newtonsoft scalar read without coercion; every coercion is refused."""
    if kind == "str":
        if value is None or isinstance(value, str):
            return value
    elif kind == "bool":
        if isinstance(value, bool):
            return value
    elif kind == "int":
        if isinstance(value, int) and not isinstance(value, bool) and -2**31 <= value < 2**31:
            return value
    elif kind == "double":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    raise DialogsInputError(f"{what}: {value!r} is not a {kind} this port reads without coercion")


def _read_enum(names, value, what):
    """StringEnumConverter: a name (any case) or a defined integer value."""
    if isinstance(value, str):
        match = next((n for n in names if n.lower() == value.strip().lower()), None)
        if match is not None:
            return match
    elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(names):
        return names[value]
    raise DialogsInputError(f"{what}: {value!r} is not one of {names}")


def _read_double_list(existing, value, what):
    """A List<double> property: null sets null; an array is APPENDED to the list the property
    already holds (ObjectCreationHandling.Auto)."""
    if value is None:
        return None
    if not isinstance(value, list) or isinstance(value, _Obj):
        raise DialogsInputError(f"{what} must be an array")
    out = list(existing) if existing is not None else []
    out.extend(_read_scalar("double", v, what) for v in value)
    if len(out) > MAX_LIST_ITEMS:
        raise DialogsInputError(f"{what} exceeds {MAX_LIST_ITEMS} items")
    return out


# ---------------------------------------------------------- shading object --

SHADING_LAYER_PREFIX = "LEAF-PVCASE-SHADING-"                 # LayerNames.cs:47
SHADING_RESTRICTION_LAYER = "LEAF-PVCASE-SHADING-RESTRICTION"  # LayerNames.cs:53
SHADING_TREE_LAYER = SHADING_LAYER_PREFIX + "TREE"             # LeafShadingObjectCommand.cs:179
SHADING_KINDS = ("Tree", "Station", "Fence", "Vegetation")     # ShadingObject.cs:7 (the combo order, ShadingObjectsForm.cs:58)
# ShadingObjectParams (ShadingObject.cs:14-33), the numeric fields in LoadFields order (ShadingObjectsForm.cs:157-170).
SHADING_DEFAULTS = (("TreeTopDiameter", 4.0), ("TreeTrunkHeight", 3.0), ("TreeTotalHeight", 8.0),
                    ("TreeRestrictionOffset", 1.0), ("StationLength", 5.0), ("StationWidth", 3.0),
                    ("StationHeight", 3.0), ("StationRestrictionOffset", 1.0), ("FenceHeight", 2.0),
                    ("FenceWidth", 0.2), ("VegetationHeight", 12.0))
SHADING_FIELDS = tuple(name for name, _ in SHADING_DEFAULTS)
# G30a: the e1 form_values keys, the ShadingObjectParams property names in lower snake_case.
SHADING_FORM_KEYS = (("TreeTopDiameter", "tree_top_diameter"), ("TreeTrunkHeight", "tree_trunk_height"),
                     ("TreeTotalHeight", "tree_total_height"), ("TreeRestrictionOffset", "tree_restriction_offset"),
                     ("StationLength", "station_length"), ("StationWidth", "station_width"),
                     ("StationHeight", "station_height"), ("StationRestrictionOffset", "station_restriction_offset"),
                     ("FenceHeight", "fence_height"), ("FenceWidth", "fence_width"),
                     ("VegetationHeight", "vegetation_height"))
LABEL_HEIGHT = 0.5      # AddLabel's DBText height (LeafShadingObjectCommand.cs:453)
LABEL_DROP = 0.5        # the label sits this far below the canopy (LeafShadingObjectCommand.cs:190)


def shading_defaults():
    """`new ShadingObjectParams()` (ShadingObject.cs:12-34)."""
    return dict({"Kind": "Tree"}, **dict(SHADING_DEFAULTS))


def _validated_shading(params):
    if not isinstance(params, dict) or set(params) != {"Kind", *SHADING_FIELDS}:
        raise DialogsInputError(f"shading params must carry exactly Kind and {SHADING_FIELDS}")
    if params["Kind"] not in SHADING_KINDS:
        raise DialogsInputError(f"shading kind must be one of {SHADING_KINDS}")
    return {"Kind": params["Kind"], **{f: _finite(params[f], f) for f in SHADING_FIELDS}}


def shading_form_accept(initial=None):
    """ShadingObjectsForm opened on `initial` and accepted untouched. The command passes its
    session-static last params, a fresh ShadingObjectParams on the session's first run
    (LeafShadingObjectCommand.cs:35, :47). LoadFields writes each number with "0.##"
    (ShadingObjectsForm.cs:155-171); CommitAndClose reads it back with ParseD, falling back to
    the working value when the text does not parse (:173-195, :206-208); the kind is the combo
    index, which ShowGroupForKind set from the working kind (:146-148, :179)."""
    working = shading_defaults() if initial is None else _validated_shading(initial)
    result = {"Kind": working["Kind"]}
    for field in SHADING_FIELDS:
        parsed = parse_double(format_custom_decimals(working[field], 2))
        result[field] = working[field] if parsed is None else parsed
    return result


def shading_form_values(params):
    """The accepted form's numeric inputs keyed by their G30a snake_case names, in LoadFields
    order; values unchanged (metres). Fails closed on params that are not a full set."""
    p = _validated_shading(params)
    return {key: p[field] for field, key in SHADING_FORM_KEYS}


def place_tree(params, center):
    """PlaceTree (LeafShadingObjectCommand.cs:169-198) for an accepted Tree: the canopy circle
    of radius TreeTopDiameter / 2 on LEAF-PVCASE-SHADING-TREE, a restriction ring of radius
    canopy + TreeRestrictionOffset on LEAF-PVCASE-SHADING-RESTRICTION only when the offset is
    positive (:186-187), and the label "Tree h=<total:F1> trunk=<trunk:F1>" at
    (x, y - canopy - 0.5), height 0.5, on the tree layer (:189-192, AddLabel :447-459).
    Returns the model-space entities in creation order."""
    p = _validated_shading(params)
    if p["Kind"] != "Tree":
        raise DialogsInputError(f"only the Tree placement is ported, not {p['Kind']}")
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        raise DialogsInputError("the tree centre must be one x, y point")
    cx, cy = (_finite(v, "tree centre") for v in center)
    r_canopy = p["TreeTopDiameter"] / 2.0
    ents = [{"type": "circle", "layer": SHADING_TREE_LAYER, "center": (cx, cy), "radius": r_canopy}]
    if p["TreeRestrictionOffset"] > 0:
        ents.append({"type": "circle", "layer": SHADING_RESTRICTION_LAYER, "center": (cx, cy),
                     "radius": r_canopy + p["TreeRestrictionOffset"]})
    ents.append({"type": "text", "layer": SHADING_TREE_LAYER, "position": (cx, cy - r_canopy - LABEL_DROP),
                 "height": LABEL_HEIGHT,
                 "text": f"Tree h={format_fixed(p['TreeTotalHeight'], 1)} trunk={format_fixed(p['TreeTrunkHeight'], 1)}"})
    return ents


# ----------------------------------------------------------- project areas --

PVCASE_AREA_LAYER = "PVcase PV Area"      # PvAreaStore.cs:19
DEFAULT_PRESET_NAME = "Default"           # PvAreaManagerForm.cs:424, :430-431
_HEX = re.compile(r"[0-9A-Fa-f]{1,16}")
# PvAreaDto's properties in declaration order (PvAreaStore.cs:41-50) and Aabb2d's (PvArea.cs:10-16).
AREA_FIELDS = ("Name", "SubAreaId", "BoundaryHandle", "FramePresetName", "PitchOverrideM",
               "ExclusionZoneHandles", "CachedBounds")
BOUNDS_FIELDS = ("MinX", "MinY", "MaxX", "MaxY")


def _handle(text, live):
    """ResolveHandle then HandleOf (PvAreaStore.cs:447-465): a handle that names a live entity
    comes back upper-case without leading zeros; anything else resolves to ObjectId.Null."""
    if _is_blank(text) or not _HEX.fullmatch(text.strip()):
        return None
    value = int(text.strip(), 16)
    return f"{value:X}" if value in live else None


def _read_bounds(value, what):
    if value is None:
        return None
    if not isinstance(value, _Obj):
        raise DialogsInputError(f"{what} must be an object or null")
    out = {f: 0.0 for f in BOUNDS_FIELDS}
    for key, item in value:
        name = _match_property(key, BOUNDS_FIELDS)
        if name is not None:
            out[name] = _read_scalar("double", item, f"{what}.{name}")
    return out


def read_area_record(text, live_handles=()):
    """LoadStoredOnly then FromDtos (PvAreaStore.cs:63-99, :396-428): the areas a saved record
    holds, in stored order. None (no record) is no areas. A blank name reads as "Area"; handles
    that name no live entity drop out (a boundary becomes null, an exclusion zone is skipped)."""
    if any(not isinstance(h, str) or not _HEX.fullmatch(h) for h in live_handles):
        raise DialogsInputError("live handles must be hex entity handles")
    if text is None:
        return []
    live = {int(h, 16) for h in live_handles}
    root = _parse_json(text, "the project-area record")
    if root is None:
        return []
    if not isinstance(root, _Obj):
        raise DialogsInputError("the project-area record must be a JSON object")
    dtos = []                                            # StorageEnvelope.Areas starts empty (:38)
    for key, value in root:
        name = _match_property(key, ("Version", "Areas"))
        if name == "Version":
            _read_scalar("int", value, "Version")
        elif name == "Areas":
            if value is None:
                dtos = None
                continue
            if not isinstance(value, list) or isinstance(value, _Obj):
                raise DialogsInputError("Areas must be an array")
            dtos = (dtos or []) + list(value)
    if len(dtos or []) > MAX_AREAS:
        raise DialogsInputError(f"the record holds more than {MAX_AREAS} areas")
    areas = []
    for n, dto in enumerate(dtos or [], 1):
        if dto is None:
            continue
        if not isinstance(dto, _Obj):
            raise DialogsInputError(f"area {n} must be an object")
        raw = {"Name": None, "SubAreaId": None, "BoundaryHandle": None, "FramePresetName": None,
               "PitchOverrideM": 0.0, "ExclusionZoneHandles": [], "CachedBounds": None}
        for key, value in dto:
            name = _match_property(key, AREA_FIELDS)
            what = f"area {n} {name}"
            if name in ("Name", "SubAreaId", "BoundaryHandle", "FramePresetName"):
                raw[name] = _read_scalar("str", value, what)
            elif name == "PitchOverrideM":
                raw[name] = _read_scalar("double", value, what)
            elif name == "ExclusionZoneHandles":
                if value is None:
                    raw[name] = None
                elif isinstance(value, list) and not isinstance(value, _Obj):
                    raw[name] = (raw[name] or []) + [_read_scalar("str", v, what) for v in value]
                else:
                    raise DialogsInputError(f"{what} must be an array")
            elif name == "CachedBounds":
                raw[name] = _read_bounds(value, what)
        areas.append({"Name": "Area" if _is_blank(raw["Name"]) else raw["Name"],
                      "SubAreaId": raw["SubAreaId"],
                      "BoundaryHandle": _handle(raw["BoundaryHandle"], live),
                      "FramePresetName": raw["FramePresetName"],
                      "PitchOverrideM": raw["PitchOverrideM"],
                      "ExclusionZoneHandles": [h for h in (_handle(x, live) for x in raw["ExclusionZoneHandles"] or [])
                                               if h is not None],
                      "CachedBounds": raw["CachedBounds"]})
    return areas


def area_record_text(areas):
    """Save (PvAreaStore.cs:101-119, ToDtos :430-445): the envelope {Version: 1, Areas} as
    compact Newtonsoft JSON, each area's fields in PvAreaDto order."""
    dtos = [{"Name": a["Name"], "SubAreaId": a["SubAreaId"], "BoundaryHandle": a["BoundaryHandle"],
             "FramePresetName": a["FramePresetName"], "PitchOverrideM": _finite(a["PitchOverrideM"], "pitch"),
             "ExclusionZoneHandles": list(a["ExclusionZoneHandles"]),
             "CachedBounds": None if a["CachedBounds"] is None else
             {f: _finite(a["CachedBounds"][f], f) for f in BOUNDS_FIELDS}} for a in areas]
    return newtonsoft_json({"Version": 1, "Areas": dtos})


def _parse_pitch(raw):
    """PvAreaManagerForm.ParseDouble (:434-441): invariant parse clamped at zero, else 0. The
    current-culture fallback is never reached: the grid text is the form's own invariant text."""
    value = parse_double(raw)
    return 0.0 if value is None else max(0.0, value)


def project_areas_add_then_ok(record_text, active_preset_name, preset_names=None, live_handles=(),
                              pvcase_area_boundaries=0):
    """LEAFAREAS as e4 drives it: the manager opens on the drawing's areas (constructor,
    PvAreaManagerForm.cs:62-79), "Add Area" (:250-261), then OK (SaveAndClose :376-382).

    record_text: the drawing's saved area record (None when there is none).
    active_preset_name: FramePresetStore.GetActive().Name (None falls back to "Default", :430).
    preset_names: FramePresetStore.List() names (default: the active preset only); they only
      label the grid's preset column, which a blank area preset takes its first entry from.
    pvcase_area_boundaries: closed polylines on the PVcase PV Area layer; with no stored
      areas Load imports them (PvAreaStore.cs:52-61, :199-242), which is not ported.
    Returns (areas as saved, the record text as saved)."""
    areas = read_area_record(record_text, live_handles)
    if not areas and pvcase_area_boundaries:
        raise DialogsInputError("PVcase PV Area import (PvAreaStore.AutoImportPvcaseAreas) is not ported")
    active = DEFAULT_PRESET_NAME if _is_blank(active_preset_name) else active_preset_name
    names = [n for n in (preset_names if preset_names is not None else [active]) if not _is_blank(n)]
    for a in areas:                                                    # LoadPresetNames (:405-426)
        n = a["FramePresetName"]
        if not _is_blank(n) and _fold(n) not in {_fold(x) for x in names}:
            names.append(n)
    if not names:
        names.append(DEFAULT_PRESET_NAME)

    def refresh():                                                     # RefreshGrid (:198-222)
        rows = []
        for a in areas:
            preset = (names[0] if names else DEFAULT_PRESET_NAME) if _is_blank(a["FramePresetName"]) \
                else a["FramePresetName"]
            if _fold(preset) not in {_fold(x) for x in names}:
                names.append(preset)
            pitch = format_custom_decimals(a["PitchOverrideM"], 3) if a["PitchOverrideM"] > 0.0 else "0"
            rows.append((a, a["Name"] or "", preset, pitch, a["SubAreaId"] or ""))
        return rows

    def commit(rows):                                                  # CommitGrid (:233-248)
        for a, name, preset, pitch, sub in rows:
            a["Name"], a["FramePresetName"], a["SubAreaId"] = name, preset, sub
            a["PitchOverrideM"] = _parse_pitch(pitch)

    rows = refresh()
    commit(rows)                                                       # AddArea (:250-258)
    areas.append({"Name": f"Area {len(areas) + 1}", "SubAreaId": None, "BoundaryHandle": None,
                  "FramePresetName": active, "PitchOverrideM": 0.0, "ExclusionZoneHandles": [],
                  "CachedBounds": None})
    rows = refresh()
    commit(rows)                                                       # SaveAndClose (:376-379)
    return areas, area_record_text(areas)


# ---------------------------------------------------------- pile templates --

FULL_TEMPLATE = "Full"                  # PileTemplateStore.cs:19
PLACEMENT_MODES = ("Grid", "AxisStations")             # PileTemplate.cs:14-18
STATION_AXES = ("LocalX", "LocalY")                    # PileTemplate.cs:20-24
STATION_KINDS = ("Bearing", "Drive", "Joint", "End")   # PileTemplate.cs:26-32
# PileTemplate's writable properties in declaration order and their JSON kinds (PileTemplate.cs:62-93).
TEMPLATE_KINDS = {
    "Name": "str", "AreEqualMargins": "bool", "ShouldPlacePilesAtJoints": "bool", "IsMirrorFromMiddle": "bool",
    "DistributionType": "int", "HorizontalDistancesM": "doubles", "VerticalDistancesM": "doubles",
    "MiddleDistribution": "double", "SelectedMiddlePole": "bool", "HorizontalPoleCount": "int",
    "VerticalPoleCount": "int", "PlacementMode": "placement", "StationAxis": "axis", "ReverseStationStart": "bool",
    "Stations": "stations", "PileDiameterM": "double", "PileRevealM": "double", "PileEmbedmentM": "double",
    "MinPileLengthM": "double", "MaxPileLengthM": "double", "RevealBucketBoundariesM": "doubles",
}
STATION_FIELDS = ("OffsetM", "Kind", "Label", "CrossAxisOffsetM")    # PileTemplate.cs:34-42
NON_NEGATIVE = ("PileDiameterM", "PileRevealM", "PileEmbedmentM", "MinPileLengthM", "MaxPileLengthM")
FORM_COUNT_RANGE = (1, 100)             # the pole-count NumericUpDowns (PileTemplateManagerForm.cs:83-101)
FORM_DISTRIBUTIONS = 4                  # "Type 0" to "Type 3" (PileTemplateManagerForm.cs:135)
_LIST_SEPARATORS = re.compile(r"[,; \r\n\t]+")


def default_reveal_bucket_boundaries_m():
    """3 to 7 feet in metres (PileTemplate.cs:107-111)."""
    ft_to_m = 0.3048
    return [3.0 * ft_to_m, 4.0 * ft_to_m, 5.0 * ft_to_m, 6.0 * ft_to_m, 7.0 * ft_to_m]


def new_template(**fields):
    """`new PileTemplate { ... }` (PileTemplate.cs:62-93), then the given initializers."""
    t = {"Name": "Full", "AreEqualMargins": False, "ShouldPlacePilesAtJoints": False, "IsMirrorFromMiddle": False,
         "DistributionType": 2, "HorizontalDistancesM": [], "VerticalDistancesM": [], "MiddleDistribution": 0.0,
         "SelectedMiddlePole": False, "HorizontalPoleCount": 2, "VerticalPoleCount": 1, "PlacementMode": "Grid",
         "StationAxis": "LocalX", "ReverseStationStart": False, "Stations": [], "PileDiameterM": 0.0,
         "PileRevealM": 0.0, "PileEmbedmentM": 0.0, "MinPileLengthM": 0.0, "MaxPileLengthM": 0.0,
         "RevealBucketBoundariesM": default_reveal_bucket_boundaries_m()}
    unknown = set(fields) - set(t)
    if unknown:
        raise DialogsInputError(f"unknown template fields {sorted(unknown)}")
    t.update(fields)
    return t


def _new_station():
    """`new PileStation()` (PileTemplate.cs:34-42)."""
    return {"OffsetM": 0.0, "Kind": "Bearing", "Label": "", "CrossAxisOffsetM": 0.0}


def _clone_station(s):
    """PileStation.Clone (PileTemplate.cs:44-53)."""
    return {"OffsetM": s["OffsetM"], "Kind": s["Kind"], "Label": s["Label"] or "", "CrossAxisOffsetM": s["CrossAxisOffsetM"]}


def clone_template(t):
    """PileTemplate.Clone (PileTemplate.cs:166-196)."""
    out = dict(t)
    out["HorizontalDistancesM"] = list(t["HorizontalDistancesM"] or [])
    out["VerticalDistancesM"] = list(t["VerticalDistancesM"] or [])
    out["Stations"] = [_clone_station(s) for s in (t["Stations"] or []) if s is not None]
    out["RevealBucketBoundariesM"] = (list(t["RevealBucketBoundariesM"]) if t["RevealBucketBoundariesM"] is not None
                                      else default_reveal_bucket_boundaries_m())
    return out


def piles_per_frame(t):
    """The PilesPerFrame getter (PileTemplate.cs:95-105), with C#'s unchecked int product."""
    if t["PlacementMode"] == "AxisStations" and t["Stations"]:
        return len(t["Stations"])
    product = (t["HorizontalPoleCount"] * t["VerticalPoleCount"]) & 0xFFFFFFFF
    return product - (1 << 32) if product >= 1 << 31 else product


def _clamp_non_negative(v):
    """ClampNonNegative (PileTemplateStore.cs:263-268)."""
    return 0.0 if (not math.isfinite(v) or v < 0.0) else v


def normalize_template(t):
    """Normalize (PileTemplateStore.cs:229-261), in place."""
    t["Name"] = t["Name"].strip() if t["Name"] is not None else None
    t["HorizontalPoleCount"] = max(1, t["HorizontalPoleCount"])
    t["VerticalPoleCount"] = max(1, t["VerticalPoleCount"])
    if t["HorizontalDistancesM"] is None:
        t["HorizontalDistancesM"] = []
    if t["VerticalDistancesM"] is None:
        t["VerticalDistancesM"] = []
    stations = [s for s in (t["Stations"] or []) if s is not None and math.isfinite(s["OffsetM"])]
    stations.sort(key=lambda s: s["OffsetM"])        # OrderBy: stable
    for s in stations:
        s["Label"] = s["Label"] if s["Label"] is not None else ""
    t["Stations"] = stations
    if t["PlacementMode"] == "AxisStations" and not stations:
        t["PlacementMode"] = "Grid"
    for f in NON_NEGATIVE:
        t[f] = _clamp_non_negative(t[f])
    if not t["RevealBucketBoundariesM"]:
        t["RevealBucketBoundariesM"] = default_reveal_bucket_boundaries_m()
    return t


def _read_station(obj, what):
    if not isinstance(obj, _Obj):
        raise DialogsInputError(f"{what} must be an object")
    s = _new_station()
    for key, value in obj:
        name = _match_property(key, STATION_FIELDS)
        if name in ("OffsetM", "CrossAxisOffsetM"):
            s[name] = _read_scalar("double", value, f"{what}.{name}")
        elif name == "Kind":
            s[name] = _read_enum(STATION_KINDS, value, f"{what}.Kind")
        elif name == "Label":
            s[name] = _read_scalar("str", value, f"{what}.Label")
    return s


def _read_template(obj, what):
    """Newtonsoft's populate of a fresh PileTemplate: each JSON property in order, unknown and
    read-only ones (PilesPerFrame) skipped, list properties appended to."""
    if not isinstance(obj, _Obj):
        raise DialogsInputError(f"{what} must be an object")
    t = new_template()
    for key, value in obj:
        name = _match_property(key, tuple(TEMPLATE_KINDS))
        if name is None:
            continue
        kind, field = TEMPLATE_KINDS[name], f"{what}.{name}"
        if kind == "doubles":
            t[name] = _read_double_list(t[name], value, field)
        elif kind == "stations":
            if value is None:
                t[name] = None
            elif isinstance(value, list) and not isinstance(value, _Obj):
                t[name] = (t[name] or []) + [_read_station(v, field) for v in value]
            else:
                raise DialogsInputError(f"{field} must be an array")
        elif kind == "placement":
            t[name] = _read_enum(PLACEMENT_MODES, value, field)
        elif kind == "axis":
            t[name] = _read_enum(STATION_AXES, value, field)
        else:
            t[name] = _read_scalar(kind, value, field)
    return t


def template_json(t):
    """The template as Newtonsoft writes it: declaration order, enums by name, then the
    computed PilesPerFrame (PileTemplate.cs:95)."""
    out = {name: t[name] for name in TEMPLATE_KINDS}
    for name in ("HorizontalDistancesM", "VerticalDistancesM", "RevealBucketBoundariesM"):
        out[name] = [_finite(v, name) for v in out[name]]
    out["Stations"] = [{f: s[f] for f in STATION_FIELDS} for s in t["Stations"]]
    out["PilesPerFrame"] = piles_per_frame(t)
    return out


class PileTemplateStore:
    """PileTemplateStore (PileTemplateStore.cs): templates in a Dictionary keyed by name under
    OrdinalIgnoreCase (a later set of an existing name keeps the first key's spelling and
    position) and the active template name."""

    def __init__(self, text=None):
        """The constructor's ReadFromDisk (:157-191). `text` is the file's text, None when the
        file does not exist."""
        self._items = {}                           # folded name -> [key, template]
        self.active = FULL_TEMPLATE
        if text is not None:
            self._read(text)
        self._ensure_default()
        if not self.contains(self.active):
            self.active = FULL_TEMPLATE

    def _read(self, text):
        root = _parse_json(text, "the pile-template store")
        if root is None:
            return
        if not isinstance(root, _Obj):
            raise DialogsInputError("the pile-template store must be a JSON object")
        templates, active = None, None             # StoreDto (:270-274)
        for key, value in root:
            name = _match_property(key, ("Templates", "ActiveTemplate"))
            if name == "ActiveTemplate":
                active = _read_scalar("str", value, "ActiveTemplate")
            elif name == "Templates":
                if value is None:
                    templates = None
                    continue
                if not isinstance(value, _Obj):
                    raise DialogsInputError("Templates must be an object")
                templates = {} if templates is None else templates
                for tkey, tvalue in value:          # dictionary[key] = value: ordinal keys
                    templates[tkey] = None if tvalue is None else _read_template(tvalue, f"template {tkey!r}")
                    if len(templates) > MAX_TEMPLATES:
                        raise DialogsInputError(f"the store holds more than {MAX_TEMPLATES} templates")
        for tkey, t in (templates or {}).items():
            if t is None:
                continue
            if _is_blank(t["Name"]):
                t["Name"] = tkey
            normalize_template(t)
            self.set(t["Name"], t)
        if not _is_blank(active):
            self.active = active

    def contains(self, name):
        return name is not None and _fold(name) in self._items

    def get(self, name):
        entry = self._items.get(_fold(name)) if name is not None else None
        return None if entry is None else entry[1]

    def set(self, name, template):
        entry = self._items.get(_fold(name))
        if entry is None:
            self._items[_fold(name)] = [name, template]
        else:
            entry[1] = template

    def keys(self):
        return [key for key, _ in self._items.values()]

    def list(self):
        """List (:43-44): templates ordered by name, OrdinalIgnoreCase, stable."""
        return sorted((t for _, t in self._items.values()), key=lambda t: _fold_order(t["Name"]))

    def get_active(self):
        """GetActive (:54-61)."""
        if self.contains(self.active):
            return self.get(self.active)
        if self.contains(FULL_TEMPLATE):
            return self.get(FULL_TEMPLATE)
        return next((t for _, t in self._items.values()), None)

    def _ensure_default(self):
        """EnsureDefault / BuildDefaultFull (:209-227)."""
        if not self.contains(FULL_TEMPLATE):
            self.set(FULL_TEMPLATE, new_template(Name=FULL_TEMPLATE, HorizontalPoleCount=2, VerticalPoleCount=1,
                                                 HorizontalDistancesM=[1.0, 1.0, 1.0], VerticalDistancesM=[1.0, 1.0]))

    def save_all(self, templates, active):
        """SaveAll (:76-91); returns the file text Persist writes."""
        self._items = {}
        for t in templates:
            if t is None or _is_blank(t["Name"]):
                continue
            normalize_template(t)
            self.set(t["Name"], t)
        self._ensure_default()
        self.active = active if (not _is_blank(active) and self.contains(active)) else FULL_TEMPLATE
        return self.file_text()

    def file_text(self):
        """Persist (:193-207): {Templates (ordered by key, OrdinalIgnoreCase), ActiveTemplate}
        through JsonConvert.SerializeObject(dto, Formatting.Indented); File.WriteAllText adds
        no BOM and no trailing newline."""
        ordered = sorted(self._items.values(), key=lambda entry: _fold_order(entry[0]))
        return newtonsoft_json({"Templates": {key: template_json(t) for key, t in ordered},
                                "ActiveTemplate": self.active}, indented=True)


def format_distance_list(values):
    """FormatList (PileTemplateManagerForm.cs:319-323)."""
    if values is None:
        return ""
    return ", ".join(format_custom_decimals(v, 3) for v in values)


def parse_distance_list(text):
    """ParseList (PileTemplateManagerForm.cs:325-338): split on , ; space CR LF tab, keep what parses."""
    if _is_blank(text):
        return []
    return [v for v in (parse_double(p) for p in _LIST_SEPARATORS.split(text) if p) if v is not None]


class PileTemplateManager:
    """PileTemplateManagerForm's state machine without the window: the template map (a
    Dictionary under OrdinalIgnoreCase), the selected template and the editor fields."""

    def __init__(self, store):
        """The constructor (:34-42): clones of the store's templates, then the active one selected."""
        if not isinstance(store, PileTemplateStore):
            raise DialogsInputError("the manager takes a PileTemplateStore")
        self._store = store
        self._templates = PileTemplateStore.__new__(PileTemplateStore)
        self._templates._items, self._templates.active = {}, FULL_TEMPLATE
        for t in store.list():
            self._templates.set(t["Name"], clone_template(t))
        self.current = None
        self.fields = None
        active = store.get_active()
        self._reload(active["Name"] if active is not None and active["Name"] is not None else FULL_TEMPLATE)

    def _reload(self, selected):
        """ReloadTemplateList (:170-182): names ordered OrdinalIgnoreCase; the combo's IndexOf is
        an exact (ordinal) match, else the first item."""
        items = sorted(self._templates.keys(), key=_fold_order)
        index = items.index(selected) if selected in items else (0 if items else -1)
        if index >= 0:
            self._load_selected(items[index])

    def _load_selected(self, name):
        """LoadSelected (:184-203)."""
        t = self._templates.get(name)
        if t is None:
            return
        lo, hi = FORM_COUNT_RANGE
        self.current = name
        self.fields = {"name": t["Name"] or "",
                       "horizontal_count": min(hi, max(lo, t["HorizontalPoleCount"])),
                       "vertical_count": min(hi, max(lo, t["VerticalPoleCount"])),
                       "horizontal_distances": format_distance_list(t["HorizontalDistancesM"]),
                       "vertical_distances": format_distance_list(t["VerticalDistancesM"]),
                       "joints": t["ShouldPlacePilesAtJoints"], "mirror": t["IsMirrorFromMiddle"],
                       "middle_pole": t["SelectedMiddlePole"],
                       "distribution": max(0, min(FORM_DISTRIBUTIONS - 1, t["DistributionType"]))}

    def _save_current(self):
        """SaveCurrent (:205-232): the editor fields written back into the selected template."""
        if _is_blank(self.current):
            return
        t = self._templates.get(self.current)
        if t is None:
            return
        f = self.fields
        new_name = self.current if _is_blank(f["name"]) else f["name"].strip()
        t["Name"] = new_name
        t["HorizontalPoleCount"] = f["horizontal_count"]
        t["VerticalPoleCount"] = f["vertical_count"]
        t["HorizontalDistancesM"] = parse_distance_list(f["horizontal_distances"])
        t["VerticalDistancesM"] = parse_distance_list(f["vertical_distances"])
        t["ShouldPlacePilesAtJoints"] = f["joints"]
        t["IsMirrorFromMiddle"] = f["mirror"]
        t["SelectedMiddlePole"] = f["middle_pole"]
        t["DistributionType"] = f["distribution"] if f["distribution"] >= 0 else 2
        if _fold(self.current) != _fold(new_name):
            self._templates._items.pop(_fold(self.current), None)
            self._templates.set(new_name, t)
            self.current = new_name

    def _unique_name(self, base):
        """UniqueName (:303-310)."""
        name, suffix = base, 2
        while self._templates.contains(name):
            name = f"{base} {suffix}"
            suffix += 1
        return name

    def add_template(self):
        """The "+" button, AddTemplate (:234-247)."""
        self._save_current()
        name = self._unique_name("New")
        self._templates.set(name, new_template(Name=name, HorizontalPoleCount=2, VerticalPoleCount=1,
                                               HorizontalDistancesM=[1.0, 1.0, 1.0], VerticalDistancesM=[1.0, 1.0]))
        self._reload(name)

    def ok(self):
        """OK, SaveAndClose (:279-301): a blank or repeated name keeps the dialog open and saves
        nothing (refused here); otherwise the store's SaveAll with clones and the selected name.
        Returns the store file's text as written."""
        self._save_current()
        seen = set()
        for _, t in self._templates._items.values():
            if _is_blank(t["Name"]):
                raise DialogsInputError("Every pile template needs a name.")
            if _fold(t["Name"]) in seen:
                raise DialogsInputError("Pile template names must be unique.")
            seen.add(_fold(t["Name"]))
        return self._store.save_all([clone_template(t) for _, t in self._templates._items.values()], self.current)


def pile_templates_add_then_ok(store_text):
    """LEAFPILETEMPLATES as e6 drives it (PileTemplateCommand.cs:20-25): the store read from
    its file (None when absent), the manager opened on it, "+", then OK. Returns (the store
    after the save, the file text as written)."""
    store = PileTemplateStore(store_text)
    manager = PileTemplateManager(store)
    manager.add_template()
    return store, manager.ok()
