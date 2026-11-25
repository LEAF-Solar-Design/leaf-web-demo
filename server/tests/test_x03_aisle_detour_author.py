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


@pytest.mark.parametrize("ask, route, obstacles, expected", [
    ("Show just the part of each cable segment whose corridor falls below clearance, and mark where "
     "the clearance starts and ends along the route. Corridor width is 1 m and side clearance is .25 m.",
     [(-2, 0), (10, 0)],
     [[(2, .5), (4, .5), (4, 1.5), (2, 1.5)]],
     [(1, 2-math.sqrt(5)/4, 4+math.sqrt(5)/4, 2, ["A1"])]),
    ("Mark wire corridor clearance intervals next to keepouts with a 1 m corridor and 0 m side clearance.",
     [(-2, -2), (-2, 0), (12, 0)],
     [[(2, -1), (4, -1), (4, 1), (2, 1)],
      [(4.25, -1), (6, -1), (6, 1), (4.25, 1)],
      [(8, .5), (9, .5), (9, 1.5), (8, 1.5)]],
     [(2, 1.5, 6.5, 4, ["A1", "A2"])]),
    ("Mark the conduit corridor subsegments that encroach on the roof obstacle. Use a 100 cm corridor "
     "and 0 cm side clearance.",
     [(-3, 0), (13, 0)],
     [[(0, -2), (10, -2), (10, 2), (8, 2), (8, -1), (2, -1), (2, 2), (0, 2)]],
     [(1, -.5, 2.5, 3, ["A1"]), (1, 7.5, 10.5, 3, ["A1"])]),
])
def test_corridor_clearance_marks_only_failing_portions(ask, route, obstacles, expected):
    rows = [entity("Roof Keepout", f"A{i:X}", polygon, closed=True)
            for i, polygon in enumerate(obstacles, 1)]
    rows.append(entity("Cable", "B1", route))
    raw = intake_dxf.intake_to_dxf(dict(layers=["Roof Keepout", "Cable"], polylines=rows))
    raw = raw.replace(b"9\n$HANDSEED\n", b"9\n$INSUNITS\n70\n6\n9\n$HANDSEED\n", 1)
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, overlay = output
    assert overlay["highlight_handles"] == []
    assert "violating_segments" in report, report
    spans = report["violating_segments"]["B1"]
    assert len(spans) == len(expected)
    assert len(overlay["markers"]) == 2 * len(expected)
    assert overlay["polylines"] == [{"pts": span["points"]} for span in spans]
    assert report["clearance_violations"] == {"B1": sorted({h for *_, handles in expected for h in handles})}
    for span, (index, start, end, offset, handles) in zip(spans, expected):
        assert (span["source_segment"], span["obstacles"]) == (index, handles)
        assert span["points"][0] == pytest.approx([start, 0])
        assert span["points"][1] == pytest.approx([end, 0])
        assert span["stations"] == pytest.approx([start + offset, end + offset])


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

@pytest.mark.parametrize("ask,departure,arrival", [
    ("Find the fewest cable elbows on equal paths with a north-facing departure and east-facing arrival at equipment.",
     (0, 1), (1, 0)),
    ("Compare equal-length cable routes with fewer bends when departure faces east and arrival faces north.",
     (1, 0), (0, 1)),
    ("Show the fewest turns among shortest conduit routes. Depart heading north and arrive heading east.",
     (0, 1), (1, 0)),
])
def test_equipment_heading_turns(ask, departure, arrival):
    start, end = (0, 0), (8, 4)
    north_first = [start, (0, 4), end]
    east_first = [start, (8, 0), end]
    preferred, saved = (north_first, east_first) if departure == (0, 1) else (east_first, north_first)
    rows = [entity("Aisle", "A1", north_first), entity("Aisle", "A2", east_first),
            entity("Cable", "B1", saved)]
    raw = intake_dxf.intake_to_dxf(dict(layers=["Aisle", "Cable"], polylines=rows))
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, _ = output
    route = report["equal_length_fewer_bends"]["B1"]
    assert route["points"] == [list(point) for point in preferred]
    assert (route["length"], route["drawn_bends"], route["bends"], route["bends_saved"]) == (12, 3, 1, 2)
    assert report["equipment_travel_headings"] == {"departure": list(departure), "arrival": list(arrival)}


@pytest.mark.parametrize("ask", [
    "When two routes have the same total cable length, which one uses fewer elbows after accounting for bends at equipment entries?",
    "Find the fewest cable bends among equal routes. Departure faces east and arrival faces north, but arrival faces west.",
])
def test_equipment_heading_request_without_directions_asks_for_input(ask):
    _, source, _ = tools_fallback.author_tool(ask)
    raw = intake_dxf.intake_to_dxf({"layers": ["Cable"], "polylines": []})
    state, output = invoke(source, raw)
    assert state == "ok", output
    assert output[0]["status"] == "needs_equipment_headings"

@pytest.mark.parametrize("ask,saved_side,expected_ports", [
    ("Compare equivalent cable routes by equipment ports. Departure ports: N1 north, E1 east; Arrival ports: E2 east, S2 south.",
     "east", {"departure": "N1", "arrival": "E2"}),
    ("Find the fewest conduit elbows among shortest routes using equipment ports. Arrival ports: N3=north or S3=south; Departure ports: N4=north or E4=east.",
     "north", {"departure": "E4", "arrival": "N3"}),
    ("Choose ports for the shortest wire paths. Departure ports: North-7 north, East-8 east; Arrival ports: E-9 east, W-9 west.",
     "east", {"departure": "North-7", "arrival": "E-9"}),
])
def test_named_equipment_port_options(ask, saved_side, expected_ports):
    start, end = (0, 0), (8, 4)
    north = [start, (0, 4), end]
    east = [start, (8, 0), end]
    saved, preferred = (east, north) if saved_side == "east" else (north, east)
    rows = [entity("Aisle", "A1", north), entity("Aisle", "A2", east),
            entity("Cable", "B1", saved)]
    raw = intake_dxf.intake_to_dxf(dict(layers=["Aisle", "Cable"], polylines=rows))
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, _ = output
    route = report["port_choices"]["B1"]
    assert route["points"] == [list(point) for point in preferred]
    assert (route["length"], route["bends"], route["ports"]) == (12, 1, expected_ports)


def test_named_equipment_ports_require_both_ends():
    _, source, _ = tools_fallback.author_tool(
        "Use equipment ports on the shortest cable route. Departure ports: A north, B east.")
    raw = intake_dxf.intake_to_dxf({"layers": ["Cable"], "polylines": []})
    state, output = invoke(source, raw)
    assert state == "ok", output
    assert output[0]["status"] == "needs_equipment_ports"


@pytest.mark.parametrize("ask, aisles, expected_points, names", [
    (
        "Choose among equipment ports for the shortest legal route and fewest elbows. "
        "Departure ports: Lower east at (0,0), Upper east at (0,4); "
        "Arrival ports: Right east at (8,0), UpperRight east at (8,4).",
        [[(0, 0), (0, 4), (8, 4), (8, 0)], [(0, 0), (0, -2), (8, -2), (8, 0)]],
        [(0, 4), (8, 4)], ("Upper", "UpperRight"),
    ),
    (
        "Find the fewest conduit elbows among shortest routes using equipment ports. "
        "Arrival ports: A north at (8,0) or B east at (8,4); "
        "Departure ports: C east at (0,0) or D east at (0,4).",
        [[(0, 0), (8, 0)], [(0, 4), (8, 4)]],
        [(0, 4), (8, 4)], ("D", "B"),
    ),
    (
        "Choose ports for the shortest wire paths. "
        "Departure ports: Off east at (-3.5,1.25), On=east at (-3.5,-.1); "
        "Arrival ports: Stop=east at (+4.5,-.1), OffEnd east at (4.5,1.25).",
        [[(-5, -.1), (6, -.1)]],
        [(-3.5, -.1), (4.5, -.1)], ("On", "Stop"),
    ),
])
def test_equipment_ports_at_distinct_locations(ask, aisles, expected_points, names):
    rows = [entity("Aisle", f"C{i:X}", path) for i, path in enumerate(aisles)]
    rows.append(entity("Cable", "B1", aisles[0]))
    raw = intake_dxf.intake_to_dxf(dict(layers=["Aisle", "Cable"], polylines=rows))
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, overlay = output
    route = report["port_choices"]["B1"]
    points = [list(point) for point in expected_points]
    assert (route["length"], route["bends"], route["points"]) == (8, 0, points)
    assert route["ports"] == dict(zip(("departure", "arrival"), names))
    assert overlay["polylines"] == [{"pts": points}]
    assert report["avoidable_detours"] == {}


@pytest.mark.parametrize("ask, expected", [
    ("Pick the best physical ports for each of my two cables and label both chosen routes. "
     "Cable B1 departure ports: Left east at (0,0), Upper east at (0,4); "
     "Cable B2 arrival ports: Right east at (12,0); Cable B1 arrival ports: Right east at (8,0); "
     "Cable B2 departure ports: Left east at (4,0).",
     {"B1": ("Left", "Right", 0, 0, 8), "B2": ("Left", "Right", 4, 0, 12)}),
    ("Choose ports for the shortest wire paths. Wire b2 arrival ports: Stop=east at (12,4); "
     "Wire b1 arrival ports: End=east at (12,0); Wire b2 departure ports: Start=east at (4,4); "
     "Wire b1 departure ports: Off east at (4,1) or Start east at (4,0).",
     {"B1": ("Start", "End", 4, 0, 12), "B2": ("Start", "Stop", 4, 4, 12)}),
    ("Find the fewest conduit elbows among shortest routes with equipment ports. "
     "Conduit B2 departure ports: A east, Wrong north; Conduit B1 arrival ports: D east at (8,4); "
     "Conduit B2 arrival ports: B east; Conduit B1 departure ports: C east at (0,4).",
     {"B1": ("C", "D", 0, 4, 8), "B2": ("A", "B", 0, 4, 12)}),
])
def test_equipment_ports_bind_to_each_saved_cable(ask, expected):
    rows = [
        entity("Aisle", "C1", [(0, 0), (12, 0)]),
        entity("Aisle", "C2", [(0, 4), (12, 4)]),
        entity("Cable", "B2", [(0, 4), (12, 4)]),
        entity("Cable", "B1", [(0, 0), (12, 0)]),
    ]
    raw = intake_dxf.intake_to_dxf(dict(layers=["Aisle", "Cable"], polylines=rows))
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, overlay = output
    assert report["port_choices"].keys() == expected.keys()
    assert report["unavailable"] == {}
    for handle, (departure, arrival, start, y, end) in expected.items():
        route = report["port_choices"][handle]
        assert route["ports"] == {"departure": departure, "arrival": arrival}
        assert (route["length"], route["bends"]) == (end - start, 0)
        assert route["points"][0] == [start, y]
        assert route["points"][-1] == [end, y]
        assert all(point[1] == y for point in route["points"])
        assert {"pt": [start, y], "label": f"{handle}: {departure} to {arrival}"} in overlay["markers"]


@pytest.mark.parametrize("ask, declarations, expected", [
    ("Choose ports jointly for two cables and find the shortest legal combined routing.",
     ("Cable B1 departure ports: A east at (0,0) or X east at (-4,0); "
      "Cable B1 arrival ports: B east at (8,0); "
      "Cable B2 departure ports: Alias east at (0,0) or C east at (-2,0); "
      "Cable B2 arrival ports: D east at (10,0)."), (20, "A", "C")),
    ("Find the best combined route for these wires and allow explicit shareable ports.",
     ("Wire B1 departure ports: A east at (0,0); Wire B1 arrival ports: B east at (8,0); "
      "Wire B2 departure ports: Alias east at (0,0); Wire B2 arrival ports: D east at (10,0); "
      "Shareable ports: a."), (18, "A", "Alias")),
    ("Find shortest joint conduit routes with no use of the same physical port.",
     ("Conduit B1 departure ports: Upstream east at (0,0); Conduit B1 arrival ports: Shared east at (8,0); "
      "Conduit B2 departure ports: Other east at (8,0); Conduit B2 arrival ports: D east at (10,0)."), None),
], ids=["least-cost-exclusive", "explicit-sharing", "infeasible-cross-role"])
def test_joint_equipment_port_assignments(ask, declarations, expected):
    rows = [entity("Aisle", "C1", [(-4, 0), (10, 0)]),
            entity("Cable", "B1", [(0, 0), (8, 0)]),
            entity("Cable", "B2", [(0, 0), (10, 0)])]
    raw = intake_dxf.intake_to_dxf(dict(layers=["Aisle", "Cable"], polylines=rows))
    spec, source, _ = tools_fallback.author_tool(f"{ask} {declarations}")
    assert spec["engine_op"] == "aisle_detour"
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, overlay = output
    if expected is None:
        assert report["status"] == "no_joint_port_assignment"
        assert report["port_choices"] == {}
        assert overlay == {"highlight_handles": [], "markers": [], "polylines": []}
        return
    length, first, second = expected
    assert report["status"] == "found", report
    assert (report["combined_length"], report["combined_bends"]) == (length, 0)
    choices = report["port_choices"]
    assert choices["B1"]["ports"] == {"departure": first, "arrival": "B"}
    assert choices["B2"]["ports"] == {"departure": second, "arrival": "D"}
    assert overlay["polylines"] == [{"pts": choices[h]["points"]} for h in ("B1", "B2")]
    assert [m["label"] for m in overlay["markers"]] == [f"B1: {first} to B", f"B2: {second} to D"]


@pytest.mark.parametrize("description,cables,status", [
    ("Choose equipment ports in different physical locations for the shortest legal route and fewest elbows.",
     1, "needs_equipment_ports"),
    ("Choose the shortest cable route. Departure ports: A east at (0,0); Arrival ports: B east.",
     1, "needs_equipment_ports"),
    ("Choose the shortest cable route. Departure ports: A east at (0,0); Arrival ports: B east at (8,0).",
     2, "needs_single_cable_route"),
    ("Choose the shortest cable route. Departure ports: A east at (0,0); Arrival ports: B east at (0,0).",
     1, "needs_distinct_equipment_port_locations"),
    ("Choose the shortest cable route. Cable F1 departure ports: A east; Cable F1 arrival ports: B east.",
     2, "unknown_cable_port_handles"),
    ("Choose the shortest cable route. Cable B1 departure ports: A east; Arrival ports: B east.",
     2, "needs_equipment_ports"),
])
def test_equipment_port_locations_require_unambiguous_inputs(description, cables, status):
    rows = [entity("Cable", f"B{i:X}", [(0, i), (8, i)]) for i in range(cables)]
    raw = intake_dxf.intake_to_dxf(dict(layers=["Cable"], polylines=rows))
    _, source, _ = tools_fallback.author_tool(description)
    state, output = invoke(source, raw)
    assert state == "ok", output
    assert output[0]["status"] == status


@pytest.mark.parametrize("ask, units, scale, side", [
    ("Show where the outside edge of my cable corridor comes too close to a roof obstacle. "
     "Corridor width is 1 m and side clearance is .25 m.", 6, 1, 1),
    ("Check wire corridor clearance next to keepouts using a 1000 mm corridor and 25 cm clearance.",
     4, 1000, 1),
    ("Mark conduit corridors that encroach on panel footprints. Specify a 12 in wide corridor "
     "with clearance of 3 in per side.", 1, 12, -1),
])
def test_corridor_edge_clearance_without_an_aisle(ask, units, scale, side):
    obstacle = [(0, 0), (4 * scale, 0), (4 * scale, -2 * scale * side), (0, -2 * scale * side)]
    rows = [entity("Roof Keepout", "A1", obstacle, True)]
    for index, distance in enumerate((.5, .75, 1), start=1):
        y = distance * scale * side
        rows.append(entity("Cable", f"B{index}", [(-5 * scale, y), (-scale, y), (5 * scale, y)]))
    raw = intake_dxf.intake_to_dxf(dict(layers=["Roof Keepout", "Cable"], polylines=rows))
    raw = raw.replace(b"9\n$HANDSEED\n", f"9\n$INSUNITS\n70\n{units}\n9\n$HANDSEED\n".encode(), 1)
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, raw)
    assert state == "ok", output
    report, overlay = output
    edge = [[-scale, .5 * scale * side], [5 * scale, .5 * scale * side]]
    assert report["swept_half_width"] == .75 * scale
    assert report["clearance_violations"] == {"B1": ["A1"]}
    assert report["violating_segments"] == {"B1": [{"points": edge, "obstacles": ["A1"]}]}
    assert overlay["highlight_handles"] == ["B1"]
    assert overlay["polylines"] == [{"pts": edge}]


@pytest.mark.parametrize("suffix, status", [
    ("", "needs_corridor_clearance"),
    (" Corridor width is 1 m and side clearance is .25 m.", "needs_drawing_units"),
])
def test_corridor_clearance_requests_required_inputs(suffix, status):
    raw = intake_dxf.intake_to_dxf(dict(layers=["Cable"], polylines=[]))
    _, source, _ = tools_fallback.author_tool(
        "Show where the outside edge of my cable corridor comes too close to a roof obstacle." + suffix)
    state, output = invoke(source, raw)
    assert state == "ok", output
    assert output[0]["status"] == status


def clearance_drawing(units, scale=1, closed_aisle=False):
    upper = [START, (-5, 2), (5, 2), END]
    lower = [START, (-5, -4), (5, -4), END]
    rows = [entity("Roof Keepout", "A1", [(-1, -1), (1, -1), (1, 1), (-1, 1)], True),
            entity("Aisle", "C1", upper), entity("Aisle", "C2", lower),
            entity("Tray", "B1", lower)]
    if closed_aisle:
        rows.append(entity("Aisle", "C3", [(-7, -5), (7, -5), (7, 5), (-7, 5)], True))
    for row in rows:
        row["pts"] = [[x*scale, y*scale, z] for x, y, z in row["pts"]]
    raw = intake_dxf.intake_to_dxf(dict(layers=sorted({e["layer"] for e in rows}), polylines=rows))
    if units is None:
        return raw
    # The intake writer synthesizes a HEADER without $INSUNITS.
    return raw.replace(b"9\n$HANDSEED\n", f"9\n$INSUNITS\n70\n{units}\n9\n$HANDSEED\n".encode(), 1)


@pytest.mark.parametrize("ask,units,scale,length,gap,violation", [
    ("Which cable routes can I shorten around keepouts with tray width 1 m and side clearance 0.25 m?",
     6, 1, 14, 0.75, False),
    ("Find the shortest aisle paths for a 2000 mm wide tray with 25 centimetres of side clearance.",
     4, 1000, 18, 1.25, False),
    ("Check conduit runs around obstacles for a 6 metre tray. Clearance: 250 mm each side.",
     6, 1, None, 3.25, True),
    ("Find shorter tray routes. Tray width is 1.5 m and clearance is .25 m on each side.",
     6, 1, 14, 1, False),
])
def test_shortest_swept_tray_route(ask, units, scale, length, gap, violation):
    _, source, _ = tools_fallback.author_tool(ask)
    state, output = invoke(source, clearance_drawing(units, scale))
    assert state == "ok", output
    report, overlay = output
    assert report["swept_half_width"] == gap * scale
    assert report["crossed_obstacles"] == {}
    assert report["clearance_violations"] == ({"B1": ["A1"]} if violation else {})
    if length is None:
        assert report["unavailable"] == {"B1": "no_legal_detour"}
        assert report["shortest_legal_routes"] == {}
    else:
        assert report["unavailable"] == {}
        assert report["shortest_legal_routes"]["B1"]["length"] == length * scale
    assert overlay["highlight_handles"] == (["B1"] if length == 14 else [])


@pytest.mark.parametrize("details,units,closed_aisle,status", [
    ("tray width 2 m", 6, False, "needs_tray_clearance"),
    ("tray width 1 m and tray width 2 m with clearance 1 in", 6, False, "needs_tray_clearance"),
    ("tray width 2 ft and clearance 6 inches", None, False, "needs_drawing_units"),
    ("tray width 2 ft and clearance 6 inches", 6, True, "needs_aisle_centerlines"),
])
def test_swept_tray_route_requires_geometry_and_dimensions(details, units, closed_aisle, status):
    _, source, _ = tools_fallback.author_tool(f"Find shortest cable routes with {details}.")
    state, output = invoke(source, clearance_drawing(units, closed_aisle=closed_aisle))
    assert state == "ok", output
    assert output[0]["status"] == status
