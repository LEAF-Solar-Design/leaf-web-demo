"""Compare saved string assignments from two verified schedule artifacts."""
from solar_design_graph import GraphValidationError
from solar_project_artifacts import read_project_artifact_with_source

SCHEMA = "leaf.solar-project-schedule-comparison.v1"
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


def compare_saved_schedules(tenant, project_id, drawing_id, *, before_version_id,
                            after_version_id, before_artifact_id, after_artifact_id):
    """Compare the assignments in the artifacts' saved sources by stable string ID."""
    before, before_graph = _load_schedule(tenant, project_id, drawing_id, before_version_id, before_artifact_id)
    after, after_graph = _load_schedule(tenant, project_id, drawing_id, after_version_id, after_artifact_id)
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
