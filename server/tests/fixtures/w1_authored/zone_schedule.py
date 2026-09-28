"""Zone schedule: panel and string counts per electrical zone of the pinned W1 graph.

Authored tool source as the authoring harness returns it. Declares graph_input
'solar-w1-graph', so its intake is leaf.solar-graph-intake.v1. Standard library only.
"""


def run(intake, params):
    graph = intake["graph"]
    string_members = [set(string["ordered_panel_refs"]) for string in graph["strings"]]
    zones = []
    for zone in graph["electrical_zones"]:
        members = set(zone["panel_refs"])
        zones.append({
            "zone": zone["name"],
            "panels": len(zone["panel_refs"]),
            "strings": sum(1 for panels in string_members if panels & members),
        })
    return {
        "report": "zone-schedule",
        "drawing_id": intake["drawing_id"],
        "source_version": intake["source_version"],
        "graph_sha256": intake["graph_sha256"],
        "zones": zones,
    }, None
