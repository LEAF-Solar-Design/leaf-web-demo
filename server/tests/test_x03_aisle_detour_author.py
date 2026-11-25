"""Exercise the offline author on three real DXF aisle networks."""
import math

import pytest

from test_x03_offline_drawing_author import intake_dxf, invoke, tools_fallback
from test_x03_route_author_product import entity


START, END = (-5, 0), (5, 0)
ASKS = (
    "Find cable crossings through rotated arrays and show the shortest legal aisle detour.",
    "Reroute wires that cut through panel footprints using the shortest access aisle path.",
    "Show the minimum-length aisle detour for conduit runs intersecting angled module arrays.",
)


def simple_paths(edges, start, end):
    # Exhaust every simple path in the independently enumerated legal graph.
    pending = [([start], 0.0)]
    while pending:
        path, length = pending.pop()
        if path[-1] == end:
            yield length, path
            continue
        for a, b in edges:
            if path[-1] in (a, b):
                other = b if a == path[-1] else a
                if other not in path:
                    pending.append((path + [other], length + math.dist(other, path[-1])))


@pytest.mark.parametrize("case", range(3), ids=["competing-centerlines", "aisle-footprint", "disconnected"])
def test_shortest_legal_detour(case):
    top, bottom = (0, 2), (1, -3)
    crossing = ["B1", "9E", "11F"][case]
    entities = [
        entity("PV Array", "A1", [(-2, 0), top, (3, -1), bottom], closed=True),
        entity("DC Cable", crossing, [START, END]),
        entity("Wire", "B2", [(-2, 1.75), (-1.5, 1.75)]),  # inside bbox only
        entity("Wire", "B3", [(-2, 2), (2, 2)]),  # touches the top vertex
        dict(entity("Wire", "B4", [START, END]), space="paper"),
        entity("Notes", "D1", [START, END]),
    ]
    if case == 0:
        # Upper and lower candidates intersect the side aisles in their interiors.
        left, right = (-5, 3), (5, 3)
        low_left, low_right = (-5, -5), (5, -5)
        entities += [
            entity("Aisle", "C1", [low_left, (-5, 4)]),
            entity("Aisle", "C2", [low_right, (5, 4)]),
            entity("Aisle", "C3", [(-6, 3), (6, 3)]),
            entity("Aisle", "C4", [low_left, low_right]),
            entity("Aisle", "C5", [START, END]),  # illegal shortcut
        ]
        edges = [(START, left), (left, right), (right, END),
                 (START, low_left), (low_left, low_right), (low_right, END)]
    elif case == 1:
        entities.append(entity("Access Aisles", "C6", [(-6, -4), (6, -4), (6, 4), (-6, 4)], True))
        entities.append(entity("Conduit", "EE", [END, (1, 0), START]))
        # At these endpoints a taut path around the convex diamond turns at
        # its top or bottom. The outer aisle rectangle contains both paths.
        edges = [(START, top), (top, END), (START, bottom), (bottom, END)]
    else:
        entities += [entity("Aisles", "C7", [START, (-5, 4)]),
                     entity("Aisles", "C8", [(5, 4), END]),
                     dict(entity("Aisles", "C9", [(-5, 4), (5, 4)]), space="paper")]
        edges = [(START, (-5, 4)), ((5, 4), END)]

    raw = intake_dxf.intake_to_dxf(dict(layers=sorted({e["layer"] for e in entities}), polylines=entities))
    _, source, _ = tools_fallback.author_tool(ASKS[case])
    status, payload = invoke(source, raw)
    assert status == "ok", payload
    result, overlay = payload
    expected = [crossing, "EE"] if case == 1 else [crossing]
    assert sorted(result.get("crosses_array") or []) == sorted(expected), result
    assert sorted(overlay["highlight_handles"]) == sorted(expected)
    assert set(result["detours"]) == set(expected)
    detour = result["detours"][crossing]
    alternatives = sorted(simple_paths(edges, START, END))
    if alternatives:
        shortest, path = alternatives[0]
        assert detour["length"] == pytest.approx(shortest)
        assert detour["points"] == [list(pt) for pt in path]
        if case == 1:
            reverse = result["detours"]["EE"]
            assert reverse["length"] == pytest.approx(shortest)
            assert reverse["points"] == list(reversed(detour["points"]))
        assert sorted(poly["pts"] for poly in overlay["polylines"]) == sorted(
            row["points"] for row in result["detours"].values())
    else:
        assert detour["status"] == "no_legal_detour"
        assert overlay["polylines"] == []

@pytest.mark.parametrize("ask", [
    "Show which cable routes take an avoidable detour around the roof obstacles.",
    "Which wire runs are inefficient along my access aisles?",
    "Find a shorter conduit path around the keepouts.",
])
def test_avoidable_legal_detour(ask):
    top, bottom = (0, 2), (0, -4)
    rows = [entity("Roof Obstacles", "A1", [(-1, 0), (0, 1), (1, 0), (0, -1)], closed=True),
            entity("Aisle", "C1", [START, top, END]),
            entity("Aisle", "C2", [START, bottom, END]),
            entity("Cable", "B1", [START, bottom, END]),
            entity("Wire", "B2", [END, top, START]),
            entity("Conduit", "B3", [(20, 20), (25, 20)]),
            entity("Cable", "B4", [START, END])]
    raw = intake_dxf.intake_to_dxf(dict(layers=sorted({e["layer"] for e in rows}), polylines=rows))
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, overlay = output
    assert set(report["avoidable_detours"]) == {"B1"}
    route = report["avoidable_detours"]["B1"]
    assert route["points"] == [list(START), list(top), list(END)]
    assert route["length"] == pytest.approx(2 * math.hypot(5, 2))
    assert route["saving"] == pytest.approx(2 * (math.hypot(5, 4) - math.hypot(5, 2)))
    assert report["crossed_obstacles"] == {"B4": ["A1"]}
    assert report["unavailable"] == {"B3": "no_legal_detour"}
    assert overlay["highlight_handles"] == ["B1"]

@pytest.mark.parametrize("ask", [
    "If two permitted cable paths have the same length, show which bends I can remove.",
    "Find the shortest aisle paths for my conduit runs and minimize turns among ties.",
    "For equal length wire routes, show where I can reduce bends.",
])
def test_equal_length_prefers_fewer_bends(ask):
    simple = [START, (-5, 2), (0, 2), (5, 2), END]
    bent = [START, (-4, 0), (-4, -1), (1, -1), (1, -2), (5, -2), END]
    rows = [entity("Aisle", "C1", simple), entity("Aisle", "C2", bent),
            entity("Cable", "B1", bent), entity("Wire", "B2", [(30, 0), (32, 0)])]
    raw = intake_dxf.intake_to_dxf(dict(layers=sorted({e["layer"] for e in rows}), polylines=rows))
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, overlay = output
    route = report["equal_length_fewer_bends"]["B1"]
    assert route["points"] == [list(p) for p in simple]
    assert route["length"] == 14
    assert (route["bends"], route["drawn_bends"], route["bends_saved"]) == (2, 5, 3)
    assert report["avoidable_detours"] == {}
    assert report["unavailable"] == {"B2": "no_legal_detour"}
    assert overlay["highlight_handles"] == ["B1"]

    # Moving the two-bend route farther away makes the more winding route win.
    rows[0] = entity("Aisle", "C1", [START, (-5, 3), (5, 3), END])
    rows[2] = entity("Cable", "B1", [START, (-5, 3), (5, 3), END])
    raw = intake_dxf.intake_to_dxf(dict(layers=sorted({e["layer"] for e in rows}), polylines=rows))
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, _ = output
    assert report["equal_length_fewer_bends"] == {}
    assert report["avoidable_detours"]["B1"]["points"] == [list(p) for p in bent]
    assert report["avoidable_detours"]["B1"]["saving"] == 2
