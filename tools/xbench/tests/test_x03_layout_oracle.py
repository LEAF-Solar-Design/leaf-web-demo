import pytest
from shapely.geometry import Polygon, box

from benches.x03_layout_oracle import layout_quality


@pytest.mark.parametrize("history,duration,installed,remaining", [
    pytest.param([(0, 5, "normal")], 5, 200, 180, id="check-the-rail-clamp-after-service"),
    pytest.param([(0, 10, "hot")], 10, 400, 300, id="check-the-warm-inverter-bracket"),
    pytest.param([(0, 10, "normal"), (10, 20, "hot")], 20, 300, 210, id="verify-the-mixed-service-joint"),
])
def test_bolt_retention_over_service_history(history, duration, installed, remaining):
    from benches.x03_layout_oracle import preload_retention_quality as grade

    rule = dict(revision="lab-curve", hour_factors={"normal": 1, "hot": 2},
                retention_curve=[(0, 1), (10, 0.8), (30, 0.7)])
    # At 5, 20 and 30 equivalent hours the sheet retains 90%, 75% and 70%.
    assert grade("lab-curve", rule, history, duration, installed, remaining) == "PASS"
    assert installed > remaining + 1
    assert grade("lab-curve", rule, history, duration, installed, remaining + 1) == "FAIL"
    assert grade("lab-curve", rule, history, duration + 1, installed, remaining) == "INCOMPLETE"
    assert grade("lab-curve", rule, [(0, 31, "normal")], 31, installed, remaining) == "INCOMPLETE"


@pytest.mark.parametrize("forces,axial,mx,my", [
    pytest.param([0, 2, 3, 5], 10, 24, -14, id="check-the-unloaded-west-corner"),
    pytest.param([4, 0, 2, 2], 8, 12, -4, id="verify-the-eccentric-cabinet-load"),
    pytest.param([5, 1, 0, 4], 10, 12, -10, id="trace-the-diagonal-rail-reactions"),
])
def test_compression_only_baseplate_equilibrium(forces, axial, mx, my):
    from benches.x03_layout_oracle import baseplate_contact_quality as grade

    pads = {"a": (0, 0), "b": (2, 0), "c": (0, 3), "d": (2, 3)}
    load = (axial, mx, my)
    reactions = dict(zip(pads, forces))
    model = "vertical-point-contacts-v1"
    assert grade(model, pads, reactions, *load) == "PASS"
    # Shift reactions along the two diagonals. Both moments and the force sum stay equal.
    fictitious = {pad: force + shift for (pad, force), shift in zip(reactions.items(), [-6, 6, 6, -6])}
    assert grade(model, pads, fictitious, *load) == "FAIL"
    assert grade(model, pads, reactions, axial, mx + 1, my) == "FAIL"
    assert grade(model, dict(pads, a=None), reactions, *load) == "INCOMPLETE"


@pytest.mark.parametrize("rows,cols,hole,thickness,shear,tension,capacity", [
    pytest.param([10, 30], [0, 20], 4, 2, 10, 20, 1600, id="check-two-row-rail-splice"),
    pytest.param([20], [0, 30, 60], 6, 3, 5, 10, 1950, id="check-wide-end-bracket"),
    pytest.param([40, 10, 25], [-10, 10], 2, 4, 3, 7, 1344, id="check-three-row-clip"),
])
def test_rectangular_block_tearout(rows, cols, hole, thickness, shear, tension, capacity):
    from benches.x03_layout_oracle import rectangular_block_shear_quality as grade

    # Independent net side/end lengths in mm are (24,16), (17,48) and (35,18).
    rule = dict(revision="lab", model="two-net-sides-net-end-v1", shear_mpa=shear, tension_mpa=tension)
    args = ("lab", rule, rows, cols, hole, thickness)
    assert grade(*args, capacity) == "PASS"
    # Each specimen has an independent connection rating of 1000 N per bolt under equal sharing.
    assert capacity + 1 < 1000 * len(rows) * len(cols)
    assert grade(*args, capacity + 1) == "FAIL"
    assert grade("lab", rule, rows, [], hole, thickness, capacity) == "INCOMPLETE"


@pytest.mark.parametrize("depth,thickness,coefficient,stress_ceiling,resistance", [
    pytest.param(100, 2, 50000, 60, 4000, id="check-a-slender-edge-rail"),
    pytest.param(200, 5, 32000, 40, 20000, id="check-a-deep-support-web"),
    pytest.param(40, 4, 10000, 30, 4800, id="check-a-stocky-equipment-rail"),
])
def test_pinned_rail_web_shear(depth, thickness, coefficient, stress_ceiling, resistance):
    from benches.x03_layout_oracle import rail_bending_stress_quality, rail_web_shear_quality as grade

    rule = dict(revision="web-a", model="elastic-web-shear-v1",
                buckling_coefficient_mpa=coefficient, allowable_shear_mpa=stress_ceiling)
    assert rail_bending_stress_quality({"web": 1}, {"web": 0.001}, {"web": 2000}) == "PASS"
    assert grade("web-a", rule, depth, thickness, resistance) == "PASS"
    assert grade("web-a", rule, depth, thickness, -(resistance + 1)) == "FAIL"
    assert grade("web-a", rule, depth, None, resistance) == "INCOMPLETE"


@pytest.mark.parametrize("force_rating,moment_rating,force,moment", [
    pytest.param(1000, 80, 250, 60, id="check-quarter-loaded-roof-rail"),
    pytest.param(600, 120, 300, -60, id="check-reverse-bending-at-ridge"),
    pytest.param(200, 40, 150, 10, id="check-compression-heavy-end-span"),
])
def test_concurrent_rail_utilization(force_rating, moment_rating, force, moment):
    from benches.x03_layout_oracle import rail_interaction_quality as grade

    rule = dict(revision="r1", interaction="linear-uniaxial-v1",
                compression_rating_n=force_rating, moment_rating_nm=moment_rating)
    # The independent limit pairs consume 1/4 + 3/4, 1/2 + 1/2 and 3/4 + 1/4.
    assert grade("r1", rule, force, moment) == "PASS"
    assert force + 1 < force_rating and abs(moment) < moment_rating
    assert grade("r1", rule, force + 1, moment) == "FAIL"
    assert grade("r1", {k: v for k, v in rule.items() if k != "interaction"}, force, moment) == "INCOMPLETE"
    assert grade("r2", rule, force, moment) == "INCOMPLETE"


@pytest.mark.parametrize("force,arm,stiffness,limit", [(100,0.1,500,0.02),(40,-0.2,400,0.02),(150,0.02,100,0.03)])
def test_rail_twist_at_permitted_bending_load(force,arm,stiffness,limit):
    from benches.x03_layout_oracle import rail_twist_quality as grade
    assert force <= 150
    assert grade(force,arm,stiffness,limit) == "PASS"
    assert grade(force,arm*1.1,stiffness,limit) == "FAIL"
    assert grade(force,arm,None,limit) == "INCOMPLETE"


@pytest.mark.parametrize("span,load,limit", [(100, 3, 0.390625), (200, 3, 6.25), (200, 1.5, 3.125)])
def test_simple_span_uniform_load(span, load, limit):
    from benches.x03_layout_oracle import rail_deflection_quality
    rail = dict(support="simply_supported", span_mm=span, load_n_per_mm=load, modulus_mpa=100000,
                inertia_mm4=100, limit_mm=limit)
    assert rail_deflection_quality([rail]) == "PASS"
    assert rail_deflection_quality([rail, dict(rail, limit_mm=limit * 0.9)]) == "FAIL"
    assert rail_deflection_quality([dict(rail, inertia_mm4=None)]) == "INCOMPLETE"


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


@pytest.mark.parametrize("slope,weight,friction,restraint,expected", [
    (30,100,0,50,"PASS"), (30,100,0.2,30,"FAIL"), (45,100,1,0,"PASS"),
    (30,100,None,100,"INCOMPLETE"),
], ids=["exact-rated-restraint", "friction-plus-restraint-shortfall", "friction-at-limit", "missing-coefficient"])
def test_downslope_resistance(slope, weight, friction, restraint, expected):
    from benches.x03_layout_oracle import sliding_resistance_quality
    case = dict(slope_deg=slope, weight=weight, friction=friction, restraint=restraint)
    assert sliding_resistance_quality([case]) == expected


@pytest.mark.parametrize("mount,zones,expected", [
    ("edge",["field"],"PASS"), ("edge",["field","drift"],"FAIL"),
    ("reinforced",["drift"],"PASS"), ("unknown",["field"],"INCOMPLETE"),
], ids=["field-module","local-drift-overload","reinforced-at-limit","missing-mount-rating"])
def test_mount_specific_snow_pressure(mount, zones, expected):
    from benches.x03_layout_oracle import module_snow_quality
    rows = [dict(model="panel", mount=mount, zones=zones)]
    assert module_snow_quality(rows, {"field":1.6,"drift":3.2}, {"panel":{"edge":2,"reinforced":3.2}}) == expected


@pytest.mark.parametrize("centroid,side,expected", [
    (12,"positive","PASS"), (11.9,"positive","FAIL"), (8,"negative","PASS"),
    (9,"positive","FAIL"), (None,"positive","INCOMPLETE"),
], ids=["exact-stability","short-ballast-arm","opposite-tipping-edge","destabilizing-ballast","missing-centroid"])
def test_ballast_moments(centroid, side, expected):
    from benches.x03_layout_oracle import overturning_quality
    wind = [dict(force_n=40,arm_m=2),dict(force_n=20,arm_m=1)]
    assert overturning_quality(wind,[dict(weight_n=50,centroid_m=centroid)],10,side) == expected


@pytest.mark.parametrize("depth,capacity,expected", [
    (0.1,5,"PASS"), (0.2,6,"FAIL"), (0,3,"PASS"), (None,10,"INCOMPLETE"),
], ids=["local-support-at-limit","local-overload-with-spare-roof-capacity","dry-first-patch","missing-water-depth"])
def test_ponding_on_tributary_areas(depth, capacity, expected):
    from benches.x03_layout_oracle import ponding_quality
    areas = {"west":[dict(depth_m=depth,area_m2=2),dict(depth_m=0.1,area_m2=3)],
             "east":[dict(depth_m=0,area_m2=8)]}
    assert ponding_quality(areas, {"west":capacity,"east":100},10) == expected


@pytest.mark.parametrize("acceleration,share,limit,expected", [
    (2,0.5,100,"PASS"), (2,0.75,120,"FAIL"), (1.5,0.8,120,"PASS"), (None,0.5,100,"INCOMPLETE"),
], ids=["equal-shear-limit","local-anchor-overload","asymmetric-limit","missing-site-acceleration"])
def test_seismic_shear(acceleration, share, limit, expected):
    from benches.x03_layout_oracle import seismic_anchor_quality
    anchors = {"a":dict(fraction=share,shear_n=limit),"b":dict(fraction=round(1-share,2),shear_n=300)}
    assert seismic_anchor_quality([dict(mass_kg=100,lateral_m_s2=acceleration,anchors=anchors)]) == expected


@pytest.mark.parametrize("force,area,limit", [(100, 0.25, 400), (27, 0.3, 90), (2500, 2.5, 1000)])
def test_bearing_pressure_is_local(force, area, limit):
    from benches.x03_layout_oracle import pad_pressure_quality
    reactions = {"corner": force, "interior": force}
    areas = {"corner": area, "interior": 10 * area}
    limits = dict.fromkeys(reactions, limit)
    assert pad_pressure_quality(reactions, areas, limits) == "PASS"
    assert pad_pressure_quality(reactions, dict(areas, corner=area / 2), limits) == "FAIL"
    assert pad_pressure_quality(reactions, {"corner": area}, limits) == "INCOMPLETE"


@pytest.mark.parametrize("equation,tension,shear", [("linear", 25, 75), ("quadratic", 60, 80), ("quadratic", 80, 60)])
def test_combined_anchor_utilization(equation, tension, shear):
    from benches.x03_layout_oracle import anchor_interaction_quality as grade
    anchor = dict(tension=tension, shear=shear, tension_rating=100, shear_rating=100)
    assert grade([anchor], equation) == "PASS"
    assert grade([anchor, dict(anchor, shear=shear+1)], equation) == "FAIL"
    assert grade([anchor], None) == "INCOMPLETE"


@pytest.mark.parametrize("factors,limit", [((1,1,1), 60), ((1.2,0.5,1.5), 67), ((1,2,0), 50)])
def test_roof_load_combinations(factors, limit):
    from benches.x03_layout_oracle import combined_load_quality as grade
    loads = dict(dead=10, maintenance=20, snow=30)
    rules = {"design": dict(zip(loads, factors)), "dead only": dict(dead=1, maintenance=0, snow=0)}
    assert grade(loads, rules, limit) == "PASS"
    assert grade(loads, rules, limit-1) == "FAIL"
    assert grade(loads, {}, limit) == "INCOMPLETE"


@pytest.mark.parametrize("tip,angle,blocked,clear", [
    ((2,0),90,(1,1),(-1,1)), ((2,0),-90,(1,-1),(1,1)), ((0,2),180,(-1,0),(1,0))])
def test_hinged_door_obstacle_inside_sweep(tip, angle, blocked, clear):
    from shapely.geometry import Point
    from benches.x03_layout_oracle import door_sweep_quality as grade
    assert grade((0,0), tip, angle, [Point(clear).buffer(0.1)]) == "PASS"
    assert grade((0,0), tip, angle, [Point(blocked).buffer(0.1)]) == "FAIL"
    assert grade((0,0), None, angle, []) == "INCOMPLETE"


@pytest.mark.parametrize("condition,k,below,above", [("pinned",1,986,987), ("cantilever",2,246,247),
                                                       ("fixed",0.5,3947,3948)])
def test_weak_axis_buckling(condition,k,below,above):
    from benches.x03_layout_oracle import buckling_quality as grade
    rail = dict(end_condition=condition,length_mm=100,modulus_mpa=100000,weak_inertia_mm4=10,compression_n=below)
    assert grade([rail],{condition:k}) == "PASS"
    assert grade([dict(rail,compression_n=above)],{condition:k}) == "FAIL"
    assert grade([rail],{}) == "INCOMPLETE"


@pytest.mark.parametrize("ballast,offset,stable", [(3,4,True), (1,4,True), (1,5,False)])
def test_mass_projection_against_support_triangle(ballast,offset,stable):
    from benches.x03_layout_oracle import support_polygon_quality as grade
    # At y=1 the triangular base runs from x=0 to x=2.
    supports = [(0,0),(4,0),(0,2)]
    parts = [dict(mass=ballast,center=(0,1,1)),dict(mass=1,center=(offset,1,3))]
    assert grade(parts,supports) == ("PASS" if stable else "FAIL")
    assert grade(parts+[dict(mass=10,center=(9,1,0))],supports) == "FAIL"
    assert grade([dict(mass=3)],supports) == "INCOMPLETE"


@pytest.mark.parametrize("left,right,span,ratio", [(2,6,1000,0.004),(-2,3,2000,0.0025),(8,8,500,0)])
def test_differential_settlement(left,right,span,ratio):
    from benches.x03_layout_oracle import settlement_quality as grade
    rule = [("A","B",span,ratio)]
    assert grade({"A":left,"B":right},rule) == "PASS"
    assert grade({"A":left,"B":right+0.1},rule) == "FAIL"
    assert grade({"A":left},rule) == "INCOMPLETE"


@pytest.mark.parametrize("low,high,expected", [(100,25,"PASS"),(500,50,"FAIL"),(800,50,"FAIL")])
def test_miner_sum_over_all_stress_bins(low,high,expected):
    from benches.x03_layout_oracle import fatigue_quality as grade
    curve = dict(revision="lot-a",cycles_to_failure={20:1000,40:100})
    bins = {20:low,40:high}
    assert grade(bins,[20,40],curve,"lot-a","miner-linear") == expected
    assert grade({20:low},[20,40],curve,"lot-a","miner-linear") == "INCOMPLETE"
    assert grade(bins,[20,40],curve,"lot-b","miner-linear") == "FAIL"


@pytest.mark.parametrize("zone,position,outside", [((5, 12), 5, 4), ((30, 50), 50, 51), ((70, 75), 72, 76)])
def test_module_edge_clamp_placement(zone, position, outside):
    from benches.x03_layout_oracle import clamp_zone_quality
    rule = dict(revision="mount-v2", zones_mm={"near": zone, "far": (90, 100)})
    def check(positions):
        return clamp_zone_quality("mount-v2", rule, positions)
    assert check({"near": position, "far": 100}) == "PASS"
    assert check({"near": outside, "far": 100}) == "FAIL"
    assert check({"near": position}) == "INCOMPLETE"


@pytest.mark.parametrize("xy,force,moment,peak", [
    ({"a": (-1,0), "b": (1,0)}, (0,8), 4, 6),
    ({"a": (7,3), "b": (7,7)}, (12,0), 8, 8),
    ({"a": (-2,0), "b": (0,0), "c": (2,0)}, (0,12), -8, 6),
])
def test_peak_anchor_shear_from_eccentric_load(xy, force, moment, peak):
    from benches.x03_layout_oracle import eccentric_anchor_quality
    def grade(coords, limit):
        return eccentric_anchor_quality(coords, dict.fromkeys(xy, limit), *force, moment)
    assert grade(xy, peak) == "PASS"
    assert grade(xy, peak-0.1) == "FAIL"
    assert grade({}, peak) == "INCOMPLETE"


@pytest.mark.parametrize("sources,modes,gap", [([20],[22],2), ([30,60],[35,65],5), ([2.2],[1,2.4],0.2)])
def test_support_modes_clear_operating_frequencies(sources, modes, gap):
    from benches.x03_layout_oracle import resonance_quality as grade
    assert grade(sources, modes, gap) == "PASS"
    assert grade(sources, modes + [sources[-1]], gap) == "FAIL"
    assert grade(sources, [], gap) == "INCOMPLETE"


@pytest.mark.parametrize("torque,k,d,min_force,proof", [
    ((10,12),(0.2,0.25),0.01,4000,6000),
    ((32,36),(0.15,0.2),0.02,8000,12000),
    ((6,7.5),(0.1,0.12),0.005,10000,15000),
])
def test_joint_clamps_across_torque_and_friction_ranges(torque,k,d,min_force,proof):
    from benches.x03_layout_oracle import bolt_preload_quality as grade
    assert grade(torque,k,d,min_force,proof) == "PASS"
    assert grade(torque,k,d,min_force+1,proof) == "FAIL"
    assert grade(torque,k,d,min_force,proof-1) == "FAIL"
    assert grade(torque,None,d,min_force,proof) == "INCOMPLETE"


@pytest.mark.parametrize("moment,modulus,allowable", [(240,0.000002,120e6), (-60,0.000001,60e6), (450,0.000003,150e6)])
def test_rail_section_stress_at_positive_and_negative_moments(moment,modulus,allowable):
    from benches.x03_layout_oracle import rail_bending_stress_quality as grade
    moments = {"support":moment,"span":0}
    sections = {"support":modulus,"span":modulus}
    limits = dict.fromkeys(moments,allowable)
    assert grade(moments,sections,limits) == "PASS"
    assert grade(moments,sections,dict(limits,support=allowable-1)) == "FAIL"
    assert grade(moments,{"span":modulus},limits) == "INCOMPLETE"


@pytest.mark.parametrize("plate,t,d,allowable,force", [
    ("bracket",2,5,100,1000), ("rail",4,8,75,-2400), ("gusset",3,10,120,3600),
])
def test_local_hole_bearing_even_with_spare_peer_capacity(plate,t,d,allowable,force):
    from benches.x03_layout_oracle import hole_bearing_quality as grade
    section = dict(thickness_mm=t,diameter_mm=d,allowable_mpa=allowable)
    holes = {plate:section,"spare":section}
    assert grade({plate:force,"spare":0},holes) == "PASS"
    assert grade({plate:abs(force)+1,"spare":0},holes) == "FAIL"
    assert grade({plate:force},{plate:dict(section,diameter_mm=None)}) == "INCOMPLETE"


@pytest.mark.parametrize("holes,net_width", [
    ([(0,10,8)],32), ([(0,10,8),(0,30,8)],24),
    ([(0,10,8),(10,30,8)],25.25),
])
def test_plate_capacity_uses_minimum_net_path(holes,net_width):
    from benches.x03_layout_oracle import net_section_quality as grade
    capacity = net_width * 2 * 100
    assert grade(40,2,holes,capacity,100) == "PASS"
    assert grade(40,2,holes,capacity+1,100) == "FAIL"
    assert grade(40,2,None,capacity,100) == "INCOMPLETE"


@pytest.mark.parametrize("sizes,stress,capacity", [([(20,3)],100,6000), ([(10,2),(15,4)],50,4000), ([(8,2.5),(8,2.5)],80,3200)])
def test_joint_load_at_effective_throat_capacity(sizes,stress,capacity):
    from benches.x03_layout_oracle import weld_throat_quality as grade
    welds = [dict(effective_length_mm=length,throat_mm=throat) for length,throat in sizes]
    assert grade(welds,capacity,stress) == "PASS"
    assert grade(welds,capacity+1,stress) == "FAIL"
    assert grade(welds+[{}],capacity,stress) == "INCOMPLETE"




@pytest.mark.parametrize("direct,arm,toe,share,amplified", [
    (100,20,10,0.5,200),(240,10,20,1,360),(80,15,5,0.25,140),
])
def test_flange_toe_amplifies_bolt_load(direct,arm,toe,share,amplified):
    from benches.x03_layout_oracle import bolt_prying_quality as grade
    model = "lever-stiffness-fraction-v1"
    assert grade(direct,arm,toe,share,amplified,model) == "PASS"
    assert grade(direct,arm,toe,share,amplified-1,model) == "FAIL"
    assert grade(direct,arm,None,share,amplified,model) == "INCOMPLETE"


@pytest.mark.parametrize("length,pitch,diameter,width,stress,capacity", [
    (6,1,5,0.5,100,4712.39), (4.8,1.5,8,0.75,80,4523.89), (9,2,10,1,50,6283.19),
])
def test_internal_thread_pullout_capacity(length,pitch,diameter,width,stress,capacity):
    from benches.x03_layout_oracle import thread_engagement_quality as grade
    assert grade(length,pitch,diameter,width,stress,capacity-0.1) == "PASS"
    assert grade(length,pitch,diameter,width,stress,capacity+0.1) == "FAIL"
    assert grade(length,pitch,None,width,stress,capacity) == "INCOMPLETE"


@pytest.mark.parametrize("preload,mu,planes,resistance", [
    ([10000],0.3,1,3000), ([5000,7500],0.4,2,10000), ([8000,8000,4000],0.25,1,5000),
])
def test_joint_slip_from_installed_preloads(preload,mu,planes,resistance):
    from benches.x03_layout_oracle import joint_slip_quality as grade
    assert grade(preload,mu,planes,-resistance) == "PASS"
    assert grade(preload,mu,planes,resistance+1) == "FAIL"
    assert grade(preload,mu,None,resistance) == "INCOMPLETE"


@pytest.mark.parametrize("depth,perimeter,resistance,capacity", [
    pytest.param(100,800,0.5,40000,id="interior-support"),
    pytest.param(150,600,0.4,36000,id="edge-support"),
    pytest.param(200,350,0.8,56000,id="corner-support"),
])
def test_slab_punching_shear(depth,perimeter,resistance,capacity):
    from benches.x03_layout_oracle import punching_shear_quality as grade
    rule = "uniform-critical-section-v1"
    assert grade(capacity,depth,perimeter,resistance,rule) == "PASS"
    assert grade(capacity+1,depth,perimeter,resistance,rule) == "FAIL"
    assert grade(capacity,depth,None,resistance,rule) == "INCOMPLETE"


@pytest.mark.parametrize("stress,temp,years,elastic,final", [(2,25,5,4,6),(3,40,10,3,6),(5,60,2,5,6)])
def test_service_clearance_after_creep(stress,temp,years,elastic,final):
    from benches.x03_layout_oracle import creep_deflection_quality as grade
    sheet = {"revision":"r1","additional_creep_factors":{(stress,temp):[(0,0),(87600,1)]}}
    args = ("r1",sheet,stress,temp,years*8760,elastic)
    assert grade(*args,final) == "PASS"
    assert elastic <= final-0.5
    assert grade(*args,final-0.5) == "FAIL"
    assert grade("r1",sheet,stress,temp+1,years*8760,elastic,final) == "INCOMPLETE"


@pytest.mark.parametrize("span,length,width,load,moment", [
    (800,200,100,800,1400),(600,600,200,2400,900),(1000,400,400,4000,2000),
])
def test_local_deck_moment_with_acceptable_bearing(span,length,width,load,moment):
    from benches.x03_layout_oracle import deck_pad_bending_quality as grade
    rule = "centered-one-way-strip-v1"
    assert load/(length*width) < 0.1
    assert grade(span,length,width,load,moment,rule) == "PASS"
    assert grade(span,length,width,load,moment-1,rule) == "FAIL"
    assert grade(span,length,None,load,moment,rule) == "INCOMPLETE"


@pytest.mark.parametrize("locations,y,capacity", [
    ([40,50],50,600), ([0,10],0,200), ([20,40,70],50,1200),
])
def test_anchor_row_overlap_and_edges(locations,y,capacity):
    from benches.x03_layout_oracle import anchor_row_breakout_quality as grade
    rule = dict(revision="row1",model="row-square-projection-v1",projection_factor=1,resistance_n_per_mm2=1)
    assert grade("row1",rule,locations,y,(100,100),10,capacity) == "PASS"
    assert grade("row1",rule,locations,y,(100,100),10,capacity+1) == "FAIL"
    assert grade("row1",{},locations,y,(100,100),10,capacity) == "INCOMPLETE"


@pytest.mark.parametrize("length,safe,unstable", [(2,3100,3200),(4,1500,1600),(8,780,790)])
def test_beam_unbraced_length_can_govern(length,safe,unstable):
    from benches.x03_layout_oracle import lateral_torsional_quality as grade
    # EI = 4000 N m2, GJ = 1000 N m2. Section bending capacity alone is 4000 N m.
    props = (length,200e9,2e-8,80e9,1.25e-8)
    rule = "elastic-uniform-no-warping"
    assert unstable < 4000
    assert grade(rule,"fork-supported",*props,safe) == "PASS"
    assert grade(rule,"fork-supported",*props,unstable) == "FAIL"
    assert grade(rule,None,*props,safe) == "INCOMPLETE"
