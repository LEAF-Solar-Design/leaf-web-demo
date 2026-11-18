import pytest
from shapely.geometry import Polygon, box

from benches.x03_layout_oracle import layout_quality


@pytest.mark.parametrize("width,setback", [(10, 0), (12, 1), (16, 2)])
def test_panels_fit_usable_area(width, setback):
    hole = box(5, 5, 7, 7)
    area = Polygon(box(0, 0, width, width).exterior.coords, [hole.exterior.coords])
    first = box(setback, setback, setback+1, setback+1)
    adjacent = box(setback+1, setback, setback+2, setback+1)
    assert layout_quality([first, adjacent], area, setback) == "PASS"
    assert layout_quality([first, first], area, setback) == "FAIL"
    assert layout_quality([hole], area, setback) == "FAIL"
    assert layout_quality([first], area, setback+0.01) == "FAIL"
    assert layout_quality([None], area, setback) == "INCOMPLETE"


@pytest.mark.parametrize("angle,pitch", [(0, 2), (90, 3), (32, 4)])
def test_rotated_rows_and_pitch(angle, pitch):
    from shapely.affinity import rotate
    from benches.x03_layout_oracle import row_layout_quality
    rows = [[rotate(box(x, y, x+2, y+1), angle, origin=(0, 0)) for x in (0, 3)] for y in (0, pitch)]
    def grade(orientation, minimum):
        return row_layout_quality(rows, box(-20, -20, 20, 20), 0, orientation=orientation,
                                  row_axis=angle, min_pitch=minimum)
    assert grade(angle, pitch) == "PASS"
    assert grade(angle+90, pitch) == "FAIL"
    assert grade(angle, pitch+0.01) == "FAIL"
    assert grade(None, pitch) == "INCOMPLETE"


@pytest.mark.parametrize("unit,panel_start,panel_end", [
    ("m", 0.3048, 0.6096), ("mm", 304.8, 609.6), ("ft", 1, 2),
])
def test_mixed_unit_layout(unit, panel_start, panel_end):
    from benches.x03_layout_oracle import measured_layout_quality
    # One square foot, one foot from the boundary, on a site recorded in metres.
    area = box(0, 0, 1, 1)
    panels = [box(panel_start, panel_start, panel_end, panel_end)]
    def grade(distance, units):
        return measured_layout_quality(panels, area, distance, panel_unit=units,
                                       area_unit="m", setback_unit="ft")
    assert grade(1, unit) == "PASS"
    assert grade(1.01, unit) == "FAIL"
    assert grade(1, "kg") == "FAIL"
    assert grade(1, None) == "INCOMPLETE"


@pytest.mark.parametrize("panel,ballast,share,local_mass", [(40, 20, 0.5, 30), (30, 10, 0.75, 30), (12, 8, 1, 20)])
def test_roof_load_on_individual_zones(panel, ballast, share, local_mass):
    from benches.x03_layout_oracle import zone_load_quality
    zones = {"edge": dict(area=2, limit=local_mass/2), "center": dict(area=100, limit=20)}
    def check(limits, ballast_record):
        return zone_load_quality({"rack": panel}, ballast_record,
                                 {"rack": {"edge": share, "center": 1-share}}, limits)
    assert check(zones, {"rack": ballast}) == "PASS"
    zones["edge"]["limit"] -= 0.01
    assert check(zones, {"rack": ballast}) == "FAIL"
    assert check(zones, {}) == "INCOMPLETE"


@pytest.mark.parametrize("offset", [-4, 0, 12])
def test_versioned_shade_masks(offset):
    from benches.x03_layout_oracle import shade_exclusion_quality
    panel = box(offset, 1, offset+2, 2)
    def grade(polygons, policy, revision="v2"):
        shade = dict(drawing="site", revision=revision, polygons=polygons)
        return shade_exclusion_quality([panel], shade, drawing="site", revision="v2", boundary_policy=policy)
    edge = box(offset+2, 0, offset+3, 3)
    assert grade([edge], "allow-touch") == "PASS"
    assert grade([edge], "exclude-touch") == "FAIL"
    assert grade([box(offset+1, 0, offset+3, 3)], "allow-touch") == "FAIL"
    assert grade([], "exclude-touch") == "PASS"
    assert grade(None, "allow-touch") == "INCOMPLETE"
    assert grade([], "allow-touch", "v1") == "FAIL"


@pytest.mark.parametrize("edges", [[(0,1),(1,2)], [(0,1),(0,2),(2,3)], [(0,1),(1,2),(2,3),(3,0),(3,4)]])
def test_service_access_matches_node_graph(edges):
    from shapely.geometry import LineString, Point
    from benches.x03_layout_oracle import access_path_quality
    # Convex polygon vertices keep the chosen graph planar for these fixtures.
    coords = [(0,0), (2,0), (3,2), (1,4), (-1,2)]
    target = max(max(edge) for edge in edges)
    for omitted in [None, *edges]:
        remaining = [edge for edge in edges if edge != omitted]
        reachable = {0}
        for _ in coords:
            for a,b in remaining:
                if a in reachable or b in reachable:
                    reachable.update((a,b))
        paths = [LineString([coords[a], coords[b]]) for a,b in remaining]
        assert access_path_quality(paths, [Point(coords[0])], [Point(coords[target])]) == (
            "PASS" if target in reachable else "FAIL")
    assert access_path_quality(paths, None, [Point(coords[target])]) == "INCOMPLETE"


@pytest.mark.parametrize("offset, underside, expected", [
    (0, 2, "FAIL"), (0, 4, "PASS"), (2, 2, "PASS"), (0, None, "INCOMPLETE"),
], ids=["catch-beam-intersection", "check-exact-service-gap", "clear-offset-duct", "missing-elevation"])
def test_equipment_overhead_clearance(offset, underside, expected):
    from benches.x03_layout_oracle import overhead_quality
    unit = dict(footprint=box(0, 0, 1, 1), bottom=0, top=3)
    beam = dict(footprint=box(offset, 0, offset+1, 1), bottom=underside, top=6)
    assert overhead_quality([unit], [beam], 1) == expected


@pytest.mark.parametrize("normal, tilt, bearing, tol, result", [
    ((0, 1, 1), 45, 359, 1, "PASS"), ((1, 0, 1), 45, 0, 10, "FAIL"),
    ((0, -1, 1), 40, 180, 2, "FAIL"), ((None, 1, 1), 45, 0, 1, "INCOMPLETE"),
], ids=["north-wraparound", "reject-east-facing-plane", "reject-excess-pitch", "missing-normal"])
def test_roof_normal(normal, tilt, bearing, tol, result):
    from benches.x03_layout_oracle import roof_orientation_quality
    assert roof_orientation_quality(normal, tilt, bearing, tol, tol) == result


@pytest.mark.parametrize("stations, span, overhang, verdict", [
    ([5, 1, 9], 4, 1, "PASS"), ([1, 4, 9], 4, 1, "FAIL"),
    ([2, 5, 9], 4, 1, "FAIL"), ([1, 5, 9], None, 1, "INCOMPLETE"),
], ids=["verify-boundary-attachments", "find-unsupported-midspan", "flag-end-cantilever", "missing-span-rule"])
def test_recorded_rail_stations(stations, span, overhang, verdict):
    from benches.x03_layout_oracle import rail_support_quality
    assert rail_support_quality(0, 10, stations, span, overhang) == verdict


@pytest.mark.parametrize("areas,rating,expected", [
    ({"edge": 2}, 6, "PASS"), ({"field": 2, "edge": 1}, 4, "FAIL"),
    ({"field": 3}, 4, "PASS"), ({"corner": 2}, 20, "INCOMPLETE"),
], ids=["edge-at-limit","mixed-zone-attachment-overload","interior-support","missing-corner-pressure"])
def test_wind_zone_tributary(areas, rating, expected):
    from benches.x03_layout_oracle import wind_uplift_quality
    assert wind_uplift_quality({"field": 1, "edge": 3}, {"a": areas, "b": {"field": 1}},
                               {"a": rating, "b": 100}) == expected


@pytest.mark.parametrize("gap,fixed_left,right_arm,expected", [
    (2, 0, 10, "PASS"), (1, 0, 10, "FAIL"), (0, 10, 0, "PASS"), (1, None, 10, "INCOMPLETE"),
], ids=["two-moving-ends-at-limit","undersized-joint","fixed-at-both-joint-ends","missing-anchor"])
def test_rail_expansion(gap, fixed_left, right_arm, expected):
    from benches.x03_layout_oracle import rail_joint_quality
    right = 10 + gap
    rails = [dict(start=0, fixed=fixed_left, end=10, alpha_per_c=0.001),
             dict(start=right, fixed=right+right_arm, end=right+10, alpha_per_c=0.001)]
    assert rail_joint_quality(rails, -20, 80) == expected


@pytest.mark.parametrize("x,y,expected", [
    (1, 1, "PASS"), (9.5, 1, "FAIL"), (4, 0.5, "FAIL"), (-2, 1, "FAIL"),
], ids=["exact-end-distance", "too-near-far-end", "too-near-side", "outside-support"])
def test_attachment_member_edges(x, y, expected):
    from shapely.geometry import Point
    from benches.x03_layout_oracle import member_edge_quality
    rows = [dict(member="purlin", point=Point(x, y))]
    assert member_edge_quality({"purlin": box(0, 0, 10, 2)}, rows, 1) == expected
    assert member_edge_quality({}, rows, 1) == "INCOMPLETE"


@pytest.mark.parametrize("footprint,policy,expected", [
    (box(2,0,3,1), "touch-allowed", "PASS"), (box(2,0,3,1), "touch-forbidden", "FAIL"),
    (box(-3,-0.1,-2.5,0.1), "touch-allowed", "FAIL"), (box(1,0,3,1), "touch-allowed", "FAIL"),
], ids=["exact-keepout", "exclude-boundary", "blocked-approach", "crosses-drain-radius"])
def test_drain_keepout_and_access(footprint, policy, expected):
    from shapely.geometry import Point
    from benches.x03_layout_oracle import drain_access_quality
    drain = dict(center=Point(0,0), radius=2, access=box(-4,-0.5,0,0.5))
    assert drain_access_quality([footprint], [drain], policy) == expected
    assert drain_access_quality([footprint], [dict(drain, access=None)], policy) == "INCOMPLETE"
