"""The drawing-owned design preset store behind LEAFPROFILE, kept in the graph's top-level extra.

The plugin writes its design-profiles record into the drawing (DesignProfileManager.SaveToDrawing)
and takes the current settings from its own Settings.Default. Studio keeps both per drawing, in
graph["extra"]["design_profiles"] = {"schema", "current_settings", "record"}:

  current_settings  the 60 current-setting fields a preset copies (solar_design_profiles.SETTINGS_FIELD_NAMES)
  record            the kernel's decoded record: {"Version": 1, "ActivePrefix", "Profiles"}; every
                    profile's CreatedUtc is null and its DrawingState is the plugin's default snapshot

Top-level extra is an unconstrained object in contract/solar-design-graph.v1.schema.json and sits
outside every digest a design output binds (solar_solve_results.upstream_basis and
solar_sizing_client.sizing_basis read project, settings, zones, frames and panels only), so a
preset edit changes no entity and stales no output. No graph schema change is needed.

Bounds, each failing closed with a named GraphValidationError: a name is 1 to 128 characters after
trimming with no control character; a stored prefix is one letter A to Z or P plus two digits; at
most 64 presets; a text setting is a string of at most 256 characters (never null); an integer
setting is in 0..1000000; a number setting is finite and in -1000000..1000000 and is stored as a
float; the whole store is at most 262144 canonical bytes. Every pass is linear in the preset count;
no I/O, nothing cached.
"""
import copy
import json
import math
import re

import solar_design_profiles as profiles
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError

STORE_KEY = "design_profiles"
STORE_SCHEMA = "leaf.solar-design-presets.v1"
MAX_PRESETS = 64
MAX_NAME_CHARS = 128
MAX_TEXT_CHARS = 256
MAX_INT = 1000000
MAX_NUMBER = 1000000
MAX_STORE_BYTES = 262144
INVALID_SETTINGS = "INVALID_DESIGN_PRESET_REQUEST"
SETTINGS_OUT_OF_RANGE = "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"
STORE_INVALID = "DESIGN_PRESETS_STORE_INVALID"
STORE_TOO_LARGE = "DESIGN_PRESETS_STORE_TOO_LARGE"
_KIND = dict(profiles.PRESET_FIELDS)
_STORED_PREFIX = re.compile(r"[A-Z]|P[0-9]{2}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_STORE_KEYS = frozenset(("schema", "current_settings", "record"))
_RECORD_KEYS = frozenset(("Version", "ActivePrefix", "Profiles"))
_PROFILE_KEYS = frozenset(("Name", "Prefix", "Settings", "CreatedUtc", "DrawingState", "ReOptResults"))
_DEFAULT_STATE = profiles.drawing_state_from(None)


def valid_name(name):
    """A stored or requested preset name: already trimmed, 1..128 characters, no control character."""
    return (type(name) is str and 0 < len(name) <= MAX_NAME_CHARS and name.strip() == name
            and _CONTROL.search(name) is None)


def settings_error(settings):
    """None when `settings` is exactly the 60 current-setting fields within Studio's bounds, else
    the refusal code. Shape and kind are checked for every field before any range."""
    if type(settings) is not dict or set(settings) != set(profiles.SETTINGS_FIELD_NAMES):
        return INVALID_SETTINGS
    for field in profiles.SETTINGS_FIELD_NAMES:
        kind, value = _KIND[field], settings[field]
        if kind == "s":
            ok = type(value) is str
        elif kind == "b":
            ok = type(value) is bool
        elif kind == "i":
            ok = type(value) is int
        else:
            ok = type(value) is int or (type(value) is float and math.isfinite(value))
        if not ok:
            return INVALID_SETTINGS
    for field in profiles.SETTINGS_FIELD_NAMES:
        kind, value = _KIND[field], settings[field]
        if ((kind == "s" and len(value) > MAX_TEXT_CHARS)
                or (kind == "i" and not 0 <= value <= MAX_INT)
                or (kind == "f" and (not -MAX_NUMBER <= value <= MAX_NUMBER
                                     or (value == 0 and math.copysign(1, value) < 0)))):
            return SETTINGS_OUT_OF_RANGE
    return None


def normalized_settings(settings):
    """A validated settings object in stored form: number fields as floats, in declaration order."""
    return {field: float(settings[field]) if _KIND[field] == "f" else settings[field]
            for field in profiles.SETTINGS_FIELD_NAMES}


def _stored_settings_ok(settings):
    return (settings_error(settings) is None
            and all(type(settings[f]) is float for f in profiles.SETTINGS_FIELD_NAMES if _KIND[f] == "f"))


def _check_store(store):
    """True when `store` is a store this module could have written."""
    if type(store) is not dict or set(store) != _STORE_KEYS or store["schema"] != STORE_SCHEMA:
        return False
    if not _stored_settings_ok(store["current_settings"]):
        return False
    record = store["record"]
    if type(record) is not dict or set(record) != _RECORD_KEYS:
        return False
    if type(record["Version"]) is not int or record["Version"] != profiles.PROFILE_STORE_VERSION:
        return False
    items = record["Profiles"]
    if type(items) is not list or len(items) > MAX_PRESETS:
        return False
    names, prefixes = set(), set()
    for item in items:
        if type(item) is not dict or set(item) != _PROFILE_KEYS:
            return False
        name, prefix, preset = item["Name"], item["Prefix"], item["Settings"]
        if (not valid_name(name) or type(prefix) is not str or not _STORED_PREFIX.fullmatch(prefix)
                or type(preset) is not dict or set(preset) != set(profiles.PRESET_FIELD_NAMES)
                or preset["Name"] != name or preset["IsBuiltIn"] is not False
                or not _stored_settings_ok({f: preset[f] for f in profiles.SETTINGS_FIELD_NAMES})
                or item["CreatedUtc"] is not None
                or json.dumps(item["DrawingState"], sort_keys=True, allow_nan=False) !=
                   json.dumps(_DEFAULT_STATE, sort_keys=True, allow_nan=False)
                or item["ReOptResults"] is not None):
            return False
        names.add(name.upper())
        prefixes.add(prefix.upper())
    if len(names) != len(items) or len(prefixes) != len(items):
        return False
    active = record["ActivePrefix"]
    if not items:
        return active is None
    return type(active) is str and any(item["Prefix"] == active for item in items)


def load(graph):
    """(current settings or None, manager) for a validated graph. An absent store is an empty
    manager with no current settings; a malformed one is DESIGN_PRESETS_STORE_INVALID."""
    manager = profiles.DesignProfileManager()
    extra = graph["extra"]
    if STORE_KEY not in extra:
        return None, manager
    store = extra[STORE_KEY]
    try:
        valid = _check_store(store) and len(canonical_bytes(store)) <= MAX_STORE_BYTES
    except (TypeError, ValueError, RecursionError):
        valid = False
    if not valid:
        raise GraphValidationError(STORE_INVALID)
    record = store["record"]
    manager.active_prefix = record["ActivePrefix"]
    manager.profiles = [profiles.DesignProfile(item["Name"], item["Prefix"], copy.deepcopy(item["Settings"]),
                                               None, copy.deepcopy(item["DrawingState"]), None)
                        for item in record["Profiles"]]
    return copy.deepcopy(store["current_settings"]), manager


def save(graph, current, manager):
    """Write the store into graph["extra"] in place. Every profile's CreatedUtc is null: a pure
    builtin has no clock, and the drawing version that published the change carries its time."""
    for profile in manager.profiles:
        profile.created_utc = None
    store = {"schema": STORE_SCHEMA, "current_settings": normalized_settings(current),
             "record": manager.record()}
    if len(canonical_bytes(store)) > MAX_STORE_BYTES:
        raise GraphValidationError(STORE_TOO_LARGE)
    if not _check_store(store):
        raise GraphValidationError(STORE_INVALID)
    graph["extra"][STORE_KEY] = store
    return store


def _row(row_id, row_type, **fields):
    return {"id": {"entity_id": row_id}, "type": row_type, "quantity": 1, "unit": "each", **fields}


def _order(row):
    row_id, row_type = row["id"]["entity_id"], row["type"]
    suffix = row_id[len(row_type) + 1:]
    return (row_type, (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def state_rows(manager):
    """The G32 state rows the parity receipts record for Create, Swap and Delete: one design-profile
    row per preset in stored order, then the active prefix and the record version."""
    record = manager.record()
    rows = [_row(f"design-profile-{n}", "design-profile", name=item["Name"], prefix=item["Prefix"],
                 settings=profiles.canonical_settings_text(item["Settings"]))
            for n, item in enumerate(record["Profiles"], 1)]
    rows.append(_row("report-active-prefix", "report", name="active-prefix", value=record["ActivePrefix"] or ""))
    rows.append(_row("report-profile-version", "report", name="profile-version", value=record["Version"]))
    return sorted(rows, key=_order)


def list_rows(manager):
    """The G32 rows the parity receipts record for List: the preset count and "<prefix> <name>" each."""
    rows = [_row("report-profiles", "report", name="profiles", value=len(manager.profiles))]
    rows += [_row(f"report-profile-{n}", "report", name=f"profile-{n}", value=f"{p.prefix} {p.name}")
             for n, p in enumerate(manager.profiles, 1)]
    return sorted(rows, key=_order)
