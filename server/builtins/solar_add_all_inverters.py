"""Place central inverters through the X02 explicit deterministic fallback tool."""
import copy
import math
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



def _graph_panel_groups(graph):
    """Project canonical frame bounds into the same drawing units as the device bridge."""
    scale = graph["project"]["units"]["meters_per_unit"]
    panels = {panel["id"]: panel for panel in graph["panels"]}
    groups = []
    for frame in graph["frames"]:
        if frame["validity"]["state"] != "valid" or not frame["panel_refs"]:
            raise GraphValidationError("SOLAR_OUTPUT_NOT_CURRENT", frame["id"])
        members = [panels[ref] for ref in frame["panel_refs"]]
        if any(panel["validity"]["state"] != "valid" for panel in members):
            raise GraphValidationError("SOLAR_OUTPUT_NOT_CURRENT", frame["id"])
        angle = frame["provenance"].get("row_angle", math.radians(members[0]["angle"]))
        if type(angle) not in (int, float) or not math.isfinite(angle):
            raise GraphValidationError("INVALID_DRAWING_CONTEXT", frame["id"])
        cosine, sine = math.cos(angle), math.sin(angle)
        along = [panel["centre"][0] * cosine + panel["centre"][1] * sine for panel in members]
        across = [-panel["centre"][0] * sine + panel["centre"][1] * cosine for panel in members]
        half_width = frame["module_width_along_row"] / 2
        half_height = frame["module_height_across_row"] / 2
        left, right = min(along) - half_width, max(along) + half_width
        bottom, top = min(across) - half_height, max(across) + half_height
        outline = [[(u * cosine - v * sine) / scale, (u * sine + v * cosine) / scale]
                   for u, v in ((left, bottom), (right, bottom), (right, top), (left, top))]
        groups.append({"handle": digest({"frame_id": frame["id"]}), "outlines": [outline]})
    if not groups:
        raise GraphValidationError("INVALID_DRAWING_CONTEXT", "frames")
    return groups


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
    if type(source_intake) is not dict:
        raise GraphValidationError("INVALID_DRAWING_CONTEXT", "source_intake")
    recorded = "panel_groups" in source_intake
    groups = copy.deepcopy(source_intake["panel_groups"]) if recorded else _graph_panel_groups(before)
    if type(groups) is not list:
        raise GraphValidationError("INVALID_DRAWING_CONTEXT", "panel_groups")
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
        "outline_source": "reviewed_intake" if recorded else "canonical_graph_frame_bounds",
        "tool": TOOL, "source_graph_sha256": digest(before), "request_sha256": digest(params),
        "inverter_refs": [item["id"] for item in result["inverters"]
                          if item["id"] not in previous_inverters]}
    for item in result["routes"] + result["schedules"]:
        if item["validity"]["state"] != "stale":
            item["validity"] = {"state": "stale", "reasons": ["equipment_changed"]}
    return finish_mutation(before, result, TOOL)
