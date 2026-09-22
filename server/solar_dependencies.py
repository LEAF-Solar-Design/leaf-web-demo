"""Pure dependency calculations shared by the existing solar execution rails."""
from __future__ import annotations

try:
    from .solar_design_graph import GraphValidationError, entities, validate_graph
except ImportError:
    from solar_design_graph import GraphValidationError, entities, validate_graph


def membership_changes(before: dict, after: dict) -> dict:
    """Report both sides of a transfer, including ordered membership and counts."""
    before = validate_graph(before)
    after = validate_graph(after)
    if before["project"]["id"] != after["project"]["id"]:
        raise GraphValidationError("PROJECT_ID_MISMATCH")
    old = {item["id"]: item for item in before["strings"]}
    new = {item["id"]: item for item in after["strings"]}
    changes = {}
    for entity_id in sorted(old.keys() | new.keys()):
        previous = old.get(entity_id)
        current = new.get(entity_id)
        previous_members = previous["ordered_panel_refs"] if previous else []
        current_members = current["ordered_panel_refs"] if current else []
        if previous is None or current is None or previous_members != current_members:
            changes[entity_id] = {
                "before": previous_members,
                "after": current_members,
                "before_count": len(previous_members),
                "after_count": len(current_members),
            }
    return changes


def dependency_index(graph: dict) -> dict[str, set[str]]:
    """Map each source id to derived consumers; no persistence or execution."""
    graph = validate_graph(graph)
    index = {entity["id"]: set() for entity in entities(graph)}

    def edge(source, target):
        if source is not None and source != target:
            index.setdefault(source, set()).add(target)

    project_id = graph["project"]["id"]
    settings_id = graph["settings"]["id"]
    for entity in entities(graph):
        edge(project_id, entity["id"])
        if entity["kind"] not in ("project", "settings"):
            edge(settings_id, entity["id"])
    for frame in graph["frames"]:
        edge(frame["electrical_zone_ref"], frame["id"])
        for panel_id in frame["panel_refs"]:
            edge(frame["id"], panel_id)
    for zone in graph["electrical_zones"]:
        for panel_id in zone["panel_refs"]:
            edge(zone["id"], panel_id)
    for string in graph["strings"]:
        for panel_id in string["ordered_panel_refs"]:
            edge(panel_id, string["id"])
        if string["inverter_ref"] is not None:
            edge(string["id"], string["inverter_ref"])
    for route in graph["routes"]:
        edge(route["from_ref"], route["id"])
        edge(route["to_ref"], route["id"])
    for schedule in graph["schedules"]:
        for source in schedule["source_refs"]:
            edge(source, schedule["id"])
    return index


def affected_entities(before: dict, after: dict, changed_ids: list[str]) -> list[str]:
    """Transitive affected ids using both graphs, so removed edges still count."""
    if type(changed_ids) is not list or len(changed_ids) > 100000 or any(type(item) is not str for item in changed_ids):
        raise GraphValidationError("INVALID_CHANGED_IDS")
    old = dependency_index(before)
    new = dependency_index(after)
    changed_memberships = membership_changes(before, after)
    if any(item not in old and item not in new for item in changed_ids):
        raise GraphValidationError("UNKNOWN_CHANGED_ID")
    affected = set(changed_ids) | set(changed_memberships)
    pending = list(affected)
    while pending:
        current = pending.pop()
        for dependent in old.get(current, set()) | new.get(current, set()):
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return sorted(affected)
