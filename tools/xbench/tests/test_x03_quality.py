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
