"""Studio port of LEAFCOMBINERAUTO's placement phase (contract G35c): place(intake) -> solution.

Literal port of Branch2025 (read 2026-09-24 at C:/tmp/solar-parity/wt-b25-s69, master 6b940d51, the
captured build). Paths below are relative to that tree; Core means LeafSolarDesign.Core/CombinerPlacement.

  input plan        CombinerAutoCmd.cs:339-360    options, then MaxL1PerL2 from L1CollectorsPerL2
                    CombinerAutoCmd.cs:2635-2669  the "Combiner input plan" dialog; Apply with its
                                                  default persists the chosen strings-per-input to
                                                  CombinerBoxConnections (:2681), which the intake's
                                                  commandContext records (:2587)
                    CombinerAutoCmd.cs:3052-3081  ApplyCombinerInputTargetToOptions
                    CombinerAutoCmd.cs:3083-3103  L2 DC inputs (mppt x strings per mppt), physical SKU
  request           CombinerAutoCmd.cs:434-444, :455-458 (no cloud: CombinerPlacementEngine.Place)
  engine            Core/CombinerPlacementEngine.cs:28-266   stages, L1 numbering, SKU, assignments
                    Core/CombinerPlacementEngine.cs:392-908  the joint L2/row-end optimizer
                    Core/CombinerPlacementEngine.cs:933-942  ChooseSku over
                    Core/CombinerBoxAutoResizer.cs:133       the SKU catalog 8..32
  alleys            Core/AlleyDetector.cs:82-446             inner-row gaps and inter-group aisles
  alignment         Core/AlignmentDeriver.cs:31-158          trench, nearest aisle, bbox edge
  partition         Core/StringPartitioner.cs:34-272         per-L2 walk, fill, rebalance
  placement space   Core/PlacementSpace.cs:39-568            row segments, extents, subarray blocks
  positioner        Core/CombinerPositioner.cs:45-1553       candidates, cost, perimeter routing
  pre-built strings CombinerAutoCmd.cs:3867-3931             pseudo panels spread along each string

Every tie-break is the plugin's: strict "<" keeps the first minimum, dictionaries keep insertion order,
and the few .NET sorts that are not total orders get the stable order that .NET's insertion sort gives
on the short lists involved (eight L2s here). Math.Round(x, 1) in the mount key is .NET's half-to-even
on x * 10 (MountKey, CombinerPlacementEngine.cs:836-842). "%" on doubles is IEEE fmod.

Declared reconstruction (G35c keeps the placement dump's compact panel groups, which drop the per-panel
records the engine reads, CombinerPlacementDumpCmd.cs:1377-1420). The engine's PanelGroupInputXY list
is rebuilt from the intake itself:
  * a group whose bounds are all zero had every panel centre at (0, 0) (bounds are the min and max of
    the panel centres, CombinerPlacementDumpCmd.cs:1383-1390), so it gets panelCount panels there;
  * any other group owns the CAD panel polylines whose bounding-box centre lies inside its bounds; a
    polyline inside two groups' bounds goes to the group whose other panels share its footprint; the
    rebuilt count and extremes must equal the dump's panelCount and bounds, or the intake is refused;
  * each group's module (its panelDef, CombinerAutoCmd.cs:4056-4061, :4104-4114) is the side lengths of
    its most common panel polyline (panels may be drawn rotated, so the sides, not the bounding box),
    sized per PanelDef.cs:41-66 for the drawing's installation design (Roof: along the row the long
    side, across the row the short side); a group at the origin takes the drawing's most common module.
Only the alley detector (inner-row gaps, aisle thresholds) and the clearance check read these panels.

Not ported (refused rather than guessed): tracker rows (the SAT branch needs TrackerRowClusterer) and
the cloud placement client (the captured run had it disabled, routeHomerunsInCloud false). The Vdrop
validator's warnings are not part of the solution this module returns.

CLI: python server/solar_inverter_combiner.py --intake <combiner-intake.json> --out <solution.json>
Stdlib only; fails closed on a malformed intake (PlacementError, exit 2).
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
import time

INTAKE_FORMAT = "combiner-intake-v1"
INTAKE_STAGE = "input-before-placement"
SKU_CATALOG = (8, 12, 16, 20, 24, 28, 32)          # CombinerBoxAutoResizer.cs:133
INT_MIN = -2147483648                              # int.MinValue, CombinerGroup.LastRowIndex
DOUBLE_MAX = 1.7976931348623157e308                # double.MaxValue
DOUBLE_MIN = -DOUBLE_MAX                           # double.MinValue
MAX_PANELS = 200000                                # intake bound: panels, strings, L2s, alleys scale
MAX_STRINGS = 20000
MAX_L2 = 1000

# AlleyDetector.cs:36-57
DEFAULT_MIN_ALLEY_WIDTH = 3.0
DEFAULT_MIN_AISLE_WIDTH = 10.0
DEFAULT_MAX_ROAD_WIDTH = 60.0
DEFAULT_PARALLEL_TOLERANCE_RAD = 5.0 * math.pi / 180.0

# CombinerPositioner.cs:21-25
L2_ROAD_MATCH_MAX_DISTANCE_FT = 150.0
L2_ROAD_MATCH_EXTRA_TOLERANCE_FT = 25.0
MIN_PREFERRED_ACCESS_ROAD_LENGTH_FT = 300.0
MAX_FALLBACK_ALLEY_CANDIDATES_PER_GROUP = 24
MAX_PREFERRED_ACCESS_ROAD_LINES = 16

# CombinerPlacementTypes.cs:293-345 (CreateDefault), used where the plugin falls back to a default
DEFAULTS = {
    "TargetInputs": 16, "MinInputs": 8, "MaxInputs": 24, "AllowRebalanceAboveTarget": True,
    "MaxL1PerL2": 0, "EnableJointL2RowEndOptimization": True, "JointL2RowEndMaxPasses": 4,
    "JointL2RowEndTailRatio": 4.0, "JointL2RowEndTailPenalty": 1.5, "JointL2RowEndWireWeight": 0.02,
    "JointL2RowEndMinSavingsFt": 0.001, "JointL2RowEndMaxCandidateL2": 8,
    "JointL2RowEndMaxSwapPartnersPerL2": 4, "JointL2RowEndMaxMilliseconds": 30000,
    "EnclosureClearanceFt": 4.0, "MinAlleyWidthFt": DEFAULT_MIN_ALLEY_WIDTH,
    "MaxRoadWidthFt": DEFAULT_MAX_ROAD_WIDTH, "RoadAccessFreeDistanceFt": 35.0, "RoadAccessWeight": 10.0,
    "HomeRunConductorsPerString": 2.0, "ApplyConductorFactor": False, "RowSegmentFallbackGapFt": 10.0,
    "RowSegmentPitchMultiplier": 1.5, "RowSegmentMinGapFt": 10.0, "RowSegmentMaxGapFt": 30.0,
    "TrackerRowMatchAlongPaddingFt": 30.0, "SubarrayBlockGapMultiplier": 2.75,
    "SubarrayBlockMinGapFt": 65.0, "SubarrayBlockAlongToleranceFt": 220.0,
    "SubarrayPerimeterClearanceFt": 18.0, "MaxRowEndReachFt": 250.0, "MaxAcrossRowDeviationFt": 30.0,
    "Weights": {"Alpha": 1.0, "Beta": 4.0, "Gamma": 0.5, "Delta": 1e9, "Epsilon": 0.3},
}

_INT_OPTIONS = ("TargetInputs", "MinInputs", "MaxInputs", "MaxL1PerL2", "JointL2RowEndMaxPasses",
                "JointL2RowEndMaxCandidateL2", "JointL2RowEndMaxSwapPartnersPerL2",
                "JointL2RowEndMaxMilliseconds")
_BOOL_OPTIONS = ("AllowRebalanceAboveTarget", "EnableJointL2RowEndOptimization", "ApplyConductorFactor")
_FLOAT_OPTIONS = ("JointL2RowEndTailRatio", "JointL2RowEndTailPenalty", "JointL2RowEndWireWeight",
                  "JointL2RowEndMinSavingsFt", "EnclosureClearanceFt", "MinAlleyWidthFt", "MaxRoadWidthFt",
                  "RoadAccessFreeDistanceFt", "RoadAccessWeight", "HomeRunConductorsPerString",
                  "RowSegmentFallbackGapFt", "RowSegmentPitchMultiplier", "RowSegmentMinGapFt",
                  "RowSegmentMaxGapFt", "SubarrayBlockGapMultiplier", "SubarrayBlockMinGapFt",
                  "SubarrayBlockAlongToleranceFt", "SubarrayPerimeterClearanceFt", "MaxRowEndReachFt",
                  "MaxAcrossRowDeviationFt")
_WEIGHTS = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")


class PlacementError(ValueError):
    """The intake is malformed or asks for a branch this port refuses to guess."""


# ── small helpers (fail closed on bad input) ─────────────────────────────────


def _num(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlacementError(f"{what}: expected a number, got {type(value).__name__}")
    value = float(value)
    if not math.isfinite(value):
        raise PlacementError(f"{what}: not finite")
    return value


def _int(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlacementError(f"{what}: expected an integer")
    return value


def _bool(value, what):
    if not isinstance(value, bool):
        raise PlacementError(f"{what}: expected a boolean")
    return value


def _dict(value, what):
    if not isinstance(value, dict):
        raise PlacementError(f"{what}: expected an object")
    return value


def _list(value, what, limit):
    if not isinstance(value, list):
        raise PlacementError(f"{what}: expected a list")
    if len(value) > limit:
        raise PlacementError(f"{what}: {len(value)} entries exceeds the bound {limit}")
    return value


def _point(value, what, upper=False):
    value = _dict(value, what)
    kx, ky = ("X", "Y") if upper else ("x", "y")
    return (_num(value.get(kx), f"{what}.{kx}"), _num(value.get(ky), f"{what}.{ky}"))


def _dist(a, b):
    """XY.DistanceTo (CombinerPlacementInputs.cs:41): a - b, then sqrt of the squares."""
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return math.sqrt(dx * dx + dy * dy)


def _net_round1(value):
    """.NET Math.Round(value, 1): value * 10, round half to even, / 10."""
    if abs(value) >= 1e16:
        return value
    return float(round(value * 10.0)) / 10.0


def _positive_or_default(value, fallback):
    return value if value > 0 else fallback


def _positive_double_or_default(value, fallback):
    return value if value > 0.0 and math.isfinite(value) else fallback


def _non_negative_or_default(value, fallback):
    return value if value >= 0.0 and math.isfinite(value) else fallback


def _normalize_undirected_angle(angle):
    pi = math.pi
    while angle < 0.0:
        angle += pi
    while angle >= pi:
        angle -= pi
    return angle


def distance_point_to_segment(p, a, b):
    """AlignmentDeriver.DistancePointToSegment, AlignmentDeriver.cs:144-158."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return _dist(p, a)
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len_sq
    if t < 0:
        t = 0.0
    elif t > 1:
        t = 1.0
    cx = a[0] + t * dx
    cy = a[1] + t * dy
    ddx = p[0] - cx
    ddy = p[1] - cy
    return math.sqrt(ddx * ddx + ddy * ddy)


def closest_point_on_segment(p, a, b):
    """CombinerPositioner.cs:1543-1553."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return a
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len_sq
    if t < 0:
        t = 0.0
    elif t > 1:
        t = 1.0
    return (a[0] + t * dx, a[1] + t * dy)


# ── data types (CombinerPlacementInputs.cs, CombinerPlacementTypes.cs, Alley.cs) ─


class Options:
    """CombinerPlacementOptions (CombinerPlacementTypes.cs:177-345), read from the intake."""

    def __init__(self, raw):
        raw = _dict(raw, "inputs.options")
        for key in _INT_OPTIONS:
            setattr(self, key, _int(raw.get(key, DEFAULTS[key]), f"options.{key}"))
        for key in _BOOL_OPTIONS:
            setattr(self, key, _bool(raw.get(key, DEFAULTS[key]), f"options.{key}"))
        for key in _FLOAT_OPTIONS:
            setattr(self, key, _num(raw.get(key, DEFAULTS[key]), f"options.{key}"))
        weights = _dict(raw.get("Weights", DEFAULTS["Weights"]), "options.Weights")
        self.Weights = {k: _num(weights.get(k, DEFAULTS["Weights"][k]), f"options.Weights.{k}")
                        for k in _WEIGHTS}
        end_policy = raw.get("EndPolicy", 0)
        if end_policy not in (0, "NearestInverter"):
            raise PlacementError("options.EndPolicy: only NearestInverter is ported")


class PanelInput:
    __slots__ = ("center", "width_along_row", "height_across_row")

    def __init__(self, center, width_along_row, height_across_row):
        self.center = center
        self.width_along_row = width_along_row
        self.height_across_row = height_across_row


class PanelGroupInput:
    __slots__ = ("group_id", "row_angle_rad", "panels", "is_synthesized_from_outline")

    def __init__(self, group_id, row_angle_rad, panels, synthesized):
        self.group_id = group_id
        self.row_angle_rad = row_angle_rad
        self.panels = panels
        self.is_synthesized_from_outline = synthesized


class StringSummary:
    __slots__ = ("l2_number", "string_number", "panels", "centroid", "row_index", "col_index_min",
                 "col_index_max", "endpoint_a", "endpoint_b", "row_angle_rad", "physical_row_key",
                 "group_id")


class CombinerGroup:
    __slots__ = ("l2_number", "strings", "last_row_index")

    def __init__(self, l2_number):
        self.l2_number = l2_number
        self.strings = []
        self.last_row_index = INT_MIN


class Alley:
    __slots__ = ("kind", "start", "end", "width", "row_angle_rad", "group_id", "group_id_a", "group_id_b")

    def __init__(self, kind, start, end, width, row_angle_rad, group_id=None, group_id_a=None,
                 group_id_b=None):
        self.kind = kind
        self.start = start
        self.end = end
        self.width = width
        self.row_angle_rad = row_angle_rad
        self.group_id = group_id
        self.group_id_a = group_id_a
        self.group_id_b = group_id_b

    @property
    def mid(self):
        return ((self.start[0] + self.end[0]) * 0.5, (self.start[1] + self.end[1]) * 0.5)


class RowExtent:
    __slots__ = ("key", "row_index", "row_angle_rad", "min_point", "max_point", "min_u", "max_u",
                 "center_t", "has_projection_bounds", "subarray_block_id", "block_min_u", "block_max_u",
                 "block_min_t", "block_max_t", "block_row_pitch_ft", "has_subarray_block",
                 "chord_width_ft", "rack_segments")

    def __init__(self):
        self.subarray_block_id = 0
        self.block_min_u = self.block_max_u = self.block_min_t = self.block_max_t = 0.0
        self.block_row_pitch_ft = 0.0
        self.has_subarray_block = False
        self.chord_width_ft = 0.0
        self.rack_segments = None


# ── input plan (CombinerAutoCmd.cs:3052-3103) ────────────────────────────────


def resolve_physical_sku(strings_per_input):
    """ResolveCombinerPhysicalSku, CombinerAutoCmd.cs:3093-3103."""
    for sku in SKU_CATALOG:
        if sku >= strings_per_input:
            return sku
    return strings_per_input


def apply_input_target(options, strings_per_input, l2_num_mppt, l2_strings_per_mppt):
    """ApplyCombinerInputTargetToOptions, CombinerAutoCmd.cs:3052-3081 (and :3083-3091)."""
    if options is None or strings_per_input <= 0:
        return
    physical_sku = resolve_physical_sku(strings_per_input)
    options.TargetInputs = strings_per_input
    options.MaxInputs = max(strings_per_input, physical_sku)
    options.MinInputs = max(strings_per_input // 2, 2)
    options.AllowRebalanceAboveTarget = False
    dc_inputs = l2_num_mppt * l2_strings_per_mppt if l2_num_mppt > 0 and l2_strings_per_mppt > 0 else 0
    if dc_inputs > 0:
        options.MaxL1PerL2 = dc_inputs


# ── intake reconstruction (see the module docstring) ─────────────────────────


def _polyline_record(entity, index):
    points = _list(entity.get("points"), f"cadContext panel {index}.points", 4096)
    if len(points) < 3:
        return None
    xs = []
    ys = []
    for k, q in enumerate(points):
        x, y = _point(q, f"cadContext panel {index}.points[{k}]")
        xs.append(x)
        ys.append(y)
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    footprint = (round(max_x - min_x, 6), round(max_y - min_y, 6))
    # The module's own sides (the polyline may be drawn rotated, so its bounding box is not the module).
    side_a = math.hypot(xs[1] - xs[0], ys[1] - ys[0])
    side_b = math.hypot(xs[2] - xs[1], ys[2] - ys[1])
    module = (min(side_a, side_b), max(side_a, side_b))
    return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, footprint, module)


def _module_of(records):
    """The most common module (sides rounded to 1e-6 to group), valued by its first polyline."""
    if not records:
        return None
    keys = [(round(r[3][0], 6), round(r[3][1], 6)) for r in records]
    modal = Counter(keys).most_common(1)[0][0]
    return records[keys.index(modal)][3]


def _module_sizes(module, installation):
    """PanelDef.ColumnDimension / RowDimension (PanelDef.cs:41-66): (along the row, across the row)."""
    short, long_ = module if module is not None else (0.0, 0.0)
    if installation == "Roof":
        return long_, short
    return short, long_


def reconstruct_panel_groups(intake, installation="Roof"):
    """The engine's PanelGroupInputXY list rebuilt from the intake (declared reconstruction)."""
    inputs = _dict(intake.get("inputs"), "inputs")
    compact = _list(inputs.get("panelGroups"), "inputs.panelGroups", MAX_PANELS)
    cad = _dict(_dict(inputs.get("cadContext"), "inputs.cadContext").get("geometry"),
                "inputs.cadContext.geometry")
    entities = _list(cad.get("panels", []), "inputs.cadContext.geometry.panels", MAX_PANELS)
    polylines = []
    for i, entity in enumerate(entities):
        entity = _dict(entity, f"cadContext panel {i}")
        if entity.get("type") != "Polyline" or "points" not in entity:
            continue
        rec = _polyline_record(entity, i)
        if rec is not None:
            polylines.append(rec)

    drawing_module = _module_of(polylines)

    groups = []
    for gi, g in enumerate(compact):
        g = _dict(g, f"inputs.panelGroups[{gi}]")
        gid = g.get("id")
        if not isinstance(gid, str):
            raise PlacementError(f"inputs.panelGroups[{gi}].id: expected a string")
        bounds = _dict(g.get("bounds"), f"panel group {gid}.bounds")
        lo = _point(bounds.get("min"), f"panel group {gid}.bounds.min")
        hi = _point(bounds.get("max"), f"panel group {gid}.bounds.max")
        count = _int(g.get("panelCount"), f"panel group {gid}.panelCount")
        if count < 0 or count > MAX_PANELS:
            raise PlacementError(f"panel group {gid}.panelCount out of range")
        groups.append({"id": gid, "angle": _num(g.get("rowAngleRad", 0.0), f"panel group {gid}.rowAngleRad"),
                       "synth": _bool(g.get("isSynthesizedFromOutline", False), f"panel group {gid}.synth"),
                       "count": count, "lo": lo, "hi": hi,
                       "zero": count > 0 and lo == (0.0, 0.0) and hi == (0.0, 0.0)})

    located = [grp for grp in groups if grp["count"] > 0 and not grp["zero"]]
    owners = []
    for cx, cy, _, _ in polylines:
        owners.append([grp["id"] for grp in located
                       if grp["lo"][0] <= cx <= grp["hi"][0] and grp["lo"][1] <= cy <= grp["hi"][1]])
    members = {grp["id"]: [] for grp in groups}
    for rec, own in zip(polylines, owners):
        if len(own) == 1:
            members[own[0]].append(rec)
    group_footprint = {gid: (Counter(r[2] for r in recs).most_common(1)[0][0] if recs else None)
                       for gid, recs in members.items()}
    for rec, own in zip(polylines, owners):
        if len(own) > 1:
            match = [gid for gid in own if group_footprint[gid] == rec[2]]
            if len(match) != 1:
                raise PlacementError("a CAD panel lies in several panel groups and its footprint does "
                                     "not single one out")
            members[match[0]].append(rec)

    result = []
    for grp in groups:
        if grp["zero"]:
            # Unobservable here (no located panels), and inert: panels at the origin form one row and
            # no aisle, and sit far from every candidate. The drawing's most common module stands in.
            width_along, height_across = _module_sizes(drawing_module, installation)
            panels = [PanelInput((0.0, 0.0), width_along, height_across) for _ in range(grp["count"])]
        else:
            recs = members[grp["id"]]
            # BuildPanelGroupInputsWithDiagnostics sizes every panel of a group from that group's
            # panelDef (CombinerAutoCmd.cs:4056-4061, :4104-4114).
            width_along, height_across = _module_sizes(_module_of(recs) or drawing_module, installation)
            if len(recs) != grp["count"]:
                raise PlacementError(f"panel group {grp['id']}: {len(recs)} CAD panels inside its bounds, "
                                     f"the dump counted {grp['count']}")
            if recs:
                xs = [r[0] for r in recs]
                ys = [r[1] for r in recs]
                if (abs(min(xs) - grp["lo"][0]) > 1e-6 or abs(max(xs) - grp["hi"][0]) > 1e-6 or
                        abs(min(ys) - grp["lo"][1]) > 1e-6 or abs(max(ys) - grp["hi"][1]) > 1e-6):
                    raise PlacementError(f"panel group {grp['id']}: rebuilt panel extremes differ from "
                                         "the dump's bounds")
            panels = [PanelInput((r[0], r[1]), width_along, height_across) for r in recs]
        if not panels:
            # BuildPanelGroupInputsWithDiagnostics drops a group with no panels (:4117-4122)
            continue
        result.append(PanelGroupInput(grp["id"], grp["angle"], panels, grp["synth"]))
    return result


def build_strings(intake):
    """The pre-built StringSummary list, pseudo panels per CombinerAutoCmd.cs:3890-3905."""
    raw = _list(_dict(intake.get("inputs"), "inputs").get("preBuiltStrings"), "inputs.preBuiltStrings",
                MAX_STRINGS)
    strings = []
    for i, r in enumerate(raw):
        r = _dict(r, f"preBuiltStrings[{i}]")
        s = StringSummary()
        s.l2_number = _int(r.get("L2Number"), f"preBuiltStrings[{i}].L2Number")
        s.string_number = _int(r.get("StringNumber"), f"preBuiltStrings[{i}].StringNumber")
        s.group_id = r.get("GroupId")
        s.row_index = _int(r.get("RowIndex"), f"preBuiltStrings[{i}].RowIndex")
        s.col_index_min = _int(r.get("ColIndexMin"), f"preBuiltStrings[{i}].ColIndexMin")
        s.col_index_max = _int(r.get("ColIndexMax"), f"preBuiltStrings[{i}].ColIndexMax")
        s.row_angle_rad = _num(r.get("RowAngleRad"), f"preBuiltStrings[{i}].RowAngleRad")
        key = r.get("PhysicalRowKey")
        s.physical_row_key = key if isinstance(key, str) else None
        s.centroid = _point(r.get("centroid"), f"preBuiltStrings[{i}].centroid")
        s.endpoint_a = _point(r.get("endpointA"), f"preBuiltStrings[{i}].endpointA")
        s.endpoint_b = _point(r.get("endpointB"), f"preBuiltStrings[{i}].endpointB")
        count = _int(r.get("PanelCount"), f"preBuiltStrings[{i}].PanelCount")
        if count < 0 or count > 10000:
            raise PlacementError(f"preBuiltStrings[{i}].PanelCount out of range")
        first, last = s.endpoint_a, s.endpoint_b
        panels = []
        for p in range(count):
            t = 0.5 if count == 1 else float(p) / (count - 1)
            panels.append((first[0] + (last[0] - first[0]) * t, first[1] + (last[1] - first[1]) * t))
        s.panels = panels
        strings.append(s)
    return strings


# ── AlleyDetector (AlleyDetector.cs) ─────────────────────────────────────────


def detect_alleys(panel_groups, min_alley_width=DEFAULT_MIN_ALLEY_WIDTH, max_road_width=DEFAULT_MAX_ROAD_WIDTH,
                  parallel_tolerance_rad=DEFAULT_PARALLEL_TOLERANCE_RAD, min_aisle_width=DEFAULT_MIN_AISLE_WIDTH):
    """AlleyDetector.Detect, AlleyDetector.cs:82-174."""
    if panel_groups is None:
        raise PlacementError("panelGroups is null")
    if panel_groups:
        sample = 0.0
        for g in panel_groups:
            if sample > 0:
                break
            if g is None or g.panels is None:
                continue
            for p in g.panels:
                if p.height_across_row > 0:
                    sample = p.height_across_row
                    break
        if sample > 0:
            if min_aisle_width == DEFAULT_MIN_AISLE_WIDTH:
                min_aisle_width = 3.0 * sample
            if max_road_width == DEFAULT_MAX_ROAD_WIDTH:
                max_road_width = 60.0 * sample

    alleys = []
    for g in panel_groups:
        if g is None or not g.panels:
            continue
        if g.is_synthesized_from_outline:
            continue
        alleys.extend(_detect_inner_row_alleys(g, min_alley_width))
    for i in range(len(panel_groups)):
        for j in range(i + 1, len(panel_groups)):
            a = panel_groups[i]
            b = panel_groups[j]
            if a is None or b is None or not a.panels or not b.panels:
                continue
            alley = _try_detect_inter_group_alley(a, b, min_aisle_width, max_road_width, parallel_tolerance_rad)
            if alley is not None:
                alleys.append(alley)
    return alleys


def _detect_inner_row_alleys(group, min_alley_width):
    """AlleyDetector.cs:180-292."""
    result = []
    n = len(group.panels)
    if n < 2:
        return result
    theta = group.row_angle_rad
    cs = math.cos(theta)
    sn = math.sin(theta)
    cx = 0.0
    cy = 0.0
    for p in group.panels:
        cx += p.center[0]
        cy += p.center[1]
    cx /= n
    cy /= n
    u = [0.0] * n
    v = [0.0] * n
    mean_height = 0.0
    for i, p in enumerate(group.panels):
        dx = p.center[0] - cx
        dy = p.center[1] - cy
        u[i] = dx * cs + dy * sn
        v[i] = -dx * sn + dy * cs
        mean_height += p.height_across_row
    mean_height /= n
    if mean_height <= 0:
        return result
    tol = 0.4 * mean_height

    idx = sorted(range(n), key=lambda k: v[k])
    rows = []
    current = [idx[0]]
    cur_max_v = v[idx[0]]
    for k in range(1, n):
        i = idx[k]
        if v[i] - cur_max_v <= tol:
            current.append(i)
            if v[i] > cur_max_v:
                cur_max_v = v[i]
        else:
            rows.append(current)
            current = [i]
            cur_max_v = v[i]
    rows.append(current)
    if len(rows) < 2:
        return result

    for r in range(len(rows) - 1):
        row_a = rows[r]
        row_b = rows[r + 1]
        center_va = _mean_v(v, row_a)
        center_vb = _mean_v(v, row_b)
        gap = (center_vb - center_va) - mean_height
        if gap < min_alley_width:
            continue
        alley_center_v = (center_va + center_vb) * 0.5
        u_min = DOUBLE_MAX
        u_max = DOUBLE_MIN
        for k in row_a + row_b:
            if u[k] < u_min:
                u_min = u[k]
            if u[k] > u_max:
                u_max = u[k]
        start = (cx + u_min * cs - alley_center_v * sn, cy + u_min * sn + alley_center_v * cs)
        end = (cx + u_max * cs - alley_center_v * sn, cy + u_max * sn + alley_center_v * cs)
        result.append(Alley("InnerRow", start, end, gap, theta, group_id=group.group_id))
    return result


def _mean_v(v, indices):
    s = 0.0
    for k in indices:
        s += v[k]
    return s / len(indices)


def _try_detect_inter_group_alley(a, b, min_aisle_width, max_road_width, parallel_tolerance_rad):
    """AlleyDetector.cs:357-428."""
    d_theta = abs(a.row_angle_rad - b.row_angle_rad)
    if d_theta > math.pi / 2.0:
        d_theta = math.pi - d_theta
    if d_theta > parallel_tolerance_rad:
        return None
    theta = a.row_angle_rad
    cs = math.cos(theta)
    sn = math.sin(theta)
    u_min_a, u_max_a, v_min_a, v_max_a = _project_extent(a, cs, sn)
    u_min_b, u_max_b, v_min_b, v_max_b = _project_extent(b, cs, sn)
    u_overlap_min = max(u_min_a, u_min_b)
    u_overlap_max = min(u_max_a, u_max_b)
    if u_overlap_max <= u_overlap_min:
        return None
    if v_min_b > v_max_a:
        gap = v_min_b - v_max_a
        aisle_v = (v_max_a + v_min_b) * 0.5
    elif v_min_a > v_max_b:
        gap = v_min_a - v_max_b
        aisle_v = (v_max_b + v_min_a) * 0.5
    else:
        return None
    if gap < min_aisle_width or gap > max_road_width:
        return None
    start = (u_overlap_min * cs - aisle_v * sn, u_overlap_min * sn + aisle_v * cs)
    end = (u_overlap_max * cs - aisle_v * sn, u_overlap_max * sn + aisle_v * cs)
    return Alley("InterGroup", start, end, gap, theta, group_id_a=a.group_id, group_id_b=b.group_id)


def _project_extent(group, cs, sn):
    u_min = DOUBLE_MAX
    u_max = DOUBLE_MIN
    v_min = DOUBLE_MAX
    v_max = DOUBLE_MIN
    for p in group.panels:
        x, y = p.center
        u = x * cs + y * sn
        v = -x * sn + y * cs
        if u < u_min:
            u_min = u
        if u > u_max:
            u_max = u
        if v < v_min:
            v_min = v
        if v > v_max:
            v_max = v
    return u_min, u_max, v_min, v_max


# ── AlignmentDeriver (AlignmentDeriver.cs) ───────────────────────────────────


def resolve_alignment(user_trench, alleys, panel_groups, inverter_location):
    """AlignmentDeriver.Resolve, AlignmentDeriver.cs:31-68."""
    if user_trench is not None and len(user_trench) >= 2:
        return list(user_trench)
    best = None
    best_dist = DOUBLE_MAX
    for a in alleys or ():
        if a is None or a.kind != "InterGroup":
            continue
        d = distance_point_to_segment(inverter_location, a.start, a.end)
        if d < best_dist:
            best_dist = d
            best = a
    if best is not None:
        return [best.start, best.end]
    return _bbox_edge_facing(panel_groups, inverter_location)


def _bbox_edge_facing(panel_groups, inv):
    """AlignmentDeriver.cs:74-142."""
    if not panel_groups:
        return [inv, inv]
    min_x = DOUBLE_MAX
    max_x = DOUBLE_MIN
    min_y = DOUBLE_MAX
    max_y = DOUBLE_MIN
    for g in panel_groups:
        if g is None or g.panels is None:
            continue
        for p in g.panels:
            x, y = p.center
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
    if min_x > max_x or min_y > max_y:
        return [inv, inv]
    d_right = inv[0] - max_x
    d_left = min_x - inv[0]
    d_top = inv[1] - max_y
    d_bottom = min_y - inv[1]
    max_d = max(max(d_left, d_right), max(d_bottom, d_top))
    if max_d == d_right:
        return [(max_x, min_y), (max_x, max_y)]
    if max_d == d_left:
        return [(min_x, min_y), (min_x, max_y)]
    if max_d == d_top:
        return [(min_x, max_y), (max_x, max_y)]
    return [(min_x, min_y), (max_x, min_y)]


# ── StringPartitioner (StringPartitioner.cs) ─────────────────────────────────


def partition(all_strings, options, l2_locations=None):
    """StringPartitioner.Partition, StringPartitioner.cs:34-73."""
    if options.TargetInputs <= 0:
        raise PlacementError("TargetInputs must be > 0")
    if options.MinInputs < 0:
        raise PlacementError("MinInputs must be >= 0")
    if options.MaxInputs < options.TargetInputs:
        raise PlacementError("MaxInputs must be >= TargetInputs")
    if options.MinInputs > options.TargetInputs:
        raise PlacementError("MinInputs must be <= TargetInputs")
    by_l2 = {}
    for s in all_strings:
        if s is None:
            continue
        by_l2.setdefault(s.l2_number, []).append(s)
    result = []
    for l2 in sorted(by_l2):
        has_loc = l2_locations is not None and l2 in l2_locations
        loc = l2_locations[l2] if has_loc else (0.0, 0.0)
        result.extend(_partition_one_l2(l2, by_l2[l2], options, has_loc, loc))
    return result


def _partition_one_l2(l2_number, strings, options, has_l2_location, l2_location):
    """StringPartitioner.cs:81-159."""
    ordered = _build_linear_walk_order(strings, has_l2_location, l2_location)
    groups = []
    current = CombinerGroup(l2_number)
    for s in ordered:
        start_new = False
        if len(current.strings) >= options.TargetInputs:
            start_new = True
        elif (len(current.strings) >= options.MinInputs and current.last_row_index != INT_MIN and
              s.row_index != current.last_row_index):
            start_new = True
        if start_new:
            groups.append(current)
            current = CombinerGroup(l2_number)
        current.strings.append(s)
        current.last_row_index = s.row_index
    if current.strings:
        groups.append(current)

    if len(groups) >= 2:
        last = groups[-1]
        if len(last.strings) < options.MinInputs:
            prev = groups[-2]
            combined = len(last.strings) + len(prev.strings)
            cap = options.MaxInputs if options.AllowRebalanceAboveTarget else options.TargetInputs
            if combined <= cap:
                prev.strings.extend(last.strings)
                prev.last_row_index = last.last_row_index
                groups.pop()
            else:
                while len(last.strings) < options.MinInputs and len(prev.strings) > options.MinInputs:
                    moved = prev.strings.pop()
                    last.strings.insert(0, moved)
    return groups


def _string_distance(s, point):
    """StringPartitioner.cs:253-262."""
    best = _dist(s.centroid, point)
    d = _dist(s.endpoint_a, point)
    if d < best:
        best = d
    d = _dist(s.endpoint_b, point)
    if d < best:
        best = d
    return best


def _build_linear_walk_order(strings, has_l2_location, l2_location):
    """StringPartitioner.cs:168-237."""
    if not strings:
        return []
    col_key = lambda s: (s.col_index_min, s.string_number)          # CompareColumnThenString :246-251
    if not has_l2_location:
        return sorted(strings, key=lambda s: (s.row_index, s.col_index_min, s.string_number))
    rows = {}
    for s in strings:
        if s is None:
            continue
        rows.setdefault(s.row_index, []).append(s)
    infos = []
    for row_index, row in rows.items():
        row.sort(key=col_key)
        if len(row) > 1:
            first = _string_distance(row[0], l2_location)
            last = _string_distance(row[-1], l2_location)
            if last < first:
                row.reverse()
        nearest = DOUBLE_MAX
        for s in row:
            d = _string_distance(s, l2_location)
            if d < nearest:
                nearest = d
        infos.append((nearest, row_index, row))
    infos.sort(key=lambda info: (info[0], info[1]))
    ordered = []
    for _, _, row in infos:
        ordered.extend(row)
    return ordered


# ── PlacementSpace (PlacementSpace.cs) ───────────────────────────────────────


class PlacementSpace:
    def __init__(self, row_extents, alleys, row_segment_gap_ft):
        self.row_extents = row_extents
        self.alleys = alleys if alleys is not None else []
        self.row_segment_gap_ft = row_segment_gap_ft

    @classmethod
    def create(cls, strings, alleys, options):
        """PlacementSpace.Create, PlacementSpace.cs:39-52 (no tracker rows)."""
        extents, gap = build_row_extents(strings, options)
        return cls(extents, alleys, gap)


def _upper_median(values):
    """PlacementSpace.Median, PlacementSpace.cs:450-455."""
    if not values:
        return 0.0
    values.sort()
    return values[len(values) // 2]


def _string_placement_point(s):
    """PlacementSpace.cs:538-546."""
    if abs(s.centroid[0]) > 1e-9 or abs(s.centroid[1]) > 1e-9:
        return s.centroid
    return ((s.endpoint_a[0] + s.endpoint_b[0]) / 2.0, (s.endpoint_a[1] + s.endpoint_b[1]) / 2.0)


def build_row_extents(strings, options):
    """PlacementSpace.BuildRowExtents, PlacementSpace.cs:63-133."""
    result = {}
    by_row = {}
    for s in strings:
        if s is None:
            continue
        cs = math.cos(s.row_angle_rad)
        sn = math.sin(s.row_angle_rad)
        p = _string_placement_point(s)
        by_row.setdefault(s.row_index, []).append([s, p[0] * cs + p[1] * sn])

    pitch = _typical_row_pitch(by_row)
    gap_ft = _row_segment_gap(options, pitch)
    for row_index, row in by_row.items():
        if not row:
            continue
        row.sort(key=lambda rp: rp[1])
        segment = []
        seg_index = 0
        last = None
        for rp in row:
            if segment and _should_split_row_gap(last, rp, pitch, gap_ft):
                _add_row_extent_segment(result, row_index, seg_index, segment)
                seg_index += 1
                segment = []
            segment.append(rp[0])
            last = rp
        _add_row_extent_segment(result, row_index, seg_index, segment)
    _attach_subarray_blocks(result, options)
    return result, gap_ft


def _should_split_row_gap(a, b, pitch, gap_ft):
    """PlacementSpace.cs:465-478."""
    if a is None or b is None:
        return False
    gap = b[1] - a[1]
    threshold = pitch + gap_ft if pitch > 1e-9 else gap_ft
    return gap > threshold


def _typical_row_pitch(by_row):
    """PlacementSpace.cs:480-499."""
    if not by_row:
        return 0.0
    gaps = []
    for row in by_row.values():
        if row is None or len(row) < 2:
            continue
        row.sort(key=lambda rp: rp[1])
        for i in range(1, len(row)):
            gap = row[i][1] - row[i - 1][1]
            if 1.0 < gap < 300.0:
                gaps.append(gap)
    return _upper_median(gaps)


def _row_segment_gap(options, pitch):
    """PlacementSpace.cs:501-525."""
    fallback = _positive_or_default(options.RowSegmentFallbackGapFt, DEFAULTS["RowSegmentFallbackGapFt"])
    multiplier = _positive_or_default(options.RowSegmentPitchMultiplier, DEFAULTS["RowSegmentPitchMultiplier"])
    derived = pitch * (multiplier - 1.0) if pitch > 1e-9 and multiplier > 1.0 else fallback
    value = min(fallback, derived)
    lo = _positive_or_default(options.RowSegmentMinGapFt, DEFAULTS["RowSegmentMinGapFt"])
    hi = _positive_or_default(options.RowSegmentMaxGapFt, DEFAULTS["RowSegmentMaxGapFt"])
    if hi < lo:
        hi = lo
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _add_row_extent_segment(result, row_index, seg_index, segment):
    """PlacementSpace.cs:150-197 (ApplyTrackerExtent is a no-op without tracker rows)."""
    if not segment:
        return
    theta = segment[0].row_angle_rad
    cs = math.cos(theta)
    sn = math.sin(theta)
    min_u = DOUBLE_MAX
    max_u = DOUBLE_MIN
    min_point = (0.0, 0.0)
    max_point = (0.0, 0.0)
    for s in segment:
        for p in (s.endpoint_a, s.endpoint_b, s.centroid):
            u = p[0] * cs + p[1] * sn
            if u < min_u:
                min_u = u
                min_point = p
            if u > max_u:
                max_u = u
                max_point = p
    if min_u == DOUBLE_MAX:
        return
    key = f"row:{row_index}:seg:{seg_index}"
    ext = RowExtent()
    ext.key = key
    ext.row_index = row_index
    ext.row_angle_rad = theta
    ext.min_point = min_point
    ext.max_point = max_point
    ext.min_u = min_u
    ext.max_u = max_u
    ts = []
    for s in segment:
        p = _string_placement_point(s)
        ts.append(p[0] * -sn + p[1] * cs)
    ext.center_t = _upper_median(ts)
    ext.has_projection_bounds = True
    result[key] = ext
    for s in segment:
        s.physical_row_key = key


def _attach_subarray_blocks(extents, options):
    """PlacementSpace.cs:328-419."""
    if not extents:
        return
    rows = [r for r in extents.values() if r is not None and r.has_projection_bounds]
    if not rows:
        return
    rows.sort(key=lambda r: (r.center_t, r.min_u))
    pitch = _estimate_row_pitch(rows)
    min_gap = _positive_or_default(options.SubarrayBlockMinGapFt, DEFAULTS["SubarrayBlockMinGapFt"])
    mult = _positive_or_default(options.SubarrayBlockGapMultiplier, DEFAULTS["SubarrayBlockGapMultiplier"])
    along_tol = _positive_or_default(options.SubarrayBlockAlongToleranceFt,
                                     DEFAULTS["SubarrayBlockAlongToleranceFt"])
    gap_threshold = max(min_gap, pitch * mult)
    blocks = []
    for row in rows:
        best = None
        best_score = DOUBLE_MAX
        for block in blocks:
            t_gap = max(0.0, row.center_t - block[4])
            if t_gap > gap_threshold:
                continue
            if not (max(row.min_u, block[1]) <= min(row.max_u, block[2]) + along_tol):
                continue
            overlap = min(row.max_u, block[2]) - max(row.min_u, block[1])
            score = t_gap - max(0.0, overlap) * 0.001
            if score < best_score:
                best_score = score
                best = block
        if best is None:
            # [id, minU, maxU, minCenterT, maxCenterT]
            best = [len(blocks) + 1, row.min_u, row.max_u, row.center_t, row.center_t]
            blocks.append(best)
        best[1] = min(best[1], row.min_u)
        best[2] = max(best[2], row.max_u)
        best[3] = min(best[3], row.center_t)
        best[4] = max(best[4], row.center_t)
        row.subarray_block_id = best[0]
        row.block_min_u = best[1]
        row.block_max_u = best[2]
        row.block_min_t = best[3]
        row.block_max_t = best[4]
        row.block_row_pitch_ft = pitch
        row.has_subarray_block = True
    by_id = {b[0]: b for b in blocks}
    for row in rows:
        block = by_id.get(row.subarray_block_id)
        if block is None:
            continue
        row.block_min_u = block[1]
        row.block_max_u = block[2]
        row.block_min_t = block[3]
        row.block_max_t = block[4]


def _estimate_row_pitch(rows):
    """PlacementSpace.cs:434-448."""
    if rows is None or len(rows) < 2:
        return 24.0
    t = sorted(r.center_t for r in rows)
    gaps = [t[i] - t[i - 1] for i in range(1, len(t)) if 1.0 < t[i] - t[i - 1] < 120.0]
    pitch = _upper_median(gaps)
    return pitch if pitch > 0.0 else 24.0


# ── CombinerPositioner (CombinerPositioner.cs) ───────────────────────────────


class PositionResult:
    """CombinerPositioner.PositionResult, CombinerPositioner.cs:27-40."""
    __slots__ = ("location", "snap", "cost", "trunk_route_length", "straight_trunk_length",
                 "sum_home_run_length", "max_home_run_length", "placement_extent_kind",
                 "placement_extent_key", "row_end_reach_ft")

    def __init__(self):
        self.location = (0.0, 0.0)
        self.snap = "Centroid"
        self.cost = DOUBLE_MAX
        self.trunk_route_length = 0.0
        self.straight_trunk_length = 0.0
        self.sum_home_run_length = 0.0
        self.max_home_run_length = 0.0
        self.placement_extent_kind = None
        self.placement_extent_key = None
        self.row_end_reach_ft = 0.0


class _Frame:
    __slots__ = ("theta", "cs", "sn", "min_point", "max_point")

    def __init__(self, theta, cs, sn, min_point, max_point):
        self.theta = theta
        self.cs = cs
        self.sn = sn
        self.min_point = min_point
        self.max_point = max_point


class _Route:
    """RouteContext.Create, CombinerPositioner.cs:344-372."""
    __slots__ = ("frame", "edge_point", "is_left_end", "has_subarray_block", "block_min_u", "block_max_u",
                 "block_min_t", "block_max_t", "block_row_pitch_ft", "clearance_ft")

    def __init__(self, frame, edge_point, is_left_end, extent, options):
        self.frame = frame
        self.edge_point = edge_point
        self.is_left_end = is_left_end
        self.clearance_ft = options.SubarrayPerimeterClearanceFt
        self.has_subarray_block = False
        self.block_min_u = self.block_max_u = self.block_min_t = self.block_max_t = 0.0
        self.block_row_pitch_ft = 0.0
        if extent is not None and extent.has_subarray_block:
            self.has_subarray_block = True
            self.block_min_u = extent.block_min_u
            self.block_max_u = extent.block_max_u
            self.block_min_t = extent.block_min_t
            self.block_max_t = extent.block_max_t
            self.block_row_pitch_ft = extent.block_row_pitch_ft


class _Candidate:
    __slots__ = ("point", "source", "route", "kind", "key", "reach", "fallback")

    def __init__(self, point, source, route=None, kind="", key="", reach=0.0, fallback=False):
        self.point = point
        self.source = source
        self.route = route
        self.kind = kind or ""
        self.key = key or ""
        self.reach = reach
        self.fallback = fallback


class ClearanceIndex:
    """ClearanceViolation (CombinerPositioner.cs:1123-1139) over a uniform grid: the same predicate,
    dx*dx + dy*dy < r*r with r = max(W, H) * 0.5, checked only against panels in the neighbouring cells,
    so the answer equals the plugin's linear scan without its O(panels) cost per candidate."""

    def __init__(self, panels):
        self._cells = {}
        max_r = 0.0
        entries = []
        for pn in panels:
            r = max(pn.width_along_row, pn.height_across_row) * 0.5
            if r <= 0:
                continue
            entries.append((pn.center[0], pn.center[1], r))
            if r > max_r:
                max_r = r
        self._size = max(2.0 * max_r, 1.0)
        for x, y, r in entries:
            key = (math.floor(x / self._size), math.floor(y / self._size))
            self._cells.setdefault(key, []).append((x, y, r * r))

    def violates(self, p):
        if not self._cells:
            return False
        ix = math.floor(p[0] / self._size)
        iy = math.floor(p[1] / self._size)
        for gx in (ix - 1, ix, ix + 1):
            for gy in (iy - 1, iy, iy + 1):
                for x, y, r2 in self._cells.get((gx, gy), ()):
                    dx = p[0] - x
                    dy = p[1] - y
                    if dx * dx + dy * dy < r2:
                        return True
        return False


def compute_centroid(group):
    """CombinerPositioner.ComputeCentroid, CombinerPositioner.cs:228-253."""
    sx = 0.0
    sy = 0.0
    n = 0
    for s in group.strings:
        if s.panels:
            for x, y in s.panels:
                sx += x
                sy += y
                n += 1
            continue
        fx, fy = _resolve_string_centroid(s)
        sx += fx
        sy += fy
        n += 1
    if n == 0:
        return (0.0, 0.0)
    return (sx / n, sy / n)


def _resolve_string_centroid(s):
    """CombinerPositioner.cs:972-988."""
    if abs(s.centroid[0]) > 1e-9 or abs(s.centroid[1]) > 1e-9:
        return s.centroid
    has_a = abs(s.endpoint_a[0]) > 1e-9 or abs(s.endpoint_a[1]) > 1e-9
    has_b = abs(s.endpoint_b[0]) > 1e-9 or abs(s.endpoint_b[1]) > 1e-9
    if has_a or has_b:
        return ((s.endpoint_a[0] + s.endpoint_b[0]) / 2.0, (s.endpoint_a[1] + s.endpoint_b[1]) / 2.0)
    return (0.0, 0.0)


def _project_u(p, cs, sn):
    return p[0] * cs + p[1] * sn


def _project_t(p, cs, sn):
    return p[0] * -sn + p[1] * cs


def _row_string_extent(row_strings, cs, sn):
    """TryComputeRowStringExtent, CombinerPositioner.cs:841-900; returns None when empty."""
    min_u = DOUBLE_MAX
    max_u = DOUBLE_MIN
    min_point = (0.0, 0.0)
    max_point = (0.0, 0.0)
    sum_t = 0.0
    count_t = 0

    def points(s):
        if s.panels:
            return s.panels
        return (s.endpoint_a, s.endpoint_b, _resolve_string_centroid(s))

    for s in row_strings:
        for p in points(s):
            u = _project_u(p, cs, sn)
            t = _project_t(p, cs, sn)
            if u < min_u:
                min_u = u
                min_point = p
            if u > max_u:
                max_u = u
                max_point = p
            sum_t += t
            count_t += 1
    if min_u == DOUBLE_MAX:
        return None
    center_t = sum_t / count_t if count_t > 0 else 0.0
    return (min_u, max_u, center_t, min_point, max_point)


def _resolve_row_key(s):
    """CombinerPositioner.cs:912-917."""
    if s is None:
        return "row:<null>"
    if s.physical_row_key:
        return s.physical_row_key
    return f"row:{s.row_index}"


def _enumerate_candidates(group, centroid, alleys, alignment, options, prefer_road, row_extents, out):
    """CombinerPositioner.cs:412-610."""
    clearance = options.EnclosureClearanceFt
    by_row = {}
    for s in group.strings:
        by_row.setdefault(_resolve_row_key(s), []).append(s)

    for row_key, row_strings in by_row.items():
        if not row_strings:
            continue
        theta = row_strings[0].row_angle_rad
        cs = math.cos(theta)
        sn = math.sin(theta)
        slice_ext = _row_string_extent(row_strings, cs, sn)
        if slice_ext is None:
            continue
        full = row_extents.get(row_key) if row_extents is not None else None
        if full is not None:
            theta = full.row_angle_rad
            cs = math.cos(theta)
            sn = math.sin(theta)
            recomputed = _row_string_extent(row_strings, cs, sn)
            if recomputed is not None:
                slice_ext = recomputed
        s_min_u, s_max_u, s_center_t, s_min_point, s_max_point = slice_ext
        perp_x = -sn
        perp_y = cs
        snap = "RoadSideRowEnd" if prefer_road else "RowEndNearestInverter"
        added_physical = False

        if full is not None:
            full_min_u = full.min_u if full.has_projection_bounds else _project_u(full.min_point, cs, sn)
            full_max_u = full.max_u if full.has_projection_bounds else _project_u(full.max_point, cs, sn)
            if full_max_u < full_min_u:
                full_min_u, full_max_u = full_max_u, full_min_u
            left_reach = max(0.0, s_min_u - full_min_u)
            right_reach = max(0.0, full_max_u - s_max_u)
            left_across = abs(_project_t(full.min_point, cs, sn) - s_center_t)
            right_across = abs(_project_t(full.max_point, cs, sn) - s_center_t)
            frame = _Frame(theta, cs, sn, full.min_point, full.max_point)
            if left_reach <= options.MaxRowEndReachFt and left_across <= options.MaxAcrossRowDeviationFt:
                _add_row_end_candidates(out, full.min_point, True, clearance, full.chord_width_ft, cs, sn,
                                        perp_x, perp_y, prefer_road, snap,
                                        _Route(frame, full.min_point, True, full, options),
                                        "physical-row-end", full.key, left_reach, False)
                added_physical = True
            if right_reach <= options.MaxRowEndReachFt and right_across <= options.MaxAcrossRowDeviationFt:
                _add_row_end_candidates(out, full.max_point, False, clearance, full.chord_width_ft, cs, sn,
                                        perp_x, perp_y, prefer_road, snap,
                                        _Route(frame, full.max_point, False, full, options),
                                        "physical-row-end", full.key, right_reach, False)
                added_physical = True

        if not added_physical:
            frame = _Frame(theta, cs, sn, s_min_point, s_max_point)
            slice_key = f"{row_key}|L2:{group.l2_number}"
            _add_row_end_candidates(out, s_min_point, True, clearance, 0.0, cs, sn, perp_x, perp_y,
                                    prefer_road, snap, _Route(frame, s_min_point, True, full, options),
                                    "ownership-slice-fallback", slice_key, 0.0, True)
            _add_row_end_candidates(out, s_max_point, False, clearance, 0.0, cs, sn, perp_x, perp_y,
                                    prefer_road, snap, _Route(frame, s_max_point, False, full, options),
                                    "ownership-slice-fallback", slice_key, 0.0, True)

    if not prefer_road:
        _add_nearby_alley_candidates(centroid, alleys, out)
    if alignment is not None and len(alignment) >= 2:
        out.append(_Candidate(closest_point_on_segment(centroid, alignment[0], alignment[-1]), "TrenchPoint"))


def _add_row_end_candidates(out, edge, is_left, clearance, chord_width, cs, sn, perp_x, perp_y, prefer_road,
                            snap, route, kind, key, reach, is_fallback):
    """CombinerPositioner.cs:612-734 (rack segments exist only with tracker rows, which are refused)."""
    if prefer_road and not is_fallback:
        if chord_width > 1e-6:
            half = chord_width * 0.5
            out.append(_Candidate((edge[0] + half * perp_x, edge[1] + half * perp_y), snap, route, kind, key,
                                  reach, False))
            out.append(_Candidate((edge[0] - half * perp_x, edge[1] - half * perp_y), snap, route, kind, key,
                                  reach, False))
            return
    if prefer_road and is_fallback:
        out.append(_Candidate(edge, snap, route, kind, key, reach, True))
        return
    along_sign = -1.0 if is_left else 1.0
    along = (edge[0] + along_sign * clearance * cs, edge[1] + along_sign * clearance * sn)
    if not prefer_road:
        out.append(_Candidate(along, snap, route, kind, key, reach, is_fallback))
    out.append(_Candidate((along[0] + clearance * perp_x, along[1] + clearance * perp_y), snap, route, kind, key,
                          reach, is_fallback))
    out.append(_Candidate((along[0] - clearance * perp_x, along[1] - clearance * perp_y), snap, route, kind, key,
                          reach, is_fallback))


def _add_nearby_alley_candidates(centroid, alleys, out):
    """CombinerPositioner.cs:919-952."""
    if not alleys:
        return
    scored = [(distance_point_to_segment(centroid, a.start, a.end), a) for a in alleys if a is not None]
    scored.sort(key=lambda item: item[0])
    for _, alley in scored[:min(MAX_FALLBACK_ALLEY_CANDIDATES_PER_GROUP, len(scored))]:
        out.append(_Candidate(alley.mid, "AlleyMidpoint"))
        out.append(_Candidate(closest_point_on_segment(centroid, alley.start, alley.end), "AlleyMidpoint"))


def _frame_project(p, frame):
    return (p[0] * frame.cs + p[1] * frame.sn, p[0] * -frame.sn + p[1] * frame.cs)


def _row_exit_home_run(s, combiner, route):
    """CombinerPositioner.cs:1100-1121."""
    edge_u, edge_t = _frame_project(route.edge_point, route.frame)
    edge_to_combiner = _dist(route.edge_point, combiner)
    ua, ta = _frame_project(s.endpoint_a, route.frame)
    a = abs(ua - edge_u) + abs(ta - edge_t) + edge_to_combiner
    ub, tb = _frame_project(s.endpoint_b, route.frame)
    b = abs(ub - edge_u) + abs(tb - edge_t) + edge_to_combiner
    return a if a < b else b


def home_run_length(s, combiner):
    """CombinerPositioner.cs:1092-1098."""
    da = _dist(combiner, s.endpoint_a)
    db = _dist(combiner, s.endpoint_b)
    return da if da < db else db


def _clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _point_from_frame(u, t, frame):
    return (frame.cs * u + -frame.sn * t, frame.sn * u + frame.cs * t)


def _expanded_block_bounds(route):
    """CombinerPositioner.cs:1345-1361; (minU, maxU, minT, maxT)."""
    clearance = route.clearance_ft if route.clearance_ft > 0.0 else DEFAULTS["SubarrayPerimeterClearanceFt"]
    across = max(clearance, route.block_row_pitch_ft * 0.5 + 6.0)
    return (route.block_min_u - clearance, route.block_max_u + clearance,
            route.block_min_t - across, route.block_max_t + across)


def _make_perimeter_point(side, value, b):
    if side == 1 or side == 3:
        return (side, _clamp(value, b[2], b[3]))
    return (side, _clamp(value, b[0], b[1]))


def _target_exit_side_points(u, t, b):
    """CombinerPositioner.cs:1363-1382."""
    result = []
    if u <= b[0]:
        result.append(_make_perimeter_point(3, t, b))
    if u >= b[1]:
        result.append(_make_perimeter_point(1, t, b))
    if t <= b[2]:
        result.append(_make_perimeter_point(0, u, b))
    if t >= b[3]:
        result.append(_make_perimeter_point(2, u, b))
    if result:
        return result
    left = abs(u - b[0])
    right = abs(u - b[1])
    bottom = abs(t - b[2])
    top = abs(t - b[3])
    best = min(min(left, right), min(bottom, top))
    if best == left:
        result.append(_make_perimeter_point(3, t, b))
    elif best == right:
        result.append(_make_perimeter_point(1, t, b))
    elif best == bottom:
        result.append(_make_perimeter_point(0, u, b))
    else:
        result.append(_make_perimeter_point(2, u, b))
    return result


def _perimeter_world_point(point, b, frame):
    side, value = point
    if side == 0:
        return _point_from_frame(value, b[2], frame)
    if side == 1:
        return _point_from_frame(b[1], value, frame)
    if side == 2:
        return _point_from_frame(value, b[3], frame)
    return _point_from_frame(b[0], value, frame)


def _perimeter_coordinate(point, b):
    side, value = point
    width = b[1] - b[0]
    height = b[3] - b[2]
    if side == 0:
        return _clamp(value, b[0], b[1]) - b[0]
    if side == 1:
        return width + (_clamp(value, b[2], b[3]) - b[2])
    if side == 2:
        return width + height + (b[1] - _clamp(value, b[0], b[1]))
    return width + height + width + (b[3] - _clamp(value, b[2], b[3]))


def _perimeter_distance(a_point, b_point, b):
    """CombinerPositioner.cs:1403-1414 ("%" is IEEE fmod)."""
    width = b[1] - b[0]
    height = b[3] - b[2]
    if width <= 1e-9 or height <= 1e-9:
        return 0.0
    perimeter = 2.0 * (width + height)
    a = _perimeter_coordinate(a_point, b)
    c = _perimeter_coordinate(b_point, b)
    clockwise = math.fmod(c - a + perimeter, perimeter)
    counter = math.fmod(a - c + perimeter, perimeter)
    return min(clockwise, counter)


def _row_exit_connector_length(frm, to, route):
    """CombinerPositioner.cs:1426-1434."""
    if route is None:
        return _dist(frm, to)
    fu, ft = _frame_project(frm, route.frame)
    tu, tt = _frame_project(to, route.frame)
    return abs(tt - ft) + abs(tu - fu)


def _perimeter_connector_length(frm, to, route):
    """CombinerPositioner.cs:1315-1343."""
    if route is None:
        return _dist(frm, to)
    if not route.has_subarray_block:
        return _row_exit_connector_length(frm, to, route)
    b = _expanded_block_bounds(route)
    fu, ft = _frame_project(frm, route.frame)
    tu, tt = _frame_project(to, route.frame)
    start = (3 if route.is_left_end else 1, _clamp(ft, b[2], b[3]))
    start_leg = _dist(frm, _perimeter_world_point(start, b, route.frame))
    best = math.inf
    for exit_point in _target_exit_side_points(tu, tt, b):
        world = _perimeter_world_point(exit_point, b, route.frame)
        d = start_leg + _perimeter_distance(start, exit_point, b) + _dist(world, to)
        if d < best:
            best = d
    return _row_exit_connector_length(frm, to, route) if math.isinf(best) else best


def _project_to_polyline(p, line):
    """TryProjectPointToPolyline, CombinerPositioner.cs:1450-1492; (point, distance, chain) or None."""
    if line is None or len(line) < 2:
        return None
    found = None
    best = math.inf
    chain = 0.0
    for i in range(1, len(line)):
        a = line[i - 1]
        b = line[i]
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        length = math.sqrt(dx * dx + dy * dy)
        if length <= 1e-9:
            continue
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (length * length)
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        q = (a[0] + t * dx, a[1] + t * dy)
        d = _dist(p, q)
        if d < best:
            best = d
            found = (q, d, chain + t * length)
        chain += length
    return found


def _polyline_length(line):
    if line is None or len(line) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(line)):
        total += _dist(line[i - 1], line[i])
    return total


def _distance_to_polyline(p, line):
    best = math.inf
    if line is None or len(line) < 2:
        return best
    for j in range(1, len(line)):
        d = distance_point_to_segment(p, line[j - 1], line[j])
        if d < best:
            best = d
    return best


class _RoadContext:
    __slots__ = ("has_roads", "nearest_l2_road", "preferred")

    def __init__(self):
        self.has_roads = False
        self.nearest_l2_road = math.inf
        self.preferred = []


def _build_access_road_context(l2_location, road_lines):
    """CombinerPositioner.cs:1223-1299; preferred entries are (line, l2Distance, length, projection)."""
    ctx = _RoadContext()
    if not road_lines or not any(line is not None and len(line) >= 2 for line in road_lines):
        return ctx
    lines = []
    for line in road_lines:
        if line is None or len(line) < 2:
            continue
        proj = _project_to_polyline(l2_location, line)
        if proj is None:
            continue
        lines.append((line, proj[1], _polyline_length(line), proj))
    if not lines:
        return ctx
    pool = [entry for entry in lines if entry[2] >= MIN_PREFERRED_ACCESS_ROAD_LENGTH_FT]
    if not pool:
        pool = lines
    for entry in pool:
        if entry[1] < ctx.nearest_l2_road:
            ctx.nearest_l2_road = entry[1]
    pool = sorted(pool, key=lambda entry: entry[1])
    ctx.has_roads = True
    cutoff = math.inf
    if not math.isinf(ctx.nearest_l2_road) and ctx.nearest_l2_road <= L2_ROAD_MATCH_MAX_DISTANCE_FT:
        cutoff = min(L2_ROAD_MATCH_MAX_DISTANCE_FT, ctx.nearest_l2_road + L2_ROAD_MATCH_EXTRA_TOLERANCE_FT)
    for entry in pool:
        if len(ctx.preferred) >= MAX_PREFERRED_ACCESS_ROAD_LINES:
            break
        if entry[1] > cutoff and ctx.preferred:
            break
        ctx.preferred.append(entry)
    return ctx


def _routed_trunk_length(combiner, l2_location, road, route):
    """CombinerPositioner.cs:1189-1221."""
    if not road.has_roads or not road.preferred:
        return _perimeter_connector_length(combiner, l2_location, route)
    best = math.inf
    for line, _, _, l2_proj in road.preferred:
        if line is None or len(line) < 2:
            continue
        cb = _project_to_polyline(combiner, line)
        if cb is None:
            continue
        to_road = _perimeter_connector_length(combiner, cb[0], route)
        road_run = abs(l2_proj[2] - cb[2])
        total = to_road + road_run + l2_proj[1]
        if total < best:
            best = total
    return _perimeter_connector_length(combiner, l2_location, route) if math.isinf(best) else best


def _score(p, group, l2_location, alignment, clearance, options, road, route):
    """ScoreCandidateDetailed, CombinerPositioner.cs:1009-1080; (cost, trunk, sum, max, road distance)."""
    w = options.Weights
    sum_hr = 0.0
    max_hr = 0.0
    n = len(group.strings)
    for s in group.strings:
        hr = _row_exit_home_run(s, p, route) if route is not None else home_run_length(s, p)
        sum_hr += hr
        if hr > max_hr:
            max_hr = hr
    avg_hr = sum_hr / n if n > 0 else 0.0
    factor = (options.HomeRunConductorsPerString if options.HomeRunConductorsPerString > 0.0 else 1.0) \
        if options.ApplyConductorFactor else 1.0
    priced_sum = sum_hr * factor
    priced_max = max_hr * factor
    priced_avg = avg_hr * factor
    trunk = _routed_trunk_length(p, l2_location, road, route)
    imbalance = priced_max - priced_avg
    if imbalance < 0:
        imbalance = 0.0
    penalty = w["Delta"] if clearance.violates(p) else 0.0
    trench_dev = 0.0
    if alignment is not None and len(alignment) >= 2:
        trench_dev = distance_point_to_segment(p, alignment[0], alignment[-1])
        if trench_dev < 5.0:
            trench_dev = 0.0
    road_dev = 0.0
    preferred_road = math.inf
    if road.has_roads:
        preferred_road = math.inf
        for line, _, _, _ in road.preferred:
            d = _distance_to_polyline(p, line)
            if d < preferred_road:
                preferred_road = d
        road_dev = preferred_road
        if road_dev < options.RoadAccessFreeDistanceFt:
            road_dev = 0.0
        else:
            road_dev -= options.RoadAccessFreeDistanceFt
    cost = (w["Alpha"] * priced_sum + w["Beta"] * trunk + w["Gamma"] * imbalance + penalty
            + w["Epsilon"] * trench_dev + options.RoadAccessWeight * road_dev)
    return (cost, trunk, sum_hr, max_hr, preferred_road)


def position(group, l2_location, space, alignment, road_lines, clearance, options):
    """CombinerPositioner.Place, CombinerPositioner.cs:68-222."""
    if group is None:
        raise PlacementError("group is null")
    alleys = space.alleys if space is not None else None
    row_extents = space.row_extents if space is not None else None
    centroid = compute_centroid(group)
    candidates = []
    road = _build_access_road_context(l2_location, road_lines)
    _enumerate_candidates(group, centroid, alleys, alignment, options, road.has_roads, row_extents, candidates)
    if not road.has_roads or not candidates:
        candidates.append(_Candidate(centroid, "Centroid"))

    scored = [(c, _score(c.point, group, l2_location, alignment, clearance, options, road, c.route))
              for c in candidates]

    has_row_edge = False
    has_preferred_row_edge = False
    nearest_edge = math.inf
    for c, _ in scored:
        if c.route is None:
            continue
        has_row_edge = True
        if not c.fallback:
            has_preferred_row_edge = True
    for c, _ in scored:
        if c.route is None:
            continue
        if has_preferred_row_edge and c.fallback:
            continue
        d = _dist(c.route.edge_point, l2_location)
        if d < nearest_edge:
            nearest_edge = d

    nearest_lane_road = math.inf
    if has_row_edge and road.has_roads:
        for c, sc in scored:
            if c.route is None:
                continue
            if has_preferred_row_edge and c.fallback:
                continue
            if _dist(c.route.edge_point, l2_location) > nearest_edge + 1e-6:
                continue
            if sc[4] < nearest_lane_road:
                nearest_lane_road = sc[4]

    best = PositionResult()
    for c, sc in scored:
        if has_row_edge:
            if c.route is None:
                continue
            if has_preferred_row_edge and c.fallback:
                continue
            if _dist(c.route.edge_point, l2_location) > nearest_edge + 1e-6:
                continue
            if road.has_roads and not math.isinf(nearest_lane_road) and sc[4] > nearest_lane_road + 1e-6:
                continue
        if sc[0] < best.cost:
            best.cost = sc[0]
            best.location = c.point
            best.snap = c.source
            best.trunk_route_length = sc[1]
            best.straight_trunk_length = _dist(c.point, l2_location)
            best.sum_home_run_length = sc[2]
            best.max_home_run_length = sc[3]
            best.placement_extent_kind = c.kind
            best.placement_extent_key = c.key
            best.row_end_reach_ft = c.reach
    return best


# ── CombinerPlacementEngine (CombinerPlacementEngine.cs) ─────────────────────


class _Working:
    __slots__ = ("group", "original_l2", "parent_l2", "position", "initial_trunk", "moved_l2", "moved_position")

    def __init__(self, group, pos):
        self.group = group
        self.original_l2 = group.l2_number
        self.parent_l2 = group.l2_number
        self.position = pos
        self.initial_trunk = effective_trunk_length(pos)
        self.moved_l2 = False
        self.moved_position = False


def effective_trunk_length(pos):
    """CombinerPlacementEngine.cs:877-882."""
    return pos.trunk_route_length if pos.trunk_route_length > 0.0 else pos.straight_trunk_length


def mount_key(pos):
    """CombinerPlacementEngine.cs:836-842, as a comparable tuple."""
    return (pos.placement_extent_kind or "", pos.placement_extent_key or "",
            _net_round1(pos.location[0]), _net_round1(pos.location[1]))


def _joint_objective(pos, median, options):
    """CombinerPlacementEngine.cs:844-856."""
    trunk = effective_trunk_length(pos)
    ratio = _positive_double_or_default(options.JointL2RowEndTailRatio, 4.0)
    tail_penalty = _non_negative_or_default(options.JointL2RowEndTailPenalty, 1.5)
    wire_weight = _non_negative_or_default(options.JointL2RowEndWireWeight, 0.02)
    excess = trunk - median * ratio
    if excess < 0.0:
        excess = 0.0
    return trunk + tail_penalty * excess + wire_weight * pos.sum_home_run_length


def _median_trunk(placements):
    """CombinerPlacementEngine.cs:858-866 (the lower median)."""
    if not placements:
        return 0.0
    values = sorted(effective_trunk_length(p.position) for p in placements)
    return values[(len(values) - 1) // 2]


def _total_trunk(placements):
    total = 0.0
    for p in placements:
        total += effective_trunk_length(p.position)
    return total


class _Joint:
    """State of OptimizeJointL2RowEndPlacements, CombinerPlacementEngine.cs:392-483."""

    def __init__(self, placements, l2s, l2_lookup, space, alignment, road_lines, clearance, options):
        self.placements = placements
        self.l2s = l2s
        self.l2_lookup = l2_lookup
        self.space = space
        self.alignment = alignment
        self.road_lines = road_lines
        self.clearance = clearance
        self.options = options
        self.moves = 0
        self.l2_moved = 0
        self.swaps = 0
        self.evaluations = 0
        self.cache_hits = 0
        self.budget_hit = False
        self.counts = {l2[0]: 0 for l2 in l2s}
        for p in placements:
            self.counts[p.parent_l2] = self.counts.get(p.parent_l2, 0) + 1
        self.mount_use = {}
        for p in placements:
            key = mount_key(p.position)
            self.mount_use[key] = self.mount_use.get(key, 0) + 1
        self.cache = {(id(p), p.parent_l2): p.position for p in placements}
        self._keep = placements            # id() keys stay valid while the list lives

    def evaluate(self, placement, l2_number, l2_location):
        """EvaluatePlacementForL2, CombinerPlacementEngine.cs:672-712."""
        key = (id(placement), l2_number)
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        original = placement.group.l2_number
        placement.group.l2_number = l2_number
        try:
            self.evaluations += 1
            result = position(placement.group, l2_location, self.space, self.alignment, self.road_lines,
                              self.clearance, self.options)
            self.cache[key] = result
            return result
        finally:
            placement.group.l2_number = original

    def mount_available(self, released_a, released_b, target):
        """CombinerPlacementEngine.cs:803-815."""
        count = self.mount_use.get(target, 0)
        if released_a is not None and mount_key(released_a.position) == target:
            count -= 1
        if released_b is not None and mount_key(released_b.position) == target:
            count -= 1
        return count <= 0

    def release(self, placement):
        key = mount_key(placement.position)
        count = self.mount_use.get(key)
        if count is None:
            return
        if count <= 1:
            del self.mount_use[key]
        else:
            self.mount_use[key] = count - 1

    def reserve(self, placement):
        key = mount_key(placement.position)
        self.mount_use[key] = self.mount_use.get(key, 0) + 1

    def apply(self, placement, new_l2, new_pos):
        """ApplyPlacement, CombinerPlacementEngine.cs:650-670."""
        old_l2 = placement.parent_l2
        old_mount = mount_key(placement.position)
        placement.parent_l2 = new_l2
        placement.group.l2_number = new_l2
        placement.position = new_pos
        if old_l2 != new_l2:
            placement.moved_l2 = True
            self.l2_moved += 1
        if old_mount != mount_key(new_pos):
            placement.moved_position = True
        self.moves += 1

    def candidate_l2s(self, placement, max_candidates):
        """CandidateL2s, CombinerPlacementEngine.cs:714-746."""
        origin = placement.position.location
        ranked = sorted(self.l2s, key=lambda l2: _dist(origin, l2[1]))
        result = []
        included = False
        for l2 in ranked:
            if len(result) >= max_candidates:
                break
            if l2[0] == placement.parent_l2:
                included = True
            result.append(l2)
        if not included:
            for l2 in self.l2s:
                if l2[0] == placement.parent_l2:
                    result.append(l2)
                    break
        return result


def _rank_placements(placements, median, options):
    """CombinerPlacementEngine.cs:748-773: objective desc, trunk desc, index asc."""
    ranked = [(_joint_objective(p.position, median, options), effective_trunk_length(p.position), i, p)
              for i, p in enumerate(placements)]
    ranked.sort(key=lambda r: (-r[0], -r[1], r[2]))
    return ranked


def _budget_exhausted(joint, started, budget_ms):
    if budget_ms > 0 and (time.monotonic() - started) * 1000.0 >= budget_ms:
        joint.budget_hit = True
        return True
    return False


def _find_best_joint_move(joint, current, current_objective, ranked, median, started, budget_ms,
                          max_candidate_l2, max_swap_partners):
    """FindBestJointMove, CombinerPlacementEngine.cs:485-617; (A, B, newL2A, newL2B, posA, posB, gain)."""
    options = joint.options
    best = None
    cap = options.MaxL1PerL2
    for number, target_location in joint.candidate_l2s(current, max_candidate_l2):
        if _budget_exhausted(joint, started, budget_ms):
            break
        if number not in joint.l2_lookup:
            continue
        target_location = joint.l2_lookup[number]
        target_pos = joint.evaluate(current, number, target_location)
        target_mount = mount_key(target_pos)
        if number == current.parent_l2 or cap <= 0 or joint.counts.get(number, 0) < cap:
            if joint.mount_available(current, None, target_mount):
                gain = current_objective - _joint_objective(target_pos, median, options)
                if gain > 0.0 and (best is None or gain > best[6]):
                    best = (current, None, number, 0, target_pos, None, gain)
            continue
        checked = 0
        for _, _, _, partner in ranked:
            if checked >= max_swap_partners:
                break
            if _budget_exhausted(joint, started, budget_ms):
                break
            if partner is current or partner.parent_l2 != number:
                continue
            checked += 1
            if current.parent_l2 not in joint.l2_lookup:
                continue
            original_location = joint.l2_lookup[current.parent_l2]
            partner_pos = joint.evaluate(partner, current.parent_l2, original_location)
            partner_mount = mount_key(partner_pos)
            if (target_mount == partner_mount or not joint.mount_available(current, partner, target_mount) or
                    not joint.mount_available(current, partner, partner_mount)):
                continue
            partner_objective = _joint_objective(partner.position, median, options)
            gain = (current_objective + partner_objective - _joint_objective(target_pos, median, options)
                    - _joint_objective(partner_pos, median, options))
            if gain > 0.0 and (best is None or gain > best[6]):
                best = (current, partner, number, current.parent_l2, target_pos, partner_pos, gain)
    return best


def _commit_joint_move(joint, move):
    """CommitJointMove, CombinerPlacementEngine.cs:619-648."""
    a, b, new_l2_a, new_l2_b, pos_a, pos_b, _ = move
    if b is not None:
        joint.release(a)
        joint.release(b)
        joint.apply(a, new_l2_a, pos_a)
        joint.apply(b, new_l2_b, pos_b)
        joint.reserve(a)
        joint.reserve(b)
        joint.swaps += 1
        return
    joint.release(a)
    old_l2 = a.parent_l2
    joint.apply(a, new_l2_a, pos_a)
    joint.reserve(a)
    if old_l2 != new_l2_a:
        joint.counts[old_l2] = joint.counts.get(old_l2, 0) - 1
        joint.counts[new_l2_a] = joint.counts.get(new_l2_a, 0) + 1


def optimize_joint(placements, l2s, l2_lookup, space, alignment, road_lines, clearance, options):
    """OptimizeJointL2RowEndPlacements, CombinerPlacementEngine.cs:392-483. Returns the state (counters)."""
    joint = _Joint(placements, l2s, l2_lookup, space, alignment, road_lines, clearance, options)
    if (not placements or l2s is None or len(l2s) <= 1 or not options.EnableJointL2RowEndOptimization or
            options.MaxL1PerL2 <= 0):
        return joint
    max_passes = _positive_or_default(options.JointL2RowEndMaxPasses, 4)
    max_candidate_l2 = _positive_or_default(options.JointL2RowEndMaxCandidateL2, 8)
    max_swap_partners = _positive_or_default(options.JointL2RowEndMaxSwapPartnersPerL2, 4)
    min_savings = _non_negative_or_default(options.JointL2RowEndMinSavingsFt, 0.001)
    budget_ms = max(options.JointL2RowEndMaxMilliseconds, 0)
    started = time.monotonic()
    for pass_index in range(max_passes):
        median = _median_trunk(placements)
        tail_threshold = median * _positive_double_or_default(options.JointL2RowEndTailRatio, 4.0)
        ranked = _rank_placements(placements, median, options)
        moves_this_pass = 0
        for _, _, _, current in ranked:
            if _budget_exhausted(joint, started, budget_ms):
                break
            current_objective = _joint_objective(current.position, median, options)
            if pass_index == 0 and effective_trunk_length(current.position) <= tail_threshold:
                continue
            best = _find_best_joint_move(joint, current, current_objective, ranked, median, started, budget_ms,
                                         max_candidate_l2, max_swap_partners)
            if best is None or best[6] <= min_savings:
                continue
            _commit_joint_move(joint, best)
            moves_this_pass += 1
        if joint.budget_hit or (moves_this_pass == 0 and pass_index > 0):
            break
    return joint


def choose_sku(actual_inputs, options):
    """CombinerPlacementEngine.ChooseSku, CombinerPlacementEngine.cs:933-942."""
    for sku in SKU_CATALOG:
        if sku >= actual_inputs and sku <= options.MaxInputs:
            return sku
    return options.MaxInputs


def _point_json(p):
    """CombinerPlacementDumpCmd.cs:1607-1625: {x, y, z} with z 0."""
    return {"x": p[0], "y": p[1], "z": 0.0}


def _alley_json(a):
    """ProjectAlley, CombinerPlacementDumpCmd.cs:1554-1565."""
    return {"kind": a.kind, "width": a.width, "rowAngleRad": a.row_angle_rad, "start": _point_json(a.start),
            "end": _point_json(a.end), "center": _point_json(a.mid)}


def _read_polyline(raw, what):
    if raw is None:
        return None
    raw = _list(raw, what, MAX_PANELS)
    return [_point(p, f"{what}[{i}]") for i, p in enumerate(raw)]


def place(intake, installation="Roof"):
    """LEAFCOMBINERAUTO's placement phase on one intake; returns the plugin's solution shape."""
    intake = _dict(intake, "intake")
    if intake.get("format") != INTAKE_FORMAT:
        raise PlacementError(f"intake format must be {INTAKE_FORMAT}")
    if intake.get("stage") != INTAKE_STAGE:
        raise PlacementError(f"intake stage must be {INTAKE_STAGE}")
    inputs = _dict(intake.get("inputs"), "inputs")
    context = _dict(intake.get("commandContext"), "commandContext")

    if _list(inputs.get("trackerRows", []), "inputs.trackerRows", MAX_PANELS):
        raise PlacementError("tracker rows are not ported (TrackerRowClusterer); refusing")
    if context.get("routeHomerunsInCloud") is True:
        raise PlacementError("cloud combiner placement is not ported; refusing")

    options = Options(inputs.get("options"))
    # The input plan (CombinerAutoCmd.cs:400-432): the persisted strings-per-input the dialog applied.
    strings = build_strings(intake)
    if strings:
        apply_input_target(options,
                           _int(context.get("combinerBoxConnections", 0), "commandContext.combinerBoxConnections"),
                           _int(context.get("l2NumMppt", 0), "commandContext.l2NumMppt"),
                           _int(context.get("l2StringsPerMppt", 0), "commandContext.l2StringsPerMppt"))

    l2s = []
    for i, raw in enumerate(_list(inputs.get("l2Inverters"), "inputs.l2Inverters", MAX_L2)):
        raw = _dict(raw, f"l2Inverters[{i}]")
        l2s.append((_int(raw.get("Number"), f"l2Inverters[{i}].Number"),
                    _point(raw.get("InsertPt"), f"l2Inverters[{i}].InsertPt", upper=True)))
    panel_groups = reconstruct_panel_groups(intake, installation)
    trench = _read_polyline(inputs.get("alignmentLine"), "inputs.alignmentLine")
    roads = []
    for i, line in enumerate(_list(inputs.get("accessRoadLines", []), "inputs.accessRoadLines", MAX_PANELS)):
        pts = line.get("points") if isinstance(line, dict) else line
        roads.append(_read_polyline(pts, f"inputs.accessRoadLines[{i}]"))

    empty = {"combiners": [], "l1ToL2Assignments": {}, "detectedAlleys": []}
    if not panel_groups and not strings:
        return empty                                             # CombinerPlacementEngine.cs:51-60
    if not l2s:
        return empty                                             # :61-70

    # Stage 1: alleys (CombinerPlacementEngine.cs:73-76)
    alleys = detect_alleys(panel_groups, options.MinAlleyWidthFt, options.MaxRoadWidthFt)
    # Stage 2: alignment (:92-93)
    sx = 0.0
    sy = 0.0
    for _, (x, y) in l2s:
        sx += x
        sy += y
    alignment = resolve_alignment(trench, alleys, panel_groups, (sx / len(l2s), sy / len(l2s)))
    # Stage 3: strings (:97-115); the pre-built list always exists on this path
    if not strings:
        return {"combiners": [], "l1ToL2Assignments": {}, "detectedAlleys": [_alley_json(a) for a in alleys]}
    # Stage 4: partition, placement space (:118-121)
    l2_lookup = {}
    for number, loc in l2s:
        l2_lookup[number] = loc
    groups = partition(strings, options, l2_lookup)
    space = PlacementSpace.create(strings, alleys, options)
    all_panels = [p for g in panel_groups for p in g.panels]
    clearance = ClearanceIndex(all_panels)

    working = []
    for g in groups:
        if g.l2_number not in l2_lookup:
            continue                                             # :131-137
        pos = position(g, l2_lookup[g.l2_number], space, alignment, roads, clearance, options)
        working.append(_Working(g, pos))

    optimize_joint(working, l2s, l2_lookup, space, alignment, roads, clearance, options)

    combiners = []
    assignments = {}
    next_l1 = 1
    for wp in working:
        if wp.parent_l2 not in l2_lookup:
            continue                                             # :192-198
        l1 = next_l1
        next_l1 += 1
        g = wp.group
        combiners.append({
            "L1Number": l1,
            "ParentL2Number": wp.parent_l2,
            "location": _point_json(wp.position.location),
            "ServedStringIds": [s.string_number for s in g.strings],
            "InputCountUsed": len(g.strings),
            "InputCountSku": choose_sku(len(g.strings), options),
            "snap": wp.position.snap,
        })
        assignments[str(l1)] = wp.parent_l2
    return {"combiners": combiners, "l1ToL2Assignments": assignments,
            "detectedAlleys": [_alley_json(a) for a in space.alleys]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio port of LEAFCOMBINERAUTO's placement phase (G35c).")
    parser.add_argument("--intake", required=True, help="combiner-intake.json (format combiner-intake-v1)")
    parser.add_argument("--out", required=True, help="where to write the solution JSON")
    args = parser.parse_args(argv)
    try:
        with open(args.intake, encoding="utf-8-sig") as fh:
            intake = json.load(fh)
        solution = place(intake)
    except (OSError, json.JSONDecodeError, PlacementError) as exc:
        print(f"solar_inverter_combiner: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(solution, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)
    print(f"combiners {len(solution['combiners'])} alleys {len(solution['detectedAlleys'])} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
