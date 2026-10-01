"""Route ONE trench between two picked points: the Studio counterpart of LEAFTRENCH (trench-routing).

Plugin semantics, read from the licensed source (Branch2025 master Commands.cs:5714-5933 and
LeafSolarDesign.Core/TrenchRouting.cs after R27, #326 and #332): LEAFTRENCH takes two picked points,
collects the panel-group polylines and inverter blocks as obstacles and every polyline on the trench
layer as alignment-bonus segments, converts its metric grid options (step 1 m, padding 5 m) to drawing
units by INSUNITS, routes in DRAWING units on the 8-connected lattice, and commits one open polyline
on the trench layer with the TrenchXData defaults (depth 1.0 m, width 0.6 m, voltage class MIXED).

Frozen decisions (sf-w3-trench-routing-manual):

* Units. The request's points are DRAWING units (what a pick returns). The router runs in drawing
  units with solar_inverter_outputs.routing_options_for_units(project drawing_units), the R27 port
  that LEAFTRENCHAUTO already uses; every graph coordinate in (metres) is divided by meters_per_unit
  first and every lattice vertex out is multiplied by it. The committed route stores metres
  (point_units "m") and length_ft = the sum of its point-to-point distances in metres / 0.3048.
  Routing the same picks in metres is NOT equivalent: on the W1 inch fixture a boundary midpoint
  falls on the other side of a frame edge and the route changes.
* Obstacles. One axis-aligned box per frame over its panels' turned module footprints (the frame
  has no outline polyline in the v1 graph; inferred, the extents fallback of
  solar_inverter_outputs.panel_group_outline), then one square of side INVERTER_OBSTACLE_M per
  inverter centred on its position (the graph keeps no block extents; inferred from the declared
  InverterBlockSize, 24 inches). Alignment segments: every leg of every other trench route, width
  TRENCH_WIDTH, in graph order.
* add-trench appends one valid trench route with open ends (from_ref and to_ref null) and nothing
  else changes. reroute-trench replaces one trench's points and length, opens its ends, keeps its
  id, trench record and created provenance, and marks every route whose pathway_ref names it, and
  every schedule whose source_refs names such a route, stale with the one reason "trench_changed"
  (an entity already stale keeps its first cause). A string riding the trench refuses
  TRENCH_STRING_RIDERS_UNSUPPORTED: staling a string cannot be undone by any W1 tool. Picks that
  reproduce the stored points exactly refuse TRENCH_UNCHANGED, so no empty version is published.

Fails closed: every check runs before the private copy is written; the caller's graph and params are
never mutated; a router refusal never escapes as a generic error. Bounded: one pass over panels, frames,
inverters and routes; the router's own 200-cells-per-axis lattice cap; at most MAX_TRENCH_SEGMENTS
alignment segments.
"""
import copy
from datetime import datetime, timezone
import math

import solar_ground_dsteps as dsteps
import solar_inverter_outputs as router
from solar_design_graph import GraphValidationError, _bounded_json, new_id
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation

TOOL = "solar-trench-manual"
INVALID = "INVALID_TRENCH_MANUAL_REQUEST"
REASON = "trench_changed"
RULE = "leaftrench-two-point"
METRES_PER_FOOT = 0.3048
MAX_REF = 128
MAX_REV = 2147483647
MAX_COORD_DU = 1e12
MAX_TRENCH_SEGMENTS = 100_000
INVERTER_OBSTACLE_M = 0.6096
POINT_KEYS = ("start_x", "start_y", "end_x", "end_y")
ADD_KEYS = frozenset(("expected_rev",) + POINT_KEYS)
REROUTE_KEYS = ADD_KEYS | {"trench_ref"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= MAX_COORD_DU


def _valid_request(request, keys):
    return (type(request) is dict and set(request) == keys
            and type(request["expected_rev"]) is int and 0 <= request["expected_rev"] <= MAX_REV
            and all(_number(request[key]) for key in POINT_KEYS)
            and ("trench_ref" not in keys or (type(request["trench_ref"]) is str
                                              and 1 <= len(request["trench_ref"]) <= MAX_REF)))


def routing_inputs(graph, skip_ref=None):
    """(outlines, segments) in DRAWING units, graph order: one box per frame with panels, one square per
    inverter, then every leg of every trench route except skip_ref as (a, b, TRENCH_WIDTH)."""
    mpu = graph["project"]["units"]["meters_per_unit"]
    frames = {frame["id"]: frame for frame in graph["frames"]}
    boxes = {}
    for panel in graph["panels"]:
        frame = frames.get(panel["frame_ref"])
        if frame is None:
            continue
        width, height = float(frame["module_width_along_row"]), float(frame["module_height_across_row"])
        turn = math.radians(panel["angle"])
        cos, sin = abs(math.cos(turn)), abs(math.sin(turn))
        hx, hy = (width * cos + height * sin) / 2, (width * sin + height * cos) / 2
        cx, cy = panel["centre"][0], panel["centre"][1]
        lo_x, lo_y, hi_x, hi_y = cx - hx, cy - hy, cx + hx, cy + hy
        box = boxes.get(frame["id"])
        if box is None:
            boxes[frame["id"]] = [lo_x, lo_y, hi_x, hi_y]
        else:
            box[0], box[1] = min(box[0], lo_x), min(box[1], lo_y)
            box[2], box[3] = max(box[2], hi_x), max(box[3], hi_y)
    outlines = []
    for frame in graph["frames"]:
        box = boxes.get(frame["id"])
        if box is not None:
            x0, y0, x1, y1 = (value / mpu for value in box)
            outlines.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    half = 0.5 * INVERTER_OBSTACLE_M / mpu
    for inverter in graph["inverters"]:
        x, y = inverter["position"][0] / mpu, inverter["position"][1] / mpu
        outlines.append([(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)])
    segments = []
    for route in graph["routes"]:
        if route["route_kind"] == "trench" and route["id"] != skip_ref:
            points = [(point[0] / mpu, point[1] / mpu) for point in route["points"]]
            segments.extend((a, b, router.TRENCH_WIDTH) for a, b in zip(points, points[1:]))
            if len(segments) > MAX_TRENCH_SEGMENTS:
                raise GraphValidationError("TRENCH_GEOMETRY_OUT_OF_RANGE")
    return outlines, segments


def _route(graph, request, skip_ref=None):
    """(points in metres, length_ft, picked points, lattice segment count) for the request's picks."""
    units = graph["project"]["units"]
    mpu = units["meters_per_unit"]
    start = (float(request["start_x"]), float(request["start_y"]))
    end = (float(request["end_x"]), float(request["end_y"]))
    if math.hypot(end[0] - start[0], end[1] - start[1]) < 1e-9:
        raise GraphValidationError("TRENCH_ENDPOINTS_IDENTICAL")
    outlines, segments = routing_inputs(graph, skip_ref)
    try:
        ok, path, _ = router.route_path(start, end, outlines, segments,
                                        router.routing_options_for_units(units["drawing_units"]))
    except (router.InverterOutputError, OverflowError, ArithmeticError, ValueError):
        raise GraphValidationError("TRENCH_GEOMETRY_OUT_OF_RANGE") from None
    if not ok or not path:
        raise GraphValidationError("TRENCH_ROUTE_FAILED")
    vertices = [path[0][0]] + [b for _, b in path]
    points = [[x * mpu, y * mpu] for x, y in vertices]
    if not all(math.isfinite(value) for point in points for value in point):
        raise GraphValidationError("TRENCH_GEOMETRY_OUT_OF_RANGE")
    length_ft = sum(math.dist(a, b) for a, b in zip(points, points[1:])) / METRES_PER_FOOT
    picked = [[request["start_x"], request["start_y"]], [request["end_x"], request["end_y"]]]
    return points, length_ft, picked, len(path)


def _extra(picked, segment_count):
    return {"source": "derived", "rule": RULE, "layer": dsteps.TRENCH_LAYER,
            "picked_points_du": picked, "segments": segment_count}


def add_trench(graph, request):
    """Append one trench route along the router's path between the two picked points."""
    _bounded_json(request)
    if not _valid_request(request, ADD_KEYS):
        raise GraphValidationError(INVALID)
    before = checked_graph(graph, request["expected_rev"])
    points, length_ft, picked, segment_count = _route(before, request)
    after = copy.deepcopy(before)
    trench = {
        "id": new_id("route"), "kind": "route", "rev": before["rev"],
        "provenance": {"created_by": TOOL, "created_at": datetime.now(timezone.utc).isoformat(),
                       "last_writer": TOOL, "source_rev": before["rev"],
                       "source_hash": before["source_hash"],
                       "catalog_versions": copy.deepcopy(before["catalog_versions"])},
        "extra": {"leaftrench": _extra(picked, segment_count)},
        "validity": {"state": "valid", "reasons": []},
        "route_kind": "trench", "points": points, "from_ref": None, "to_ref": None,
        "wire_gauge": "", "length_ft": length_ft, "point_units": "m", "length_units": "ft",
        "trench": {"depth_m": dsteps.TRENCH_DEFAULT_DEPTH_M, "width_m": dsteps.TRENCH_DEFAULT_WIDTH_M,
                   "voltage_class": dsteps.TRENCH_DEFAULT_VOLTAGE_CLASS},
    }
    after["routes"].append(trench)
    return {"graph": finish_mutation(before, after, TOOL), "trench_ref": trench["id"],
            "rider_refs": [], "schedule_refs": []}


def _stale(entity):
    if entity["validity"]["state"] != "stale":
        entity["validity"] = {"state": "stale", "reasons": [REASON]}


def reroute_trench(graph, request):
    """Re-route one trench between two new picks and mark its riders and their schedules stale."""
    _bounded_json(request)
    if not _valid_request(request, REROUTE_KEYS):
        raise GraphValidationError(INVALID)
    before = checked_graph(graph, request["expected_rev"])
    ref = request["trench_ref"]
    target = next((route for route in before["routes"] if route["id"] == ref), None)
    if target is None or target["route_kind"] != "trench":
        raise GraphValidationError("TRENCH_NOT_FOUND")
    if any(string.get("pathway_ref") == ref for string in before["strings"]):
        raise GraphValidationError("TRENCH_STRING_RIDERS_UNSUPPORTED")
    points, length_ft, picked, segment_count = _route(before, request, skip_ref=ref)
    if points == target["points"]:
        raise GraphValidationError("TRENCH_UNCHANGED")
    after = copy.deepcopy(before)
    riders, schedules = [], []
    for route in after["routes"]:
        if route["id"] == ref:
            route.update(points=points, length_ft=length_ft, from_ref=None, to_ref=None,
                         validity={"state": "valid", "reasons": []})
            route["extra"]["leaftrench"] = _extra(picked, segment_count)
        elif route.get("pathway_ref") == ref:
            _stale(route)
            riders.append(route["id"])
    rider_set = set(riders)
    for schedule in after["schedules"]:
        if not rider_set.isdisjoint(schedule["source_refs"]):
            _stale(schedule)
            schedules.append(schedule["id"])
    return {"graph": finish_mutation(before, after, TOOL), "trench_ref": ref,
            "rider_refs": riders, "schedule_refs": schedules}


OPERATIONS = {"add-trench": add_trench, "reroute-trench": reroute_trench}


def run(graph, params):
    _bounded_json(params)
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError(INVALID)
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](graph, request)["graph"]
