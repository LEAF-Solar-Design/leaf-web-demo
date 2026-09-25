"""Studio ports of the plugin's inverter cabling commands (contract G35, G35a, G35c): combiner-auto-place
(i5 LEAFCOMBINERAUTO), route-l2-feeders (i11 RouteL2Feeders), homeruns (i12 HomerunsAuto),
lightweight-cabling-feeders (i13 LEAFLITEFEEDERS), inverter-move (i17 MOVEINV, declared) and
inverter-position (POSITIONINV, declared).

Literal ports of Branch2025 (read 2026-09-24 at C:/tmp/solar-parity/wt-b25-s69, master 6b940d51, the
captured build):

  combiner-auto-place (CombinerAutoCmd.Run, CombinerAutoCmd.cs:52-760), after the placement solution
  (server/solar_inverter_combiner.py place(intake), G35c):
    :524-569     one L1 block per placed combiner, in solution order, through PlaceNewInverterBlock
                 (StringHomeRunCmd.cs:1363-1586): number = L1Number, the placed location, no placement
                 marker, the combiner symbol scale (StringHomeRunCmd.cs:1480-1484, a host quantity),
                 the CombinerBoxConnections snapshot as its input count (:1568-1575)
    :601-622     L1ToL2Assignments merged with the solution's, L1ToL2InputAssignments seeded
                 (MpptBalanceAnalyzer.cs:854-877) from the merged map for the solution's L1s under
                 the host's L2NumMppt (the intake's commandContext), then one drawing-properties save
    :3160-3221   PersistStringAssociations: each served string (combiners in L1 order) gets the
                 combiner's number in memory, and the string -> L1 map replaces the combiner string
                 assignment store (CombinerStringAssignmentStore.cs:85-125)
    :3867-3931   the served string ids are the pre-built strings' cableByStringId keys; a pre-built
                 string's EndpointA and EndpointB are its polyline's first and last vertex
    :2413-2468   then HomerunsAuto's automatic mode (OptiHomerunCmd.Run(true, true), :121-192):
      :141-154     every String-layer string
      :2034-2134   DrawUtilityScaleDirectHomeruns (the same direct router as homeruns below): the
                   existing homeruns erased, then one straight leg per string endpoint marker to the
                   string's combiner, length / 12 in feet
      :3506-3595   the combiner: the number the association set on the string (the cached Cable,
                   AcadCommandBase.cs:1137-1139; else the circuit's number, Cable.cs:36-41) in the L1
                   dictionary (:1951-1968), the one of that number or the closest to the start marker,
                   else (L1/L2 mode) the nearest registered L1 to the markers' midpoint
      :176-181     then RouteL2Feeders (below) when both levels are registered
    The L2 numbers the feeder routing reads are the NUMBER attributes the state does not carry; the
    intake's l2Inverters record them with each block's insertion point (G35c), matched here to the
    state's L2 devices by position.

  route-l2-feeders (OptiHomerunCmd.RouteL2Feeders, OptiHomerunCmd.cs:1042-1368)
    :1005-1040   the phantom-L2 guard: an L2 both far from every combiner (over 4x the median nearest
                 distance) and stacked within 100 units of another L2 is dropped
    :1061-1068   max L1 per L2 = ceil(L1 / L2), or the host's L1CollectorsPerL2 when smaller
    :1085-1096   the L1s ordered by their nearest-L2 distance
    :1102-1135   each L1 to its cheapest L2 under the cap; the no-pass rule triples the cost of an L2
                 reachable only past another (through <= 1.10 x direct); none under the cap: the least
                 loaded, first of equals
    :909-988     OptimizeFeederAssignmentBySwaps: capacity-preserving pairwise swaps, 60 passes at most,
                 a swap taken below -1e-6
    :1150-1155   the assignments replace L1ToL2Assignments and the drawing properties are saved (one
                 save: the G27 catalog append)
    :1412-1452   every existing feeder record's circuit is adopted and never redrawn
    :1773-1820   DeriveVerticalLaneXs: the X gaps between the merged X spans of the panel-group
                 outlines, plus two field-edge lanes a quarter of the first band's width outside
    :1712-1769, :1822-1852
                 DrawNearestLaneCombFeeders: cb -> (lane, cb.y) -> (lane, inv.y) -> inv, deduplicated,
                 length the polyline's length / 12 in feet; the printed count line
  homeruns (Commands.cs:1082-1178 HomerunsAuto -> OptiHomerunCmd.Run(true, true), :121-339)
    :124-130     no L1 combiner: "No inverters found in drawing"
    :2027-2032   the direct-draw path is always taken
    :2034-2134   DrawUtilityScaleDirectHomeruns: the existing homeruns erased (:3884), then one straight
                 homerun per string endpoint (start, end) to the string's combiner, length / 12 in feet
    :3506-3595   the combiner: FindClosestInverter, else (L1/L2 mode) FindNearestRegisteredL1Combiner by
                 the midpoint of the string's two endpoint markers, first of equals
    :176-181     then RouteL2Feeders (L1/L2 mode, both levels present)
  lightweight-cabling-feeders (LightweightCablingCommands.cs:96-149)
    CablingSolver.cs:53-146   AssignFeeders (cap CapPerInverter 36, no weight cap, tail bias: the farthest
                              combiner first, squared-distance swaps), LightweightCablingOptions.cs:37,68,71
    LightweightCablingCommands.cs:488-493
                              DeriveLanes: the rack extents; the rooftop fixture has none, so no lane and
                              each comb path drops at the inverter's X (NearestLaneX, Engine.cs:260-270)
    LightweightCablingEngine.cs:226-258, LightweightCablingCommands.cs:171-193, :453-486
                              BuildFeederPaths (comb), every Leaf feeder erased, the paths drawn with a
                              record that carries no sizing (stored length 0)
  inverter-move (StringHomeRunCmd.InverterMoveSave(false), StringHomeRunCmd.cs:385-600)

Declared divergences (G35; findings under docs/parity/divergences/):
  inverter-move: the plugin moves the block to the jig's acquired point (InverterMoveJig.cs:51-69 takes
    it as is) and erases the block's homeruns, and its reroute finds no inverters. Studio moves the
    device to the same acquired point (G35b: the point answer is the point AutoCAD delivered, object
    snap already applied by the host, so no snap runs here), then reroutes each erased homerun with the
    ported direct router (one straight leg from the same string endpoint to the moved device), and saves
    once. The device row matches; the homerun rows are the declared divergence.
  inverter-position: the plugin's POSITIONINV optimum search is bounded since Branch2025 #312
    (InverterMoveSearch.Plan, StringHomeRunCmd.cs:29-74: a 20-unit grid over the device's string extents
    grown 50 each side, :821-831, doubled until at most MaxCandidatePoints 10000). Studio searches exactly
    that plan (position_plan) in the same row-major order and keeps the first strictly shortest candidate
    outside every panel-group outline and its OUTLINE_BUFFER band (:853-882; :736 expands each outline by
    50), then moves and reroutes as inverter-move. The declared part: the score is Studio's rerouted
    straight-leg total, not the jig's axis route (InverterMoveJig.cs:155-240), and POSITION_TIME_BUDGET_S
    is a Studio-only safety bound that fails closed and never changes a finished search's answer.

HOST INPUTS: values the commands read that the drawing state does not carry (host per-user settings,
block attributes, the picked entity); named by the plugin value they stand for. The producer records
the captured values and how each was measured.

Device numbers: the plugin keeps a device's number only on its NUMBER attribute, which the G35 state
does not carry (inverter_evidence.py:22-30). Studio reads it where the drawing records it next to the
device: the private `_number` of a device an engine placed, else the circuit of the feeder that starts
(L1) or ends (L2) exactly at the device's insertion point.

The string -> combiner association HomerunsAuto resolves through the circuit tag and the combiners'
NUMBER attributes (host tagging settings and attributes the state does not carry) is read from where the
drawing records it: the combiner the string's existing homeruns end at. A string with none takes the
ported nearest-combiner fallback. A polyline redrawn over identical vertices measures the identical
length, so such a leg keeps its stored length.

Pure functions over plain data: no CAD host, no I/O, no network. Every engine takes a G35 state, never
mutates it, and returns (new state, printed lines). Bounded: linear or N x M in devices, strings and
outline vertices; the swap passes are capped at 60 as in the plugin. Malformed input fails closed with
InverterCablingError; a path this port does not cover raises InverterCablingNotPortedError.
"""
from __future__ import annotations

import copy
import importlib.util
import math
from pathlib import Path
import re
import sys
import time


def _load_sibling(name):
    """A server module by path (any cwd), shared through sys.modules so its error classes are one."""
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load_sibling("solar_inverter_state")
dev = _load_sibling("solar_inverter_devices")
comb = _load_sibling("solar_inverter_combiner")

# OptiHomerunCmd.cs:1021-1022: the phantom-L2 guard.
PHANTOM_MEDIAN_FACTOR, PHANTOM_STACK_RADIUS = 4.0, 100.0
# OptiHomerunCmd.cs:1118: the no-pass rule.
NO_PASS_RATIO, NO_PASS_PENALTY = 1.10, 3.0
# OptiHomerunCmd.cs:954, CablingSolver.cs:119: swap passes; OptiHomerunCmd.cs:969: the swap threshold.
MAX_SWAP_PASSES, SWAP_EPSILON = 60, 1e-6
# OptiHomerunCmd.cs:1810: the field-edge lane offset, a quarter of the first band (1000 if degenerate).
EDGE_LANE_FRACTION, DEGENERATE_BAND_WIDTH = 0.25, 1000.0
# OptiHomerunCmd.cs:1838, LightweightCablingEngine.cs DedupePath: the path dedupe epsilon.
PATH_EPSILON = 1e-6
INCHES_PER_FOOT = 12.0
# LightweightCablingOptions.cs:37, :68, :71.
LITE_CAP_PER_INVERTER, LITE_TAIL_BIAS, LITE_DIRECT = 36, True, False
# StringHomeRunCmd.cs:426: the buffer kept around every panel-group outline by the optimum search.
OUTLINE_BUFFER = 50.0
# StringHomeRunCmd.cs:31, :824-831 (Branch2025 #312): the plugin's POSITIONINV plan, a 20-unit step over
# the string extents grown 50 each side, at most 10000 candidates.
POSITION_STEP, POSITION_EXTENTS_MARGIN, POSITION_MAX_CANDIDATES = 20.0, 50.0, 10_000
# inverter-position (declared): Studio's safety bound; fails closed, never changes a finished answer.
POSITION_TIME_BUDGET_S = 10.0
MAX_OUTLINE_VERTICES_TOTAL = 1_000_000
MAX_DEVICES = 10_000
UNASSIGNED_CIRCUIT = "-"
HOST_KEYS = frozenset({"UseL2Collectors", "L1CollectorsPerL2", "RackExtents", "MovedDevice", "PositionDevice",
                       "CombinerSymbolScale", "L2Numbers"})
# combiner-auto-place: the intake's full doubles against the state's printed ones (the dump keeps about
# 16 significant digits), in drawing units; a match must be unique.
MATCH_EPSILON = 1e-6
# CombinerAutoCmd.cs:2635-2669: the input plan dialog's answer the capture gave (G30a form_values).
INPUT_PLAN_APPLY = "Apply"
# inverter_evidence.py:116-117: the circuits a homerun and a feeder carry.
_HOMERUN_CIRCUIT = re.compile(r"[+-]?(?P<string>[0-9]+)/(?P<type>[A-Za-z]?)(?P<device>[0-9]+)(?P<mppt>[A-Za-z]*)")
_FEEDER_CIRCUIT = re.compile(r"F(?P<source>[0-9]+)/(?P<target>[0-9]+)")


class InverterCablingError(ValueError):
    """A malformed state, host input or answer: nothing is drawn."""


class InverterCablingNotPortedError(InverterCablingError):
    """The command would take a path this port does not cover (named in the message)."""


# ------------------------------------------------------------------ inputs --

def _host(host):
    if not isinstance(host, dict) or set(host) - HOST_KEYS:
        raise InverterCablingError(f"host inputs must be an object of {sorted(HOST_KEYS)}")
    return host


def _host_bool(host, key):
    value = host.get(key)
    if type(value) is not bool:
        raise InverterCablingError(f"host input {key} is required (true or false)")
    return value


def _host_device(host, key):
    value = host.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 2 or value[0] not in ("L1", "L2") or \
            type(value[1]) is not int or value[1] <= 0:
        raise InverterCablingError(f"host input {key} must be [\"L1\" | \"L2\", number]")
    return value[0], value[1]


def _dist(a, b):
    """Point2d.GetDistanceTo / XY.DistanceTo: sqrt(dx*dx + dy*dy)."""
    dx, dy = a[0] - b[0], a[1] - b[1]
    return math.sqrt(dx * dx + dy * dy)


def _path_length(points):
    return sum(_dist(a, b) for a, b in zip(points, points[1:]))


def _xy(quantity, what="position"):
    x, y = st.point_of(quantity, what)
    return (x, y)


def validate_outlines(panel_groups):
    """The panel-group outlines as polygons [[(x, y), ...], ...] in GetPanelGroupData order."""
    if not isinstance(panel_groups, list):
        raise InverterCablingError("panel groups must be a list of {handle, outlines}")
    out, total = [], 0
    for group in panel_groups:
        if not isinstance(group, dict):
            raise InverterCablingError("a panel group is {handle, outlines}")
        for poly in group.get("outlines") or []:
            if not isinstance(poly, list):
                raise InverterCablingError("an outline is a list of points")
            total += len(poly)
            if total > MAX_OUTLINE_VERTICES_TOTAL:
                raise InverterCablingError(f"outlines hold more than {MAX_OUTLINE_VERTICES_TOTAL} vertices")
            try:
                out.append([dev._finite_xy(p, "outline vertex") for p in poly])
            except dev.InverterDeviceError as exc:
                raise InverterCablingError(str(exc)) from None
    return out


# ----------------------------------------------------------------- devices --

def _feeder_ends(row):
    """(L1, L2) numbers of a feeder row: its stored from/to, else its circuit."""
    source, target = row.get("from"), row.get("to")
    if type(source) is int and type(target) is int:
        return source, target
    detail = row.get("_detail") if isinstance(row.get("_detail"), dict) else {}
    match = _FEEDER_CIRCUIT.fullmatch(str(detail.get("circuit") or "").strip())
    return (int(match.group("source")), int(match.group("target"))) if match else (None, None)


def levels(state):
    """(L1 list, L2 list) of {row, number, position}, each ordered by number (then position): the
    registration lists App.gInverterList and App.gL2CollectorList (DocumentEventHandler.cs:310-318).
    The number is the device's `_number`, else the feeder circuit that starts (L1) or ends (L2) at its
    insertion point, else None."""
    starts, ends = {}, {}
    for row in state["rows"]["cable"]:
        if row.get("cable_kind") != "feeder" or len(row.get("vertices") or []) < 2:
            continue
        source, target = _feeder_ends(row)
        if source is not None:
            starts.setdefault(_xy(row["vertices"][0], "feeder vertex"), source)
        if target is not None:
            ends.setdefault(_xy(row["vertices"][-1], "feeder vertex"), target)
    devices = state["rows"]["device"]
    if len(devices) > MAX_DEVICES:
        raise InverterCablingError(f"more than {MAX_DEVICES} devices")
    l1, l2 = [], []
    for row in devices:
        position = _xy(row["position"])
        is_l2 = dev._is_l2(row)
        number = row.get("_number") if type(row.get("_number")) is int else None
        if number is None:
            number = (ends if is_l2 else starts).get(position)
        (l2 if is_l2 else l1).append({"row": row, "number": number, "position": position})

    def key(item):
        return (item["number"] is None, item["number"] or 0, st.order_key(*item["position"]))
    return sorted(l1, key=key), sorted(l2, key=key)


def _numbered(items, level):
    if any(item["number"] is None for item in items):
        raise InverterCablingNotPortedError(f"an {level} device whose number the state does not record")
    return items


def _find_device(state, level, number):
    l1, l2 = levels(state)
    for item in (l1 if level == "L1" else l2):
        if item["number"] == number:
            return item
    raise InverterCablingError(f"no {level} device numbered {number} in the state")


def _feet(value):
    return {"kind": "length", "value": float(value), "unit": "ft"}


def _feeder_row(state, l1, l2, points, length_ft):
    return {"cable_kind": "feeder", "from": l1, "to": l2,
            "vertices": [st.coordinate(x, y) for x, y in points], "length": _feet(length_ft),
            "_pair": st.new_pair(state, "cable"),
            "_detail": {"circuit": f"F{l1}/{l2}", "gauge": "", "closed": False}}


def _homerun_to(circuit):
    match = _HOMERUN_CIRCUIT.fullmatch(str(circuit or "").strip())
    return int(match.group("device")) if match else None


def _homerun_row(state, string_id, segment, circuit, start, end, length_ft):
    return {"cable_kind": "dc-homerun", "segment": segment, "from": string_id, "to": _homerun_to(circuit),
            "vertices": [st.coordinate(*start), st.coordinate(*end)], "length": _feet(length_ft),
            "_pair": st.new_pair(state, "cable"),
            "_detail": {"circuit": circuit, "gauge": "", "closed": False}}


# --------------------------------------------------------- route-l2-feeders --

def filter_phantom_l2(l1, l2):
    """FilterPhantomL2Collectors (OptiHomerunCmd.cs:1005-1040): (kept L2 list, dropped numbers)."""
    if not l1 or len(l2) < 4:
        return l2, []
    near = [min(_dist(i["position"], c["position"]) for c in l1) for i in l2]
    ordered = sorted(near)
    threshold = max(ordered[len(ordered) // 2] * PHANTOM_MEDIAN_FACTOR, 1.0)
    phantoms = set()
    for index, item in enumerate(l2):
        if near[index] <= threshold:
            continue
        if any(j != index and _dist(item["position"], other["position"]) < PHANTOM_STACK_RADIUS
               for j, other in enumerate(l2)):
            phantoms.add(item["number"])
    return [item for item in l2 if item["number"] not in phantoms], sorted(phantoms)


def assign_l1_to_l2(l1, l2, user_cap):
    """RouteL2Feeders' balanced nearest assignment with the no-pass rule (OptiHomerunCmd.cs:1061-1135),
    then OptimizeFeederAssignmentBySwaps (:909-988). Returns (assignments in insertion order, new)."""
    balanced = (len(l1) + len(l2) - 1) // len(l2) if l2 else len(l1)
    max_per_l2 = user_cap if 0 < user_cap < balanced else balanced

    def nearest(item):
        best = math.inf
        for inverter in l2:
            d = _dist(item["position"], inverter["position"])
            if d < best:
                best = d
        return best
    ordered = sorted(l1, key=nearest)
    assignments, load, new = {}, {item["number"]: 0 for item in l2}, 0
    for item in ordered:
        best_cost, best = math.inf, None
        for inverter in l2:
            if load[inverter["number"]] >= max_per_l2:
                continue
            cost = _dist(item["position"], inverter["position"])
            for other in l2:
                if other["number"] == inverter["number"]:
                    continue
                through = _dist(item["position"], other["position"]) + \
                    _dist(other["position"], inverter["position"])
                if through <= cost * NO_PASS_RATIO:
                    cost *= NO_PASS_PENALTY
                    break
            if cost < best_cost:
                best_cost, best = cost, inverter
        if best is None:
            least = math.inf
            for inverter in l2:
                if load[inverter["number"]] < least:
                    least, best = load[inverter["number"]], inverter
        if best is not None:
            assignments[item["number"]] = best["number"]
            load[best["number"]] += 1
            new += 1
    _swap_optimize_feeders(assignments, l1, l2)
    return assignments, new


def _swap_optimize_feeders(assignments, l1, l2):
    """OptimizeFeederAssignmentBySwaps (OptiHomerunCmd.cs:909-988), in place."""
    if len(assignments) < 2:
        return
    l2_numbers, l2_pos, l2_index = [], [], {}
    for inverter in l2:
        if inverter["number"] in l2_index:
            continue
        l2_index[inverter["number"]] = len(l2_numbers)
        l2_numbers.append(inverter["number"])
        l2_pos.append(inverter["position"])
    if len(l2_numbers) < 2:
        return
    cb_pos = {item["number"]: item["position"] for item in l1}
    cbs = list(assignments)
    dist = [[_dist(cb_pos.get(n, (0.0, 0.0)), p) for p in l2_pos] for n in cbs]
    assign = [l2_index.get(assignments[n], 0) for n in cbs]
    passes, improved = 0, True
    while improved and passes < MAX_SWAP_PASSES:
        improved = False
        passes += 1
        for i in range(len(cbs)):
            a = assign[i]
            for k in range(i + 1, len(cbs)):
                b = assign[k]
                if b == a:
                    continue
                delta = (dist[i][b] + dist[k][a]) - (dist[i][a] + dist[k][b])
                if delta < -SWAP_EPSILON:
                    assign[i], assign[k] = b, a
                    a = b
                    improved = True
    for i, number in enumerate(cbs):
        assignments[number] = l2_numbers[assign[i]]


def derive_vertical_lanes(outlines):
    """DeriveVerticalLaneXs (OptiHomerunCmd.cs:1773-1820) over the panel-group outlines."""
    spans = []
    for poly in outlines:
        if len(poly) < 3:
            continue
        xs = [p[0] for p in poly]
        if max(xs) > min(xs):
            spans.append([min(xs), max(xs)])
    spans.sort(key=lambda span: span[0])
    bands = []
    for span in spans:
        if not bands or span[0] > bands[-1][1]:
            bands.append([span[0], span[1]])
        elif span[1] > bands[-1][1]:
            bands[-1][1] = span[1]
    lanes = [(bands[i - 1][1] + bands[i][0]) / 2.0 for i in range(1, len(bands))]
    if bands:
        width = bands[0][1] - bands[0][0]
        edge = (width if width > 0 else DEGENERATE_BAND_WIDTH) * EDGE_LANE_FRACTION
        lanes.insert(0, bands[0][0] - edge)
        lanes.append(bands[-1][1] + edge)
    return lanes


def nearest_lane_x(lanes, combiner_x, inverter_x):
    """NearestLaneX (OptiHomerunCmd.cs:1822-1834, LightweightCablingEngine.cs:260-270)."""
    if not lanes:
        return inverter_x
    best, best_d = lanes[0], abs(lanes[0] - combiner_x)
    for lane in lanes[1:]:
        d = abs(lane - combiner_x)
        if d < best_d:
            best, best_d = lane, d
    return best


def dedupe_path(raw):
    """DedupePathPoints (OptiHomerunCmd.cs:1836-1852): drop repeats, then collinear middles."""
    points = []
    for p in raw:
        if not points or _dist(points[-1], p) > PATH_EPSILON:
            points.append(p)
    i = 1
    while i < len(points) - 1:
        a, b, c = points[i - 1], points[i], points[i + 1]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) < PATH_EPSILON:
            del points[i]
        else:
            i += 1
    return points


def comb_path(cb, inv, lanes):
    bus = nearest_lane_x(lanes, cb[0], inv[0])
    return dedupe_path([cb, (bus, cb[1]), (bus, inv[1]), inv])


def _route_l2_feeders(new, outlines, host, lines):
    """RouteL2Feeders on `new`, in place (OptiHomerunCmd.cs:1042-1189)."""
    lines.append("--- L2 Feeder Routing ---")
    l1, l2 = levels(new)
    l1, l2 = _numbered(l1, "L1"), _numbered(l2, "L2")
    l2, phantoms = filter_phantom_l2(l1, l2)
    if phantoms:
        lines.append(f"  Phantom-L2 guard: excluded {len(phantoms)} stacked far-from-array block(s) from "
                     f"feeder routing (INV {','.join(str(n) for n in phantoms)}).")
    user_cap = host.get("L1CollectorsPerL2")
    if type(user_cap) is not int:
        raise InverterCablingError("host input L1CollectorsPerL2 is required")
    if not l1:
        lines.append("No L1-to-L2 assignments could be made.")
        return
    assignments, created = assign_l1_to_l2(l1, l2, user_cap)
    if created > 0:
        new["setting"]["L1ToL2Assignments"] = {str(k): v for k, v in assignments.items()}
        st.save_drawing_properties(new)
        lines.append(f"{created} new combiner box assignment(s) saved.")
    if not assignments:
        lines.append("No L1-to-L2 assignments could be made.")
        return
    adopted = set()
    for row in new["rows"]["cable"]:
        if row.get("cable_kind") == "feeder":
            detail = row.get("_detail") if isinstance(row.get("_detail"), dict) else {}
            circuit = detail.get("circuit")
            if not isinstance(circuit, str) or not circuit.strip():
                source, target = _feeder_ends(row)
                circuit = f"F{source}/{target}" if source is not None and target is not None else ""
            if circuit.strip():
                adopted.add(circuit.upper())
    lanes = derive_vertical_lanes(outlines)
    if not lanes:
        raise InverterCablingNotPortedError("the legacy TrunkRouter feeder path (no vertical land lane)")
    lines.append(f"  Nearest-lane comb feeder routing: {len(lanes)} vertical land lane(s) for "
                 f"{len(assignments)} combiner assignment(s).")
    by_number_1 = {item["number"]: item for item in l1}
    by_number_2 = {item["number"]: item for item in l2}
    count = 0
    for l1_number, l2_number in assignments.items():
        if f"F{l1_number}/{l2_number}".upper() in adopted:
            continue
        cb, inv = by_number_1.get(l1_number), by_number_2.get(l2_number)
        if cb is None or inv is None:
            continue
        points = comb_path(cb["position"], inv["position"], lanes)
        if len(points) < 2:
            continue
        new["rows"]["cable"].append(_feeder_row(new, l1_number, l2_number, points,
                                                _path_length(points) / INCHES_PER_FOOT))
        count += 1
    lines.append(f"{count} nearest-lane comb feeder(s) drawn.")


def route_l2_feeders(state, panel_groups, host):
    """RouteL2Feeders (i11) on the Studio state. Returns (new state, printed lines)."""
    host = _host(host)
    outlines = validate_outlines(panel_groups)
    new = copy.deepcopy(state)
    lines = []
    _route_l2_feeders(new, outlines, host, lines)
    st.sort_rows(new)
    return new, lines


# ----------------------------------------------------------------- homeruns --

def _strings(state):
    """The String-layer strings with their geometry and circuit, in the state's row order."""
    geometry = {g.get("string"): g for g in state["geometry"]["strings"] if isinstance(g, dict)}
    result = []
    for row in state["rows"]["string-assignment"]:
        detail = row.get("_detail") if isinstance(row.get("_detail"), dict) else {}
        g = geometry.get(row["string"], {})
        start, end = g.get("start"), g.get("end")
        result.append({"string": row["string"], "circuit": detail.get("circuit"),
                       "start": dev._finite_xy(start, "string start") if start else None,
                       "end": dev._finite_xy(end, "string end") if end else None})
    return result


def homeruns_auto(state, panel_groups, host):
    """HomerunsAuto (i12) on the Studio state: every homerun redrawn straight from each string endpoint
    to the string's combiner, then RouteL2Feeders. Returns (new state, printed lines)."""
    host = _host(host)
    outlines = validate_outlines(panel_groups)
    new = copy.deepcopy(state)
    lines = []
    l1, l2 = levels(new)
    if not l1:
        lines.append("No inverters found in drawing. Place inverters first.")
        return new, lines
    strings = [s for s in _strings(new)]
    if not strings:
        lines.append("No strings selected.")
        return new, lines
    lines.append(f"{len(strings)} strings selected.")
    combiners = {item["position"]: item for item in l1}
    # EraseExistingHomeruns (OptiHomerunCmd.cs:3884): the association and stored lengths they carry.
    previous, kept = {}, []
    for row in new["rows"]["cable"]:
        if row.get("cable_kind") == "dc-homerun":
            previous.setdefault(row.get("from"), []).append(row)
        else:
            kept.append(row)
    new["rows"]["cable"] = kept
    drawn = 0
    for item in strings:
        if item["start"] is None and item["end"] is None:
            continue
        target = None
        for old in previous.get(item["string"], []):
            end = _xy(old["vertices"][-1], "homerun vertex")
            if end in combiners:
                target = combiners[end]
                break
        if target is None:
            anchor = item["start"] if item["end"] is None else item["end"] if item["start"] is None else \
                ((item["start"][0] + item["end"][0]) / 2.0, (item["start"][1] + item["end"][1]) / 2.0)
            best = math.inf
            for combiner in l1:
                d = _dist(anchor, combiner["position"])
                if d < best:
                    best, target = d, combiner
        for segment, leg in (("start", item["start"]), ("end", item["end"])):
            if leg is None:
                continue
            length = _dist(leg, target["position"]) / INCHES_PER_FOOT
            for old in previous.get(item["string"], []):
                if old.get("segment") == segment and \
                        [_xy(v, "homerun vertex") for v in old["vertices"]] == [leg, target["position"]]:
                    length = old["length"]["value"]
                    break
            new["rows"]["cable"].append(_homerun_row(new, item["string"], segment, item["circuit"], leg,
                                                     target["position"], length))
            drawn += 1
    lines.append(f"HomerunsAuto: drew {drawn} straight-line DC homerun(s).")
    if _host_bool(host, "UseL2Collectors") and l1 and l2:
        _route_l2_feeders(new, outlines, host, lines)
    st.sort_rows(new)
    return new, lines


# ------------------------------------------------------- lite cabling feeders --

def assign_feeders_lite(cbs, invs, cap=LITE_CAP_PER_INVERTER, tail_bias=LITE_TAIL_BIAS):
    """CablingSolver.AssignFeeders (CablingSolver.cs:53-88) with weight 1 per combiner and no weight
    cap, then SwapOptimize (:92-146). `cbs`, `invs`: [(number, (x, y))]. Returns {cb: inv} in
    assignment order."""
    if not cbs or not invs:
        return {}
    max_per = cap if cap > 0 else (len(cbs) + len(invs) - 1) // len(invs)
    load = {n: 0 for n, _ in invs}

    def nearest(c):
        return min(_dist(c[1], i[1]) for i in invs)
    # OrderBy / OrderByDescending are stable sorts (equal keys keep the list order).
    order = sorted(cbs, key=lambda c: -nearest(c)) if tail_bias else sorted(cbs, key=nearest)
    assign = {}
    for number, pos in order:
        best, best_d = None, math.inf
        for inv_number, inv_pos in invs:
            if load[inv_number] < max_per:
                d = _dist(pos, inv_pos)
                if d < best_d:
                    best_d, best = d, inv_number
        if best is None:
            best = min(invs, key=lambda i: load[i[0]])[0]
        assign[number] = best
        load[best] += 1
    _swap_optimize_lite(cbs, invs, assign, tail_bias)
    return assign


def _swap_optimize_lite(cbs, invs, assign, tail_bias):
    """CablingSolver.SwapOptimize (CablingSolver.cs:92-146), in place, weight cap 0."""
    n, m = len(cbs), len(invs)
    if n < 2 or m < 2:
        return
    index = {number: j for j, (number, _) in enumerate(invs)}
    dist = [[_dist(cbs[i][1], invs[j][1]) for j in range(m)] for i in range(n)]
    a = [index[assign[cbs[i][0]]] for i in range(n)]
    passes, improved = 0, True
    while improved and passes < MAX_SWAP_PASSES:
        improved = False
        passes += 1
        for i in range(n):
            ai = a[i]
            for k in range(i + 1, n):
                bk = a[k]
                if bk == ai:
                    continue
                if tail_bias:
                    delta = (dist[i][bk] * dist[i][bk] + dist[k][ai] * dist[k][ai]) - \
                            (dist[i][ai] * dist[i][ai] + dist[k][bk] * dist[k][bk])
                else:
                    delta = (dist[i][bk] + dist[k][ai]) - (dist[i][ai] + dist[k][bk])
                if delta >= -SWAP_EPSILON:
                    continue
                a[i], a[k] = bk, ai
                ai = bk
                improved = True
    for i in range(n):
        assign[cbs[i][0]] = invs[a[i]][0]


def lite_feeders(state, host):
    """LEAFLITEFEEDERS (i13) on the Studio state: the feeders reassigned and redrawn as comb paths.
    Returns (new state, printed lines)."""
    host = _host(host)
    racks = host.get("RackExtents")
    if not isinstance(racks, list):
        raise InverterCablingError("host input RackExtents (the drawing's rack extents) is required")
    if racks:
        raise InverterCablingNotPortedError("LaneDeriver over rack extents")
    new = copy.deepcopy(state)
    l1, l2 = levels(new)
    if not l1 or not l2:
        return new, ["LEAFLITEFEEDERS: need placed combiners (L1) and inverters (L2). Run LEAFLITEPLACE first."]
    l1, l2 = _numbered(l1, "L1"), _numbered(l2, "L2")
    cbs = [(item["number"], item["position"]) for item in l1]
    invs = [(item["number"], item["position"]) for item in l2]
    assign = assign_feeders_lite(cbs, invs)
    lanes = []
    cb_pos, inv_pos = dict(cbs), dict(invs)
    erased = sum(1 for row in new["rows"]["cable"] if row.get("cable_kind") == "feeder")
    new["rows"]["cable"] = [row for row in new["rows"]["cable"] if row.get("cable_kind") != "feeder"]
    drawn = 0
    for cb, inv in assign.items():
        points = [cb_pos[cb], inv_pos[inv]] if LITE_DIRECT else comb_path(cb_pos[cb], inv_pos[inv], lanes)
        if len(points) < 2:
            continue
        new["rows"]["cable"].append(_feeder_row(new, cb, inv, points, 0.0))
        drawn += 1
    st.sort_rows(new)
    return new, [f"LEAFLITEFEEDERS: {drawn} orthogonal comb feeder(s) drawn over {len(lanes)} lanes"
                 + (f" (replaced {erased})." if erased > 0 else ".")]


# ------------------------------------------------ inverter move and position --

def _move_and_reroute(new, target, x, y):
    """Move the device to (x, y) and reroute every homerun ending at its old insertion point as one
    straight leg from the same string endpoint (the ported direct router). Returns rerouted count."""
    old = target["position"]
    rerouted = 0
    cables = []
    for row in new["rows"]["cable"]:
        if row.get("cable_kind") == "dc-homerun" and _xy(row["vertices"][-1], "homerun vertex") == old:
            leg = _xy(row["vertices"][0], "homerun vertex")
            detail = row.get("_detail") if isinstance(row.get("_detail"), dict) else {}
            fresh = _homerun_row(new, row["from"], row.get("segment"), detail.get("circuit"), leg, (x, y),
                                 _dist(leg, (x, y)) / INCHES_PER_FOOT)
            fresh["to"] = row.get("to")
            cables.append(fresh)
            rerouted += 1
        else:
            cables.append(row)
    new["rows"]["cable"] = cables
    target["row"]["position"] = st.coordinate(x, y)
    return rerouted


def _device_homerun_legs(state, position):
    return [_xy(row["vertices"][0], "homerun vertex") for row in state["rows"]["cable"]
            if row.get("cable_kind") == "dc-homerun" and _xy(row["vertices"][-1], "homerun vertex") == position]


def inverter_move(state, host, answers):
    """MOVEINV (i17, declared) on the Studio state. `answers`: [picked entity, acquired point] (G22,
    G35b: the point the jig acquired, taken as is); the picked entity is resolved through host
    MovedDevice (the handle names an entity the state does not carry). Returns (new state, printed lines)."""
    host = _host(host)
    if not isinstance(answers, (list, tuple)) or len(answers) != 2 or not all(isinstance(a, str) for a in answers):
        raise InverterCablingError("MOVEINV takes two answers: the inverter and the new point")
    level, number = _host_device(host, "MovedDevice")
    try:
        x, y = dev.parse_point(answers[1])
    except dev.InverterDeviceError as exc:
        raise InverterCablingError(str(exc)) from None
    new = copy.deepcopy(state)
    target = _find_device(new, level, number)
    if not _device_homerun_legs(new, target["position"]):
        return new, ["The selected inverter has no homeruns to move."]
    rerouted = _move_and_reroute(new, target, x, y)
    st.save_drawing_properties(new)
    st.sort_rows(new)
    return new, [f"Inverter {number} moved; {rerouted} homerun(s) rerouted."]


def _near_outline(x, y, outlines, buffer):
    for poly in outlines:
        if dev.point_is_inside(x, y, poly):
            return True
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[i + 1 if i + 1 < n else 0]
            dx, dy = b[0] - a[0], b[1] - a[1]
            length2 = dx * dx + dy * dy
            t = 0.0 if length2 <= 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / length2))
            if _dist((x, y), (a[0] + t * dx, a[1] + t * dy)) < buffer:
                return True
    return False


def _plan_count(extent, step):
    """Math.Max(1, Math.Ceiling(extent / step)) in doubles; an overflowed quotient stays infinite."""
    quotient = extent / step
    return max(1.0, quotient if math.isinf(quotient) else float(math.ceil(quotient)))


def position_plan(min_x, min_y, max_x, max_y, step=POSITION_STEP, cap=POSITION_MAX_CANDIDATES):
    """InverterMoveSearch.Plan (StringHomeRunCmd.cs:35-68): the step doubles (capped at the longer side)
    until columns x rows <= cap, then the row-major points (min_x + column * step, min_y + row * step) kept
    only inside the half-open extents; none for a zero width or height. At most cap points, O(cap).
    Fails closed with InverterCablingError where the plugin throws (:38-46)."""
    if type(step) not in (int, float) or not math.isfinite(step) or step <= 0:
        raise InverterCablingError("the position search step must be a finite positive number")
    if type(cap) is not int or cap < 1:
        raise InverterCablingError("the position search cap must be a positive integer")
    bounds = (min_x, min_y, max_x, max_y)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds):
        raise InverterCablingError("the position search extents must be finite and ordered")
    min_x, min_y, max_x, max_y = (float(v) for v in bounds)
    width, height = max_x - min_x, max_y - min_y
    if not math.isfinite(width) or not math.isfinite(height) or width < 0 or height < 0:
        raise InverterCablingError("the position search extents must be finite and ordered")
    points = []
    if width == 0 or height == 0:
        return points
    step = float(step)
    columns, rows = _plan_count(width, step), _plan_count(height, step)
    while columns * rows > cap:
        step = min(step * 2, max(width, height))
        columns, rows = _plan_count(width, step), _plan_count(height, step)
    for row in range(int(rows)):
        for column in range(int(columns)):
            x, y = min_x + column * step, min_y + row * step
            if x < max_x and y < max_y:
                points.append((x, y))
    return points


def _position_extents(state, position, legs):
    """The plan extents of StringHomeRunCmd.cs:804-826: the extents of the device's strings (those whose
    homeruns end at it, the state's record of mInverterNumber), each string's polyline vertices and its
    homerun legs, grown POSITION_EXTENTS_MARGIN each side. Returns (min_x, min_y, max_x, max_y)."""
    names = {row.get("from") for row in state["rows"]["cable"]
             if row.get("cable_kind") == "dc-homerun" and _xy(row["vertices"][-1], "homerun vertex") == position}
    points = list(legs)
    for g in state["geometry"]["strings"]:
        if isinstance(g, dict) and g.get("string") in names:
            try:
                points += [dev._finite_xy(v, "string vertex") for v in g.get("vertices") or []]
            except dev.InverterDeviceError as exc:
                raise InverterCablingError(str(exc)) from None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs) - POSITION_EXTENTS_MARGIN, min(ys) - POSITION_EXTENTS_MARGIN,
            max(xs) + POSITION_EXTENTS_MARGIN, max(ys) + POSITION_EXTENTS_MARGIN)


def optimum_position(legs, extents, outlines, budget_s=POSITION_TIME_BUDGET_S, clock=time.monotonic):
    """The plugin's optimum search (StringHomeRunCmd.cs:828-882) over position_plan(*extents): each
    planned point in row-major order, outside every outline and its OUTLINE_BUFFER band (:853-863),
    scored and kept only when strictly shorter than the best so far (:872, first of equals wins; the
    score is the rerouted straight-leg total, identical for both axis toggles of :865). Returns
    (point, score), or (None, None) when no planned point survives (:911-913). The time budget is a
    Studio safety bound: past it the search fails closed; within it the answer is the plan's."""
    deadline = clock() + budget_s
    best, best_cost = None, None
    for p in position_plan(*extents):
        if clock() > deadline:
            raise InverterCablingError(f"the position search exceeded {budget_s} s")
        value = sum(_dist(p, leg) for leg in legs)
        # The outline test only for a would-be winner: the same accepted sequence, fewer polygon walks.
        if (best_cost is None or value < best_cost) and not _near_outline(p[0], p[1], outlines, OUTLINE_BUFFER):
            best, best_cost = p, value
    return best, best_cost


def inverter_position(state, panel_groups, host):
    """POSITIONINV (declared) on the Studio state: the device named by host PositionDevice moved to the
    plugin plan's optimum and its homeruns rerouted. Returns (new state, printed lines)."""
    host = _host(host)
    outlines = validate_outlines(panel_groups)
    level, number = _host_device(host, "PositionDevice")
    new = copy.deepcopy(state)
    target = _find_device(new, level, number)
    legs = _device_homerun_legs(new, target["position"])
    if not legs:
        return new, ["The selected inverter has no homeruns to position."]
    before = sum(_dist(target["position"], leg) for leg in legs)
    best, after = optimum_position(legs, _position_extents(new, target["position"], legs), outlines)
    if best is None:
        # StringHomeRunCmd.cs:911-913: nothing moves.
        return new, [f"Failed to find optimum position for Inverter: {number}"]
    x, y = best
    if (x, y) == target["position"]:
        return new, [f"Inverter {number} is already at its optimum position."]
    rerouted = _move_and_reroute(new, target, x, y)
    st.save_drawing_properties(new)
    st.sort_rows(new)
    return new, [f"Inverter {number} positioned; {rerouted} homerun(s) rerouted; total homerun length "
                 f"{before / INCHES_PER_FOOT:.1f} ft -> {after / INCHES_PER_FOOT:.1f} ft."]


# ------------------------------------------------------ combiner-auto-place --

def seed_input_assignments(l1_to_l2, l2_num_mppt):
    """MpptBalanceAnalyzer.SeedDefaultL1ToL2InputAssignments (MpptBalanceAnalyzer.cs:854-877): the L1s of
    each L2, in number order, take MPPT slots 0, 1, ... modulo the L2's MPPT count."""
    result = {}
    if not l1_to_l2 or l2_num_mppt <= 0:
        return result
    by_l2 = {}
    for l1, l2 in l1_to_l2.items():
        by_l2.setdefault(l2, []).append(l1)
    for l1s in by_l2.values():
        for index, l1 in enumerate(sorted(l1s)):
            result[l1] = index % l2_num_mppt
    return result


def _int_map(value, name):
    """A stored {number: number} setting as ints (absent or empty: {}); fails closed on anything else."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InverterCablingError(f"setting {name} must be an object of numbers")
    out = {}
    for key, item in value.items():
        if not (isinstance(key, str) and re.fullmatch(r"-?[0-9]{1,9}", key)) or type(item) is not int:
            raise InverterCablingError(f"setting {name} must map numbers to numbers")
        out[int(key)] = item
    return out


def _stored_map(mapping):
    return {str(key): mapping[key] for key in sorted(mapping)}


class _PointIndex:
    """Points bucketed on a unit grid: a MATCH_EPSILON lookup reads at most 9 cells (no N x M scan)."""

    def __init__(self):
        self.cells = {}

    @staticmethod
    def _cell(p):
        return (math.floor(p[0]), math.floor(p[1]))

    def add(self, p, item):
        self.cells.setdefault(self._cell(p), []).append((p, item))

    def near(self, p):
        cx, cy = self._cell(p)
        return [item for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for q, item in self.cells.get((cx + dx, cy + dy), ()) if _dist(p, q) <= MATCH_EPSILON]


def _intake_context(intake):
    context = intake.get("commandContext") if isinstance(intake, dict) else None
    if not isinstance(context, dict):
        raise InverterCablingError("the combiner intake carries no commandContext")
    values = {}
    for key in ("l2NumMppt", "combinerBoxConnections"):
        value = context.get(key, 0)
        if type(value) is not int or value < 0:
            raise InverterCablingError(f"the combiner intake's commandContext.{key} must be a non-negative integer")
        values[key] = value
    return values


def _number_l2_from_intake(new, intake):
    """The L2 NUMBER attributes (App.gL2CollectorList, DocumentEventHandler.cs:310-318) as the intake's
    l2Inverters record them, set on the state's L2 devices matched by insertion point. Every intake L2
    must match exactly one state L2 and every state L2 one intake L2, or the intake is not this state's."""
    inputs = intake.get("inputs") if isinstance(intake, dict) else None
    raw = inputs.get("l2Inverters") if isinstance(inputs, dict) else None
    if not isinstance(raw, list) or len(raw) > MAX_DEVICES:
        raise InverterCablingError("the combiner intake carries no l2Inverters list")
    index = _PointIndex()
    l2_rows = [row for row in new["rows"]["device"] if dev._is_l2(row)]
    for row in l2_rows:
        index.add(_xy(row["position"]), row)
    numbered = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InverterCablingError(f"l2Inverters[{i}] must be an object")
        try:
            number = comb._int(item.get("Number"), f"l2Inverters[{i}].Number")
            point = comb._point(item.get("InsertPt"), f"l2Inverters[{i}].InsertPt", upper=True)
        except comb.PlacementError as exc:
            raise InverterCablingError(str(exc)) from None
        hits = index.near(point)
        if len(hits) != 1 or id(hits[0]) in numbered:
            raise InverterCablingError(f"the intake's L2 {number} matches no single L2 device of the state")
        numbered.add(id(hits[0]))
        hits[0]["_number"] = number
    if len(numbered) != len(l2_rows):
        raise InverterCablingError("the state holds an L2 device the combiner intake does not record")


def _string_ids(new, intake):
    """{pre-built string id: String-layer string handle}: each intake string (CombinerAutoCmd.cs:3867-3931)
    matched to the one state string whose first and last polyline vertex are its EndpointA and EndpointB."""
    try:
        strings = comb.build_strings(intake)
    except comb.PlacementError as exc:
        raise InverterCablingError(str(exc)) from None
    index = _PointIndex()
    for g in new["geometry"]["strings"]:
        vertices = g.get("vertices") if isinstance(g, dict) else None
        if not isinstance(vertices, list) or not vertices or g.get("string") is None:
            continue
        first = dev._finite_xy(vertices[0], "string vertex")
        last = dev._finite_xy(vertices[-1], "string vertex")
        index.add(first, (g["string"], last))
    ids = {}
    for s in strings:
        hits = [handle for handle, last in index.near(s.endpoint_a) if _dist(last, s.endpoint_b) <= MATCH_EPSILON]
        if len(hits) != 1:
            raise InverterCablingError(f"the intake's string {s.string_number} matches no single string of the state")
        if s.string_number in ids:
            raise InverterCablingError(f"the intake repeats the string id {s.string_number}")
        ids[s.string_number] = hits[0]
    return ids


def _draw_direct_homeruns(new, association, host, lines):
    """HomerunsAuto's automatic mode (OptiHomerunCmd.Run(true, true), :121-192) on `new`, in place: every
    String-layer string, the existing homeruns erased (:3884), one straight leg per endpoint marker to the
    string's combiner (DrawUtilityScaleDirectHomeruns, :2034-2134). Returns the legs drawn."""
    l1, _ = levels(new)
    by_number = {}
    for item in l1:                                    # FindAllInverterBlocks (:1951-1968)
        by_number.setdefault(item["number"], []).append(item)
    if not by_number:
        lines.append("No inverters found in drawing. Place inverters first.")
        return 0
    strings = _strings(new)
    if not strings:
        lines.append("No strings selected.")
        return 0
    lines.append(f"{len(strings)} strings selected.")
    new["rows"]["cable"] = [row for row in new["rows"]["cable"] if row.get("cable_kind") != "dc-homerun"]
    use_l2 = _host_bool(host, "UseL2Collectors")
    drawn = fallback = 0
    for item in strings:
        number = association.get(item["string"])
        if number is None:
            number = _homerun_to(item["circuit"])      # Cable.cs:36-41: the circuit's number
        target = None
        candidates = by_number.get(number)
        if candidates:                                 # FindClosestInverter (:3506-3551)
            target = candidates[0]
            if len(candidates) > 1 and item["start"] is not None:
                best = _dist(item["start"], target["position"])
                for candidate in candidates:
                    d = _dist(item["start"], candidate["position"])
                    if d < best:
                        best, target = d, candidate
        elif use_l2:                                   # FindNearestRegisteredL1Combiner (:3553-3595)
            anchor = item["start"] if item["end"] is None else item["end"] if item["start"] is None else \
                ((item["start"][0] + item["end"][0]) / 2.0, (item["start"][1] + item["end"][1]) / 2.0)
            if anchor is not None:
                best = math.inf
                for candidate in l1:
                    d = _dist(anchor, candidate["position"])
                    if d < best:
                        best, target = d, candidate
                if target is not None:
                    fallback += 1
        if target is None or (item["start"] is None and item["end"] is None):
            continue
        for segment, leg in (("start", item["start"]), ("end", item["end"])):
            if leg is None:
                continue
            new["rows"]["cable"].append(_homerun_row(new, item["string"], segment, item["circuit"], leg,
                                                     target["position"],
                                                     _dist(leg, target["position"]) / INCHES_PER_FOOT))
            drawn += 1
    lines.append(f"HomerunsAuto: drew {drawn} straight-line DC homerun(s).")
    if fallback:
        lines.append(f"HomerunsAuto: assigned {fallback} string(s) to nearest L1 combiner because their circuit "
                     f"tag did not match a registered combiner number.")
    return drawn


def combiner_auto_place(state, panel_groups, host, form_values, intake):
    """LEAFCOMBINERAUTO (i5) on the Studio state: the placement solution of `intake` (the command's
    input-before-placement dump, G35c; place() is the port of CombinerPlacementEngine.Place), its L1
    blocks inserted, the L1/L2 and string/L1 associations persisted, then HomerunsAuto's automatic mode
    (direct homeruns, then RouteL2Feeders). Returns (new state, printed lines)."""
    host = _host(host)
    outlines = validate_outlines(panel_groups)
    if not isinstance(form_values, dict) or form_values != {"combiner_input_plan": INPUT_PLAN_APPLY}:
        raise InverterCablingError('LEAFCOMBINERAUTO takes the form value {"combiner_input_plan": "Apply"}')
    if not isinstance(intake, dict):
        raise InverterCablingError("LEAFCOMBINERAUTO needs its combiner intake")
    if not _host_bool(host, "UseL2Collectors"):
        raise InverterCablingNotPortedError("LEAFCOMBINERAUTO outside L1/L2 mode (legacy single-level blocks)")
    context = _intake_context(intake)
    inputs = intake.get("inputs")
    if not isinstance(inputs, dict):
        raise InverterCablingError("the combiner intake carries no inputs")
    new = copy.deepcopy(state)
    if any(not dev._is_l2(row) for row in new["rows"]["device"]) or inputs.get("existingL1s"):
        raise InverterCablingNotPortedError("LEAFCOMBINERAUTO over existing L1 combiners")
    installation = new["setting"].get("InstallationDesign", st.DECLARED_DEFAULTS["InstallationDesign"])
    try:
        solution = comb.place(intake, installation)
    except comb.PlacementError as exc:
        if "not ported" in str(exc):
            raise InverterCablingNotPortedError(str(exc)) from None
        raise InverterCablingError(str(exc)) from None
    lines = []
    placed = solution["combiners"]
    if not placed:                                     # CombinerAutoCmd.cs:493-503
        lines.append("LEAFCOMBINERAUTO: No combiners produced. See warnings above.")
        return new, lines
    _number_l2_from_intake(new, intake)
    string_ids = _string_ids(new, intake)
    try:
        scale = dev._symbol_scale(host, False)
    except dev.InverterDeviceError as exc:
        raise InverterCablingError(str(exc)) from None

    # 7. The L1 blocks (CombinerAutoCmd.cs:524-569, StringHomeRunCmd.cs:1363-1586).
    for pc in placed:
        location = pc["location"]
        new["rows"]["device"].append(dev._device_row(
            new, is_l2=False, x=float(location["x"]), y=float(location["y"]), scale=scale, placement=None,
            number=pc["L1Number"], box_inputs=context["combinerBoxConnections"]))

    # 8. L1/L2 assignments and the seeded MPPT inputs, one save (CombinerAutoCmd.cs:601-622).
    solution_map = {int(k): v for k, v in solution["l1ToL2Assignments"].items()}
    assignments = _int_map(new["setting"].get("L1ToL2Assignments"), "L1ToL2Assignments")
    assignments.update(solution_map)
    new["setting"]["L1ToL2Assignments"] = _stored_map(assignments)
    if context["l2NumMppt"] > 0:
        slots = _int_map(new["setting"].get("L1ToL2InputAssignments"), "L1ToL2InputAssignments")
        for l1, slot in seed_input_assignments(assignments, context["l2NumMppt"]).items():
            if l1 in solution_map:
                slots[l1] = slot
        new["setting"]["L1ToL2InputAssignments"] = _stored_map(slots)
    st.save_drawing_properties(new)

    # PersistStringAssociations (CombinerAutoCmd.cs:3160-3221).
    string_to_l1, association = {}, {}
    for pc in sorted(placed, key=lambda c: c["L1Number"]):
        for string_id in pc["ServedStringIds"]:
            handle = string_ids.get(string_id)
            if handle is None:
                continue
            string_to_l1[string_id] = pc["L1Number"]
            association[handle] = pc["L1Number"]
    if string_to_l1:
        new["setting"][st.L1_SETTING] = _stored_map(string_to_l1)

    total = sum(pc["InputCountUsed"] for pc in placed)
    distinct_l2 = len(set(solution_map.values()))
    lines.append(f"LEAFCOMBINERAUTO: placed {len(placed)} of {len(placed)} combiner(s) for {total} strings across "
                 f"{distinct_l2} L2 inverter(s). Associated {len(string_to_l1)} string(s) to L1 combiner blocks.")

    # TryRouteCablingAfterCombinerPlacement (CombinerAutoCmd.cs:2413-2468).
    lines.append("LEAFCOMBINERAUTO: routing DC cabling now via HomerunsAuto automatic mode.")
    homeruns = _draw_direct_homeruns(new, association, host, lines)
    l1, l2 = levels(new)
    if l1 and l2:                                      # OptiHomerunCmd.cs:176-181
        _route_l2_feeders(new, outlines, host, lines)
    feeders = sum(1 for row in new["rows"]["cable"] if row.get("cable_kind") == "feeder")
    if homeruns > 0 and (not l2 or feeders > 0):
        lines.append(f"LEAFCOMBINERAUTO: automatic cabling complete ({homeruns} DC homerun cable(s), "
                     f"{feeders} feeder cable(s)).")
    st.sort_rows(new)
    return new, lines


# ------------------------------------------------------------- lightweight cabling studio --
#
# LEAFLITEPLACE / LEAFCABLEVIEW (LightweightCablingCommands.cs:65-89, the studio palette
# CablingViewPaletteControl.cs): opening the studio reads the drawing's raw environment and draws nothing
# (LoadEnvironment, :255-276); Simulate runs the engine (Simulate, LightweightCablingCommands.cs:205-220, the
# engine in server/solar_lite_cabling.py); Commit writes that result (CommitResult, :225-270). The palette's
# options are the engine defaults (ReadOptions, :221-246, over the controls' defaults, :138-157).

lite = _load_sibling("solar_lite_cabling")
LITE_HOMERUN_LAYER = "Homerun"                     # LightweightCablingCommands.cs:56
LITE_FORM_KEYS = {"cabling_redesign_simulate", "cabling_redesign_commit"}


def _lite_strings(state, intake):
    """BuildRequest's strings (LightweightCablingCommands.cs:444-455): one per String-layer polyline, its A and
    B terminals the polyline's first and last vertex (BuildStringSummariesFromCables, CombinerAutoCmd.cs:
    3826-3905), in the plugin's cable dictionary order. That order is host state no drawing row carries: it is
    the order the combiner intake records (the same builder's list at i5), each string matched to the one
    state string with those terminals, and every state string must be matched."""
    order = _string_ids(state, intake)
    geometry = {g.get("string"): g for g in state["geometry"]["strings"] if isinstance(g, dict)}
    if len(order) != len(geometry):
        raise InverterCablingError("the combiner intake does not order every string of the state")
    strings = []
    for sid in sorted(order):
        vertices = geometry[order[sid]]["vertices"]
        strings.append((dev._finite_xy(vertices[0], "string vertex"),
                        dev._finite_xy(vertices[-1], "string vertex")))
    return strings


def _lite_inverters(state, host):
    """BuildRequest's ExistingInverters: every registered L2 (App.gL2CollectorList, DocumentEventHandler.cs:
    305-313) with its NUMBER attribute. The state carries no number; host L2Numbers [[x, y, number]] records
    them, measured from the capture. The list is taken in number order."""
    table = host.get("L2Numbers")
    if not isinstance(table, list) or len(table) > lite.MAX_INVERTERS:
        raise InverterCablingError("host input L2Numbers (the L2 blocks' NUMBER attributes) is required")
    index = _PointIndex()
    for entry in table:
        if not (isinstance(entry, list) and len(entry) == 3 and type(entry[2]) is int):
            raise InverterCablingError("a host L2Numbers entry is not [x, y, number]")
        index.add(dev._finite_xy(entry[:2], "L2Numbers point"), entry[2])
    _, l2 = levels(state)
    result = []
    for item in l2:
        hits = index.near(item["position"])
        if len(hits) != 1:
            raise InverterCablingError("an L2 device matches no single host L2Numbers entry")
        result.append((hits[0], item["position"]))
    if len({number for number, _ in result}) != len(result):
        raise InverterCablingError("two L2 devices share a number")
    return sorted(result, key=lambda item: item[0])


def _lite_status_line(command, strings, inverters):
    return f"{command}: environment: {len(strings)} strings, {len(inverters)} inverters - Simulate to string."


def lite_studio_open(state, host, intake, command="LEAFLITEPLACE"):
    """LEAFLITEPLACE (l1) on the Studio state: the studio opens on the raw environment and draws nothing.
    The capture's reopened drawing carries one more CreateDefault() catalog pair than before it (the G27
    per-save duplication), so the step saves the drawing properties once. Returns (new state, printed lines)."""
    host = _host(host)
    new = copy.deepcopy(state)
    strings, inverters = _lite_strings(new, intake), _lite_inverters(new, host)
    st.save_drawing_properties(new)
    return new, [_lite_status_line(command, strings, inverters)]


def lite_studio_commit(state, host, intake, form_values, command="LEAFLITEPLACE"):
    """The studio opened, Simulate, then Commit (l2) on the Studio state: the engine's result written as
    CommitResult does (LightweightCablingCommands.cs:225-270): the lite feeders and homeruns and every L1
    combiner block erased, one L1 block per simulated combiner (PlaceNewInverterBlock, the combiner symbol
    scale a host quantity), the adopted inverters left in place, the routed homeruns and the comb feeders
    drawn. Returns (new state, printed lines)."""
    host = _host(host)
    if not isinstance(form_values, dict) or set(form_values) != LITE_FORM_KEYS:
        raise InverterCablingError("the studio form values are Simulate and Commit")
    racks = host.get("RackExtents")
    if not isinstance(racks, list):
        raise InverterCablingError("host input RackExtents (the drawing's rack extents) is required")
    if racks:
        raise InverterCablingNotPortedError("the studio over rack extents")
    try:
        scale = dev._symbol_scale(host, False)
    except dev.InverterDeviceError as exc:
        raise InverterCablingError(str(exc)) from None
    strings, inverters = _lite_strings(state, intake), _lite_inverters(state, host)
    lines = [_lite_status_line(command, strings, inverters)]
    options = lite.default_options()
    try:
        result = lite.place(strings, inverters, None, options)
    except lite.LiteCablingNotPortedError as exc:
        raise InverterCablingNotPortedError(str(exc)) from None
    except lite.LiteCablingError as exc:
        raise InverterCablingError(str(exc)) from None
    if not result["success"]:
        lines.append("engine: " + "; ".join(result["warnings"]))
        return copy.deepcopy(state), lines
    if not result["homerun_paths"]:
        raise InverterCablingNotPortedError("euclidean (non row-aware) homeruns")

    new = copy.deepcopy(state)

    def lite_homerun(row):
        return row.get("cable_kind") == "dc-homerun" and (row.get("_detail") or {}).get("layer") == LITE_HOMERUN_LAYER
    cables = new["rows"]["cable"]
    erased_feeders = sum(1 for row in cables if row.get("cable_kind") == "feeder")
    erased_homeruns = sum(1 for row in cables if lite_homerun(row))
    new["rows"]["cable"] = [row for row in cables if row.get("cable_kind") != "feeder" and not lite_homerun(row)]
    l1, _ = levels(new)
    erased_combiners = len(l1)
    dropped = {id(item["row"]) for item in l1}
    new["rows"]["device"] = [row for row in new["rows"]["device"] if id(row) not in dropped]

    for c in result["combiners"]:
        x, y = c["location"]
        new["rows"]["device"].append(dev._device_row(new, is_l2=False, x=x, y=y, scale=scale, placement=None,
                                                     number=c["number"], box_inputs=0))
    homeruns = 0
    for _, cb, points in result["homerun_paths"]:
        circuit = str(cb)
        new["rows"]["cable"].append({
            "cable_kind": "dc-homerun", "segment": "start", "from": None, "to": _homerun_to(circuit),
            "vertices": [st.coordinate(x, y) for x, y in points], "length": _feet(0.0),
            "_pair": st.new_pair(new, "cable"),
            "_detail": {"circuit": circuit, "gauge": "", "closed": False, "layer": LITE_HOMERUN_LAYER}})
        homeruns += 1
    lanes = result["lanes"] if len(result["lanes"]) >= 2 else []
    cb_pos = {c["number"]: c["location"] for c in result["combiners"]}
    inv_pos = {inv["number"]: inv["location"] for inv in result["inverters"]}
    direct = options["DirectFeeders"]
    feeders = 0
    for cb, inv, points in lite.build_feeder_paths(result["assignments"], cb_pos, inv_pos, lanes, direct):
        new["rows"]["cable"].append(_feeder_row(new, cb, inv, points, 0.0))
        feeders += 1
    replaced = erased_combiners + erased_homeruns + erased_feeders
    lines.append(f"committed: {len(result['combiners'])} combiners, {len(result['inverters'])} inverters "
                 f"(adopted, not moved), {homeruns} homeruns, {feeders} {'direct' if direct else 'comb'} feeders"
                 + (f" - replaced {erased_combiners} combiners / {erased_homeruns + erased_feeders} cables."
                    if replaced > 0 else "."))
    lines.extend(lite.printed(f) for f in result["findings"])
    st.sort_rows(new)
    return new, lines
