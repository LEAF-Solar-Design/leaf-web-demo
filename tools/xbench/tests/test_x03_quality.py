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
