"""Panel groups for intake graphs and the existing licensed drawing path.

The local run commits caller-named membership with kernel matrix placement and
graph-derived geometry and assignments. The drawing path keeps its broker-supplied
matrix transaction. Both paths validate sizing before creating groups.
"""
import copy
import hashlib
import math
from uuid import UUID

from mutation_plan import emit_plan, plan_sha256, validate_mutations
from solar_design_graph import MAX_NODES, GraphValidationError, _bounded_json
import solar_panel_group_kernel as kernel
from solar_sizing_client import advance, checked_graph, require_sizing


_CELL_KEYS = ("code", "panel_ref", "seq", "inverter_id", "string_input_number", "x", "y", "angle")
# One dict plus its keys and values per cell: 29,411 cells at MAX_NODES 500000.
_MAX_MATRIX_CELLS = MAX_NODES // (1 + 2 * len(_CELL_KEYS))


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


def run(graph, params):
    """Commit deterministic frames on an intake-embedded graph."""
    try:
        group_keys = {"name", "panel_refs", "alignment_tolerance",
                      "module_width_along_row", "module_height_across_row"}
        if (type(params) is not dict
                or set(params) - {"expected_rev", "groups", "cancel"}
                or type(params.get("cancel", False)) is not bool):
            raise GraphValidationError("INVALID_GROUP_REQUEST")
        groups = params.get("groups")
        if not params.get("cancel", False) and (
                type(groups) is not list or not 1 <= len(groups) <= 128
                or any(type(group) is not dict or set(group) != group_keys for group in groups)):
            raise GraphValidationError("INVALID_GROUP_REQUEST")
        result = checked_graph(graph, params.get("expected_rev"))
        if params.get("cancel", False):
            return result
        require_sizing(result)

        panels = {panel["id"]: panel for panel in result["panels"]}
        names = {frame["name"].casefold() for frame in result["frames"]}
        seen, seen_handles = set(), set()
        prepared, panel_to_group = [], {}
        for index, group in enumerate(groups):
            name, refs = group["name"], group["panel_refs"]
            if (type(name) is not str or not name.strip() or len(name) > 255
                    or "[" in name or name.casefold() in names
                    or type(refs) is not list or not 2 <= len(refs) <= 4096
                    or any(type(ref) is not str for ref in refs)
                    or any(type(group[key]) not in (int, float)
                           or not 0 < group[key] <= 1e6 or not math.isfinite(group[key])
                           for key in ("alignment_tolerance", "module_width_along_row",
                                       "module_height_across_row"))):
                raise GraphValidationError("INVALID_GROUP_REQUEST")
            names.add(name.casefold())
            if len(set(refs)) != len(refs) or seen.intersection(refs):
                raise GraphValidationError("INVALID_GROUP_COVERAGE")
            seen.update(refs)
            members = []
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
                try:
                    order = kernel.handle_sort_key(handle)
                except kernel.PanelGroupKernelError:
                    raise GraphValidationError("INVALID_PANEL_HANDLE") from None
                members.append((order, panel))
                panel_to_group[ref] = index
            members = [panel for _, panel in sorted(members, key=lambda member: member[0])]
            angle_keys = {kernel.angle_key(math.radians(panel["angle"])) for panel in members}
            if len(angle_keys) != 1:
                raise GraphValidationError("MIXED_GROUP_ANGLE")
            row_angle = kernel.group_row_angle(math.radians(members[0]["angle"]))
            prepared.append((group, members, next(iter(angle_keys)), row_angle))

        # Traverse strings once, distributing each sequence through its panel index.
        sequences = [[] for _ in groups]
        for string in result["strings"]:
            portions = {}
            for ref in string["ordered_panel_refs"]:
                index = panel_to_group.get(ref)
                if index is not None:
                    portions.setdefault(index, []).append(ref)
            for index, refs in portions.items():
                sequences[index].append({"string_ref": string["id"], "ordered_panel_refs": refs})
        string_inputs = {
            assignment["string_ref"]: (inverter["id"], assignment["input_number"])
            for inverter in result["inverters"] for assignment in inverter["input_assignments"]
        }
        zones = [(zone["id"], set(zone["panel_refs"])) for zone in result["electrical_zones"]]
        changed = []
        cells_used = 0
        for index, (group, members, angle_key, row_angle) in enumerate(prepared):
            refs = group["panel_refs"]
            by_handle = {panel["provenance"]["source_handle"]: panel for panel in members}
            try:
                placed = kernel.group_matrix(
                    [{"handle": panel["provenance"]["source_handle"], "centre": panel["centre"]}
                     for panel in members], row_angle, group["alignment_tolerance"],
                    max_cells=_MAX_MATRIX_CELLS - cells_used)
            except kernel.PanelGroupMatrixLimitError:
                raise GraphValidationError("GRAPH_LIMIT_EXCEEDED") from None
            cells_used += len(placed) * len(placed[0]) if placed else 0
            placed_refs = {by_handle[handle]["id"] for row in placed for handle in row
                           if handle is not None}
            if placed_refs != set(refs):
                raise GraphValidationError("GROUP_MATRIX_COLLISION")
            zone_ref = next((zone for zone, coverage in zones if set(refs) <= coverage), None)
            if result["settings"]["extra"]["string_sizing"]["mode"] == "zones" and zone_ref is None:
                raise GraphValidationError("INVALID_ZONE_COVERAGE")

            identity = str(result["rev"]) + ":" + ",".join(sorted(refs))
            raw = hashlib.sha256((result["source_hash"] + ":frame:" + identity).encode("utf-8")).digest()[:16]
            frame_id = f"leaf:frame:{UUID(bytes=raw, version=4)}"
            matrix = []
            for row_number, row in enumerate(placed):
                cells = []
                for column, handle in enumerate(row):
                    panel = by_handle[handle] if handle is not None else None
                    inverter_id, input_number = string_inputs.get(
                        panel["assignment"]["string_ref"], (None, None)) if panel else (None, None)
                    cells.append(dict(zip(_CELL_KEYS, (
                        "panel" if panel else "empty", panel["id"] if panel else None,
                        panel["assignment"]["seq"] if panel else None,
                        inverter_id, input_number,
                        panel["centre"][0] if panel else 0.0,
                        panel["centre"][1] if panel else 0.0,
                        panel["angle"] if panel else 0.0,
                    ), strict=True)))
                    if panel:
                        panel["frame_ref"] = frame_id
                        panel["matrix_cell"] = {"row": row_number, "col": column}
                        changed.append(panel)
                matrix.append(cells)
            assignments = []
            for ref in refs:
                assignment = panels[ref]["assignment"]
                inverter_id, input_number = string_inputs.get(assignment["string_ref"], (None, None))
                assignments.append({"panel_ref": ref, "string_ref": assignment["string_ref"],
                                    "seq": assignment["seq"], "inverter_id": inverter_id,
                                    "string_input_number": input_number})
            frame = {
                "id": frame_id, "kind": "frame", "rev": result["rev"],
                "extra": {}, "validity": {"state": "valid", "reasons": []},
                "name": group["name"], "panel_refs": refs[:],
                "insertion_point": members[0]["centre"][:], "installation_design": "Roof",
                "module_rows": len(matrix), "module_columns": len(matrix[0]),
                "module_slots": len(matrix) * len(matrix[0]), "module_power_watts": 0,
                "module_width_along_row": group["module_width_along_row"],
                "module_height_across_row": group["module_height_across_row"],
                "electrical_zone_ref": zone_ref, "matrix": matrix,
                "sequences": sequences[index], "panel_assignments": assignments,
                "provenance": {
                    "created_by": "solar-panel-groups", "last_writer": "solar-panel-groups",
                    "created_at": members[0]["provenance"]["created_at"],
                    "source_rev": result["rev"], "source_hash": result["source_hash"],
                    "kernel": "solar_panel_group_kernel", "angle_key": angle_key, "row_angle": row_angle,
                },
            }
            result["frames"].append(frame)
            changed.append(frame)
        return advance(result, changed, "solar-panel-groups")
    except GraphValidationError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError, ArithmeticError, RecursionError, ValueError):
        raise GraphValidationError("GROUP_KERNEL_REFUSED") from None
