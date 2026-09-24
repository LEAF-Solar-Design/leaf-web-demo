"""Studio ports of the plugin's inverter cabling commands (contract G35, G35a): route-l2-feeders (i11
RouteL2Feeders), homeruns (i12 HomerunsAuto), lightweight-cabling-feeders (i13 LEAFLITEFEEDERS),
inverter-move (i17 MOVEINV, declared) and inverter-position (POSITIONINV, declared).
combiner-auto-place (i5 LEAFCOMBINERAUTO) is NOT ported here: see combiner_auto_place.

Literal ports of Branch2025 (read 2026-09-24 at C:/tmp/solar-parity/wt-b25-s69, master 6b940d51, the
captured build):

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
  inverter-position: the plugin's POSITIONINV optimum search is unbounded (it hung the capture host and
    saved nothing). Studio searches a bounded grid (POSITION_GRID_SIDE x POSITION_GRID_SIDE candidates,
    POSITION_TIME_BUDGET_S seconds, fails closed past either) for the point that minimises the device's
    total homerun length outside every panel-group outline and its OUTLINE_BUFFER band
    (StringHomeRunCmd.cs:415-436 expands each outline by 50), then moves and reroutes as inverter-move.

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
# inverter-position (declared): the hard bounds of the Studio search.
POSITION_GRID_SIDE = 64                        # 4096 candidate points at most
POSITION_TIME_BUDGET_S = 10.0
MAX_OUTLINE_VERTICES_TOTAL = 1_000_000
MAX_DEVICES = 10_000
UNASSIGNED_CIRCUIT = "-"
HOST_KEYS = frozenset({"UseL2Collectors", "L1CollectorsPerL2", "RackExtents", "MovedDevice", "PositionDevice"})
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


def optimum_position(legs, current, outlines, grid_side=POSITION_GRID_SIDE, budget_s=POSITION_TIME_BUDGET_S,
                     clock=time.monotonic):
    """The bounded optimum search (declared): over a grid_side x grid_side grid spanning the legs and
    the current point, the candidate with the least total leg length outside every outline and its
    OUTLINE_BUFFER band; the current point when none beats it. Fails closed past the time budget."""
    if type(grid_side) is not int or not 2 <= grid_side <= POSITION_GRID_SIDE:
        raise InverterCablingError(f"the grid side must be an integer in 2..{POSITION_GRID_SIDE}")
    deadline = clock() + budget_s
    xs = [p[0] for p in legs] + [current[0]]
    ys = [p[1] for p in legs] + [current[1]]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)

    def cost(p):
        return sum(_dist(p, leg) for leg in legs)
    best, best_cost = current, cost(current)
    for r in range(grid_side):
        for c in range(grid_side):
            if clock() > deadline:
                raise InverterCablingError(f"the position search exceeded {budget_s} s")
            p = (min_x + (max_x - min_x) * c / (grid_side - 1), min_y + (max_y - min_y) * r / (grid_side - 1))
            value = cost(p)
            if value < best_cost and not _near_outline(p[0], p[1], outlines, OUTLINE_BUFFER):
                best, best_cost = p, value
    return best, best_cost


def inverter_position(state, panel_groups, host):
    """POSITIONINV (declared) on the Studio state: the device named by host PositionDevice moved to the
    bounded optimum and its homeruns rerouted. Returns (new state, printed lines)."""
    host = _host(host)
    outlines = validate_outlines(panel_groups)
    level, number = _host_device(host, "PositionDevice")
    new = copy.deepcopy(state)
    target = _find_device(new, level, number)
    legs = _device_homerun_legs(new, target["position"])
    if not legs:
        return new, ["The selected inverter has no homeruns to position."]
    before = sum(_dist(target["position"], leg) for leg in legs)
    (x, y), after = optimum_position(legs, target["position"], outlines)
    if (x, y) == target["position"]:
        return new, [f"Inverter {number} is already at its optimum position."]
    rerouted = _move_and_reroute(new, target, x, y)
    st.save_drawing_properties(new)
    st.sort_rows(new)
    return new, [f"Inverter {number} positioned; {rerouted} homerun(s) rerouted; total homerun length "
                 f"{before / INCHES_PER_FOOT:.1f} ft -> {after / INCHES_PER_FOOT:.1f} ft."]


# ------------------------------------------------------ combiner-auto-place --

def combiner_auto_place(state, panel_groups, host, form_values):
    """LEAFCOMBINERAUTO (i5). Not ported: the placement is CombinerPlacementEngine.Place with the
    combiner input planner (LeafSolarDesign.Core/CombinerPlacement, about 7,000 lines: StringPartitioner,
    CombinerPositioner, AlleyDetector, PlacementSpace, CombinerStringOptimizer, VdropValidator,
    CombinerBoxAutoResizer) over panel-group matrix data the G35 state does not carry."""
    raise InverterCablingNotPortedError("LEAFCOMBINERAUTO's CombinerPlacementEngine and combiner input planner")
