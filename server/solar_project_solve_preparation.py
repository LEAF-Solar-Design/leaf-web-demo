"""Read-only frame requests from an authenticated, verified drawing context.

No grant resolution, network operation or publication happens during preparation.
Admission still binds and checks the complete request before the broker runs it.
"""
from solar_design_graph import GraphValidationError
from solar_project_graph import _companion
from solar_solve_request import build_stringer_request


def prepare_solve(intake, frame_ref, *, is_head):
    base = {"schema": "leaf.project-solve-preparation.v1", "available": False}
    if not is_head:
        return {**base, "reason": "STALE_VERSION"}
    try:
        graph = _companion(intake)
        if graph is None:
            return {**base, "reason": "GRAPH_REQUIRED"}
        choices = [{"id": frame["id"], "name": frame["name"],
                    "panel_count": len(frame["panel_refs"])} for frame in graph["frames"]]
        result = {**base, "available": True, "graph_revision": graph["rev"],
                  "graph_sha256": intake["solar_design_graph_sha256"], "frames": choices}
        if frame_ref is None:
            return result
        request = build_stringer_request(graph, frame_ref,
            max_string_length=graph["settings"]["panels_in_sequence"],
            dwgname=graph["project"]["name"], full_frame=True)
        return {**result, "frame_ref": frame_ref, "request": request}
    except GraphValidationError as exc:
        return {**base, "reason": exc.code}
