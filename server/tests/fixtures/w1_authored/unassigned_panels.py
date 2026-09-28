"""Unassigned-panel selector: grouped panels that no string carries, in stored order.

Authored tool source as the authoring harness returns it. Declares graph_input
'solar-w1-graph', so its intake is leaf.solar-graph-intake.v1. Standard library only.
"""


def run(intake, params):
    graph = intake["graph"]
    strung = set()
    for string in graph["strings"]:
        strung.update(string["ordered_panel_refs"])
    selected = [panel for panel in graph["panels"]
                if panel.get("frame_ref") is not None and panel["id"] not in strung]
    return {
        "report": "unassigned-panels",
        "drawing_id": intake["drawing_id"],
        "source_version": intake["source_version"],
        "graph_sha256": intake["graph_sha256"],
        "panel_refs": [panel["id"] for panel in selected],
        "handles": [(panel.get("provenance") or {}).get("source_handle") for panel in selected],
        "count": len(selected),
    }, None
