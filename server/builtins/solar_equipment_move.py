"""Move one picked inverter or collector to a picked point and persist its rerouted homeruns: the plugin's
MOVEINV (StringHomeRunCmd.InverterMove, inverter-move, ported as solar_inverter_cabling.inverter_move,
receipt inverter-move/rooftop-inverters-i17) on the design graph.

The graph never meets the kernel directly. server/solar_electrical_route_bridge.py projects the graph's
equipment AND its homerun and feeder routes into a G35 state; the kernel moves the device to the point and
redraws every homerun of the device's strings as one straight leg from the same string endpoint to the new
point; the bridge writes the moved device and the redrawn legs back onto a copy of the graph, keeping every
route id, every trench and every untouched route exactly.

Frozen decisions (sf-w2-equipment-move):
  - The device is named by its GRAPH id (inverter_ref), never by a kernel level or number. The builtin finds
    the bridge row bound to that id and asks the kernel for that row's own level and number; when the kernel's
    numbering does not resolve to exactly that row, the move refuses (EQUIPMENT_DEVICE_AMBIGUOUS).
  - The point is in drawing units, the unit the kernel and the plugin's jig take; the graph position becomes
    [x * meters_per_unit, y * meters_per_unit] (the bridge's rule for a moved device).
  - A device with no homerun to move is a named refusal (NO_HOMERUNS_TO_MOVE), never a successful no-op, and
    a point equal to the device's current position is refused (EQUIPMENT_POSITION_UNCHANGED).
  - Nothing is ever erased. The plugin erases the attached homeruns of strings its number does not select; on
    a bridge state every attached homerun belongs to a selected string, so a kernel answer that drops one is a
    mapping failure (EQUIPMENT_MOVE_MAPPING_FAILED) and nothing is written.
  - Length. The kernel stores a rerouted leg's length as its drawing-unit length over 12 (inches to feet).
    The builtin multiplies that by meters_per_unit / 0.0254, so a leg's feet are right in every drawing unit
    (the factor is exactly 1.0 in an inch drawing, so an inch drawing keeps the kernel's number exactly).
  - Feeders. MOVEINV never redraws a feeder: a feeder from or to the moved device keeps its points, length,
    gauge and pathway_ref exactly, and becomes stale with the reason "equipment_moved" (it no longer meets
    the device). Trenches are never touched. A rerouted homerun loses its pathway_ref (the bridge's rule) and
    keeps its validity.
  - Schedules. Every schedule whose source_refs name the moved device, a rerouted homerun, a staled feeder or
    a string whose homeruns were rerouted becomes stale with the reason "equipment_moved". An entity already
    stale keeps its state and reasons, so the first cause stays.
  - This is a move to a picked point only. The automatic optimum search (POSITIONINV, inverter_position) is a
    separate capability and is never called here.

Readiness (hook): "equipment_assignment_required" with no inverter, "complete_routing_required" when no
homerun route ends at an inverter, else ready. One pass over inverters and routes; never raises on a valid
graph.

Contract: fails closed. Every check runs before the private copy is written; the bridge and the graph
validator have the last word; the input graph and request are never mutated. Never creates or removes a
device or a route. Cost: two bridge projections and one kernel pass, each linear in devices, routes and
vertices; one pass over schedules.
"""
import math

import solar_electrical_route_bridge as bridge
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import checked_graph
from solar_solve_results import HOMERUN_KINDS, finish_mutation

cab = bridge.cab

TOOL = "solar-equipment-move"
INVALID = "INVALID_EQUIPMENT_MOVE_REQUEST"
REASON = "equipment_moved"
RECEIPT_SCHEMA = "leaf.solar-equipment-move.v1"
REQUEST_KEYS = frozenset({"expected_rev", "inverter_ref", "point"})
MAX_REF = 128
MAX_COORDINATE = bridge.MAX_COORDINATE      # drawing units, the bridge's bound
INCH_METRES = 0.0254
NO_HOMERUNS_LINE = "The selected inverter has no homeruns to move."


def _refuse(code):
    raise GraphValidationError(code)


def _request(request):
    _bounded_json(request)
    if (type(request) is not dict or set(request) != REQUEST_KEYS
            or type(request["expected_rev"]) is not int
            or type(request["inverter_ref"]) is not str
            or not 1 <= len(request["inverter_ref"]) <= MAX_REF
            or type(request["point"]) is not list or len(request["point"]) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or abs(value) > MAX_COORDINATE for value in request["point"])):
        _refuse(INVALID)
    return request["expected_rev"], request["inverter_ref"], (float(request["point"][0]),
                                                              float(request["point"][1]))


def _homerun_keys(state):
    return {(row["from"], row["segment"]): row for row in state["rows"]["cable"]
            if row.get("cable_kind") == "dc-homerun"}


def _stale(entity):
    if entity["validity"]["state"] != "stale":
        entity["validity"] = {"state": "stale", "reasons": [REASON]}
        return True
    return False


def move_equipment(graph, request):
    expected_rev, ref, (x, y) = _request(request)
    before = checked_graph(graph, expected_rev)
    inverter = next((item for item in before["inverters"] if item["id"] == ref), None)
    if inverter is None:
        _refuse("MISSING_EQUIPMENT")
    try:
        state, binding = bridge.state_from_graph(before)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None
    pairs = [pair for pair, item in binding["devices"].items() if item["id"] == ref]
    if len(pairs) != 1:
        _refuse("EQUIPMENT_MOVE_MAPPING_FAILED")
    level = "L2" if inverter["is_l2"] else "L1"
    l1, l2 = cab.levels(state)
    found = [item for item in (l2 if inverter["is_l2"] else l1) if item["number"] == inverter["number"]]
    if len(found) != 1 or found[0]["row"]["_pair"] != pairs[0]:
        _refuse("EQUIPMENT_DEVICE_AMBIGUOUS")
    if (x, y) == found[0]["position"]:
        _refuse("EQUIPMENT_POSITION_UNCHANGED")
    old_rows = _homerun_keys(state)
    try:
        after, lines = cab.inverter_move(state, {"MovedDevice": [level, inverter["number"]]},
                                         [pairs[0], f"{x!r},{y!r}"])
    except cab.InverterCablingError:
        raise GraphValidationError("EQUIPMENT_MOVE_MAPPING_FAILED") from None
    if lines == [NO_HOMERUNS_LINE]:
        _refuse("NO_HOMERUNS_TO_MOVE")
    new_rows = _homerun_keys(after)
    if set(new_rows) != set(old_rows) or len(new_rows) != sum(
            row.get("cable_kind") == "dc-homerun" for row in after["rows"]["cable"]):
        _refuse("EQUIPMENT_MOVE_MAPPING_FAILED")
    redrawn = [key for key, row in new_rows.items() if row["_pair"] != old_rows[key]["_pair"]]
    if not redrawn or lines != [f"Inverter {inverter['number']} moved; {len(redrawn)} homerun(s) rerouted."]:
        _refuse("EQUIPMENT_MOVE_MAPPING_FAILED")
    factor = before["project"]["units"]["meters_per_unit"] / INCH_METRES
    if factor != 1.0:
        for key in redrawn:
            length = new_rows[key]["length"]
            length["value"] = length["value"] * factor
    try:
        result, _ = bridge.graph_from_state(before, after, binding)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None
    old_routes = {route["id"]: route for route in before["routes"]}
    if [route["id"] for route in result["routes"]] != [route["id"] for route in before["routes"]]:
        _refuse("EQUIPMENT_MOVE_MAPPING_FAILED")
    rerouted = [route for route in result["routes"]
                if route["route_kind"] in HOMERUN_KINDS and route != old_routes[route["id"]]]
    if len(rerouted) != len(redrawn) or any(route["to_ref"] != ref for route in rerouted):
        _refuse("EQUIPMENT_MOVE_MAPPING_FAILED")
    feeders = [route for route in result["routes"] if route["route_kind"] == "feeder"
               and ref in (route["from_ref"], route["to_ref"])]
    staled_feeders = [route["id"] for route in feeders if _stale(route)]
    sources = ({ref} | {route["id"] for route in rerouted} | {route["id"] for route in feeders}
               | {route["from_ref"] for route in rerouted})
    staled_schedules = [schedule["id"] for schedule in result["schedules"]
                        if not sources.isdisjoint(schedule["source_refs"]) and _stale(schedule)]
    result = finish_mutation(before, result, TOOL)
    moved = next(item for item in result["inverters"] if item["id"] == ref)
    return {"graph": result, "receipt": {
        "schema": RECEIPT_SCHEMA, "inverter_ref": ref, "point": [x, y],
        "position": list(moved["position"]), "rerouted_route_refs": [route["id"] for route in rerouted],
        "stale_feeder_refs": staled_feeders, "stale_schedule_refs": staled_schedules,
        "lines": list(lines)}}


def input_readiness(graph):
    """Whether MOVEINV has a device with a homerun to move; never raises on a valid graph."""
    inverters = {inverter["id"] for inverter in graph["inverters"]}
    if not inverters:
        return {"input_ready": False, "input_reason": "equipment_assignment_required"}
    if not any(route["route_kind"] in HOMERUN_KINDS and route["to_ref"] in inverters
               for route in graph["routes"]):
        return {"input_ready": False, "input_reason": "complete_routing_required"}
    return {"input_ready": True, "input_reason": None}


OPERATIONS = {"move-equipment": move_equipment}


def run(graph, params):
    _bounded_json(params)
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        _refuse(INVALID)
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](graph, request)["graph"]
