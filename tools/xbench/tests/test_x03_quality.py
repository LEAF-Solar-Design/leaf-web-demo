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
