"""Studio port of the plugin's design profiles (contract G32): LEAFPROFILE and the per-drawing
profile manager it drives.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s62):

  ProfileCommand.cs          Execute (:16-41): the Create/Swap/Delete/List keyword prompt;
                             CreateProfile (:43-80): trimmed name, case-insensitive duplicate refusal
                             (:55-59), the adoption prompt only for the first profile, default Yes
                             (:61-75); SwapProfile (:82-114): at least two profiles, keywords are the
                             prefixes of every profile but the active one (:91-99); DeleteProfile
                             (:116-167): keywords are every prefix, a Yes/No confirm defaulting to No
                             (:141-154); ListProfiles (:169-191)
  DesignProfileManager.cs    NextPrefix (:148-157): the first free letter A..Z, then "P" + (count + 1);
                             CreateProfile (:163-191): prefix, settings copy, adoption, save the active
                             profile's state, append, make it active; SwapTo (:196-232); DeleteProfile
                             (:237-286): a deleted active profile hands over to Profiles[0]; GetActive-
                             Profile (:288-293); RenameProfile (:298-310); SaveActiveProfileState
                             (:316-325); SetProfileLayersFrozen (:327-354); AdoptExistingLayers and
                             RenameLayerIfExists (:356-399); the base scoped layers (:30-35),
                             ResolveLayerName (:89-102), IsProfileScopedLayer (:107-126),
                             IsLayerInActiveProfile (:132-139); the record written by SaveToDrawing
                             (:458-506) and read by LoadFromDrawing (:508-557)
  DesignProfile.cs           the profile's fields and their serialized order (:12-29), the adoption
                             layer list (:41-51), DrawingStateSnapshot's defaults (:58-76) and copy
                             (:78-135), the record envelope and its Version 1 (:171-176)
  ProjectPreset.cs           FromCurrentSettings (:307-393) and ApplyToSettings (:399-472)

Pure functions over NEUTRAL data: the plugin's current settings are a dict keyed by the
preset's field names, layers are {name: {"frozen": bool, "entities": int}}, drawing-level
counters are the snapshot dict. How the plugin stores the record in a drawing is not part of
this module; `record()` is the decoded record, `record_json()` its text as the plugin writes it.

Bounds and failure: a settings object is validated field by field (validate_preset), a name
is at most MAX_NAME_CHARS, a manager holds at most MAX_PROFILES, a record text is at most
MAX_RECORD_CHARS. Malformed input to the command or the manager fails closed with
ProfileInputError. load_record keeps the plugin's own rule (:546-549): an unreadable record is
an empty manager, flagged `corrupt` so a caller can tell. Every pass is linear in the profile
count; nothing is cached.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math

# DesignProfileStore.Version (DesignProfile.cs:173), written by SaveToDrawing (DesignProfileManager.cs:462).
PROFILE_STORE_VERSION = 1
SUBCOMMANDS = ("Create", "Swap", "Delete", "List")                      # ProfileCommand.cs:25-28
# DesignProfileManager._baseScopedLayers (:30-35): the layers ResolveLayerName scopes.
BASE_SCOPED_LAYERS = ("String", "Panel Group", "Feeder", "HOMERUN-TRUNK", "HOMERUN-BRANCH",
                      "Optimizers", "LEAF-ZONEHEIGHT", "Inverters", "HOMERUN-DEBUG")
# DesignProfile.ProfileScopedLayers (DesignProfile.cs:41-51): the layers adoption renames.
PROFILE_SCOPED_LAYERS = ("String", "Panel Group", "Feeder", "HOMERUN-TRUNK", "HOMERUN-BRANCH",
                         "Optimizers", "LEAF-ZONEHEIGHT", "Inverters")

# ProjectPreset's fields in declaration order (the order the record serializes them), each with
# its JSON kind: s string (null allowed), b bool, i int, f double.
PRESET_FIELDS = (
    ("Name", "s"), ("IsBuiltIn", "b"), ("InstallationDesign", "s"), ("MaxPanelGap", "f"),
    ("AlignmentTolerance", "f"), ("ExtractPanelsFromRackBlocks", "b"), ("ModuleLayer", "s"),
    ("PanelGroupLayer", "s"), ("PanelGroupLabelHeight", "f"), ("PanelGroupOutlineWidth", "f"),
    ("StringLayer", "s"), ("StringWidth", "f"), ("TagHeight", "f"), ("LabelPlacement", "s"),
    ("TagFirst", "s"), ("TagSecond", "s"), ("TagThird", "s"), ("TagDelim1", "s"), ("TagDelim2", "s"),
    ("NumPanelsInSequence", "i"), ("NumMppt", "i"), ("StringsPerMppt", "i"), ("UseCombinerBox", "b"),
    ("CombinerBoxSize", "i"), ("CombinerBoxConnections", "i"), ("SolarEdgeContinuousNumbering", "b"),
    ("SolarEdgeMatchPdfColors", "b"), ("OptimizerBlock", "s"), ("UseL2Collectors", "b"),
    ("L1CollectorsPerL2", "i"), ("L2NumMppt", "i"), ("L2StringsPerMppt", "i"),
    ("InverterSelection", "s"), ("ModuleSelection", "s"), ("Vmp", "f"), ("Imp", "f"), ("Pmp", "f"),
    ("Voc", "f"), ("BVoc", "f"), ("MinTemp", "f"), ("WireSize", "s"), ("CableMaterial", "s"),
    ("NecCableType", "s"), ("MaxConductorTemp", "s"), ("ConduitType", "s"),
    ("CableInstallationMethod", "s"), ("AmbientTemperature", "s"), ("NoGroupedConductors", "s"),
    ("DistanceAboveRoof", "s"), ("CableTray", "s"), ("VdRunMax", "s"), ("SafetyFactor", "s"),
    ("DeratingFactor", "s"), ("HomeRunLayer", "s"), ("InverterBlock", "s"), ("InverterBlockSize", "f"),
    ("TagColor", "s"), ("UseInverterColor", "b"), ("UseOptimizers", "b"), ("OptimizerRatio", "s"),
    ("L2InverterSelection", "s"), ("NumPanels", "i"),
)
PRESET_FIELD_NAMES = tuple(name for name, _ in PRESET_FIELDS)
_PRESET_KIND = dict(PRESET_FIELDS)
# The plugin's current settings: every preset field but the preset's own identity.
SETTINGS_FIELD_NAMES = tuple(n for n in PRESET_FIELD_NAMES if n not in ("Name", "IsBuiltIn"))
# The cable-sizing fields FromCurrentSettings reads from the UserInformation store (:374-390) and
# ApplyToSettings writes back only when CableMaterial is non-empty (:459-469).
CABLE_FIELDS = ("CableMaterial", "NecCableType", "MaxConductorTemp", "ConduitType",
                "CableInstallationMethod", "AmbientTemperature", "NoGroupedConductors",
                "DistanceAboveRoof", "CableTray", "VdRunMax", "SafetyFactor", "DeratingFactor")
# FromCurrentSettings' `??` fallbacks (ProjectPreset.cs:326-330, 340-341, 356, 359-362, 367-368, 378-389).
FROM_SETTINGS_NULL_DEFAULTS = dict(
    {"TagFirst": "Inv. #", "TagSecond": "String #", "TagThird": "MPPT #", "TagDelim1": ".",
     "TagDelim2": "", "InverterSelection": "", "ModuleSelection": "", "WireSize": "",
     "HomeRunLayer": "HomeRun", "InverterBlock": "Inverter", "TagColor": "Bylayer",
     "OptimizerRatio": "1:1", "L2InverterSelection": ""},
    **{f: "" for f in CABLE_FIELDS})
# ApplyToSettings' `??` fallbacks (ProjectPreset.cs:423-424, 442, 445-448, 453-454, 463-467).
APPLY_NULL_DEFAULTS = dict(
    {"InverterSelection": "", "ModuleSelection": "", "WireSize": "", "HomeRunLayer": "HomeRun",
     "InverterBlock": "Inverter", "TagColor": "Bylayer", "OptimizerRatio": "1:1",
     "L2InverterSelection": ""},
    **{f: "" for f in CABLE_FIELDS})

# DrawingStateSnapshot's defaults in serialized order (DesignProfile.cs:60-76); FromDrawingProperties
# of a missing drawing-properties object is exactly this (:80).
DEFAULT_DRAWING_STATE = (("StringNumber", 1), ("InverterNumber", 1), ("MPPTLetter", "a"),
                         ("PanelGroupNumber", 1), ("PanelGroupColour", 0), ("ElevationZones", []),
                         ("ElectricalZones", []), ("L1ToL2Assignments", {}), ("L1ToL2InputAssignments", {}),
                         ("InverterTypes", {}), ("InverterTypeAssignments", {}))
DRAWING_STATE_FIELDS = tuple(k for k, _ in DEFAULT_DRAWING_STATE)

# Studio-side bounds (the plugin has none; these fail closed long before memory matters).
MAX_NAME_CHARS = 1024
MAX_TEXT_CHARS = 16384
MAX_PROFILES = 1000
MAX_RECORD_CHARS = 2 * 1024 * 1024
MAX_ANSWERS = 16


class ProfileInputError(ValueError):
    """Malformed input to the profile command or manager: nothing is changed."""


# ---------------------------------------------------------------------------
#  Settings: the preset copy and its restore (ProjectPreset.cs)
# ---------------------------------------------------------------------------

def _check_value(field, kind, value):
    if kind == "s":
        if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT_CHARS):
            raise ProfileInputError(f"{field} must be a string of at most {MAX_TEXT_CHARS} chars or null")
    elif kind == "b":
        if type(value) is not bool:
            raise ProfileInputError(f"{field} must be a boolean")
    elif kind == "i":
        if type(value) is not int or abs(value) > 2 ** 31 - 1:
            raise ProfileInputError(f"{field} must be a 32-bit integer")
    elif type(value) not in (int, float) or not math.isfinite(value):
        raise ProfileInputError(f"{field} must be a finite number")


def validate_preset(preset):
    """A ProjectPreset object as the record carries it: exactly PRESET_FIELDS, each of its kind.
    Returns the preset unchanged; fails closed."""
    if not isinstance(preset, dict) or set(preset) != set(PRESET_FIELD_NAMES):
        raise ProfileInputError(f"a preset must carry exactly the {len(PRESET_FIELDS)} ProjectPreset fields")
    for field, kind in PRESET_FIELDS:
        _check_value(field, kind, preset[field])
    return preset


def validate_settings(settings):
    """The plugin's current settings: exactly SETTINGS_FIELD_NAMES, each of its kind."""
    if not isinstance(settings, dict) or set(settings) != set(SETTINGS_FIELD_NAMES):
        raise ProfileInputError(f"current settings must carry exactly the {len(SETTINGS_FIELD_NAMES)} setting fields")
    for field in SETTINGS_FIELD_NAMES:
        _check_value(field, _PRESET_KIND[field], settings[field])
    return settings


def current_settings_from_preset(preset):
    """The current settings a captured preset was copied from: every field but Name and IsBuiltIn."""
    validate_preset(preset)
    return {f: copy.deepcopy(preset[f]) for f in SETTINGS_FIELD_NAMES}


def preset_from_current_settings(settings, name):
    """ProjectPreset.FromCurrentSettings (ProjectPreset.cs:307-393): the current settings under
    `name`, IsBuiltIn false, the `??` fallbacks applied, in declaration order."""
    validate_settings(settings)
    out = {"Name": name, "IsBuiltIn": False}
    for field in SETTINGS_FIELD_NAMES:
        value = settings[field]
        if value is None and field in FROM_SETTINGS_NULL_DEFAULTS:
            value = FROM_SETTINGS_NULL_DEFAULTS[field]
        out[field] = copy.deepcopy(value)
    return out


def apply_to_settings(preset, settings):
    """ProjectPreset.ApplyToSettings (ProjectPreset.cs:399-472), in place on `settings`: every
    field, the tagging fields as they are (:430-431), the cable-sizing fields only when
    CableMaterial is non-empty (:460)."""
    validate_preset(preset)
    validate_settings(settings)
    for field in SETTINGS_FIELD_NAMES:
        if field in CABLE_FIELDS:
            continue
        value = preset[field]
        if value is None and field in APPLY_NULL_DEFAULTS:
            value = APPLY_NULL_DEFAULTS[field]
        settings[field] = copy.deepcopy(value)
    if preset["CableMaterial"]:
        for field in CABLE_FIELDS:
            value = preset[field]
            settings[field] = "" if value is None else value
    return settings


def canonical_settings_text(settings):
    """G32: the canonical JSON text of a profile's settings object (sorted keys, compact,
    UTF-8 text unescaped), the comparator's own canonical form."""
    return json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------------------
#  Drawing-level state (DrawingStateSnapshot, DesignProfile.cs:58-135)
# ---------------------------------------------------------------------------

def drawing_state_from(props):
    """FromDrawingProperties (:78-107): a copy of the drawing's counters, or the defaults when
    the drawing has no properties object."""
    if props is None:
        return {k: copy.deepcopy(v) for k, v in DEFAULT_DRAWING_STATE}
    if not isinstance(props, dict) or not set(DRAWING_STATE_FIELDS) <= set(props):
        raise ProfileInputError("drawing properties must carry every DrawingStateSnapshot field")
    return {k: copy.deepcopy(props[k]) for k in DRAWING_STATE_FIELDS}


def apply_drawing_state(state, props):
    """ApplyToDrawingProperties (:109-135), in place; a missing properties object is a no-op."""
    if props is None or state is None:
        return
    if not isinstance(props, dict) or not isinstance(state, dict):
        raise ProfileInputError("drawing properties and the profile's drawing state must be objects")
    defaults = dict(DEFAULT_DRAWING_STATE)
    for k in DRAWING_STATE_FIELDS:
        props[k] = copy.deepcopy(state.get(k, defaults[k]))


def format_created_utc(moment):
    """Newtonsoft's ISO form of a UTC DateTime: seconds, then up to seven fraction digits with
    trailing zeros dropped (no dot when all are zero), then Z."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ProfileInputError("created time must be a timezone-aware datetime")
    moment = moment.astimezone(timezone.utc)
    fraction = f"{moment.microsecond:06d}0".rstrip("0")
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + ("." + fraction if fraction else "") + "Z"


# ---------------------------------------------------------------------------
#  Layers (the manager's layer-table work, over {name: {"frozen", "entities"}})
# ---------------------------------------------------------------------------

def _same(a, b):
    """string.Equals(a, b, StringComparison.OrdinalIgnoreCase), nulls included."""
    if a is None or b is None:
        return a is None and b is None
    return a.upper() == b.upper()


def _layer_key(layers, name):
    """LayerTable.Has / the indexer: symbol-table names match case-insensitively."""
    if name is None:
        return None
    upper = name.upper()
    for key in layers:
        if key.upper() == upper:
            return key
    return None


def _check_layers(layers):
    if layers is None:
        return {}
    if not isinstance(layers, dict) or not all(
            isinstance(k, str) and isinstance(v, dict) and type(v.get("frozen", False)) is bool
            and type(v.get("entities", 0)) is int for k, v in layers.items()):
        raise ProfileInputError("layers must map a name to {frozen: bool, entities: int}")
    return layers


def _has_prefix(name, prefix):
    return name.upper().startswith((prefix + "_").upper())


def is_profile_scoped_layer(layer_name, settings):
    """IsProfileScopedLayer (:107-126): the base list, or the configured string, home-run or
    panel-group layer."""
    if any(_same(layer_name, b) for b in BASE_SCOPED_LAYERS):
        return True
    for field in ("StringLayer", "HomeRunLayer", "PanelGroupLayer"):
        configured = settings.get(field) if isinstance(settings, dict) else None
        if configured and _same(layer_name, configured):
            return True
    return False


def resolve_layer_name(active_prefix, base_name, settings):
    """ResolveLayerName (:89-102)."""
    if not base_name or not active_prefix:
        return base_name
    return active_prefix + "_" + base_name if is_profile_scoped_layer(base_name, settings) else base_name


def is_layer_in_active_profile(active_prefix, layer_name):
    """IsLayerInActiveProfile (:132-139): everything is in scope without profiles."""
    if not active_prefix:
        return True
    return _has_prefix(layer_name, active_prefix)


def set_profile_layers_frozen(layers, prefix, frozen):
    """SetProfileLayersFrozen (:327-354); returns the layer names touched, in table order."""
    touched = []
    for name, layer in layers.items():
        if _has_prefix(name, prefix):
            layer["frozen"] = frozen
            touched.append(name)
    return touched


def adopt_existing_layers(layers, prefix, settings):
    """AdoptExistingLayers and RenameLayerIfExists (:356-399), in place; returns {old: new}."""
    renames = {}

    def rename(old_key, target):
        entry = layers.pop(old_key)
        layers[target] = entry
        renames[old_key] = target

    for base in PROFILE_SCOPED_LAYERS:
        key = _layer_key(layers, base)
        if key is None:
            continue
        target = prefix + "_" + base
        if _layer_key(layers, target) is not None:
            continue                                   # already prefixed (:367)
        rename(key, target)
    for field in ("StringLayer", "HomeRunLayer", "PanelGroupLayer"):
        base = settings.get(field)
        if not base:
            continue
        if any(_same(base, b) for b in BASE_SCOPED_LAYERS):
            continue                                   # handled above (:388)
        if "_" in base:
            continue                                   # the plugin's double-rename guard (:389)
        key = _layer_key(layers, base)
        if key is None:
            continue
        target = prefix + "_" + base
        if _layer_key(layers, target) is not None:
            continue
        rename(key, target)
    return renames


# ---------------------------------------------------------------------------
#  Profiles and the manager (DesignProfileManager.cs)
# ---------------------------------------------------------------------------

class DesignProfile:
    """One profile, its record fields in serialized order (DesignProfile.cs:14-29)."""

    __slots__ = ("name", "prefix", "settings", "created_utc", "drawing_state", "reopt_results")

    def __init__(self, name, prefix, settings, created_utc, drawing_state=None, reopt_results=None):
        self.name = name
        self.prefix = prefix
        self.settings = settings
        self.created_utc = created_utc
        self.drawing_state = drawing_state
        self.reopt_results = reopt_results

    def record(self):
        return {"Name": self.name, "Prefix": self.prefix, "Settings": copy.deepcopy(self.settings),
                "CreatedUtc": self.created_utc, "DrawingState": copy.deepcopy(self.drawing_state),
                "ReOptResults": copy.deepcopy(self.reopt_results)}

    def __repr__(self):
        return f"[{self.prefix}] {self.name}"                       # ToString (:31-34)


class DesignProfileManager:
    """The per-drawing coordinator: the active prefix and the stored profile list."""

    def __init__(self):
        self.active_prefix = None
        self.profiles = []
        self.corrupt = False

    # -- lookups --
    def find(self, prefix):
        return next((p for p in self.profiles if _same(p.prefix, prefix)), None)

    def get_active_profile(self):
        """GetActiveProfile (:288-293)."""
        if not self.active_prefix:
            return None
        return self.find(self.active_prefix)

    def next_prefix(self):
        """NextPrefix (:148-157)."""
        for code in range(ord("A"), ord("Z") + 1):
            prefix = chr(code)
            if all(not _same(p.prefix, prefix) for p in self.profiles):
                return prefix
        return "P" + str(len(self.profiles) + 1)

    # -- CRUD --
    def _save_active_profile_state(self, settings, drawing_props):
        """SaveActiveProfileState (:316-325)."""
        active = self.get_active_profile()
        if active is not None:
            active.settings = preset_from_current_settings(settings, active.name)
            active.drawing_state = drawing_state_from(drawing_props)

    def create_profile(self, name, adopt_existing, settings, *, created_utc, drawing_props=None, layers=None):
        """CreateProfile (:163-191). Returns the new profile."""
        if not isinstance(name, str) or not name or len(name) > MAX_NAME_CHARS:
            raise ProfileInputError(f"a profile name is 1 to {MAX_NAME_CHARS} chars")
        if len(self.profiles) >= MAX_PROFILES:
            raise ProfileInputError(f"a drawing holds at most {MAX_PROFILES} profiles")
        layers = _check_layers(layers)
        prefix = self.next_prefix()
        profile = DesignProfile(name, prefix, preset_from_current_settings(settings, name),
                                format_created_utc(created_utc), drawing_state_from(drawing_props))
        if adopt_existing and not self.profiles:
            adopt_existing_layers(layers, prefix, settings)
        self._save_active_profile_state(settings, drawing_props)
        self.profiles.append(profile)
        self.active_prefix = prefix
        return profile

    def swap_to(self, target_prefix, settings, *, drawing_props=None, layers=None):
        """SwapTo (:196-232): save the outgoing state, freeze its layers, thaw the target's,
        restore the target's settings and drawing state, make it active."""
        if _same(self.active_prefix, target_prefix):
            return
        target = self.find(target_prefix)
        if target is None:
            raise ProfileInputError(f"Profile '{target_prefix}' not found.")
        layers = _check_layers(layers)
        self._save_active_profile_state(settings, drawing_props)
        if self.active_prefix:
            set_profile_layers_frozen(layers, self.active_prefix, True)
        set_profile_layers_frozen(layers, target_prefix, False)
        apply_to_settings(target.settings, settings)
        apply_drawing_state(target.drawing_state, drawing_props)
        self.active_prefix = target_prefix

    def delete_profile(self, prefix, settings, *, layers=None):
        """DeleteProfile (:237-286): erase the profile's layers and their entities, drop it; a
        deleted active profile hands over to the first remaining one. Returns
        {"layers": [erased layer names], "entities": erased entity count}."""
        erased = {"layers": [], "entities": 0}
        profile = self.find(prefix)
        if profile is None:
            return erased
        layers = _check_layers(layers)
        was_active = _same(self.active_prefix, prefix)
        for name in [n for n in layers if _has_prefix(n, prefix)]:
            erased["entities"] += layers.pop(name).get("entities", 0)
            erased["layers"].append(name)
        self.profiles.remove(profile)
        if was_active:
            self.active_prefix = self.profiles[0].prefix if self.profiles else None
            if self.active_prefix:
                set_profile_layers_frozen(layers, self.active_prefix, False)
                following = self.find(self.active_prefix)
                if following is not None and following.settings is not None:
                    apply_to_settings(following.settings, settings)
        return erased

    def rename_profile(self, prefix, new_name):
        """RenameProfile (:298-310)."""
        profile = self.find(prefix)
        if profile is None:
            raise ProfileInputError(f"Profile '{prefix}' not found.")
        if not isinstance(new_name, str) or not new_name or len(new_name) > MAX_NAME_CHARS:
            raise ProfileInputError(f"a profile name is 1 to {MAX_NAME_CHARS} chars")
        profile.name = new_name
        if profile.settings is not None:
            profile.settings["Name"] = new_name

    # -- the record (SaveToDrawing :458-506, LoadFromDrawing :508-557) --
    def record(self):
        """The decoded design-profiles record SaveToDrawing writes: Version 1, the active prefix,
        every profile in stored order."""
        return {"Version": PROFILE_STORE_VERSION, "ActivePrefix": self.active_prefix,
                "Profiles": [p.record() for p in self.profiles]}

    def record_json(self):
        """The record's text as the plugin writes it (Newtonsoft, compact, non-ASCII unescaped)."""
        return json.dumps(self.record(), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def load_record(text):
    """LoadFromDrawing (:508-557): no record is an empty manager, and so is an unreadable one
    (the plugin's legacy mode), flagged `corrupt`. Profiles missing from the record are []."""
    mgr = DesignProfileManager()
    if text is None:
        return mgr
    try:
        if not isinstance(text, str) or len(text) > MAX_RECORD_CHARS:
            raise ValueError("record text")
        store = json.loads(text)
        if store is None:
            return mgr
        if not isinstance(store, dict):
            raise ValueError("record shape")
        active = store.get("ActivePrefix")
        raw = store.get("Profiles") or []
        if (active is not None and not isinstance(active, str)) or not isinstance(raw, list) \
                or len(raw) > MAX_PROFILES:
            raise ValueError("record shape")
        profiles = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("profile shape")
            name, prefix, settings = item.get("Name"), item.get("Prefix"), item.get("Settings")
            if not isinstance(name, str) or not isinstance(prefix, str):
                raise ValueError("profile shape")
            if settings is not None:
                validate_preset(settings)
            profiles.append(DesignProfile(name, prefix, settings, item.get("CreatedUtc"),
                                          item.get("DrawingState"), item.get("ReOptResults")))
    except (ValueError, TypeError, RecursionError):
        mgr.corrupt = True
        return mgr
    mgr.active_prefix = active
    mgr.profiles = profiles
    return mgr


# ---------------------------------------------------------------------------
#  LEAFPROFILE (ProfileCommand.cs)
# ---------------------------------------------------------------------------

class _Prompts:
    """The editor's answers, in order. An exhausted or empty answer is Enter."""

    def __init__(self, answers):
        if not isinstance(answers, (list, tuple)) or len(answers) > MAX_ANSWERS or \
                not all(isinstance(a, str) and len(a) <= MAX_NAME_CHARS for a in answers):
            raise ProfileInputError(f"answers must be at most {MAX_ANSWERS} strings")
        self._answers = list(answers)
        self.consumed = 0

    def _next(self):
        if self.consumed >= len(self._answers):
            return ""
        self.consumed += 1
        return self._answers[self.consumed - 1]

    def string(self):
        """GetString with spaces allowed: ("ok", text) or ("none", "") on Enter."""
        text = self._next()
        return ("ok", text) if text else ("none", "")

    def keyword(self, keywords, allow_none):
        """GetKeywords: a case-insensitive keyword or its unique abbreviation. Enter is "none"
        when the prompt allows it and "cancel" otherwise; any other answer is refused."""
        text = self._next()
        if not text:
            return ("none", "") if allow_none else ("cancel", "")
        exact = [k for k in keywords if _same(k, text)]
        partial = [k for k in keywords if k.upper().startswith(text.upper())]
        match = exact or (partial if len(partial) == 1 else [])
        if not match:
            raise ProfileInputError(f"answer {text!r} is not one of {list(keywords)}")
        return ("ok", match[0])


def leafprofile(manager, settings, answers, *, created_utc=None, drawing_props=None, layers=None):
    """LEAFPROFILE (ProfileCommand.cs:16-191) against `manager` and the plugin's current
    `settings` (both updated in place). Returns {"subcommand", "messages", "listed",
    "consumed", "saved"}: the editor lines, the (prefix, name) pairs a List printed, how many
    answers were read, and whether the manager wrote its record."""
    if not isinstance(manager, DesignProfileManager):
        raise ProfileInputError("manager must be a DesignProfileManager")
    validate_settings(settings)
    layers = _check_layers(layers)
    prompts = _Prompts(answers)
    out = {"subcommand": None, "messages": [], "listed": [], "consumed": 0, "saved": False}
    say = out["messages"].append

    status, sub = prompts.keyword(SUBCOMMANDS, allow_none=False)
    if status == "ok":
        out["subcommand"] = sub
        if sub == "Create":
            _create(manager, settings, prompts, say, out, created_utc, drawing_props, layers)
        elif sub == "Swap":
            _swap(manager, settings, prompts, say, out, drawing_props, layers)
        elif sub == "Delete":
            _delete(manager, settings, prompts, say, out, layers)
        else:
            _list(manager, say, out)
    out["consumed"] = prompts.consumed
    return out


def _create(mgr, settings, prompts, say, out, created_utc, drawing_props, layers):
    """CreateProfile (ProfileCommand.cs:43-80)."""
    status, text = prompts.string()
    if status != "ok" or not text.strip():
        return
    name = text.strip()
    if any(_same(p.name, name) for p in mgr.profiles):
        say(f"Profile '{name}' already exists.")
        return
    adopt = False
    if not mgr.profiles:
        status, answer = prompts.keyword(("Yes", "No"), allow_none=True)
        adopt = answer == "Yes" if status == "ok" else True          # Enter takes the default Yes
    moment = created_utc if created_utc is not None else datetime.now(timezone.utc)
    profile = mgr.create_profile(name, adopt, settings, created_utc=moment,
                                 drawing_props=drawing_props, layers=layers)
    out["saved"] = True
    say(f"Profile '{profile.name}' created (prefix: {profile.prefix}).")
    say(f"  Active profile is now: {profile.name}")


def _swap(mgr, settings, prompts, say, out, drawing_props, layers):
    """SwapProfile (ProfileCommand.cs:82-114)."""
    if len(mgr.profiles) < 2:
        say("Need at least 2 profiles to swap. Use LEAFPROFILE Create first.")
        return
    keywords = [p.prefix for p in mgr.profiles if not _same(p.prefix, mgr.active_prefix)]
    status, target_prefix = prompts.keyword(keywords, allow_none=False)
    if status != "ok":
        return
    changed = not _same(mgr.active_prefix, target_prefix)
    mgr.swap_to(target_prefix, settings, drawing_props=drawing_props, layers=layers)
    out["saved"] = changed
    target = mgr.get_active_profile()
    say(f"Swapped to profile '{target.name if target else ''}' (prefix: {target_prefix}).")


def _delete(mgr, settings, prompts, say, out, layers):
    """DeleteProfile (ProfileCommand.cs:116-167)."""
    if not mgr.profiles:
        say("No profiles to delete.")
        return
    status, prefix = prompts.keyword([p.prefix for p in mgr.profiles], allow_none=False)
    if status != "ok":
        return
    profile = mgr.find(prefix)
    if profile is None:
        return
    status, confirm = prompts.keyword(("Yes", "No"), allow_none=True)
    if status != "ok" or confirm != "Yes":
        say("Deletion cancelled.")
        return
    mgr.delete_profile(prefix, settings, layers=layers)
    out["saved"] = True
    say(f"Profile '{profile.name}' deleted.")
    active = mgr.get_active_profile()
    say(f"  Active profile is now: {active.name}" if active is not None else "  No profiles remaining.")


def _list(mgr, say, out):
    """ListProfiles (ProfileCommand.cs:169-191); read only."""
    if not mgr.profiles:
        say("No design profiles configured. Use LEAFPROFILE Create to start.")
        return
    say("")
    say("  Design Profiles:")
    say("  " + "\u2500" * 37)
    for p in mgr.profiles:
        marker = " \u25c4 ACTIVE" if _same(p.prefix, mgr.active_prefix) else ""
        s = p.settings or {}
        say(f"  [{p.prefix}] {p.name}{marker}")
        say(f"      Module: {s.get('ModuleSelection') if s.get('ModuleSelection') is not None else '(none)'}")
        say(f"      Inverter: {s.get('InverterSelection') if s.get('InverterSelection') is not None else '(none)'}")
        n = s.get("NumPanelsInSequence")
        say(f"      String length: {'' if n is None else n}")
        out["listed"].append((p.prefix, p.name))
    say("")
