"""Read XY clearance from the panel outlines retained with a project version."""

import math
import re

import solar_panel_group_kernel as kernel
from solar_design_graph import GraphValidationError


def _handle(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9A-Fa-f]{1,32}", value):
        return value.upper().lstrip("0") or "0"
    return None


def _straight(entity):
    bulges = entity.get("bulges", [])
    return isinstance(bulges, list) and all(type(value) in (int, float) and value == 0 for value in bulges)


def _outline(kind, entity):
    # The retained block representation omits child bulges.
    if kind != "polylines" or not _straight(entity):
        return None
    raw = entity.get("pts")
    if not isinstance(raw, list) or len(raw) not in (4, 5) or kernel.panel_from_polyline(entity) is None:
        return None
    points = tuple((float(point[0]), float(point[1])) for point in raw)
    if len(points) == 5 and all(kernel.v_close(a, b) for a, b in zip(points[0], points[-1])):
        points = points[:4]
    return points if len(points) == len(set(points)) == 4 else None


def panel_outlines(graph, refs, intake):
    if not isinstance(intake, dict):
        raise GraphValidationError("STRING_EDGE_SOURCE_REQUIRED")
    panels = {panel["id"]: panel for panel in graph["panels"] if panel["id"] in refs}
    handles = {}
    for ref in refs:
        provenance = panels.get(ref, {}).get("provenance", {})
        handle = _handle(provenance.get("source_handle"))
        if (handle is None or handle in handles or provenance.get("created_by") != "solar-panels-from-drawing"
                or provenance.get("source_hash") != graph["source_hash"]):
            raise GraphValidationError("STRING_EDGE_OUTLINE_UNAVAILABLE")
        handles[handle] = ref
    outlines = {}
    scale = graph["project"]["units"]["meters_per_unit"]
    for kind in ("polylines", "inserts"):
        entities = intake.get(kind, [])
        if not isinstance(entities, list):
            raise GraphValidationError("STRING_EDGE_OUTLINE_UNAVAILABLE")
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            ref = handles.get(_handle(entity.get("handle")))
            if ref is None:
                continue
            outline = _outline(kind, entity)
            if outline is None or ref in outlines:
                raise GraphValidationError("STRING_EDGE_OUTLINE_UNAVAILABLE")
            centre = tuple(sum(point[axis] for point in outline) / 4 for axis in (0, 1))
            if math.dist(centre, panels[ref]["centre"]) * scale > 1e-8:
                raise GraphValidationError("STRING_EDGE_OUTLINE_UNAVAILABLE")
            outlines[ref] = outline
    if len(outlines) != len(refs):
        raise GraphValidationError("STRING_EDGE_OUTLINE_UNAVAILABLE")
    return outlines


def _edges(points):
    return tuple(zip(points, (*points[1:], points[0])))


def distance(left, right):
    """Minimum clearance between two convex outlines, in drawing units."""
    x, y = left[0]
    left = tuple((a - x, b - y) for a, b in left)
    right = tuple((a - x, b - y) for a, b in right)
    left_edges, right_edges = _edges(left), _edges(right)
    for a, b in (*left_edges, *right_edges):
        nx, ny = a[1] - b[1], b[0] - a[0]
        projection_left = [px * nx + py * ny for px, py in left]
        projection_right = [px * nx + py * ny for px, py in right]
        if max(projection_left) < min(projection_right) or max(projection_right) < min(projection_left):
            return min(kernel.segment_distance(point, start, end)
                       for points, edges in ((left, right_edges), (right, left_edges))
                       for point in points for start, end in edges)
    return 0.0
