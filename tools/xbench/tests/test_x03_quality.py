import pytest
from benches.x03_quality import inverter_quality, route_quality, string_quality


@pytest.mark.parametrize("initial,capacity,intervals,peak", [
    (20,100,[(10,100,50)],25), (22,200,[(4,150,50),(4,150,50)],26),
    (24,100,[(10,20,100),(5,150,50)],25)])
def test_pack_thermal_balance_within_electrical_rating(initial,capacity,intervals,peak):
    from benches.x03_quality import battery_thermal_quality as grade
    from benches.x03_quality import battery_power_quality
    ratings = dict(charge_kw=5, discharge_kw=10)
    assert battery_power_quality([8], ratings, ratings) == "PASS"
    assert grade(initial,20,capacity,peak,intervals) == "PASS"
    assert grade(initial,20,capacity,peak-0.1,intervals) == "FAIL"
    assert grade(initial,20,capacity,peak,[(5,100,None)]) == "INCOMPLETE"


@pytest.mark.parametrize("volts,minimum", [(250,1), (500,5), (1000,100)])
def test_insulation_resistance_requires_valid_test_voltage(volts,minimum):
    from benches.x03_quality import insulation_resistance_quality as grade
    rule = dict(min_volts=volts,max_volts=volts,min_megohms=minimum)
    reading = dict(volts=volts,megohms=minimum)
    assert grade(reading,rule) == "PASS"
    assert grade(dict(reading,megohms=minimum/2),rule) == "FAIL"
    assert grade(dict(reading,volts=volts/2),rule) == "FAIL"
    assert grade(dict(volts=volts),rule) == "INCOMPLETE"


@pytest.mark.parametrize("ask,maximum,adequate", [
    ("Check the PV home run", 980, 1000), ("Check a 48 V storage lead", 60, 60),
    ("Check the AC feeder", 480, 600)])
def test_dielectric_rating_independent_of_ampacity(ask, maximum, adequate):
    from benches.x03_quality import conductor_ampacity_quality, insulation_quality
    assert conductor_ampacity_quality(100, 50, {"ambient": 1}, ["ambient"]) == "PASS"
    circuits = {ask: (maximum, ["lead", "tail"])}
    assert insulation_quality(circuits, {"lead": adequate, "tail": adequate}) == "PASS"
    assert insulation_quality(circuits, {"lead": adequate, "tail": maximum - 1}) == "FAIL"
    assert insulation_quality(circuits, {"lead": adequate}) == "INCOMPLETE"


@pytest.mark.parametrize("sensor,limits,value", [("cold rack", (0, 45), 0), ("hot rack", (5, 40), 40),
                                                  ("pack core", (-5, 35), 19)])
def test_prevent_charging_outside_recorded_range(sensor, limits, value):
    from benches.x03_quality import charging_temperature_quality as grade
    policy = {sensor: limits}
    assert grade({sensor: value}, policy, True) == "PASS"
    for unsafe in (limits[0] - 1, limits[1] + 1):
        assert grade({sensor: unsafe}, policy, True) == "FAIL"
        assert grade({sensor: unsafe}, policy, False) == "PASS"
    assert grade({}, policy, True) == "INCOMPLETE"
    assert grade({sensor: value}, None, True) == "INCOMPLETE"


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
    assert grade(paths, assignments, LineString(list(trunk.coords)[::-1])) == "FAIL"
    assert grade(paths, {"a": "y", "b": "x"}, trunk) == "FAIL"
    assert grade(paths, assignments, LineString([(0, 4), (span, 4)])) == "FAIL"
    assert grade(paths, assignments, None) == "INCOMPLETE"
    assert grade({"a": paths["a"]}, assignments, trunk) == "FAIL"


@pytest.mark.parametrize("rated,factors,capacity", [
    (100, {"heat": 0.8, "bundle": 0.75}, 60),
    (75, {"ambient": 0.92}, 69),
    (50, {"ambient": 0.8, "group": 0.8, "installation": 0.5}, 16),
])
def test_recorded_ampacity(rated, factors, capacity):
    from benches.x03_quality import conductor_ampacity_quality
    names = list(factors)
    assert conductor_ampacity_quality(rated, capacity, factors, names) == "PASS"
    assert conductor_ampacity_quality(rated, capacity+0.01, factors, names) == "FAIL"
    assert conductor_ampacity_quality(rated, capacity, {}, names) == "INCOMPLETE"


@pytest.mark.parametrize("length,length_unit,resistance,res_unit,amps,volts", [
    (100, "m", 0.01, "ohm/m", 3, 3), (0.4, "km", 2, "ohm/km", 10, 8),
    (100, "ft", 0.5, "ohm/km", 10, 0.1524),
])
def test_round_trip_drop(length, length_unit, resistance, res_unit, amps, volts):
    from benches.x03_quality import dc_drop_quality
    units = dict(length=length_unit, resistance=res_unit, current="A", drop="V")
    assert dc_drop_quality(length, resistance, amps, volts, units=units) == "PASS"
    assert dc_drop_quality(length, resistance, amps, volts-0.001, units=units) == "FAIL"
    del units["resistance"]
    assert dc_drop_quality(length, resistance, amps, volts, units=units) == "INCOMPLETE"


@pytest.mark.parametrize("radius,sign,offset", [(2, 1, 0), (3, -1, 7), (4, 1, -3)])
def test_circular_bend_and_tangent_join(radius, sign, offset):
    from benches.x03_quality import bend_radius_quality
    start = (offset, 0)
    end = (offset+radius, sign*radius)
    arc = dict(kind="arc", start=start, end=end, center=(offset, sign*radius),
               direction="ccw" if sign == 1 else "cw")
    lead = dict(kind="line", start=(offset-1, 0), end=start)
    assert bend_radius_quality([lead, arc], radius) == "PASS"
    assert bend_radius_quality([lead, arc], radius+0.01) == "FAIL"
    assert bend_radius_quality([lead, dict(kind="line", start=start, end=end)], radius) == "FAIL"
    assert bend_radius_quality([lead, dict(arc, center=None)], radius) == "INCOMPLETE"


@pytest.mark.parametrize("order", [("A", "B"), ("C", "A", "B"), ("D", "C", "B", "A")])
def test_external_leads_and_series_path(order):
    from benches.x03_quality import terminal_polarity_quality
    pins = {m: {"-": m+"1", "+": m+"2"} for m in order}
    joins = [(a+"2", b+"1") for a, b in zip(order, order[1:])]
    leads = {"-": order[0]+"1", "+": order[-1]+"2"}
    def grade(edges, ends, mapping):
        return terminal_polarity_quality(sorted(order), mapping, edges, ends)
    assert grade([edge[::-1] for edge in reversed(joins)], leads, pins) == "PASS"
    assert grade(joins, {"-": leads["+"], "+": leads["-"]}, pins) == "FAIL"
    assert grade([(order[0]+"2", order[1]+"2"), *joins[1:]], leads, pins) == "FAIL"
    assert grade(joins[1:], leads, pins) == "FAIL"
    assert grade(joins, leads, {}) == "INCOMPLETE"


@pytest.mark.parametrize("count,reference,coefficient,temps,window", [
    (10, 40, -0.002, (0, 75), (360, 420)),
    (5, 32, -0.004, (0, 50), (144, 176)),
    (4, 50, 0.001, (-25, 75), (190, 210)),
])
def test_mppt_temperature_extremes(count, reference, coefficient, temps, window):
    from benches.x03_quality import mppt_window_quality
    def check(bounds, factor):
        return mppt_window_quality(count, reference, factor, temps, bounds)
    assert check(window, coefficient) == "PASS"
    assert check((window[0]+0.01, window[1]), coefficient) == "FAIL"
    assert check((window[0], window[1]-0.01), coefficient) == "FAIL"
    assert check(window, None) == "INCOMPLETE"


@pytest.mark.parametrize("gap", [1, 2, 4])
def test_circuit_clearance_and_declared_junctions(gap):
    from shapely.geometry import LineString, Point
    from benches.x03_quality import circuit_separation_quality
    base = LineString([(-10, 0), (10, 0)])
    circuits = {"feeder": "power", "sensor": "signal"}
    rules = {frozenset(circuits.values()): gap}
    junction = {frozenset(circuits): [Point(0, 0)]}
    def check(path, allowed, ids=circuits):
        return circuit_separation_quality({"feeder": base, "sensor": path}, ids, rules, allowed)
    crossing = LineString([(0, -10), (0, 10)])
    assert check(crossing, junction) == "PASS"
    assert check(crossing, {}) == "FAIL"
    assert check(base, junction) == "FAIL"
    assert check(LineString([(-10, gap), (10, gap)]), {}) == "PASS"
    assert check(LineString([(-10, gap/2), (10, gap/2)]), {}) == "FAIL"
    assert check(crossing, junction, {}) == "INCOMPLETE"


@pytest.mark.parametrize("area,wires,fraction", [
    (100, [14, 16, 10], 0.4), (12, [6], 0.5), (2, [0.15, 0.25], 0.2),
])
def test_declared_conduit_fill(area, wires, fraction):
    from benches.x03_quality import conduit_fill_quality
    assert conduit_fill_quality(area, wires, fraction) == "PASS"
    assert conduit_fill_quality(area, wires+[0.001], fraction) == "FAIL"
    assert conduit_fill_quality(area, wires+[None], fraction) == "INCOMPLETE"
    assert conduit_fill_quality(None, wires, fraction) == "INCOMPLETE"


@pytest.mark.parametrize("ac,ratings,limits", [
    (100, [60, 60], (0.6, 1.2)), (200, [100, 80], (0.8, 1)), (80, [70, 30], (1, 1.25)),
])
def test_local_inverter_ratio(ac, ratings, limits):
    from benches.x03_quality import inverter_ratio_quality
    dc = dict(enumerate(ratings*2))
    mapping = {0: "a", 1: "a", 2: "b", 3: "b"}
    def grade(assign, watts):
        return inverter_ratio_quality({"a": ac, "b": ac}, watts, assign, dict.fromkeys(("a", "b"), limits))
    assert grade(mapping, dc) == "PASS"
    assert grade({**mapping, 3: "a"}, dc) == "FAIL"
    assert grade(mapping, {**dc, 0: None}) == "INCOMPLETE"


@pytest.mark.parametrize("volt,counts,delta", [(40,(8,9),40), (32,(10,12),64), (25,(9,12),75)])
def test_string_voltage_mismatch_on_shared_tracker(volt, counts, delta):
    from benches.x03_quality import parallel_string_quality
    strings = {i: dict(module="panel", tracker="east", series_count=n) for i,n in enumerate(counts)}
    def grade(rows, ratings, limit):
        return parallel_string_quality(rows, ratings, {"east":limit, "west":limit})
    assert grade(strings, {"panel":volt}, delta) == "PASS"
    assert grade(strings, {"panel":volt}, delta-1) == "FAIL"
    split = {**strings, 1:dict(strings[1], tracker="west")}
    assert grade(split, {"panel":volt}, delta-1) == "PASS"
    assert grade(strings, {"panel":None}, delta) == "INCOMPLETE"


@pytest.mark.parametrize("load,device,wire,equipment", [(20,20,30,20), (16,25,25,40), (10,10,10,10)])
def test_device_coordination(load, device, wire, equipment):
    from benches.x03_quality import overcurrent_quality
    assert overcurrent_quality(load, device, wire, equipment) == "PASS"
    assert overcurrent_quality(load, load-1, wire, equipment) == "FAIL"
    assert overcurrent_quality(load, device, device-1, equipment) == "FAIL"
    assert overcurrent_quality(load, device, wire, device-1) == "FAIL"
    assert overcurrent_quality(load, device, None, equipment) == "INCOMPLETE"


@pytest.mark.parametrize("ratings, occupancy, verdict", [
    ({"branch": 8, "trunk": 20}, {"branch": {"dc": 5, "ac": 4}, "trunk": {"dc": 5}}, "FAIL"),
    ({"east": 10, "west": 4}, {"east": {"a": 6, "b": 4}, "west": {}}, "PASS"),
    ({"lower": 8, "upper": 6}, {"upper": {"new": 3}}, "INCOMPLETE"),
], ids=["find-an-overloaded-branch", "fit-two-cables-at-the-limit", "audit-an-unreported-segment"])
def test_segment_capacity(ratings, occupancy, verdict):
    from benches.x03_quality import tray_capacity_quality
    assert tray_capacity_quality(ratings, occupancy) == verdict


@pytest.mark.parametrize("links, verdict", [
    ([("ground", "rack", True)], "FAIL"),
    ([("ground", "rack", True), ("rack", "frame", False)], "FAIL"),
    ([("ground", "rack", True), ("rack", "frame", False), ("frame", "ground", True)], "PASS"),
    ([("ground", "rack", None)], "INCOMPLETE"),
], ids=["find-isolated-frame", "trace-broken-jumper", "use-redundant-return", "missing-condition"])
def test_bond_inventory(links, verdict):
    from benches.x03_quality import bonding_quality
    records = [dict(a=a, b=b, intact=intact) for a, b, intact in links]
    assert bonding_quality(["rack", "frame"], "ground", records) == verdict


@pytest.mark.parametrize("phases, currents, limit, spread, expected", [
    ([["A"], ["A"], ["B"]], [6, 6, 4], 10, 20, "FAIL"),
    ([["A", "B", "C"], ["B"], ["C"]], [4, 2, 2], 6, 2, "PASS"),
    ([["A"], ["B"], ["C"]], [8, 5, 5], 10, 2, "FAIL"),
    ([["A"], None, ["C"]], [4, 4, 4], 4, 0, "INCOMPLETE"),
], ids=["audit-hidden-feeder-overload", "balance-three-phase-and-single-phase", "limit-leg-spread", "missing-phase"])
def test_phase_balance(phases, currents, limit, spread, expected):
    from benches.x03_quality import phase_balance_quality
    assert phase_balance_quality(dict(enumerate(phases)), dict(enumerate(currents)),
                                 dict.fromkeys("ABC", limit), spread) == expected


@pytest.mark.parametrize("currents, rating, cap, expected", [
    ([4, 6, 8], 14, "unprotected", "PASS"), ([3, 7, 8], 14, "unprotected", "FAIL"),
    ([3, 7, 8], 14, 12, "PASS"), ([3, 7, 8], 14, None, "INCOMPLETE"),
], ids=["check-exact-module-limit", "locate-backfed-branch", "apply-recorded-let-through", "missing-protection"])
def test_reverse_current(currents, rating, cap, expected):
    from benches.x03_quality import reverse_current_quality
    sources = dict(enumerate(currents))
    assert reverse_current_quality(sources, {0: rating, 1: 20, 2: 20},
                                   {0: cap, 1: "unprotected", 2: "unprotected"}) == expected


@pytest.mark.parametrize("faults, ratings, verdict", [
    ({"service": 22000}, {"service": 22000}, "PASS"),
    ({"main": 12000, "branch": 10001}, {"main": 65000, "branch": 10000}, "FAIL"),
    ({"east": 9380, "west": 14850}, {"east": 10000, "west": 18000}, "PASS"),
    ({"service": None}, {"service": 22000}, "INCOMPLETE"),
], ids=["check-exact-interrupting-limit", "locate-underrated-branch", "compare-independent-feeders", "missing-fault-study"])
def test_device_interrupting_ratings(faults, ratings, verdict):
    from benches.x03_quality import interrupting_rating_quality
    assert interrupting_rating_quality(faults, ratings) == verdict


@pytest.mark.parametrize("pair, approved, volts, amps, expected", [
    (["B/socket","A/plug"], True, 600, 20, "PASS"),
    (["A/plug","B/socket"], False, 400, 10, "FAIL"),
    (["A/plug","B/socket"], True, 400, 21, "FAIL"),
    (["A/plug","C/lookalike"], True, 400, 10, "INCOMPLETE"),
], ids=["approved-cross-brand","explicit-incompatibility","overcurrent","unlisted-lookalike"])
def test_mating_record(pair, approved, volts, amps, expected):
    from benches.x03_quality import connector_pair_quality
    records = [dict(parts=["A/plug","B/socket"], approved=approved, max_volts=600, max_amps=20)]
    assert connector_pair_quality(pair, records, volts, amps) == expected


@pytest.mark.parametrize("p,q,s,expected", [
    (3,4,5,"PASS"), (8,-6,9.9,"FAIL"), (-12,5,13,"PASS"), (3,4,None,"INCOMPLETE"),
], ids=["three-four-five-at-limit","leading-vars-overload","reverse-power","missing-nameplate"])
def test_apparent_power(p, q, s, expected):
    from benches.x03_quality import apparent_power_quality
    assert apparent_power_quality({"x": p}, {"x": q}, {"x": s}) == expected


@pytest.mark.parametrize("current,time,rating,expected", [
    (200, 0.25, 10000, "PASS"), (100, 0.51, 5000, "FAIL"),
    (400, 0.025, 4100, "PASS"), (300, None, 9000, "INCOMPLETE"),
], ids=["at-thermal-limit", "slow-protection-overload", "short-clearing-time", "missing-trip-evidence"])
def test_conductor_fault_energy(current, time, rating, expected):
    from benches.x03_quality import short_circuit_withstand_quality
    assert short_circuit_withstand_quality({"branch":current}, {"branch":time}, {"branch":rating}) == expected


@pytest.mark.parametrize("phases,pf,load,r,x,unit,volts", [
    (1, 1, "lagging", 2, 1, "ohm/km", 4),
    (3, 0.8, "lagging", 0.002, 0.001, "ohm/m", 3.81051177665153),
    (1, 0.6, "leading", 1, 2, "ohm/km", -2),
], ids=["single-phase-resistive", "balanced-three-phase-lagging", "leading-voltage-rise"])
def test_ac_drop(phases, pf, load, r, x, unit, volts):
    from benches.x03_quality import ac_voltage_drop_quality
    args = (phases, 10, 100, pf, load, r, x)
    assert ac_voltage_drop_quality(*args, unit, volts) == "PASS"
    assert ac_voltage_drop_quality(*args, unit, volts + 1) == "FAIL"
    assert ac_voltage_drop_quality(*args, None, volts) == "INCOMPLETE"


@pytest.mark.parametrize("currents,limit,expected", [
    ([8,8,8], 0, "PASS"), ([8,12], 0.2, "PASS"), ([8,12], 0.19, "FAIL"),
    ([5,None], 0.2, "INCOMPLETE"),
], ids=["uniform-string", "exact-twenty-percent-loss", "excess-loss-within-current-limits", "missing-module"])
def test_recorded_string_loss(currents, limit, expected):
    from benches.x03_quality import mismatch_loss_quality
    strings = {"roof": dict(enumerate(currents)), "annex": {"a": 10, "b": 10}}
    assert mismatch_loss_quality(strings, limit, "equal-voltage-min-current") == expected
    assert mismatch_loss_quality(strings, limit, None) == "INCOMPLETE"


@pytest.mark.parametrize("extra,states,expected", [
    ([], {"main":"open"}, "PASS"),
    ([("aux","protected","tie")], {"main":"open","tie":"closed"}, "FAIL"),
    ([("protected","bus",None), ("bus","aux",None)], {"main":"open"}, "FAIL"),
    ([], {}, "INCOMPLETE"),
    ([("aux","protected",None)], {}, "FAIL"),
], ids=["verify-open-disconnect", "trace-alternate-feed", "trace-reverse-backfeed", "unknown-disconnect", "known-live-despite-unknown"])
def test_protected_section_isolation(extra, states, expected):
    from benches.x03_quality import disconnect_isolation_quality
    links = [dict(a="utility", b="protected", kind="switch", id="main")]
    for a, b, switch in extra:
        links.append(dict(a=a, b=b, kind="switch" if switch else "wire", id=switch))
    assert disconnect_isolation_quality(["utility","aux","protected","bus"], ["utility","aux"],
                                        ["protected"], links, states) == expected


@pytest.mark.parametrize("modules,threshold,expected", [
    ([40,40,40], 120, "PASS"), ([30,40,40], 120, "FAIL"), ([50,45,35], 125, "PASS"),
    ([40,None,40], 120, "INCOMPLETE"),
], ids=["exact-startup-threshold", "operating-range-insufficient-to-start", "mixed-module-start", "missing-condition"])
def test_prestart_string_voltage(modules, threshold, expected):
    from benches.x03_quality import startup_voltage_quality
    assert startup_voltage_quality({"east":modules,"west":[140]}, threshold) == expected
    assert startup_voltage_quality({"east":modules}, None) == "INCOMPLETE"


@pytest.mark.parametrize("amps,ohms,limits,expected", [
    (30,[1,2],[20,10],"PASS"), (30,[1,2],[19,20],"FAIL"),
    (21,[1,2,4],[12,6,3],"PASS"), (30,[1,None],[20,10],"INCOMPLETE"),
], ids=["unequal-paths-at-limit","low-resistance-overload","three-way-12-6-3-split","missing-resistance"])
def test_parallel_conductor_sharing(amps, ohms, limits, expected):
    from benches.x03_quality import parallel_current_quality
    assert parallel_current_quality(amps, dict(enumerate(ohms)), dict(enumerate(limits))) == expected


@pytest.mark.parametrize("upstream,span,expected", [
    ([(10,4,5),(30,2,3)], [10,30], "PASS"),
    ([(10,4,5),(20,1.4,2),(30,2,3)], [10,30], "FAIL"),
    ([(10,4,5),(20,1.5,2),(30,2,3)], [10,30], "FAIL"),
    ([(15,4,5),(30,2,3)], [10,30], "INCOMPLETE"),
], ids=["selective-throughout","interior-knot-overlap","touching-envelope","missing-current-range"])
def test_protection_curve_envelopes(upstream, span, expected):
    from benches.x03_quality import coordination_quality
    downstream = [(10,1,2),(30,0.5,1)]
    assert coordination_quality(downstream, upstream, span) == expected


@pytest.mark.parametrize("segments,expected", [
    ([(0.5,20,0,20)], "PASS"),
    ([(0.5,20,0,21),(0.5,20,90,65)], "FAIL"),
    ([(0.5,20,0,21),(0.5,20,90,66)], "PASS"),
    ([(0.5,20,None,100)], "INCOMPLETE"),
], ids=["straight-at-limit","cumulative-bend-above-65N","same-pull-below-66N","missing-bend"])
def test_pull_sequence(segments, expected):
    from benches.x03_quality import pulling_tension_quality
    rows = [dict(friction=mu, normal_force_n=n, bend_degrees=a, limit_n=limit) for mu,n,a,limit in segments]
    assert pulling_tension_quality(10, rows) == expected


@pytest.mark.parametrize("initial,steps,expected", [
    (6, [(2,1,3.5)], "PASS"), (6, [(-2,1,7),(4,1,2)], "PASS"),
    (6, [(4,1,1),(-4,1,3)], "FAIL"), (6,[(-2,1,8)], "FAIL"), (None, [(2,1,3.5)], "INCOMPLETE"),
], ids=["discharge-loss","charge-then-discharge-at-reserve","intermediate-reserve-violation","idealized-charge","missing-start"])
def test_storage_energy(initial, steps, expected):
    from benches.x03_quality import battery_energy_quality
    rows = [dict(load_kw=p,hours=t,stored_kwh=e) for p,t,e in steps]
    assert battery_energy_quality(initial, 10, 2, 0.5, 0.8, rows) == expected


@pytest.mark.parametrize("power,inv,bat,expected", [
    ([4,-3], (3,5), (4,4), "PASS"), ([5], (3,6), (4,4), "FAIL"),
    ([-4], (3,6), (5,7), "FAIL"), ([1], (3,5), (4,None), "INCOMPLETE"),
], ids=["both-directions-at-limit","battery-discharge-overload","inverter-charge-overload","missing-rating"])
def test_storage_instantaneous_limits(power, inv, bat, expected):
    from benches.x03_quality import battery_energy_quality, battery_power_quality
    # Each step has ample energy headroom over a tenth of an hour.
    energy = 50
    steps = []
    for p in power:
        energy = round(energy - p/10, 2)
        steps.append(dict(load_kw=p,hours=0.1,stored_kwh=energy))
    assert battery_energy_quality(50, 100, 10, 1, 1, steps) == "PASS"
    assert battery_power_quality(power,dict(charge_kw=inv[0],discharge_kw=inv[1]),
                                 dict(charge_kw=bat[0],discharge_kw=bat[1])) == expected


@pytest.mark.parametrize("radius,tension,limit,expected", [
    (0.5,100,200,"PASS"), (0.25,75,299,"FAIL"), (2,200,101,"PASS"), (None,80,200,"INCOMPLETE"),
], ids=["bend-at-limit","tight-bend-overload","wide-bend","missing-radius"])
def test_bend_sidewall_load(radius, tension, limit, expected):
    from benches.x03_quality import sidewall_pressure_quality
    assert sidewall_pressure_quality([dict(radius_m=radius,tension_n=tension,limit_n_per_m=limit)]) == expected


@pytest.mark.parametrize("age,cycles,required,rule,expected", [
    (5,500,72,"product","PASS"), (5,500,80,"product","FAIL"),
    (5,500,80,"minimum","PASS"), (11,500,50,"product","INCOMPLETE"), (5,None,50,"minimum","INCOMPLETE"),
], ids=["aged-exact-boundary","new-capacity-overstates-usable-energy","declared-minimum-model","outside-curve","unknown-cycles"])
def test_retained_battery_energy(age, cycles, required, rule, expected):
    from benches.x03_quality import retained_energy_quality
    assert retained_energy_quality(100,required,age,cycles,[(0,1),(10,0.8)],[(0,1),(1000,0.6)],rule) == expected


@pytest.mark.parametrize("branches,trip,expected", [
    (["east","west"],15,"PASS"), (["east"],15,"FAIL"), (["east","west"],16,"FAIL"),
    (None,15,"INCOMPLETE"), (["west"],None,"INCOMPLETE"),
], ids=["exact-trip-current","uncovered-return","below-sensitivity","missing-coverage","unknown-threshold"])
def test_ground_fault_sensing(branches, trip, expected):
    from benches.x03_quality import ground_fault_quality
    assert ground_fault_quality({"east":40,"west":15},[dict(branches=branches,trip_ma=trip)]) == expected


@pytest.mark.parametrize("strings,series,amps", [(1, 4, [9]), (2, 2, [6, 8]), (3, 3, [1, 7, 10])])
def test_bank_voltage_and_individual_current(strings, series, amps):
    from benches.x03_quality import battery_bank_quality
    cells, edges, currents = {}, {}, {}
    for branch in range(strings):
        nodes = ["neg"] + [f"{branch}/{n}" for n in range(1, series)] + ["pos"]
        for n, edge in enumerate(zip(nodes, nodes[1:])):
            cells[branch, n] = (3.2, 10)
            edges[branch, n] = edge
        currents[branch, 0] = amps[branch]
    def check(graph, volts, readings):
        return battery_bank_quality(cells, graph, ("neg", "pos"), volts, readings)
    target = {2: 6.4, 3: 9.6, 4: 12.8}[series]
    assert check(edges, target, currents) == "PASS"
    assert check(edges, target + 1, currents) == "FAIL"
    assert check(edges, target, {**currents, (0, 0): 11}) == "FAIL"
    assert check({**edges, (0, 0): ("neg", "neg")}, target, currents) == "FAIL"
    assert check({}, target, currents) == "INCOMPLETE"


@pytest.mark.parametrize("third,ninth,expected", [(4, 0, 12), (0, 3, 9), (4, 3, 15)])
def test_neutral_rms_with_balanced_fundamental(third, ninth, expected):
    from benches.x03_quality import neutral_harmonic_quality
    phases = {phase: {1: (30, angle), 3: (third, 0), 9: (ninth, 0)}
              for phase, angle in zip("ABC", (0, 120, 240))}
    assert neutral_harmonic_quality(phases, expected) == "PASS"
    assert neutral_harmonic_quality(phases, expected - 1) == "FAIL"
    del phases["C"][3]
    assert neutral_harmonic_quality(phases, expected) == "INCOMPLETE"


@pytest.mark.parametrize("start,end,dt,rise,fall", [(10, 40, 3, 10, 5), (50, 30, 4, 2, 5), (-5, 5, 0.5, 20, 2)])
def test_recorded_ramp_rate(start, end, dt, rise, fall):
    from benches.x03_quality import inverter_ramp_quality as grade
    assert max(abs(start), abs(end)) <= 50
    assert grade([(0, start), (dt, end)], rise, fall) == "PASS"
    assert grade([(0, start), (dt/2, end), (dt, end)], rise, fall) == "FAIL"
    assert grade([(0, start), (None, end)], rise, fall) == "INCOMPLETE"


@pytest.mark.parametrize("initial,hours,retention,expected", [(100, 2, 0.9, 81), (40, 0.5, 0.81, 36), (12, 3, 0.5, 1.5)])
def test_idle_storage_reserve(initial, hours, retention, expected):
    from benches.x03_quality import idle_reserve_quality as grade
    assert grade(initial, 4, 4 + hours, retention, expected) == "PASS"
    assert grade(initial, 4, 4 + hours, retention, initial) == "FAIL"
    assert grade(initial, 4, 4 + hours, None, expected) == "INCOMPLETE"


@pytest.mark.parametrize("altitude,capacity", [(1500, 90), (2000, 80), (2750, 65)])
def test_altitude_limited_inverter(altitude, capacity):
    from benches.x03_quality import altitude_rating_quality
    curve = dict(model="I100", revision="r2", elevation_kw=[(1000, 100), (2000, 80), (3000, 60)],
                 interpolation="linear-no-extrapolation")
    def grade(record, metres, kw):
        return altitude_rating_quality("I100", "r2", record, metres, kw)
    assert grade(curve, altitude, capacity) == "PASS"
    assert grade(curve, altitude, 100) == "FAIL"
    assert grade(curve, 3001, 1) == "FAIL"
    assert grade(dict(curve, revision="r1"), altitude, capacity) == "FAIL"
    assert grade(None, altitude, capacity) == "INCOMPLETE"


@pytest.mark.parametrize("start,delay,limit", [(100, 10, 30), (4.5, 0.5, 12), (0, 30, 80)])
def test_shutdown_deadline_from_recorded_open(start, delay, limit):
    from benches.x03_quality import rapid_shutdown_quality as grade
    deadline = start + delay
    trace = [(start, 200), (deadline, limit), (deadline+1, 0)]
    assert grade(start, delay, limit, trace) == "PASS"
    assert grade(start, delay, limit, [(start, 200), (deadline, limit+1), (deadline+1, limit)]) == "FAIL"
    assert grade(start, delay, limit, trace + [(deadline+2, -limit-1)]) == "FAIL"
    assert grade(None, delay, limit, trace) == "INCOMPLETE"
    assert grade(start, delay, limit, [(start, 200), (deadline, None)]) == "INCOMPLETE"


@pytest.mark.parametrize("cells,limit", [([3.2, 3.3], 0.1), ([3.5, 3.5, 3.5], 0), ([4, 4.02, 4.04], 0.04)])
def test_cell_spread_with_constant_pack_voltage(cells, limit):
    from benches.x03_quality import cell_spread_quality as grade
    ids = [str(i) for i in range(len(cells))]
    readings = dict(zip(ids, cells))
    assert grade(ids, readings, limit) == "PASS"
    readings[ids[0]] -= 0.1
    readings[ids[-1]] += 0.1
    assert sum(readings.values()) == pytest.approx(sum(cells))
    assert grade(ids, readings, limit) == "FAIL"
    del readings[ids[0]]
    assert grade(ids, readings, limit) == "INCOMPLETE"


@pytest.mark.parametrize("trip,reset,delay", [(10,8,2), (110,100,5), (3.5,3.1,0.5)])
def test_sampled_relay_deadband_and_continuous_reset(trip,reset,delay):
    from benches.x03_quality import relay_trace_quality as grade
    middle = (trip+reset)/2
    trace = [(0,trip,True),(1,middle,True),(2,reset,True),(2+delay/2,middle,True),
             (3+delay,reset,True),(3+2*delay,reset,False)]
    assert grade(trace,trip,reset,delay) == "PASS"
    for index in (1,4):
        early = list(trace)
        early[index] = (*trace[index][:2],False)
        assert grade(early,trip,reset,delay) == "FAIL"
    assert grade(trace,trip,reset,None) == "INCOMPLETE"


@pytest.mark.parametrize("states,expected", [(["sun","shade"],47.5), (["shade","sun","shade"],45),
                                            (["sun","partial"],65)])
def test_substring_bypass_at_series_current(states,expected):
    from benches.x03_quality import bypass_string_quality as grade
    curves = dict(revision="r2", volts_by_current={"sun":[(0,15),(10,5)], "shade":[(0,2),(10,-8)],
                                                   "partial":[(0,6),(10,0)]})
    rows = {i:dict(shading=s,diode_drop=0.5) for i,s in enumerate(states)}
    assert grade(rows,5,"r2",curves,expected) == "PASS"
    assert grade(rows,5,"r2",curves,50*len(states)) == "FAIL"
    del rows[0]["diode_drop"]
    assert grade(rows,5,"r2",curves,expected) == "INCOMPLETE"


@pytest.mark.parametrize("onset,hz,band,trip", [(0,58,(57,59,2,4),2), (10,62,(61,63,1,3),3),
                                                  (0.5,49,(48,50,0.5,1.5),1)])
def test_frequency_disturbance_duration(onset,hz,band,trip):
    from benches.x03_quality import frequency_ride_quality as grade
    end = onset + band[3] + 2
    def trace(duration):
        return [(onset,onset+duration,hz,True),(onset+duration,end,hz,False)]
    assert grade((onset,end),trace(trip),[band]) == "PASS"
    assert grade((onset,end),trace(band[2]/2),[band]) == "FAIL"
    assert grade((onset,end),trace(band[3]+1),[band]) == "FAIL"
    assert grade((onset,end),trace(trip)[:1],[band]) == "INCOMPLETE"


@pytest.mark.parametrize("source,bus,limit", [(400,395,5), (48,48,0), (650,652,2)])
def test_main_contactor_requires_precharge_and_equalization(source,bus,limit):
    from benches.x03_quality import precharge_quality as grade
    begin = dict(time=0,action="close_precharge")
    main = dict(time=1,action="close_main",source_v=source,bus_v=bus)
    end = dict(time=2,action="open_precharge")
    assert grade([begin,main,end],limit) == "PASS"
    assert grade([main,end],limit) == "FAIL"
    assert grade([begin,dict(main,bus_v=source-limit-1),end],limit) == "FAIL"
    assert grade([begin,dict(main,bus_v=None),end],limit) == "INCOMPLETE"
    assert grade([begin,main],limit) == "INCOMPLETE"


@pytest.mark.parametrize("loss,limit", [(0,2), (20,0.5), (100,5)])
def test_island_clears_by_recorded_deadline(loss,limit):
    from benches.x03_quality import islanding_quality as grade
    due, end = loss+limit, loss+limit+2
    ok = [(loss,due,True),(due,end,False)]
    assert grade(loss,end,limit,ok) == "PASS"
    assert grade(loss,end,limit,[(loss,due+1,True),(due+1,end,False)]) == "FAIL"
    assert grade(loss,end,limit,[(loss,due,True),(due,due+1,False),(due+1,end,True)]) == "FAIL"
    assert grade(loss,end,limit,ok[:1]) == "INCOMPLETE"


@pytest.mark.parametrize("clamp,lead,rating", [(900,100,1000),(1200,250,1450),(600,0,600)])
def test_installed_surge_protection(clamp,lead,rating):
    from benches.x03_quality import surge_coordination_quality as grade
    assert grade(clamp,lead,rating) == "PASS"
    assert grade(clamp,lead+1,rating) == "FAIL"
    assert grade(clamp,lead,None) == "INCOMPLETE"


@pytest.mark.parametrize("fundamental,third,fifth,limit", [(100,3,4,5),(240,0,12,5),(120,0,0,0)])
def test_voltage_thd_with_valid_fundamental(fundamental,third,fifth,limit):
    from benches.x03_quality import voltage_thd_quality as grade
    spectrum = {1:fundamental,3:third,5:fifth}
    assert grade(spectrum,[3,5],limit) == "PASS"
    assert grade({**spectrum,5:fifth+1},[3,5],limit) == "FAIL"
    assert grade({1:fundamental,3:third},[3,5],limit) == "INCOMPLETE"


@pytest.mark.parametrize("peak,width,running", [(90,0.1,10),(300,0.5,25),(75,1,15)])
def test_transformer_energization_trip_envelope(peak,width,running):
    from benches.x03_quality import inrush_quality as grade
    policy = [(width,peak),(3,running)]
    trace = [(0,width,peak,False),(width,3,running,False)]
    assert grade(trace,policy) == "PASS"
    assert grade([(0,width,peak,True),trace[1]],policy) == "FAIL"
    assert grade([(0,width+0.1,peak,False),(width+0.1,3,running,False)],policy) == "FAIL"
    assert grade(trace[1:],policy) == "INCOMPLETE"


@pytest.mark.parametrize("ask,spectrum,rating", [
    ("Check the DC link on one switching frequency", {100: 2}, 2),
    ("Check a rectifier with two recorded harmonics", {120: 3, 240: 4}, 5),
    ("Size the filter for all three switching bands", {50: 1, 100: 2, 150: 2}, 3),
])
def test_recorded_capacitor_ripple(ask, spectrum, rating):
    from benches.x03_quality import ripple_current_quality
    bands = set(spectrum)
    assert ripple_current_quality(bands, spectrum, rating) == "PASS"
    assert ripple_current_quality(bands, spectrum, rating - 0.1) == "FAIL"
    assert ripple_current_quality(bands, {}, rating) == "INCOMPLETE"


@pytest.mark.parametrize("capacities,volts,wh", [
    ([4, 10], 6, 24), ([8, 6, 9], 10, 60), ([12, 11, 7, 14], 20, 140),
])
def test_series_pack_requested_energy_at_first_cutoff(capacities, volts, wh):
    from benches.x03_quality import series_energy_quality
    cells = dict(enumerate(capacities))
    def check(data, required):
        return series_energy_quality(set(cells), data, "first-cell-cutoff", volts, required)
    assert check(cells, wh) == "PASS"
    assert check(cells, wh + 1) == "FAIL"
    assert check(dict(list(cells.items())[1:]), wh) == "INCOMPLETE"


@pytest.mark.parametrize("segment,start,limit", [("string", 12, 2), ("combiner", 0.1, 0.2), ("feeder", 80, 1.5)])
def test_clear_recorded_arc_before_deadline(segment, start, limit):
    from fractions import Fraction
    from benches.x03_quality import arc_interruption_quality
    end = float(Fraction(str(start)) + Fraction(str(limit)))
    sensor = "sensor-" + segment
    event = {"arc": (segment, start)}
    trace = dict(sensor=sensor, detected=start, interrupted=end, through=end+1)
    def grade(record, coverage=sensor):
        return arc_interruption_quality(event, {segment: coverage}, {"arc": record}, limit)
    assert grade(trace) == "PASS"
    assert grade(dict(trace, interrupted=end+0.01)) == "FAIL"
    assert grade(dict(trace, interrupted=None, detected=None)) == "FAIL"
    assert grade(trace, False) == "FAIL"
    assert grade(None) == "INCOMPLETE"
    assert grade(dict(trace, through=start, detected=None, interrupted=None)) == "INCOMPLETE"


@pytest.mark.parametrize("condition,point,expected", [("hot", (5, 6), "PASS"), ("cold", (5, -4), "PASS"), ("high-altitude", (5, 6.1), "FAIL")])
def test_reactive_dispatch_inside_ten_kva_nameplate(condition, point, expected):
    from benches.x03_quality import reactive_envelope_quality
    curve = {"revision": "r2", "conditions": {condition: [(0,-5,8), (10,-3,4)]}}
    def grade(p, q, catalog=curve):
        return reactive_envelope_quality("r2", catalog, condition, p, q)
    assert grade(*point) == expected
    assert grade(5, -4.1) == "FAIL"
    assert grade(5, 6) == "PASS"
    assert grade(5, 6, {}) == "INCOMPLETE"


@pytest.mark.parametrize("voltage,band,minimum,maximum", [
    (0.45, (0.3, 0.5), 1, 3), (0.82, (0.8, 0.9), 4, 6), (1.2, (1.1, 1.3), 2, 5),
])
def test_voltage_excursion_trips_in_recorded_window(voltage, band, minimum, maximum):
    from benches.x03_quality import voltage_ride_quality
    profile = dict(revision="grid-2", bands=[(*band, minimum, maximum)])
    end = maximum + 2
    def grade(trip, covered):
        return voltage_ride_quality((0, end), [(0, trip, voltage, True), (trip, covered, voltage, False)], "grid-2", profile)
    assert grade(minimum, end) == grade(maximum, end) == "PASS"
    assert grade(minimum/2, end) == grade(maximum+1, end) == "FAIL"
    assert grade(minimum, end-1) == "INCOMPLETE"


@pytest.mark.parametrize("category,v,a", [("resistive",48,10), ("inductive",125,4), ("pv",600,15)])
def test_break_dc_load_under_recorded_conditions(category, v, a):
    from benches.x03_quality import dc_load_break_quality as grade
    device = dict(revision="r", load_break=True, dc_ratings={category: (v, a)})
    assert grade("r", device, category, v, a) == "PASS"
    assert grade("r", device, category, v, a+1) == "FAIL"
    assert grade("r", dict(device, load_break=False), category, v, a) == "FAIL"
    assert grade("r", dict(device, dc_ratings={}), category, v, a) == "INCOMPLETE"


@pytest.mark.parametrize("steps,loss,shortcut", [
    ([(1,2)], 0.4, 0.1), ([(2,3)], 0.9, 0.3), ([(1,1),(3,1),(5,2)], 1, 0.7),
])
def test_inverter_interval_energy_losses(steps, loss, shortcut):
    from benches.x03_quality import conversion_loss_quality as grade
    curve = dict(revision="r4", dc_kw_efficiency=[(1,0.8),(3,0.9),(5,0.95)])
    assert grade("r4",curve,steps,loss) == "PASS"
    assert grade("r4",curve,steps,shortcut) == "FAIL"
    assert grade("r4",curve,steps+[(6,1)],loss) == "INCOMPLETE"


@pytest.mark.parametrize("steps,energy", [([(3,12)],0.01), ([(2,9),(4,9)],0.015), ([(5,3),(5,6),(1,9)],0.015)])
def test_energize_battery_controls_before_main_supply(steps, energy):
    from benches.x03_quality import black_start_quality as grade
    assert grade(steps,2,5,12,energy) == "PASS"
    assert grade(steps,2,5,12,energy/2) == "FAIL"
    assert grade(steps+[(6,1)],2,5,12,10) == "FAIL"
    assert grade([(4,8),(3,5)],2,5,12,10) == "FAIL"
    assert grade(None,2,5,12,energy) == "INCOMPLETE"


@pytest.mark.parametrize("equipment,terminals,rotation,phases", [
    ("pump", ("U","V","W"), "forward", ("B","C","A")),
    ("fan", ("1","2","3"), "reverse", ("A","C","B")),
    ("drive", ("R","S","T"), "forward", ("C","A","B")),
])
def test_equipment_rotation_with_balanced_supply(equipment, terminals, rotation, phases):
    from benches.x03_quality import phase_sequence_quality as grade
    mapping = dict(zip(terminals, phases))
    def check(mapping):
        return grade(("A","B","C"), terminals, mapping, rotation)
    assert check(mapping) == "PASS"
    a, b, c = terminals
    mapping[a], mapping[b] = mapping[b], mapping[a]
    assert check(mapping) == "FAIL"
    del mapping[c]
    assert check(mapping) == "INCOMPLETE"


@pytest.mark.parametrize("capacity,start,target,power,deadline,shortcut", [
    (10,0,1,10,3,1), (20,0.25,0.75,4,3.75,2.5), (8,0.5,1,8,2,0.5),
])
def test_recharge_before_deadline_with_soc_taper(capacity,start,target,power,deadline,shortcut):
    from benches.x03_quality import recharge_deadline_quality
    curve = dict(revision="r", method="step-kw", soc_bands=[(0,0.5,10),(0.5,1,2)])
    def grade(hours, data=curve):
        return recharge_deadline_quality("r",data,capacity,start,target,power,hours)
    assert grade(deadline) == "PASS"
    assert grade(shortcut) == "FAIL"
    assert grade(deadline,dict(curve,soc_bands=[(0,0.5,10)])) == "INCOMPLETE"


@pytest.mark.parametrize("ask,angle,hz,volts,field", [
    ("Reconnect across north wrap", 358, -0.1, 4, "angle_deg"),
    ("Close after frequency settles", -2, 0.1, -4, "frequency_hz"),
    ("Transfer at matched voltage", 362, 0, 4, "voltage_v"),
])
def test_grid_closure_at_simultaneous_limits(ask, angle, hz, volts, field):
    from benches.x03_quality import grid_closure_quality as grade
    limits = dict(angle_deg=2, frequency_hz=0.1, voltage_v=4)
    observed = dict(angle_deg=angle, frequency_hz=hz, voltage_v=volts)
    assert grade(observed, limits) == "PASS"
    observed[field] = -(limits[field] + 0.01)
    assert grade(observed, limits) == "FAIL"
    del observed[field]
    assert grade(observed, limits) == "INCOMPLETE"


@pytest.mark.parametrize("degree,path,surface,air", [
    (1, [(0,0,0),(3,0,0),(3,4,0)], 7, 5),
    (2, [(1,0,2),(1,5,2),(13,5,2)], 17, 13),
    (3, [(0,0,0),(0,0,8),(0,15,8)], 23, 17),
])
def test_insulator_surface_and_air_paths(degree,path,surface,air):
    from benches.x03_quality import insulation_spacing_quality as grade
    def check(surface_min, air_min, pollution=degree):
        rule = dict(revision="sheet", degrees={degree:dict(surface_mm=surface_min,air_mm=air_min)})
        return grade(path,pollution,"sheet",rule)
    assert check(surface,air) == "PASS"
    assert check(surface+1,air) == check(surface,air+1) == "FAIL"
    assert check(surface,air,None) == "INCOMPLETE"


@pytest.mark.parametrize("ask,magnitude,rotation,error,lower,upper", [
    ("Check the motor feeder", 230, 0, 10, 5.8, 5.9),
    ("Check the rotated service reference", 120, 75, 30, 17.7, 17.9),
    ("Check the inverter output", 277, -20, -60, 37.7, 37.9),
])
def test_phase_angle_unbalance_with_equal_rms_magnitudes(ask,magnitude,rotation,error,lower,upper):
    from benches.x03_quality import voltage_unbalance_quality as grade
    # For one displaced phase, the ratio is |exp(i*error)-1| / |2+exp(i*error)|.
    phases = {p:(magnitude,rotation+a) for p,a in zip("ABC",(0,-120,120))}
    assert grade(phases,0) == "PASS"
    phases["C"] = (magnitude,rotation+120+error)
    assert grade(phases,lower) == "FAIL"
    assert grade(phases,upper) == "PASS"
    phases["C"] = (magnitude,None)
    assert grade(phases,upper) == "INCOMPLETE"


@pytest.mark.parametrize("amps,ohms,duration,limit", [(200,0.4,0.1,80),(1000,0.04,0.5,40),(80,0.25,2,20)])
def test_touch_voltage_for_fault_duration(amps,ohms,duration,limit):
    from benches.x03_quality import touch_voltage_quality as grade
    bands = [(0.1,80),(0.5,40),(2,20)]
    assert grade(amps,ohms,duration,bands) == "PASS"
    assert grade(amps+1,ohms,duration,bands) == "FAIL"
    assert grade(amps,None,duration,bands) == "INCOMPLETE"
    assert grade(amps,ohms,3,bands) == "INCOMPLETE"


@pytest.mark.parametrize("ask,farads,start,cutoff,watts,seconds", [
    ("Keep the controller online", 1,10,6,8,4),
    ("Bridge a DC bus interruption", 0.02,100,80,12,3),
    ("Hold the relay through transfer", 0.5,24,12,18,6),
])
def test_hold_up_until_load_cutoff(ask,farads,start,cutoff,watts,seconds):
    from benches.x03_quality import capacitor_hold_up_quality as grade
    assert grade(farads,start,cutoff,watts,seconds) == "PASS"
    assert grade(farads,start,cutoff,watts,seconds+0.01) == "FAIL"
    assert grade(farads,start,cutoff,None,seconds) == "INCOMPLETE"


@pytest.mark.parametrize("hz,x,c,order", [(50,0.1,0.00127,5),(60,0.15,0.00036,7),(50,0.2,0.00177,3)])
def test_bank_resonance_near_present_harmonic(hz,x,c,order):
    from benches.x03_quality import bank_resonance_quality as grade
    source = dict(revision="sc",reference_hz=hz,inductive_reactance_ohm=x)
    assert grade("sc",source,c,{1:10,order:2},2) == "FAIL"
    assert grade("sc",source,c,{1:10,order:0},2) == "PASS"
    assert grade("sc",source,c,None,2) == "INCOMPLETE"


@pytest.mark.parametrize("trace,window,dc", [
    ([(0,1,5),(1,2,-3)], (0,2), 1),
    ([(0,2,-6),(2,3,6)], (0,3), 2),
    ([(0,2,8),(2,5,-2)], (1,4), 4/3),
])
def test_grid_dc_offset_from_signed_interval_means(trace,window,dc):
    from benches.x03_quality import dc_injection_quality as grade
    # Every instantaneous magnitude is below the recorded 10 A RMS ceiling.
    assert max(abs(row[2]) for row in trace) <= 10
    assert grade(trace,[window],dc+0.000001) == "PASS"
    assert grade(trace,[window],dc-0.01) == "FAIL"
    assert grade(trace[:-1],[window],dc) == "INCOMPLETE"
    assert grade([(0,1,5),(1,2,-5)],[(0,2),(0,1)],1) == "FAIL"




@pytest.mark.parametrize("trace,window,threshold,delay,due", [
    ([60,60.1,60.2,60.3],1,0.1,2,3),
    ([50,49.9,49.7,49.6,49.5],2,0.15,1,3),
    ([60,60.2,60.2,60.4,60.6,60.8],1,0.2,2,5),
])
def test_sampled_rocof_trip(trace,window,threshold,delay,due):
    from benches.x03_quality import rocof_quality as grade
    samples = list(enumerate(trace))
    assert grade(samples,window,threshold,delay,due) == "PASS"
    assert grade(samples,window,threshold,delay,due+1) == "FAIL"
    assert grade(samples,window,threshold,delay,None) == "FAIL"
    assert grade(samples,window,threshold,None,due) == "INCOMPLETE"


@pytest.mark.parametrize("hz,expected,shortcut", [(59.5,58,50),(60.05,50,49),(61.5,40,36)])
def test_pinned_droop_dispatch(hz,expected,shortcut):
    from benches.x03_quality import droop_quality as grade
    args = (hz,60,0.1,50,(20,10))
    assert grade(*args,(40,60),expected) == "PASS"
    assert grade(*args,(40,60),shortcut) == "FAIL"
    assert grade(*args,None,expected) == "INCOMPLETE"


@pytest.mark.parametrize("amps,polarity,final,shortcut", [
    ((4,-2),"charging-positive",12.5,14),
    ((2,6),"discharging-positive",3,4),
    ((-2,6),"charging-positive",15,13),
])
def test_interval_charge_reconciliation(amps,polarity,final,shortcut):
    from benches.x03_quality import coulomb_count_quality as grade
    # Half an hour followed by one hour, except the first case starts at 12.5 Ah.
    initial = 12.5 if amps == (4,-2) else 10
    spans = [(0,1800,amps[0]),(1800,5400,amps[1])]
    assert grade((0,5400),spans,initial,final,polarity) == "PASS"
    assert grade((0,5400),spans,initial,shortcut,polarity) == "FAIL"
    assert grade((0,5400),spans[1:],initial,final,polarity) == "INCOMPLETE"


@pytest.mark.parametrize("equipment,turns,volts,dt,initial,peak", [
    ("control transformer",100,10,0.1,0,0.01),
    ("isolation transformer",200,-20,0.2,-0.01,0.03),
    ("converter winding",50,5,0.2,0.02,0.04),
])
def test_peak_flux_before_volt_seconds_cancel(equipment,turns,volts,dt,initial,peak):
    from benches.x03_quality import transformer_flux_quality as grade
    trace = [(0,dt,volts),(dt,2*dt,-volts)]
    # Both polarities have the same RMS, below the independent 24 V ceiling.
    assert abs(volts) <= 24
    assert grade((0,2*dt),trace,turns,initial,peak) == "PASS"
    assert grade((0,2*dt),trace,turns,initial,peak-0.001) == "FAIL"
    assert grade((0,2*dt),trace,turns,initial,None) == "INCOMPLETE"
    assert grade((0,2*dt),trace[:1],turns,initial,peak) == "INCOMPLETE"


@pytest.mark.parametrize("cmd,delay,safe,closed,voltage", [
    (0,1,5,True,0), (10,2,10,False,48), (20,3,12,True,120),
])
def test_open_command_requires_two_isolation_observations(cmd,delay,safe,closed,voltage):
    from benches.x03_quality import contactor_open_quality as grade
    due = cmd+delay
    start = (cmd,due,False,120)
    def check(tail):
        return grade(cmd,delay,due+1,safe,[start,tail])
    assert check((due,due+1,True,safe)) == "PASS"
    assert check((due,due+1,not closed,voltage)) == "FAIL"
    assert check((due,due+1,None,0)) == "INCOMPLETE"


@pytest.mark.parametrize("losses,ambient,r_case,r_air,peak,current,rated", [
    pytest.param([10,5],40,1,3,100,20,30,id="inverter-switch-at-full-load"),
    pytest.param([4,2],55,2,3,85,6,10,id="rectifier-in-hot-cabinet"),
    pytest.param([16,4],25,0.25,1,50,15,25,id="converter-with-heatsink"),
])
def test_junction_temperature(losses,ambient,r_case,r_air,peak,current,rated):
    from benches.x03_quality import junction_temperature_quality as grade
    assert current <= rated
    path = [("junction","case",r_case),("case","ambient",r_air)]
    assert grade(ambient,losses,path,peak) == "PASS"
    assert grade(ambient,losses,path,peak-1) == "FAIL"
    assert grade(ambient,losses,path[1:],peak) == "INCOMPLETE"
    assert grade(ambient,losses,path[:1],peak) == "INCOMPLETE"


@pytest.mark.parametrize("cap,initial,safe,ohms,deadline,energy", [
    pytest.param(0.01,100,10,100,2.31,50,id="dc-link-to-ten-volts"),
    pytest.param(0.002,200,100,500,0.70,40,id="converter-half-voltage"),
    pytest.param(0.1,20,5,10,1.39,20,id="storage-bleeder-to-quarter-voltage"),
])
def test_discharge_time_and_full_pulse_energy(cap,initial,safe,ohms,deadline,energy):
    from benches.x03_quality import discharge_resistor_quality as grade
    assert grade(cap,initial,safe,deadline,ohms,energy) == "PASS"
    assert grade(cap,initial,safe,deadline-0.01,ohms,energy) == "FAIL"
    assert grade(cap,initial,safe,deadline,ohms,energy-1) == "FAIL"
    assert grade(cap,initial,safe,deadline,ohms,None) == "INCOMPLETE"


@pytest.mark.parametrize("cells,strings,common,duty", [
    ({"A":(3,0.02),"B":(3,0.02)},[(["A","B"],0.01)],0.01,100),
    ({"A":(4,0.03),"B":(4,0.03)},[(["A"],0.01),(["B"],0.01)],0.02,100),
    ({"A":(3,0.02),"B":(3,0.04)},[(["A"],0.01),(["B"],0.02)],0,150),
])
def test_fault_duty_for_series_parallel_and_unequal_strings(cells,strings,common,duty):
    from benches.x03_quality import battery_fault_quality as grade
    assert grade(cells,strings,common,duty) == "PASS"
    assert grade(cells,strings,common,duty-1) == "FAIL"
    assert grade(cells,[(strings[0][0],None),*strings[1:]],common,duty) == "INCOMPLETE"


@pytest.mark.parametrize("nominal,phase,delay,offset", [
    pytest.param(230,0,2,0,id="pump-loses-phase-A"),
    pytest.param(120,1,1,4,id="fan-loses-phase-B-after-recovery"),
    pytest.param(277,2,0,8,id="compressor-immediate-phase-C-trip"),
])
def test_phase_loss_deadline(nominal,phase,delay,offset):
    from benches.x03_quality import phase_loss_quality as grade
    good = [nominal]*3
    lost = good.copy()
    lost[phase] = 0
    start = offset+2
    due = start+delay
    prefix = [(offset,offset+1,lost,False),(offset+1,start,good,True)]
    if delay:
        prefix.append((start,due,lost,True))
    args = ((offset,due+1),nominal*0.8,delay)
    def check(rows):
        return grade(args[0],rows,args[1],args[2])
    assert check(prefix+[(due,due+1,lost,False)]) == "PASS"
    assert check(prefix+[(due,due+1,lost,True)]) == "FAIL"
    assert check(prefix) == "INCOMPLETE"
    assert check(prefix+[(due,due+1,lost[:2],False)]) == "INCOMPLETE"


@pytest.mark.parametrize("fault,ratio,burden,max_fraction", [
    (1000,100,1,0.01),(2000,100,1,0.05),(1500,100,1,0.037),
])
def test_ct_excitation_error_budget(fault,ratio,burden,max_fraction):
    from benches.x03_quality import ct_excitation_quality as grade
    curve = {"revision":"lab","rms_v_excitation_a":[(0,0),(10,0.1),(20,1)]}
    # Excitation at 10, 20 and 15 V is 0.1, 1 and 0.55 A respectively.
    assert grade("lab",curve,fault,ratio,burden,max_fraction) == "PASS"
    assert grade("lab",curve,fault,ratio,burden,max_fraction/2) == "FAIL"
    assert grade("lab",{},fault,ratio,burden,max_fraction) == "INCOMPLETE"


@pytest.mark.parametrize("group,other,ratio,z", [("Dyn11","Dyn1",10,5),("Yy0","Yy6",20,4),("Dd6","Dd0",50,8)])
def test_parallel_bank_requires_matching_phase_and_spreads(group,other,ratio,z):
    from benches.x03_quality import transformer_parallel_quality as grade
    first = dict(revision="study",vector_group=group,tap_ratio=ratio,z_common_base=z)
    def check(**changes):
        return grade("study",[first,dict(first,**changes)],0.02,0.1)
    assert check() == "PASS"
    assert check(vector_group=other) == "FAIL"
    assert check(tap_ratio=ratio*1.03) == "FAIL"
    assert check(z_common_base=z*1.2) == "FAIL"
    assert check(vector_group=None) == "INCOMPLETE"


@pytest.mark.parametrize("sections,peak", [
    ([(100,0.1)],10), ([(100,-0.2),(50,0.4)],20), ([(20,0.5),(40,0.25)],20),
])
def test_grounded_end_sheath_voltage_profile(sections,peak):
    from benches.x03_quality import sheath_voltage_quality as grade
    rule = "single-point-bonded-linear-v1"
    assert grade(rule,sections,peak) == "PASS"
    assert grade(rule,sections,peak-1) == "FAIL"
    assert grade(rule,[(100,None)],peak) == "INCOMPLETE"
    assert grade(None,sections,peak) == "INCOMPLETE"
    assert grade("independently-bonded-spans-v1",[(10,1),(10,1)],10) == "PASS"
    assert grade(rule,[(10,1),(10,1)],10) == "FAIL"


@pytest.mark.parametrize("source,ohms,locked,delay,normal,floor", [
    pytest.param(240,0.2,150,2,220,210,id="pump"),
    pytest.param(120,0.25,80,1,110,100,id="fan"),
    pytest.param(480,0.4,200,3,450,400,id="compressor"),
])
def test_motor_sag_duration_and_depth(source,ohms,locked,delay,normal,floor):
    from benches.x03_quality import motor_start_quality as grade
    assert source-ohms*(locked/10) >= normal
    assert grade(source,ohms,locked,delay,normal,floor,delay) == "PASS"
    assert grade(source,ohms,locked,delay+0.1,normal,floor,delay) == "FAIL"
    assert grade(source,ohms,locked+1,delay,normal,floor,delay) == "FAIL"
    assert grade(source,None,locked,delay,normal,floor,delay) == "INCOMPLETE"


@pytest.mark.parametrize("spectrum,watts", [({1:4,2:2},52),({1:3,2:0,3:2},58),({1:5,2:1,3:0,4:1},72)])
def test_transformer_harmonic_loss(spectrum,watts):
    from benches.x03_quality import harmonic_load_quality as grade
    model = dict(revision="lab",highest_order=len(spectrum),copper_w_per_a2=1,eddy_w_per_a2=1,max_w=watts)
    assert grade("lab",model,spectrum) == "PASS"
    assert grade("lab",dict(model,max_w=watts-1),spectrum) == "FAIL"
    assert grade("lab",model,{1:spectrum[1]}) == "INCOMPLETE"


@pytest.mark.parametrize("points,load,expected,constant_ah", [
    pytest.param([(5, 32), (20, 2)], 10, 8, 16, id="remote-pump-backup"),
    pytest.param([(2, 72), (8, 9)], 4, 18 * 2 ** 0.5, 36, id="control-room-ups"),
    pytest.param([(3, 20), (6, 8), (24, 2)], 6, 8, 10, id="radio-bank-at-cutoff"),
])
def test_peukert_runtime(points, load, expected, constant_ah):
    from benches.x03_quality import peukert_runtime_quality

    # At geometric-midpoint currents the measured durations have a geometric mean.
    # The radio-bank value is a direct measurement at the cutoff.
    curve = dict(revision="r1", cutoff_policy="end-voltage-A", current_a_runtime_h=points)

    def grade(current, runtime, cutoff="end-voltage-A"):
        return peukert_runtime_quality("r1", curve, cutoff, current, runtime, 1e-9)

    assert grade(load, expected) == "PASS"
    assert grade(load, constant_ah) == "FAIL"
    assert grade(points[0][0] / 2, expected) == "INCOMPLETE"
    assert grade(points[-1][0] * 2, expected) == "INCOMPLETE"
    assert grade(load, expected, "other-cutoff") == "INCOMPLETE"
    assert peukert_runtime_quality("r2", curve, "end-voltage-A", load, expected, 0) == "INCOMPLETE"
    curve["current_a_runtime_h"] = points + [(points[-1][0], None)]
    assert grade(load, expected) == "INCOMPLETE"


@pytest.mark.parametrize("pulse,curve,voltage,allowed", [
    pytest.param(0.001, [(10, 16), (40, 4)], 20, 8, id="converter-switch-pulse"),
    pytest.param(0.02, [(20, 12), (80, 3)], 40, 6, id="precharge-transistor"),
    pytest.param(0.1, [(12, 10), (24, 6), (48, 3)], 24, 6, id="linear-regulator-startup"),
])
def test_semiconductor_soa(pulse, curve, voltage, allowed):
    from benches.x03_quality import semiconductor_soa_quality

    row = dict(pulse_s=pulse, voltage_v_current_a=curve)
    envelope = dict(revision="r4", curves=[row])

    def grade(current, duration=pulse):
        return semiconductor_soa_quality("r4", envelope, voltage, current, duration)

    assert grade(allowed) == "PASS"
    assert allowed + 1 < curve[0][1]  # Separate current and voltage checks would pass.
    assert grade(allowed + 1) == "FAIL"
    assert grade(allowed, pulse * 1.5) == "INCOMPLETE"
    assert semiconductor_soa_quality("r3", envelope, voltage, allowed, pulse) == "INCOMPLETE"
    row["voltage_v_current_a"] = []
    assert grade(allowed) == "INCOMPLETE"
