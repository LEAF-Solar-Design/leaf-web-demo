"""Studio's inverter-family shared state and step delta (contract G35, G35a).

A G35 state is the projection of one drawing the inverter commands read and write, in exactly the
shape the plugin adapter writes as state-iN.json (Branch2025 tools/parity/inverter_evidence.py, read
2026-09-24 at C:/tmp/solar-parity/wt-b25-s69, master 6b940d51):

  {"format": "inverter-state-v1",
   "source": {"dump_sha256": str, "reopened": bool},
   "rows": {"device": [...], "string-assignment": [...], "cable": [...], "schedule": [...],
            "lbd": [...]},
   "setting": {neutral setting name: value},
   "geometry": {"strings": [...], "panel_groups": [...]}}

Every row carries its public G35 fields plus an optional private `_detail` (never compared, never
in evidence) and, once loaded here, a private `_pair`: the identity a row keeps while an engine edits
it, standing in for the AutoCAD handle the plugin adapter pairs rows by (inverter_evidence.py:627-642).
Rows an engine creates get a fresh `_pair`; a created object is identified in evidence only by its
place in the row order, exactly as the adapter does.

This module ports, literally:

  inverter_evidence.py:589-611   the row order per kind (ORDER, _ref_key, _position_key)
  inverter_evidence.py:614-621   public_row and publish
  inverter_evidence.py:627-706   _pair_keys, row_delta (identical rows cancel first, then pairing on
                                 each key in turn where it is unique on both sides) and delta_rows
                                 (removed, changed, added; ids <kind>-<n> in the kind's row order)
  inverter_evidence.py:709-744   save_noise_only and the setting map without the route store clock
  inverter_evidence.py:160-176, 747-756
                                 the report rules and report_message
  inverter_evidence.py:759-790   step_rows: the delta rows, the G20 setting rows, the report row
  ground_evidence.py:308-353, 1370-1404
                                 DECLARED_DEFAULTS and setting_records (absent is the declared default)
  ground_evidence.py:397-429, 977-996, 1140-1142
                                 canonical, digest, the bounds, order_key, angle and the row shape

Pure functions over plain data: no CAD host, no I/O beyond load_state's one bounded read, no network.
Every malformed input fails closed with InverterStateError (a ValueError).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re

STATE_FORMAT = "inverter-state-v1"
FORMAT = "inverter-v1"
UNITS = "in"                                  # G35a: the rooftop solve fixture is in inches
ROW_KINDS = ("device", "string-assignment", "cable", "schedule", "lbd")
SCHEMA = "leaf.solar-w1-comparison.v1"
FRAME = {"coordinate_system": "world",
         "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": "none"}

# Bounds: a state file is about 350 KB for 173 strings; these hold a drawing many times larger and
# still refuse a runaway input.
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_ROWS_PER_KIND = 200_000
MAX_NODES = 100_000                           # the comparator's document bounds (ground_evidence.py:66-68)
MAX_NODE_DEPTH = 40
MAX_DOC_BYTES = 2 * 1024 * 1024

# Setting names (inverter_evidence.py:123-124).
L1_SETTING, ROUTE_SETTING = "CombinerStringL1Assignments", "UtilityCableRoutes"
ROUTE_CLOCK = "CreatedUtc"
CATALOG = "CableCatalog"

# HomerunRoutingConfig.CreateDefault() (HomerunRoutingConfig.cs:14-75, ground_evidence.py:313-321).
HOMERUN_ROUTING_DEFAULT = {
    "DcHomerunLayer": "LEAF-DC-HOMERUN", "FullRoutingStringThreshold": 100,
    "RoadCostMultiplier": 3.0, "FenceCostMultiplier": 3.0, "OpenGroundCostMultiplier": 1.0,
    "TrayCostMultiplier": 0.45, "TrenchCostMultiplier": 2.0, "RoadCrossingCostMultiplier": 0.3,
    "MinBendRadiusDrawingUnits": 36.0, "FenceBufferDrawingUnits": 2.0,
    "CableCatalog": [{"CircuitType": "DC", "Gauge": "3 AWG", "MaxLengthFt": 400.0,
                      "Description": "Default DC homerun"},
                     {"CircuitType": "DC", "Gauge": "1/0 AWG", "MaxLengthFt": 1000000.0,
                      "Description": "Upsized long DC homerun"}]}
DEFAULT_CATALOG = HOMERUN_ROUTING_DEFAULT[CATALOG]

# G20 absent-is-default (ground_evidence.py:322-353, DrawingPropertiesJson.cs:502-663).
DECLARED_DEFAULTS = {
    "BranchMaxOffset": 60.0, "AlignmentTolerance": 36.0, "ExtractPanelsFromRackBlocks": False,
    "InstallationDesign": "Ground", "StringLayer": "String", "StringWidth": 3.0, "TagHeight": 5.0,
    "TagColor": "Bylayer", "UseInverterColor": True, "HomeRunLayer": "HomeRun",
    "InverterBlock": "Inverter", "InverterBlockSize": 24.0, "PanelGroupLabelHeight": 50.0,
    "PanelGroupOutlineWidth": 2.0, "PanelGroupLayer": "Panel Group", "UseL2Collectors": False,
    "VisualScaleOverride": 0.0, "SymbolBlockScaleOverride": 0.0,
    "PanelsInSequence": 0, "OptimizerRatio": 1,
    "PanelLayerContains": "", "FlowTypeMarker": "", "NumMppt": 3, "GlobalStringSizingConfirmed": False,
    "SolarEdgePdfImportConfirmed": False, "SolarEdgeImportedStringCount": 0, "StringPerMppt": 3,
    "StringNumber": 1, "InverterNumber": 1, "MPPTLetter": "a", "PanelGroupColour": 0,
    "PanelGroupNumber": 1, "ElevationZones": [], "ElectricalZones": [], "FrameGroups": [],
    "BomColumnConfigs": [], "L1ToL2Assignments": {}, "StringToL1CombinerAssignments": {},
    "HomerunRouting": HOMERUN_ROUTING_DEFAULT, "L1ToL2InputAssignments": {}, "InverterTypes": {},
    "InverterTypeAssignments": {}, "DesignContracts": {}, "ProjectZipCode": "", "ProjectName": "",
    "ProjectCity": "", "ProjectState": "", "ProjectLatitude": 0.0, "ProjectLongitude": 0.0,
    "LeafProjectCanceled": False, "CustomerWelcomeDismissed": False, "TrackerModuleAlongAxisM": 0.0,
    "TrackerModuleCrossAxisM": 0.0, "TrackerModuleGapM": 0.0, "LbdLayer": "LBD",
    "TrenchLayer": "LEAF-PVCASE-TRENCH", "TrenchSnapDistanceM": 5.0,
    "GradingMode": 0, "ShadeLossPerModule": {},
    "VocColdPasses": None, "VocColdOverrideAccepted": False, "VocColdSuggestedStringLength": 0,
    "VocColdPerModule": 0.0, "VocColdStringVoltage": 0.0, "VocColdMaxDcVoltage": 0.0,
    "DrawingUnitIsFeet": False, "ShadeLimitAngleDeg": 0.0, "TrackerRailOverhangM": 0.0,
    "TorqueTubeHeightM": 0.0, "TrackerCorridorGapM": 0.0, "TrackerSecondaryCorridorGapM": 0.0,
    "GradingElevationM": 0.0, "LeafSpacingMinPitchM": 0.0, "TrackerModulePmaxW": 0.0,
    "TrackerTorqueTubeRadiusM": 0.0, "ShadeLossHeatmapApplied": False,
}
_ABSENT = object()
# ground_evidence.UNITS: setting_value writes a length setting (name ending in M) in metres.
_SETTING_LENGTH_UNIT = "m"

# G35 `report` rules (inverter_evidence.py:163-176): the outcome class read from the step's own printed
# line (the first pattern any line matches), else the step's declared class.
REPORT_RULES = {
    "i4": ((), "no-imbalance"),
    "i6": (((r"LEAFTRENCHAUTO: no panel groups on layer", "no-panel-groups"),
            (r"LEAFTRENCHAUTO: .*\bno trench", "no-trench")), "no-change"),
    "i7": (((r"LEAFCABLETOTRAYAUTO: snapped 0 cables", "none-snapped"),), "no-change"),
    "i9": ((), "export-failed"),
    "i10": (((r"LEAFSKIDRECONCILE: MISMATCH", "mismatch"), (r"LEAFSKIDRECONCILE: (?:OK|MATCH)", "match")),
            "no-change"),
    "i11": (((r"^0 nearest-lane comb feeder\(s\) drawn", "no-feeders"),), "no-change"),
    "i16": (((r"No HOMERUN-TRUNK layer found", "no-homerun-trunk"),), "no-change"),
    "i17": (((r"No inverters found in drawing", "no-inverters"),), "no-change"),
    "i20": (((r"LEAFCABLETOTRAY: No trench within", "no-trench"),), "no-change"),
    "i21": (((r"LEAFDEVICESPATTERN: no tracker rows found", "no-tracker-rows"),), "no-change"),
    # G36: the cabling studio's open status (CablingViewPaletteControl.cs:267).
    "l1": (((r"LEAFLITEPLACE: environment:", "studio-opened"),), "no-change"),
}
MAX_REPORT_LINES = 10_000


class InverterStateError(ValueError):
    """A malformed state, row or setting: nothing is computed from it."""


# --------------------------------------------------------------- canonical --

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def check_bounds(value, label):
    """The comparator's structural bounds (node count, depth, canonical bytes), iteratively."""
    budget = MAX_NODES
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        budget -= 1
        if budget < 0:
            raise InverterStateError(f"{label} exceeds the comparator bound of {MAX_NODES} nodes")
        if depth > MAX_NODE_DEPTH:
            raise InverterStateError(f"{label} exceeds the comparator depth bound of {MAX_NODE_DEPTH}")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    size = len(canonical(value))
    if size > MAX_DOC_BYTES:
        raise InverterStateError(f"{label} is {size} canonical bytes, over the comparator bound of {MAX_DOC_BYTES}")


def semantic_hash(value, label="document"):
    """sha256 of canonical JSON under the comparator's bounds (solar_w1_compare.semantic_hash)."""
    check_bounds(value, label)
    return digest(value)


def order_key(x, y):
    """G12: ascending (y, x) rounded to 1e-6."""
    return (round(y, 6), round(x, 6))


def coordinate(x, y):
    """G17: one coordinate quantity of 2 numbers, in drawing units (inches)."""
    return {"kind": "coordinate", "value": [float(x), float(y)], "unit": UNITS}


def angle(degrees):
    """G20: angles are quantities in degrees."""
    return {"kind": "angle", "value": float(degrees), "unit": "deg"}


def row(record, kind):
    """ground_evidence._row: {id: {entity_id}, type, quantity 1, unit each, **fields}."""
    return {"id": {"entity_id": record["id"]}, "type": kind, "quantity": 1, "unit": "each",
            **record["fields"]}


# ------------------------------------------------------------------- state --

def _finite(value, what):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise InverterStateError(f"{what} must be a finite number")
    return float(value)


def point_of(quantity, what="position"):
    """[x, y] of a G17 coordinate quantity."""
    if not isinstance(quantity, dict) or quantity.get("kind") != "coordinate" or \
            not isinstance(quantity.get("value"), list) or len(quantity["value"]) != 2:
        raise InverterStateError(f"{what} must be a 2D coordinate quantity")
    return [_finite(quantity["value"][0], what), _finite(quantity["value"][1], what)]


def validate_state(value):
    """A deep copy of a G35 state with every row's shape checked and a private `_pair` added to
    rows that lack one (loaded rows: "<kind>:<n>", their place in the loaded file)."""
    if not isinstance(value, dict) or value.get("format") != STATE_FORMAT:
        raise InverterStateError(f"a state must be an object of format {STATE_FORMAT!r}")
    for key in ("source", "rows", "setting", "geometry"):
        if not isinstance(value.get(key), dict):
            raise InverterStateError(f"state {key!r} must be an object")
    source = value["source"]
    if type(source.get("reopened")) is not bool:
        raise InverterStateError("state source must say whether it was reopened")
    state = copy.deepcopy(value)
    rows = state["rows"]
    if set(rows) != set(ROW_KINDS):
        raise InverterStateError(f"state rows must be exactly {ROW_KINDS}")
    for kind in ROW_KINDS:
        items = rows[kind]
        if not isinstance(items, list) or len(items) > MAX_ROWS_PER_KIND:
            raise InverterStateError(f"state rows {kind!r} must be a list of at most {MAX_ROWS_PER_KIND}")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise InverterStateError(f"a {kind} row must be an object")
            _check_row(kind, item)
            item.setdefault("_pair", f"{kind}:{index}")
    geometry = state["geometry"]
    for key in ("strings", "panel_groups"):
        if not isinstance(geometry.get(key), list):
            raise InverterStateError(f"state geometry {key!r} must be a list")
    return state


def _check_row(kind, item):
    try:
        ORDER[kind](item)
    except (KeyError, TypeError, ValueError, InverterStateError) as exc:
        raise InverterStateError(f"a {kind} row lacks its ordering fields: {exc}") from None
    if kind == "device":
        for key in ("role", "scale", "rotation", "placement", "hardware"):
            if key not in item:
                raise InverterStateError(f"a device row lacks {key!r}")


def load_state(path):
    """One state file, bounded, parsed with duplicate keys refused, validated."""
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise InverterStateError(f"{path.name} exceeds {MAX_STATE_BYTES} bytes")

    def unique(pairs):
        out = {}
        for key, item in pairs:
            if key in out:
                raise InverterStateError(f"{path.name} repeats the key {key!r}")
            out[key] = item
        return out

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InverterStateError(f"{path.name} is not UTF-8 JSON: {exc}") from None
    return validate_state(value)


def new_pair(state, kind):
    """A fresh private identity for a row an engine creates in `state`."""
    counter = state.setdefault("_created", 0) + 1
    state["_created"] = counter
    return f"{kind}:new-{counter}"


# ---------------------------------------------------------------- ordering --

def _hex_key(handle):
    return int(handle, 16)


def _ref_key(value):
    if type(value) is int:
        return (0, value, "")
    if type(value) is str:
        return (1, _hex_key(value), "") if re.fullmatch(r"[0-9A-F]+", value) else (1, 0, value)
    return (2, 0, "")


def _position_key(item):
    x, y = point_of(item["position"])
    return order_key(x, y)


# Row order per kind (G35): devices by number then position, strings by handle, cables by kind, from,
# to, schedules by index, LBDs by kind then position (inverter_evidence.py:604-611).
ORDER = {
    "device": lambda item: (item["number"] is None, item["number"] or 0, _position_key(item)),
    "string-assignment": lambda item: (item["string"] is None, _hex_key(item["string"] or "0")),
    "cable": lambda item: (item["cable_kind"], _ref_key(item["from"]), _ref_key(item["to"]),
                           item.get("segment") or "", canonical(item["vertices"])),
    "schedule": lambda item: (item["index"],),
    "lbd": lambda item: (item["lbd_kind"], _position_key(item)),
}


def public_row(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


def publish(state):
    """A state as the adapter writes it: every row without its private pairing identity, and no
    other private key at the top level."""
    return {**{key: value for key, value in state.items() if not key.startswith("_")},
            "rows": {kind: [{key: item for key, item in entry.items() if key != "_pair"}
                            for entry in entries] for kind, entries in state["rows"].items()}}


def sort_rows(state):
    for kind in ROW_KINDS:
        state["rows"][kind].sort(key=ORDER[kind])
    return state


# ------------------------------------------------------------------- delta --

def _pair_keys(kind, item):
    """The keys a row is matched by across two states, in order: its private identity, then a
    neutral identity (a recreated object keeps it)."""
    keys = [("handle", item.get("_pair"))]
    if kind == "device":
        keys.append(("position", canonical(item["position"])))
        keys.append(("number", (item["role"], item["number"]) if item["number"] is not None else None))
    elif kind == "string-assignment":
        keys.append(("string", item["string"]))
    elif kind == "cable":
        keys.append(("ends", (item["cable_kind"], item.get("segment"), repr(item["from"]), repr(item["to"]))))
    elif kind == "schedule":
        keys.append(("index", item["index"]))
    elif kind == "lbd":
        keys.append(("lbd", (item["lbd_kind"], canonical(item["feeder"]))))
    return keys


def row_delta(kind, before, after):
    """(added, removed, changed) rows of one kind: identical rows cancel first (by content, so an
    object recreated unchanged is no change), then the rest pair on each key in turn where it is
    unique on both sides; a paired row that differs is `changed` (its new fields). Linear in rows."""
    old = list(before)
    new = list(after)
    pool = {}
    for index, item in enumerate(old):
        pool.setdefault(canonical(public_row(item)), []).append(index)
    gone, kept = set(), []
    for item in new:
        indexes = pool.get(canonical(public_row(item)), [])
        choice = next((i for i in indexes if i not in gone and old[i].get("_pair") == item.get("_pair")), None)
        if choice is None:
            choice = next((i for i in indexes if i not in gone), None)
        if choice is None:
            kept.append(item)
        else:
            gone.add(choice)
    old = [item for index, item in enumerate(old) if index not in gone]
    new = kept
    changed = []
    passes = len(_pair_keys(kind, (new or old)[0])) if (new or old) else 0
    for position in range(passes):
        old_keys, new_keys = {}, {}
        for index, item in enumerate(old):
            key = _pair_keys(kind, item)[position][1]
            if key is not None:
                old_keys.setdefault(key, []).append(index)
        for index, item in enumerate(new):
            key = _pair_keys(kind, item)[position][1]
            if key is not None:
                new_keys.setdefault(key, []).append(index)
        matched_old, matched_new = set(), set()
        for key, indexes in new_keys.items():
            if len(indexes) == 1 and len(old_keys.get(key, ())) == 1:
                matched_new.add(indexes[0])
                matched_old.add(old_keys[key][0])
                changed.append(new[indexes[0]])
        old = [item for index, item in enumerate(old) if index not in matched_old]
        new = [item for index, item in enumerate(new) if index not in matched_new]
    return new, old, changed


CHANGE_RANK = {"removed": 0, "changed": 1, "added": 2}


def delta_rows(before, after):
    """G35 delta rows of every row kind: each row's public fields plus `change`, ordered by the kind's
    row order (then removed, changed, added), ids <kind>-<n> in that order."""
    result = {}
    for kind in ROW_KINDS:
        added, removed, changed = row_delta(kind, before["rows"][kind], after["rows"][kind])
        records = [(item, "added") for item in added] + [(item, "removed") for item in removed] + \
                  [(item, "changed") for item in changed]
        records.sort(key=lambda entry: (ORDER[kind](entry[0]), CHANGE_RANK[entry[1]]))
        if records:
            result[kind] = [{"id": f"{kind}-{number}", "key": None,
                             "fields": {**public_row(item), "change": change}}
                            for number, (item, change) in enumerate(records, 1)]
    return result


# ---------------------------------------------------------------- settings --

def _same_setting(a, b):
    if a is _ABSENT or b is _ABSENT:
        return a is b
    numeric = (int, float)
    if type(a) in numeric and type(b) in numeric:
        return float(a) == float(b)   # a C# double written as 0 or 0.0 is the same value
    return canonical(a) == canonical(b)


def setting_value(name, value):
    """G20: a length when the name ends in M, an angle when it ends in Deg, nested objects as their
    canonical JSON text, any other value exactly."""
    if value is _ABSENT:
        return None
    if isinstance(value, (dict, list)):
        return canonical(value).decode("utf-8")
    if type(value) in (int, float):
        if name.endswith("Deg"):
            return angle(value)
        if name.endswith("M"):
            return {"kind": "length", "value": float(value), "unit": _SETTING_LENGTH_UNIT}
    return value


def setting_records(before, after):
    """One `setting` record per changed drawing setting, by name. Absent is the declared default."""
    old, new = before or {}, after or {}
    records = []
    for name in sorted(set(old) | set(new)):
        default = DECLARED_DEFAULTS.get(name, _ABSENT)
        was, now = old.get(name, default), new.get(name, default)
        if not _same_setting(was, now):
            records.append({"id": f"setting-{name}", "key": None,
                            "fields": {"name": name, "value": setting_value(name, now)}})
    return records


def setting_map(value):
    """The settings compared: the stored map with the route store's wall clock removed."""
    settings = dict(value)
    routes = settings.get(ROUTE_SETTING)
    if isinstance(routes, list):
        settings[ROUTE_SETTING] = [{k: v for k, v in route.items() if k != ROUTE_CLOCK}
                                   if isinstance(route, dict) else route for route in routes]
    return settings


def save_noise_only(before, after):
    """True when the settings changed only by the plugin's per-save cable catalog duplication (every
    load-then-save appends HomerunRoutingConfig.CreateDefault()'s two entries, the G27 finding
    settings-save-duplicates-cable-catalog), or not at all."""
    records = setting_records(before, after)
    names = [record["fields"]["name"] for record in records]
    if not names:
        return True
    if names != ["HomerunRouting"]:
        return False
    old = before.get("HomerunRouting", HOMERUN_ROUTING_DEFAULT)
    new = after.get("HomerunRouting", HOMERUN_ROUTING_DEFAULT)
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if {k: v for k, v in old.items() if k != CATALOG} != {k: v for k, v in new.items() if k != CATALOG}:
        return False
    old_catalog, new_catalog = old.get(CATALOG, DEFAULT_CATALOG), new.get(CATALOG, DEFAULT_CATALOG)
    if not isinstance(old_catalog, list) or not isinstance(new_catalog, list):
        return False
    grown = new_catalog[len(old_catalog):]
    return (new_catalog[:len(old_catalog)] == old_catalog and bool(grown) and len(grown) % 2 == 0
            and all(grown[i:i + 2] == DEFAULT_CATALOG for i in range(0, len(grown), 2)))


def save_drawing_properties(state):
    """One drawing-properties save: the stored HomerunRouting keeps every field and its cable catalog
    gains CreateDefault()'s two entries (the G27 finding; the stored config is loaded, the default
    catalog appended by the deserializer, and the result written back). In place; returns state."""
    settings = state["setting"]
    stored = settings.get("HomerunRouting", HOMERUN_ROUTING_DEFAULT)
    if not isinstance(stored, dict):
        raise InverterStateError("HomerunRouting must be an object")
    routing = copy.deepcopy(stored)
    catalog = routing.get(CATALOG, DEFAULT_CATALOG)
    if not isinstance(catalog, list):
        raise InverterStateError("HomerunRouting CableCatalog must be a list")
    routing[CATALOG] = list(catalog) + copy.deepcopy(DEFAULT_CATALOG)
    settings["HomerunRouting"] = routing
    return state


# ------------------------------------------------------------------ report --

def report_message(step, printed_lines):
    """The step's outcome class from the lines it printed (REPORT_RULES), else its declared class,
    else no-change (inverter_evidence.py:747-756)."""
    patterns, default = REPORT_RULES.get(step, ((), "no-change"))
    lines = [str(line).strip() for line in list(printed_lines or ())[:MAX_REPORT_LINES]]
    for pattern, message in patterns:
        regex = re.compile(pattern)
        if any(regex.search(line) for line in lines):
            return message
    return default


def step_rows(step, before, after, printed_lines=None):
    """The G35 evidence rows of one step, in emission order (row kind, then id), plus the setting
    records: the delta rows from `before` to `after`, the G20 setting rows of the keys the step
    changed, and, when it changed nothing (the per-save catalog duplication aside), the `report`
    row of its outcome class (inverter_evidence.py:775-787). Returns (rows, setting records)."""
    rows = delta_rows(before, after)
    old_settings, new_settings = setting_map(before["setting"]), setting_map(after["setting"])
    settings = setting_records(old_settings, new_settings)
    if settings:
        rows["setting"] = settings
    if not any(kind in rows for kind in ROW_KINDS) and save_noise_only(old_settings, new_settings):
        rows["report"] = [{"id": "report-message", "key": "message",
                           "fields": {"name": "message", "value": report_message(step, printed_lines)}}]
    result = []
    for kind in sorted(rows):   # G9: every row kind in ascending id order
        for record in rows[kind]:
            result.append(row(record, kind))
    return result, settings
