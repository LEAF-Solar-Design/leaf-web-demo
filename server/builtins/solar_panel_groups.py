"""Panel group validator and licensed-lane orchestration.

The broker supplies licensed_matrix(plan=bytes, graph=dict, groups=list,
timeout=seconds). It executes the existing mutation plan in an isolated preview
and returns {plan_sha256, frames}. Frames use the solar graph schema, including
matrix, sequences and assignments. This narrow adapter must be wired by the
licensed broker; there is no local matrix fallback or caller-supplied executor.
The owner commits the returned graph and drawing together through its existing
transaction. Cancellation never calls the lane.
"""
import copy
import math

from mutation_plan import emit_plan, plan_sha256, validate_mutations
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph, require_sizing


def create_groups(graph, params, *, drawing_intake, licensed_matrix=None):
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "groups", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_GROUP_REQUEST")
    result = checked_graph(graph, params.get("expected_rev"))
    if params.get("cancel", False):
        return {"graph": result, "mutations": {}, "plan": None, "cancelled": True}
    require_sizing(result)
    groups = params.get("groups")
    if type(groups) is not list or not 1 <= len(groups) <= 128:
        raise GraphValidationError("INVALID_GROUP_REQUEST")
    panels = {p["id"]: p for p in result["panels"]}
    seen = set()
    seen_handles = set()
    names = {f["name"].casefold() for f in result["frames"]}
    mutations = {"added_groups": []}
    for group in groups:
        if type(group) is not dict or set(group) != {"name", "panel_refs"}:
            raise GraphValidationError("INVALID_GROUP_REQUEST")
        name, refs = group["name"], group["panel_refs"]
        if (type(name) is not str or not name.strip() or len(name) > 255
                or "[" in name or name.casefold() in names
                or type(refs) is not list or not 2 <= len(refs) <= 4096
                or any(type(ref) is not str for ref in refs)):
            raise GraphValidationError("INVALID_GROUP_REQUEST")
        names.add(name.casefold())
        if len(set(refs)) != len(refs) or seen.intersection(refs):
            raise GraphValidationError("INVALID_GROUP_COVERAGE")
        seen.update(refs)
        handles = []
        for ref in refs:
            panel = panels.get(ref)
            if panel is None:
                raise GraphValidationError("MISSING_PANEL")
            if panel["frame_ref"] is not None:
                raise GraphValidationError("PANEL_ALREADY_GROUPED")
            handle = panel["provenance"].get("source_handle")
            if type(handle) is not str or handle.upper() in seen_handles:
                raise GraphValidationError("AMBIGUOUS_PANEL_HANDLE")
            seen_handles.add(handle.upper())
            handles.append(handle)
        mutations["added_groups"].append({"name": name, "members": handles})
    _bounded_json(drawing_intake)
    canonical = validate_mutations(drawing_intake, mutations)
    plan = emit_plan(canonical, base_sha256=result["source_hash"], base_intake=drawing_intake)
    if licensed_matrix is None:
        raise GraphValidationError("LICENSED_MATRIX_REQUIRED")
    reply = licensed_matrix(plan=plan, graph=copy.deepcopy(result),
                            groups=copy.deepcopy(groups), timeout=50)
    _bounded_json(reply)
    if (type(reply) is not dict or set(reply) != {"plan_sha256", "frames"}
            or reply["plan_sha256"] != plan_sha256(plan)
            or type(reply["frames"]) is not list or len(reply["frames"]) != len(groups)):
        raise GraphValidationError("INVALID_LICENSED_MATRIX")
    frames = copy.deepcopy(reply["frames"])
    changed = []
    try:
        for frame, group in zip(frames, groups):
            if (frame["name"] != group["name"] or frame["panel_refs"] != group["panel_refs"]
                    or not frame["matrix"] or not frame["matrix"][0]):
                raise GraphValidationError("INVALID_LICENSED_MATRIX")
            zone_ref = frame["electrical_zone_ref"]
            if zone_ref is not None:
                zone = next((z for z in result["electrical_zones"] if z["id"] == zone_ref), None)
                if zone is None or not set(group["panel_refs"]) <= set(zone["panel_refs"]):
                    raise GraphValidationError("INVALID_ZONE_COVERAGE")
            if result["settings"]["extra"]["string_sizing"]["mode"] == "zones" and zone_ref is None:
                raise GraphValidationError("INVALID_ZONE_COVERAGE")
            cell_refs = set()
            for row_number, row in enumerate(frame["matrix"]):
                for column, cell in enumerate(row):
                    ref = cell["panel_ref"]
                    if ref is None:
                        continue
                    if ref not in group["panel_refs"] or ref in cell_refs:
                        raise GraphValidationError("INVALID_GROUP_COVERAGE")
                    cell_refs.add(ref)
                    panel = panels[ref]
                    if any(not math.isclose(cell[key], value, abs_tol=1e-9, rel_tol=1e-9)
                           for key, value in (("x", panel["centre"][0]), ("y", panel["centre"][1]),
                                              ("angle", panel["angle"]))):
                        raise GraphValidationError("GROUP_GEOMETRY_MISMATCH")
                    panel["frame_ref"] = frame["id"]
                    panel["matrix_cell"] = {"row": row_number, "col": column}
                    changed.append(panel)
            if cell_refs != set(group["panel_refs"]):
                raise GraphValidationError("INVALID_GROUP_COVERAGE")
            result["frames"].append(frame)
            changed.append(frame)
        result = advance(result, changed, "solar-panel-groups")
    except (KeyError, TypeError, IndexError, OverflowError, AttributeError):
        raise GraphValidationError("INVALID_LICENSED_MATRIX") from None
    return {"graph": result, "mutations": canonical, "plan": plan.decode("utf-8"),
            "plan_sha256": plan_sha256(plan), "cancelled": False}


def run(intake, params):
    raise RuntimeError("solar-panel-groups requires the licensed mutation broker")
