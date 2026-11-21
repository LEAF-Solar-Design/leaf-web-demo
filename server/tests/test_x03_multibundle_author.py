"""Verify the offline author against exhaustive assignments on small physical tray networks."""
from itertools import product
import math

import pytest

from test_x03_offline_drawing_author import intake_dxf, invoke, tools_fallback
from test_x03_route_author_product import entity


ASKS = (
    "Route the cable bundles on shared trays within each tray's capacity, minimizing total distance.",
    "Reroute three cable bundles through the same aisles without exceeding their separate tray capacities.",
    "Allocate all bundles to the tray network with minimum total length or report infeasibility.",
)

A, B, C = (0, 0), (6, 0), (10, 0)
TRAYS = {
    "C1": [A, B], "C2": [A, (0, 2), (6, 2), B], "C3": [B, C],
    "C4": [A, (0, -5), (10, -5), C],
}


def routes(paths, start, end):
    # Each test tray has terminals at its two ends. Recover all legal simple routes.
    if start == end:
        yield ()
        return
    for handle, points in paths.items():
        if start in (points[0], points[-1]):
            other = points[-1] if start == points[0] else points[0]
            rest = {h: pts for h, pts in paths.items() if start not in (pts[0], pts[-1])}
            for tail in routes(rest, other, end):
                yield (handle,) + tail


@pytest.mark.parametrize("case", range(3), ids=["global-choice", "shared-trunk", "infeasible"])
def test_assign_all_bundles(case):
    capacities, requests = [
        ([2, 1, 3, 1], [(A, B, 1), (A, C, 2)]),
        ([3, 2, 3, 1], [(C, A, 1), (A, C, 2), (B, A, 2)]),
        ([2, 1, 2, 1], [(A, C, 2), (A, C, 2)]),
    ][case]
    limits = dict(zip(TRAYS, capacities))
    entities = [entity(f"Tray cap {limits[h]}", h, pts) for h, pts in TRAYS.items()]
    bundles = {f"B{i}": request for i, request in enumerate(requests, 1)}
    entities.extend(entity(f"Bundle load {load}", h, [a, b]) for h, (a, b, load) in bundles.items())
    entities.append(dict(entity("Tray cap 99", "CC", [A, C]), space="paper"))
    raw = intake_dxf.intake_to_dxf(dict(layers=sorted({e["layer"] for e in entities}), polylines=entities))
    _, source, _ = tools_fallback.author_tool(ASKS[case])
    status, payload = invoke(source, raw)
    assert status == "ok", payload
    result, overlay = payload

    lengths = {h: sum(math.dist(a, b) for a, b in zip(p, p[1:])) for h, p in TRAYS.items()}
    feasible = []
    for assignment in product(*(routes(TRAYS, a, b) for a, b, _ in requests)):
        loads = {h: sum(r[2] for r, route in zip(requests, assignment) if h in route) for h in TRAYS}
        if all(loads[h] <= limits[h] for h in TRAYS):
            feasible.append((sum(lengths[h] for route in assignment for h in route), assignment, loads))
    if not feasible:
        assert result.get("status") == "infeasible", result
        assert result["routes"] == {} and overlay["polylines"] == []
        return
    assert result.get("status") == "optimal", result
    minimum = min(row[0] for row in feasible)
    assert result["total_length"] == pytest.approx(minimum)
    actual = tuple(tuple(result["routes"][h]["trays"]) for h in bundles)
    assert (minimum, actual, result["tray_loads"]) in feasible
    expected_shapes = []
    for handle, (start, end, _) in bundles.items():
        points = [start]
        route = result["routes"][handle]
        for tray in route["trays"]:
            draw = TRAYS[tray]
            if points[-1] != draw[0]:
                draw = list(reversed(draw))
            assert points[-1] == draw[0]
            points.extend(draw[1:])
        assert points[-1] == end
        assert route["points"] == [list(p) for p in points]
        expected_shapes.append(route["points"])
    assert sorted(shape["pts"] for shape in overlay["polylines"]) == sorted(expected_shapes)
