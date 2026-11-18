import pytest
from benches.x03_quality import inverter_quality, route_quality, string_quality


@pytest.mark.parametrize("bounds,obstacles,clear,crossing", [
    ((0,0,8,3), [(3,1,5,2)], [(0,.5),(8,.5)], [(0,1.5),(8,1.5)]),
    ((0,0,3,9), [(1,2,2,4),(1,6,2,8)], [(.5,0),(.5,9)], [(1.5,0),(1.5,9)]),
    ((0,0,6,6), [(2,2,4,4)], [(0,0),(1,5),(6,6)], [(0,0),(6,6)])])
def test_route_stays_in_corridor_and_outside_array(bounds, obstacles, clear, crossing):
    from shapely.geometry import LineString, box
    aisle = box(*bounds)
    arrays = [box(*bounds) for bounds in obstacles]
    assert route_quality(LineString(clear), aisle, arrays) == "PASS"
    assert route_quality(LineString(crossing), aisle, arrays) == "FAIL"
    assert route_quality(LineString([(-1,-1), clear[0]]), aisle, arrays) == "FAIL"
    assert route_quality(LineString(clear), aisle, None) == "INCOMPLETE"


@pytest.mark.parametrize("capacity,good,bad", [
    ([4,4], [2,2], [4,0]), ([4,2], [2,1], [3,0]), ([2,2,2], [1,1,1], [2,1,0])])
def test_group_quality(capacity, good, bad):
    strings = list(range(sum(good)))
    def groups(loads):
        return [strings[sum(loads[:i]):sum(loads[:i+1])] for i in range(len(loads))]
    assert inverter_quality(strings, capacity, groups(good)) == "PASS"
    assert inverter_quality(strings, capacity, groups(bad)) == "FAIL"
    assert inverter_quality(strings, capacity, [[] for c in capacity]) == "FAIL"
    assert inverter_quality(strings, [1]*len(capacity), [strings]+[[]]*(len(capacity)-1)) == "FAIL"


@pytest.mark.parametrize("graph,members,islands", [
    ({"a":"b","b":"ac","c":"b"}, "abc", "ac"),
    ({"a":"bc","b":"a","c":"a"}, "bac", "bc"),
    ({"a":"bd","b":"ac","c":"bd","d":"ac"}, "bad", "bd")])
def test_string_connectivity(graph, members, islands):
    assert string_quality(members, graph) == "PASS"
    assert string_quality(islands, graph) == "FAIL"
    assert string_quality(members+"z", graph) == "INCOMPLETE"


@pytest.mark.parametrize("bad", [
    {"a":"bcd","b":"a","c":"a","d":"a"},
    {"a":"bc","b":"ac","c":"ab"},
    {"a":"b","b":"c","c":"b"}])
def test_connected_nonchains_fail(bad):
    assert string_quality(list(bad), bad) == "FAIL"


@pytest.mark.parametrize("case", ["invalid-mask", "point-route", "zero-segment"])
def test_malformed_route_fails(case):
    from shapely.geometry import LineString, Point, Polygon, box
    route = LineString([(0,0),(1,1)])
    aisle = box(-2,-2,2,2)
    arrays = []
    if case == "invalid-mask":
        arrays = [Polygon([(0,0),(1,1),(0,1),(1,0)])]
    elif case == "point-route":
        route = Point(0,0)
    else:
        route = LineString([(0,0),(0,0),(1,1)])
    assert route_quality(route, aisle, arrays) == "FAIL"


def test_all_tiny_allocations_against_independent_exhaustive_minima():
    from fractions import Fraction
    from itertools import product
    for caps in [(2,3), (2,3,4), (3,3,3)]:
        allocations = list(product(*(range(c+1) for c in caps)))
        def spread(loads):
            fills = [Fraction(n,c) for n,c in zip(loads,caps)]
            return max(fills)-min(fills)
        for loads in allocations:
            strings = list(range(sum(loads)))
            groups = [strings[sum(loads[:i]):sum(loads[:i+1])] for i in range(len(loads))]
            best = min(spread(alt) for alt in allocations if sum(alt)==sum(loads))
            assert inverter_quality(strings,caps,groups) == ("PASS" if spread(loads)==best else "FAIL")


@pytest.mark.parametrize("devices,cap", [(20,24),(32,48),(40,72)])
def test_large_feasible_optimum_has_bounded_work(monkeypatch, devices, cap):
    import benches.x03_quality as quality
    calls = 0
    real = quality.ceil
    def counted(value):
        nonlocal calls
        calls += 1
        return real(value)
    monkeypatch.setattr(quality, "ceil", counted)
    groups = [[(i,j) for j in range(2+(i==0))] for i in range(devices)]
    strings = [s for group in groups for s in group]
    assert inverter_quality(strings,[cap]*devices,groups) == "PASS"
    assert 0 < calls <= 2 * devices * (min(cap,len(strings))+1)
    groups[0].append(groups[-1].pop())
    assert inverter_quality(strings,[cap]*devices,groups) == "FAIL"


@pytest.mark.parametrize("length", [2,4,8])
def test_shared_trunk_and_detour(length):
    from shapely.geometry import Point, LineString
    from benches.x03_quality import bundled_route_quality
    start, fork = (0,0), (length,0)
    tips = [(length+1,1),(length+1,-1)]
    ends = [(Point(start),Point(tip)) for tip in tips]
    trunk = LineString([start,fork])
    paths = [LineString([start,fork,tip]) for tip in tips]
    def check(routes, endpoints):
        return bundled_route_quality(routes,endpoints,trunk,min_shared=length,max_detour=1.5)
    assert check(paths,ends) == "PASS"
    assert check([LineString([start,tip]) for tip in tips], ends) == "FAIL"
    assert check([LineString([start,fork,(length+10,0),tips[0]]),paths[1]],ends) == "FAIL"
    assert check(paths,[None,ends[1]]) == "INCOMPLETE"


@pytest.mark.parametrize("width", [1, 2, 4])
def test_clearance_to_obstacles_and_corridor(width):
    from shapely.geometry import LineString, box
    from benches.x03_quality import clearance_quality
    route = LineString([(0,0),(10,0)])
    aisle = box(-width,-width,10+width,5*width)
    arrays = [box(3,width,4,3*width)]
    assert clearance_quality(route,aisle,arrays,width) == "PASS"
    assert clearance_quality(route,aisle,[],width+0.01) == "FAIL"
    assert clearance_quality(route,aisle,[box(3,width/2,4,width)],width) == "FAIL"
    assert clearance_quality(route,aisle,arrays,None) == "INCOMPLETE"


@pytest.mark.parametrize("limit", [1,3,5])
def test_bounded_route_and_geometry_backed_string(limit):
    from shapely.geometry import LineString, Point
    from benches.x03_quality import bundled_route_quality, string_edge_quality
    line = LineString([(0,0),(limit,0)])
    ends = [(Point(0,0),Point(limit,0))]*2
    for bad in (True,float("nan"),float("inf"),10**400):
        for shared, detour in [(bad,2),(1,bad)]:
            assert bundled_route_quality([line,line],ends,line,min_shared=shared,max_detour=detour) == "FAIL"
    graph = {"a":"b","b":"ac","c":"b"}
    points = {"a":(0,0),"b":(limit,0),"c":(limit,limit)}
    assert string_edge_quality("abc",graph,points,limit) == "PASS"
    assert string_edge_quality("abc",graph,dict(points,c=(limit,limit+1)),limit) == "FAIL"
    del points["a"]
    assert string_edge_quality("abc",graph,points,limit) == "INCOMPLETE"


@pytest.mark.parametrize("sizes", [(2, 3), (1, 2, 2), (4, 1)])
def test_every_panel_belongs_to_one_contiguous_string(sizes):
    from benches.x03_quality import string_partition_quality
    panels = list(range(sum(sizes)))
    edges = {p: [n for n in (p-1, p+1) if n in panels] for p in panels}
    strings = [panels[sum(sizes[:i]):sum(sizes[:i+1])] for i in range(len(sizes))]
    assert string_partition_quality(strings, panels, edges) == "PASS"
    assert string_partition_quality(strings[:-1], panels, edges) == "FAIL"
    assert string_partition_quality(strings + [[panels[-1]]], panels, edges) == "FAIL"
    assert string_partition_quality(strings, None, edges) == "INCOMPLETE"


@pytest.mark.parametrize("caps,options", [
    ([3, 3], [[0], [0], [0], [1]]),
    ([3, 2], [[0, 1], [0], [1]]),
    ([2, 2, 1], [[0, 1], [0, 2], [0]]),
])
def test_eligible_assignments_against_exhaustive_reference(caps, options):
    from fractions import Fraction
    from itertools import product
    strings = list(range(len(options)))
    eligibility = dict(enumerate(options))
    candidates = {}
    for assignment in product(range(len(caps)), repeat=len(strings)):
        if any(i not in options[s] for s, i in enumerate(assignment)):
            continue
        fills = [Fraction(assignment.count(i), cap) for i, cap in enumerate(caps)]
        if max(fills) <= 1:
            candidates[assignment] = max(fills) - min(fills)
    optimum = min(candidates.values())
    for assignment in product(range(len(caps)), repeat=len(strings)):
        groups = [[s for s, slot in enumerate(assignment) if slot == i] for i in range(len(caps))]
        expected = "PASS" if candidates.get(assignment) == optimum else "FAIL"
        assert inverter_quality(strings, caps, groups, eligibility=eligibility) == expected


@pytest.mark.parametrize("series,parallel,volts,amps,peak_v,total_a", [
    (3, 2, 40, 9, 120, 18), (5, 1, 32, 8, 160, 8), (2, 4, 50, 2.5, 100, 10),
])
def test_series_parallel_limits(series, parallel, volts, amps, peak_v, total_a):
    from benches.x03_quality import wiring_quality
    strings = [dict(module="M", series_count=series) for _ in range(parallel)]
    ratings = {"M": dict(volts=volts, amps=amps)}
    assert wiring_quality(strings, ratings, peak_v, total_a) == "PASS"
    assert wiring_quality(strings, ratings, peak_v-1, total_a) == "FAIL"
    assert wiring_quality(strings, ratings, peak_v, total_a-1) == "FAIL"
    assert wiring_quality(strings, {"M": {"volts": volts}}, peak_v, total_a) == "INCOMPLETE"


@pytest.mark.parametrize("amps,slots,limits", [
    ([4, 6, 2], [1, 2], [6, 8]), ([8, 2], [2, 2], [8, 6]), ([3, 3, 3], [2, 1], [7, 4]),
])
def test_tracker_limits_for_all_assignments(amps, slots, limits):
    from itertools import product
    from benches.x03_quality import mppt_quality
    trackers = {i: dict(inputs=n, amps=limits[i]) for i, n in enumerate(slots)}
    for choice in product(trackers, repeat=len(amps)):
        ok = all(choice.count(i) <= slots[i] and sum(a for a, j in zip(amps, choice) if j == i) <= limits[i]
                 for i in trackers)
        assert mppt_quality(dict(enumerate(amps)), trackers, dict(enumerate(choice))) == ("PASS" if ok else "FAIL")


@pytest.mark.parametrize("span,tolerance", [(2, 0), (4, 0.5), (8, 1)])
def test_all_assigned_terminal_paths(span, tolerance):
    from shapely.geometry import LineString, Point
    from benches.x03_quality import terminal_route_quality
    terminals = {"a": Point(-1-tolerance, 1), "b": Point(-1, -1)}
    destinations = {"x": Point(span+1, 2), "y": Point(span+1, -2)}
    assignments = {"a": "x", "b": "y"}
    paths = {t: LineString([(-1, terminals[t].y), (0, 0), (span, 0), destinations[d].coords[0]])
             for t, d in assignments.items()}
    trunk = LineString([(0, 0), (span, 0)])
    def grade(edges, mapping, shared):
        return terminal_route_quality(edges, terminals, destinations, mapping, shared, tolerance)
    assert grade(paths, assignments, trunk) == "PASS"
    assert grade(paths, {"a": "y", "b": "x"}, trunk) == "FAIL"
    assert grade(paths, assignments, LineString([(0, 4), (span, 4)])) == "FAIL"
    assert grade(paths, assignments, None) == "INCOMPLETE"
    assert grade({"a": paths["a"]}, assignments, trunk) == "FAIL"
