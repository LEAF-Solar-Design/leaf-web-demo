"""Place central inverters through the X02 explicit deterministic fallback tool."""
import copy
import re

import solar_inverter_devices as devices
import solar_electrical_state_bridge as bridge
from solar_design_graph import GraphValidationError, _bounded_json
from solar_local_graph import _load_builtin
from solar_sizing_client import checked_graph, digest
from solar_solve_results import finish_mutation

TOOL = "solar-add-all-inverters"
INVALID = "INVALID_INVERTER_PLACEMENT_REQUEST"
STRATEGY = "deterministic_grid"


def input_readiness(graph):
    if graph["project"]["installation_design"].lower() != "roof":
        return {"input_ready": False, "input_reason": "capability_not_ready"}
    if graph["settings"].get("use_l2_collectors") is not True:
        return {"input_ready": False, "input_reason": "valid_settings_required"}
    if not any(item["inverter_ref"] is None for item in graph["strings"]):
        return {"input_ready": False, "input_reason": "unassigned_strings_required"}
    if any(item["is_l2"] for item in graph["inverters"]):
        return {"input_ready": False, "input_reason": "capability_not_ready"}
    return {"input_ready": True, "input_reason": None}


def run(graph, params, *, source_intake):
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) != {"expected_rev", "hardware", "placement_strategy"}
            or params["placement_strategy"] != STRATEGY):
        raise GraphValidationError(INVALID, "placement_strategy")
    central = _load_builtin("solar-central-inverter-add")
    try:
        expected_rev, _, _, hardware = central._request({
            "expected_rev": params["expected_rev"], "hardware": params["hardware"], "point": [0, 0]})
    except GraphValidationError as error:
        raise GraphValidationError(INVALID, error.path) from None
    before = checked_graph(graph, expected_rev)
    ready = input_readiness(before)
    if not ready["input_ready"]:
        raise GraphValidationError(ready["input_reason"].upper(), "inverters")
    if type(source_intake) is not dict or type(source_intake.get("panel_groups")) is not list:
        raise GraphValidationError("INVALID_DRAWING_CONTEXT", "panel_groups")
    groups = copy.deepcopy(source_intake["panel_groups"])
    if (not 1 <= len(groups) <= devices.MAX_PANEL_GROUPS
            or any(type(group) is not dict or set(group) != {"handle", "outlines"}
                   or type(group["handle"]) is not str
                   or re.fullmatch(r"[0-9A-Fa-f]{1,64}", group["handle"]) is None
                   or type(group["outlines"]) is not list or not group["outlines"]
                   or any(type(outline) is not list or len(outline) < 3
                          for outline in group["outlines"]) for group in groups)):
        raise GraphValidationError("INVALID_DRAWING_CONTEXT", "panel_groups")
    handles = [group["handle"].upper().lstrip("0") or "0" for group in groups]
    if len(set(handles)) != len(handles):
        raise GraphValidationError("INVALID_DRAWING_CONTEXT", "panel_groups")
    try:
        state, binding = bridge.state_from_graph(before)
        state["geometry"]["panel_groups"] = [
            {"group": group["handle"].upper().lstrip("0") or "0", "position": [0.0, 0.0],
             "scale": 1.0, "rotation_deg": 0.0} for group in groups]
        devices.validate_panel_groups(groups, state)
        capacity = hardware["total_dc_inputs"]
        host = {"UseL2Collectors": True, "StringsPerCentralInverter": capacity,
                "CentralInverterSymbolScale": 1.0}
        answers = {"inverters_count_differs_from_stringsizer": "Yes",
                   "low_utilization_on_last_inverter": devices.RESIZE_KEEP}
        placed, _ = devices.inverter_add_all(state, groups, host, answers)
        result, _ = bridge.graph_from_state(
            before, placed, binding, defaults={"central_inverter": hardware},
            new_id=central._minter(before, {**params, "tool": TOOL}),
            created_at=central._created_at(before))
    except (bridge.ElectricalBridgeError, devices.InverterDeviceError) as error:
        raise GraphValidationError("CAPABILITY_NOT_READY", type(error).__name__) from None
    if len(result["inverters"]) <= len(before["inverters"]):
        raise GraphValidationError("UNASSIGNED_STRINGS_REQUIRED", "inverters")
    previous_inverters = {item["id"] for item in before["inverters"]}
    result["extra"]["inverter_placement"] = {
        "engine": "deterministic_fallback", "backend": "local_python",
        "label": "Deterministic fallback", "placement_strategy": STRATEGY,
        "fallback_reason": "Explicit deterministic grid placement",
        "tool": TOOL, "source_graph_sha256": digest(before), "request_sha256": digest(params),
        "inverter_refs": [item["id"] for item in result["inverters"]
                          if item["id"] not in previous_inverters]}
    for item in result["routes"] + result["schedules"]:
        if item["validity"]["state"] != "stale":
            item["validity"] = {"state": "stale", "reasons": ["equipment_changed"]}
    return finish_mutation(before, result, TOOL)
