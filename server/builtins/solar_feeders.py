"""Regenerate L1 feeders to L2 collectors.

Outline source, by installation design:
  - Roof: the version's stored panel groups (source_intake["panel_groups"]).
  - Ground: the footprint the conversion stored on each frame (extra.ground_outline, drawing units), read through
    solar_ground_outlines.frame_outline. One group, one polygon per frame, graph order, no scaling: route_feeders
    converts drawing units itself. The intake's panel_groups is not read on a Ground graph.
Ground preparation is pure and O(frames); readiness runs the same preparation, so it never reports ready for a
Ground graph whose footprints the run would refuse. Kernel routing refusals stay run-time refusals on both designs.
"""
import solar_feeder_graph as fg
import solar_ground_outlines as ground_outlines
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


def _is_ground(graph):
    return graph["project"]["installation_design"] == "Ground"


def ground_panel_groups(graph):
    """The kernel's panel groups for a converted Ground graph: [{"outlines": [ring, ...]}], one ring of four
    [x, y] lists per frame, in graph order and drawing units. Fresh lists; the graph is not mutated. Refuses
    INVALID_DRAWING_CONTEXT naming the first frame whose stored footprint is absent or cannot be trusted."""
    frames = graph["frames"]
    if not frames:
        _refuse("INVALID_DRAWING_CONTEXT", "frames")
    rings = []
    for index, frame in enumerate(frames):
        try:
            ring = ground_outlines.frame_outline(frame)
        except ground_outlines.GroundOutlineError as exc:
            _refuse("INVALID_DRAWING_CONTEXT", f"frames[{index}].{exc.path}")
        rings.append([[x, y] for x, y in ring])
    return [{"outlines": rings}]


def _prerequisite(graph):
    """The first unmet graph prerequisite as a readiness reason, or None."""
    if graph["settings"].get("use_l2_collectors") is not True:
        return "valid_settings_required"
    if not any(not i["is_l2"] for i in graph["inverters"]):
        return "equipment_assignment_required"
    l2 = [i for i in graph["inverters"] if i["is_l2"]]
    if not l2:
        return "string_collectors_required"
    if len({i["collector_capacity"] for i in l2}) != 1:
        return "valid_settings_required"
    return None


def input_readiness(graph):
    """Graph prerequisites; on a Ground graph also every frame's stored footprint. No drawing intake."""
    reason = _prerequisite(graph)
    if reason == "valid_settings_required":
        return {"input_ready": False, "input_reason": "valid_settings_required"}
    if reason == "equipment_assignment_required":
        return {"input_ready": False, "input_reason": "equipment_assignment_required"}
    if reason == "string_collectors_required":
        return {"input_ready": False, "input_reason": "string_collectors_required"}
    if _is_ground(graph):
        try:
            ground_panel_groups(graph)
        except GraphValidationError:
            return {"input_ready": False, "input_reason": "invalid_drawing_context"}
    return {"input_ready": True, "input_reason": None}


def _feeders(graph):
    return {r["id"]: r for r in graph["routes"] if r["route_kind"] == "feeder"}


def _roof_groups(source_intake):
    if type(source_intake) is not dict:
        _refuse("INVALID_DRAWING_CONTEXT", "source_intake")
    groups = source_intake.get("panel_groups")
    if type(groups) is not list:
        _refuse("INVALID_DRAWING_CONTEXT", "panel_groups")
    for i, group in enumerate(groups):
        if type(group) is not dict or type(group.get("outlines")) is not list:
            _refuse("INVALID_DRAWING_CONTEXT", f"panel_groups[{i}]")
    return groups


def run(graph, params, *, source_intake=None):
    _bounded_json(params)
    if type(params) is not dict or set(params) != REQUEST_KEYS or type(params["expected_rev"]) is not int:
        _refuse(INVALID)
    before = checked_graph(graph, params["expected_rev"])
    reason = _prerequisite(before)
    if reason is not None:
        _refuse(reason.upper())
    groups = ground_panel_groups(before) if _is_ground(before) else _roof_groups(source_intake)
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
