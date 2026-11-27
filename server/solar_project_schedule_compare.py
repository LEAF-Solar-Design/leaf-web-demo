"""Compare saved string assignments or cable lengths from verified schedule artifacts."""
from solar_design_graph import GraphValidationError
from solar_project_artifacts import read_project_artifact_with_source
from solar_inverter_outputs import cs_fixed, cs_round, polyline_length

SCHEMA = "leaf.solar-project-schedule-comparison.v1"
CABLE_SCHEMA = "leaf.solar-project-cable-length-comparison.v1"
_CABLE_PAGE_SIZE = 250
_CABLE_KINDS = {"start homerun", "end homerun", "feeder"}
_METRES_PER_INCH = 0.0254
_RECEIPT_FIELDS = ("artifact_id", "source_version_id", "source_intake_sha256",
                   "graph_sha256", "content_sha256")


def _load_schedule(tenant, project_id, drawing_id, version_id, artifact_id):
    meta, _, context = read_project_artifact_with_source(tenant, project_id, drawing_id, artifact_id)
    if meta["source_version_id"] != str(version_id):
        raise GraphValidationError("SCHEDULE_COMPARISON_VERSION_MISMATCH")
    if (meta["tool"], meta["media_type"]) != ("solar-electrical-schedules", "application/json"):
        raise GraphValidationError("SCHEDULE_COMPARISON_ARTIFACT_KIND")
    return {key: meta[key] for key in _RECEIPT_FIELDS}, context.graph


def _string_rows(graph):
    inverters = {item["id"]: item for item in graph["inverters"]}
    slots = {slot["string_ref"]: slot for inverter in inverters.values()
             for slot in inverter["input_assignments"]}
    rows = {}
    for string in graph["strings"]:
        string_id = string["id"]
        collector_id = string["inverter_ref"]
        collector = inverters.get(collector_id, {})
        slot = slots.get(string_id, {})
        rows[string_id] = {
            "circuit_label": string["circuit_tag"],
            "module_count": string["module_count"],
            "collector_id": collector_id,
            "mppt_letter": slot.get("mppt_letter"),
            "input_number": slot.get("input_number"),
            "upstream_inverter_id": collector.get("l2_ref"),
        }
    return rows


def _cable_rows(graph):
    rows = {}
    for route in graph["routes"]:
        kind = route["route_kind"]
        if kind not in _CABLE_KINDS:
            continue
        points = [(x / _METRES_PER_INCH, y / _METRES_PER_INCH) for x, y in route["points"]]
        feet = polyline_length(points) / 12
        # Use the precision that users see in the feeder schedule and homerun workbook.
        length = cs_round(feet, 1) if kind == "feeder" else float(cs_fixed(feet, 2))
        rows[route["id"]] = {"route_kind": kind, "from_ref": route["from_ref"],
                             "to_ref": route["to_ref"], "length_ft": length}
    return rows


def _compare_cable_lengths(before, after, before_graph, after_graph, offset):
    old_rows, new_rows = _cable_rows(before_graph), _cable_rows(after_graph)
    changes = []
    for route_id in sorted(old_rows.keys() | new_rows.keys()):
        old, new = old_rows.get(route_id), new_rows.get(route_id)
        if old is None:
            change = "added"
        elif new is None:
            change = "removed"
        elif old["length_ft"] != new["length_ft"]:
            change = "changed"
        else:
            continue
        changes.append({"route_id": route_id, "change": change, "before": old, "after": new})
    end = offset + _CABLE_PAGE_SIZE
    return {
        "schema": CABLE_SCHEMA, "before": before, "after": after,
        "offset": offset, "next_offset": end if end < len(changes) else None,
        "total_changes": len(changes),
        "changes": changes[offset:end],
    }


def compare_saved_schedules(tenant, project_id, drawing_id, *, before_version_id,
                            after_version_id, before_artifact_id, after_artifact_id,
                            cable_offset=None):
    """Compare stable string IDs by default, or return a page of cable length changes."""
    before, before_graph = _load_schedule(tenant, project_id, drawing_id, before_version_id, before_artifact_id)
    after, after_graph = _load_schedule(tenant, project_id, drawing_id, after_version_id, after_artifact_id)
    if cable_offset is not None:
        if type(cable_offset) is not int or cable_offset < 0:
            raise GraphValidationError("SIP_R6_INVALID_REQUEST")
        return _compare_cable_lengths(before, after, before_graph, after_graph, cable_offset)
    old_rows, new_rows = _string_rows(before_graph), _string_rows(after_graph)
    changes = []
    for string_id in sorted(old_rows.keys() | new_rows.keys()):
        old, new = old_rows.get(string_id), new_rows.get(string_id)
        if old == new:
            continue
        if old is None:
            change = "added"
        elif new is None:
            change = "removed"
        else:
            change = "changed"
        changed_fields = [field for field in old if old[field] != new[field]] if change == "changed" else []
        changes.append({"string_id": string_id, "change": change, "changed_fields": changed_fields,
                        "before": old, "after": new})
    return {"schema": SCHEMA, "before": before, "after": after, "changes": changes}
