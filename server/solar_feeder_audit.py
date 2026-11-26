"""Plan-view footprint evidence for feeders on an exact saved rooftop drawing."""

from math import cos, isfinite, sin

import solar_panel_group_kernel as panels
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import units_resolved


MAX_PANEL_SEGMENT_PAIRS = 2_000_000


def _panel_xy(panel, metres_per_unit):
    x, y = panel["centre"]
    scale = metres_per_unit
    along = cos(panel["angle"]), sin(panel["angle"])
    return (x * scale, y * scale), along, (
        panel["column_dim"] * scale / 2, panel["row_dim"] * scale / 2
    )


def _intersection(a, b, frame):
    centre, (cosine, sine), extents = frame

    def local(point):
        dx, dy = point[0] - centre[0], point[1] - centre[1]
        return dx * cosine + dy * sine, -dx * sine + dy * cosine

    start, end = local(a), local(b)
    lo, hi = 0.0, 1.0
    for x, y, extent in zip(start, end, extents):
        if max(x, y) < -extent or min(x, y) > extent:
            return None
        if x == y:
            continue
        near, far = sorted(((-extent - x) / (y - x), (extent - x) / (y - x)))
        lo, hi = max(lo, near), min(hi, far)
        if lo > hi:
            return None
    return lo, hi


def _crosses(points, frame):
    intervals = [_intersection(a, b, frame) for a, b in zip(points, points[1:])]
    first, last = 0, len(intervals)
    if intervals and all(interval == (0, 1) for interval in intervals):
        return True
    # Omit only the connected part of a footprint intersection at a terminal.
    # A later entry into the same footprint is a separate hit.
    while first < last and intervals[first] is not None and intervals[first][0] == 0:
        first += 1
        if intervals[first - 1][1] != 1:
            break
    if first and intervals[-1] is not None and intervals[-1][1] == 1:
        return True
    while first < last and intervals[last - 1] is not None and intervals[last - 1][1] == 1:
        last -= 1
        if intervals[last][0] != 0:
            break
    return any(interval is not None for interval in intervals[first:last])


def audit_feeders(graph, source_intake):
    """Report panel crossings only. Roof and obstruction coverage is unverified."""
    graph = validate_graph(graph)
    if graph["project"]["installation_design"] != "Roof":
        raise GraphValidationError("FEEDER_AUDIT_ROOF_REQUIRED")
    if not units_resolved(graph):
        raise GraphValidationError("FEEDER_AUDIT_UNITS_UNRESOLVED")
    layer = graph["settings"].get("panel_layer_contains")
    if not isinstance(layer, str) or not layer.strip() or type(source_intake) is not dict:
        raise GraphValidationError("FEEDER_AUDIT_PANEL_SOURCE_REQUIRED")
    try:
        recognized = panels.panels_from_intake(source_intake, layer_contains=layer)
    except panels.PanelGroupKernelError as exc:
        raise GraphValidationError("FEEDER_AUDIT_INVALID_PANEL_SOURCE") from exc
    if not recognized:
        raise GraphValidationError("FEEDER_AUDIT_PANEL_SOURCE_REQUIRED")
    mpu = graph["project"]["units"]["meters_per_unit"]
    footprints = [(p["handle"], _panel_xy(p, mpu)) for p in recognized]
    if any(not all(isfinite(value) for pair in frame for value in pair)
           or min(frame[2]) <= 0 for _, frame in footprints):
        raise GraphValidationError("FEEDER_AUDIT_INVALID_PANEL_SOURCE")

    routes = [route for route in graph["routes"] if route["route_kind"] == "feeder"]
    if sum(len(route["points"]) - 1 for route in routes) * len(footprints) > MAX_PANEL_SEGMENT_PAIRS:
        raise GraphValidationError("FEEDER_AUDIT_LIMIT_EXCEEDED")
    results = []
    for route in routes:
        points = [tuple(point[:2]) for point in route["points"]]
        # Remove repeated plan-view positions before checking terminal continuity.
        points = [point for index, point in enumerate(points)
                  if index == 0 or points[index - 1] != point]
        if len(points) < 2:
            raise GraphValidationError("FEEDER_AUDIT_INVALID_PLAN_VIEW")
        hits = sorted(handle for handle, frame in footprints if _crosses(points, frame))
        results.append({"route_ref": route["id"], "panel_handles_crossed": hits})
    return {
        "roof_boundary": "unverified",
        "obstructions": "unverified",
        "recognized_panels": len(footprints),
        "feeders": results,
    }
