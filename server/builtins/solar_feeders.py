"""Regenerate L1 feeders to L2 collectors using the version's stored outlines."""
import solar_feeder_graph as fg
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation

TOOL = "solar-feeders"
INVALID = "INVALID_FEEDER_REQUEST"
STALE_REASON = "ROUTES_CHANGED"
REQUEST_KEYS = frozenset({"expected_rev"})
CODE_MAP = {
    "FEEDER_L2_MODE_REQUIRED": "VALID_SETTINGS_REQUIRED",
    "FEEDER_COMBINERS_REQUIRED": "EQUIPMENT_ASSIGNMENT_REQUIRED",
    "FEEDER_COLLECTORS_REQUIRED": "STRING_COLLECTORS_REQUIRED",
    "FEEDER_CAPACITY_AMBIGUOUS": "VALID_SETTINGS_REQUIRED",
    "FEEDER_OUTLINES_INVALID": "INVALID_DRAWING_CONTEXT",
    "FEEDER_NOT_PORTED": "CAPABILITY_NOT_READY",
    "FEEDER_KERNEL_REFUSED": "CAPABILITY_NOT_READY",
    "FEEDER_POSTCONDITION_FAILED": "CAPABILITY_NOT_READY",
}
assert set(CODE_MAP) == set(fg.CODES)


def _refuse(code, path="<root>"):
    raise GraphValidationError(code, path)


def input_readiness(graph):
    """Graph prerequisites only; the hook receives no drawing intake."""
    if graph["settings"].get("use_l2_collectors") is not True:
        return {"input_ready": False, "input_reason": "valid_settings_required"}
    if not any(not i["is_l2"] for i in graph["inverters"]):
        return {"input_ready": False, "input_reason": "equipment_assignment_required"}
    l2 = [i for i in graph["inverters"] if i["is_l2"]]
    if not l2:
        return {"input_ready": False, "input_reason": "string_collectors_required"}
    if len({i["collector_capacity"] for i in l2}) != 1:
        return {"input_ready": False, "input_reason": "valid_settings_required"}
    return {"input_ready": True, "input_reason": None}


def _feeders(graph):
    return {r["id"]: r for r in graph["routes"] if r["route_kind"] == "feeder"}


def run(graph, params, *, source_intake=None):
    _bounded_json(params)
    if type(params) is not dict or set(params) != REQUEST_KEYS or type(params["expected_rev"]) is not int:
        _refuse(INVALID)
    before = checked_graph(graph, params["expected_rev"])
    readiness = input_readiness(before)
    if not readiness["input_ready"]:
        _refuse(readiness["input_reason"].upper())
    if type(source_intake) is not dict:
        _refuse("INVALID_DRAWING_CONTEXT", "source_intake")
    groups = source_intake.get("panel_groups")
    if type(groups) is not list:
        _refuse("INVALID_DRAWING_CONTEXT", "panel_groups")
    for i, group in enumerate(groups):
        if type(group) is not dict or type(group.get("outlines")) is not list:
            _refuse("INVALID_DRAWING_CONTEXT", f"panel_groups[{i}]")
    try:
        result, _ = fg.route_feeders(before, groups)
    except fg.FeederGraphError as exc:
        _refuse(CODE_MAP[exc.code], exc.code)
    except fg.rb.ElectricalBridgeError as exc:
        _refuse("CAPABILITY_NOT_READY", str(exc)[:64])
    old, new = _feeders(before), _feeders(result)
    changed = {ref for ref in old.keys() | new.keys()
               if ref not in old or ref not in new
               or any(old[ref][key] != new[ref][key] for key in
                      ("points", "length_ft", "from_ref", "to_ref"))}
    for schedule in result["schedules"]:
        if changed.intersection(schedule["source_refs"]):
            schedule["validity"] = {"state": "stale", "reasons": [STALE_REASON]}
    return finish_mutation(before, result, TOOL)
