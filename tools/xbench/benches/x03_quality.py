"""Independent X03 quality checks for recorded drawing outputs."""
from collections import Counter
from fractions import Fraction
from math import ceil


def route_quality(route, aisle, arrays):
    if route is None or aisle is None or arrays is None:
        return "INCOMPLETE"
    polygons = [aisle, *arrays]
    if route.is_empty or any(p is None or p.is_empty for p in polygons):
        return "INCOMPLETE"
    if (route.geom_type != "LineString" or not route.is_valid
            or any(p.geom_type not in {"Polygon", "MultiPolygon"} or not p.is_valid for p in polygons)):
        return "FAIL"
    coords = list(route.coords)
    if any(a[:2] == b[:2] for a, b in zip(coords, coords[1:])):
        return "FAIL"
    if not aisle.covers(route) or any(route.relate_pattern(a, "T********") for a in polygons[1:]):
        return "FAIL"
    return "PASS"


def inverter_quality(strings, capacities, groups, *, eligibility=None):
    if capacities is None or groups is None or strings is None:
        return "INCOMPLETE"
    if (not capacities or len(groups) != len(capacities) or
            any(type(c) is not int or c <= 0 for c in capacities)):
        return "FAIL"
    loads = tuple(map(len, groups))
    if (len(set(strings)) != len(strings) or Counter(strings) != Counter(s for g in groups for s in g)
            or any(n > c for n, c in zip(loads, capacities))):
        return "FAIL"
    fills = [Fraction(n, c) for n, c in zip(loads, capacities)]
    spread = max(fills) - min(fills)
    if eligibility is not None:
        if not isinstance(eligibility, dict) or set(eligibility) != set(strings):
            return "INCOMPLETE"
        for options in eligibility.values():
            if not isinstance(options, (list, tuple, set)) or any(
                    type(i) is not int or not 0 <= i < len(groups) for i in options):
                return "INCOMPLETE"
        if any(i not in eligibility[s] for i, group in enumerate(groups) for s in group):
            return "FAIL"
        if spread == 0:
            return "PASS"
        # Each tuple represents one reachable load distribution after this prefix.
        states = {(0,) * len(groups)}
        for string in strings:
            following = set()
            for state in states:
                for i in eligibility[string]:
                    if state[i] < capacities[i]:
                        following.add(state[:i] + (state[i] + 1,) + state[i+1:])
                        if len(following) > 100_000:
                            return "INCOMPLETE"
            states = following
        for state in states:
            fills = [Fraction(n, c) for n, c in zip(state, capacities)]
            if max(fills) - min(fills) < spread:
                return "FAIL"
        return "PASS"
    if spread == 0:
        return "PASS"
    total = len(strings)
    # Every better assignment has a minimum fill k/c. For that lower bound,
    # integer loads in [low*c, (low+spread)*c) realize every sum in their range.
    for capacity in set(capacities):
        for k in range(min(total, capacity) + 1):
            low = Fraction(k, capacity)
            bounds = [(ceil(low*c), min(c, ceil((low+spread)*c)-1)) for c in capacities]
            if (all(a <= b for a, b in bounds)
                    and sum(a for a, _ in bounds) <= total <= sum(b for _, b in bounds)):
                return "FAIL"
    return "PASS"


def string_quality(members, adjacency):
    if not members or adjacency is None or any(adjacency.get(m) is None for m in members):
        return "INCOMPLETE"
    selected = set(members)
    if len(selected) != len(members):
        return "FAIL"
    neighbors = {m: selected.intersection(adjacency[m]) for m in members}
    if any(m in edges or any(m not in neighbors[n] for n in edges) for m, edges in neighbors.items()):
        return "FAIL"
    if len(selected) == 1:
        return "PASS"
    degrees = Counter(map(len, neighbors.values()))
    if degrees != Counter({1: 2, 2: len(selected)-2}):
        return "FAIL"
    pending = [members[0]]
    reached = set()
    while pending:
        member = pending.pop()
        if member not in reached:
            reached.add(member)
            pending.extend(neighbors[member] - reached)
    return "PASS" if reached == selected else "FAIL"


def finite_measure(value):
    from math import isfinite
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def string_edge_quality(members, adjacency, coordinates, max_distance):
    from math import dist
    chain = string_quality(members, adjacency)
    if chain != "PASS":
        return chain
    if coordinates is None or max_distance is None:
        return "INCOMPLETE"
    if not finite_measure(max_distance) or max_distance < 0:
        return "FAIL"
    selected = set(members)
    for member in members:
        point = coordinates.get(member)
        if not isinstance(point, (list, tuple)) or len(point) not in (2, 3) or not all(map(finite_measure, point)):
            return "INCOMPLETE"
    for member in members:
        for other in selected.intersection(adjacency[member]):
            a, b = coordinates[member], coordinates[other]
            if len(a) != len(b):
                return "INCOMPLETE"
            if dist(a, b) > max_distance:
                return "FAIL"
    return "PASS"


def bundled_route_quality(routes, endpoints, trunk, *, min_shared, max_detour):
    """Require each branch to follow the common trunk and meet its detour ceiling."""
    if routes is None or endpoints is None or trunk is None or len(routes) != len(endpoints):
        return "INCOMPLETE"
    if not finite_measure(min_shared) or not finite_measure(max_detour):
        return "FAIL"
    if len(routes) < 2 or min_shared <= 0 or max_detour < 1:
        return "FAIL"
    common = trunk
    for route, ends in zip(routes, endpoints):
        if ends is None or len(ends) != 2 or any(p is None or p.is_empty for p in ends):
            return "INCOMPLETE"
        if route is None or route.is_empty:
            return "INCOMPLETE"
        if route.geom_type != "LineString" or not route.is_valid or not route.is_simple:
            return "FAIL"
        start, end = ends
        if (tuple(start.coords[0]) != route.coords[0] or tuple(end.coords[0]) != route.coords[-1]
                or not finite_measure(max_detour * start.distance(end))
                or route.length > max_detour * start.distance(end)):
            return "FAIL"
        common = common.intersection(route)
    return "PASS" if common.length >= min_shared else "FAIL"


def clearance_quality(route, aisle, arrays, clearance):
    from math import isfinite
    if clearance is None:
        return "INCOMPLETE"
    if type(clearance) not in (int, float) or not isfinite(clearance) or clearance < 0:
        return "FAIL"
    verdict = route_quality(route, aisle, arrays)
    if verdict != "PASS":
        return verdict
    # Distance is the exact buffer-radius test and includes boundary equality.
    distances = [route.distance(aisle.boundary), *(route.distance(a) for a in arrays)]
    return "PASS" if min(distances) >= clearance else "FAIL"


def string_partition_quality(strings, inventory, adjacency):
    """Every inventoried panel must belong to exactly one contiguous string."""
    if not isinstance(inventory, (list, tuple)) or not inventory or not isinstance(adjacency, dict):
        return "INCOMPLETE"
    if not isinstance(strings, (list, tuple)) or any(not isinstance(g, (list, tuple)) for g in strings):
        return "INCOMPLETE"
    try:
        expected = Counter(inventory)
        actual = Counter(panel for group in strings for panel in group)
    except TypeError:
        return "INCOMPLETE"
    if any(n != 1 for n in expected.values()):
        return "INCOMPLETE"
    if actual != expected or any(not group for group in strings):
        return "FAIL"
    for group in strings:
        verdict = string_quality(group, adjacency)
        if verdict != "PASS":
            return verdict
    return "PASS"


def wiring_quality(strings, module_ratings, max_voltage, max_current):
    """Grade parallel strings of identical series modules at the supplied rating point.

    Each record names the module type and series count. Limits and ratings
    are supplied for the same operating condition.
    """
    if not strings or not isinstance(module_ratings, dict) or None in (max_voltage, max_current):
        return "INCOMPLETE"
    if not all(finite_measure(v) and v > 0 for v in (max_voltage, max_current)):
        return "FAIL"
    parallel_current = Fraction(0)
    for string in strings:
        if not isinstance(string, dict) or not isinstance(string.get("module"), str):
            return "INCOMPLETE"
        rating = module_ratings.get(string["module"])
        if not isinstance(rating, dict) or any(rating.get(k) is None for k in ("volts", "amps")):
            return "INCOMPLETE"
        count = string.get("series_count")
        if count is None:
            return "INCOMPLETE"
        if type(count) is not int or count < 1 or not all(
                finite_measure(rating[k]) and rating[k] > 0 for k in ("volts", "amps")):
            return "FAIL"
        if count * Fraction(str(rating["volts"])) > Fraction(str(max_voltage)):
            return "FAIL"
        parallel_current += Fraction(str(rating["amps"]))
    return "PASS" if parallel_current <= Fraction(str(max_current)) else "FAIL"


def mppt_quality(currents, trackers, assignments):
    """Require exactly one tracker assignment per string and enforce each tracker limit."""
    if not all(isinstance(x, dict) and x for x in (currents, trackers, assignments)):
        return "INCOMPLETE"
    if set(assignments) != set(currents):
        return "FAIL"
    loads = Counter()
    amps = Counter()
    for string, tracker in assignments.items():
        if not isinstance(tracker, (str, int)) or tracker not in trackers:
            return "FAIL"
        current = currents[string]
        if current is None:
            return "INCOMPLETE"
        if not finite_measure(current) or current <= 0:
            return "FAIL"
        loads[tracker] += 1
        amps[tracker] += Fraction(str(current))
    for tracker, limits in trackers.items():
        if not isinstance(limits, dict) or any(limits.get(k) is None for k in ("inputs", "amps")):
            return "INCOMPLETE"
        count, current = limits["inputs"], limits["amps"]
        if type(count) is not int or count < 0 or not finite_measure(current) or current < 0:
            return "FAIL"
        if loads[tracker] > count or amps[tracker] > Fraction(str(current)):
            return "FAIL"
    return "PASS"


def terminal_route_quality(routes, terminals, destinations, assignments, trunk, tolerance):
    """Check complete directed planar paths including their declared common trunk."""
    from shapely.geometry import Point
    if not all(isinstance(x, dict) and x for x in (routes, terminals, destinations, assignments)):
        return "INCOMPLETE"
    if trunk is None or tolerance is None:
        return "INCOMPLETE"
    if (trunk.geom_type != "LineString" or trunk.is_empty or not trunk.is_valid
            or not finite_measure(tolerance) or tolerance < 0):
        return "FAIL"
    if set(routes) != set(terminals) or set(assignments) != set(terminals):
        return "FAIL"
    for terminal, route in routes.items():
        start = terminals[terminal]
        end = destinations.get(assignments[terminal])
        if any(p is None or p.is_empty for p in (start, end, route)):
            return "INCOMPLETE"
        if (start.geom_type != "Point" or end.geom_type != "Point" or route.geom_type != "LineString"
                or not route.is_valid or not route.is_simple or not route.covers(trunk)
                or Point(route.coords[0]).distance(start) > tolerance
                or Point(route.coords[-1]).distance(end) > tolerance):
            return "FAIL"
        # A simple route has a unique position for every point on the trunk.
        if route.project(Point(trunk.coords[0])) >= route.project(Point(trunk.coords[-1])):
            return "FAIL"
    return "PASS"


def conductor_ampacity_quality(rated_amps, required_amps, factors, required_factors):
    """Compare recorded load to base ampacity times every declared derating factor."""
    if rated_amps is None or required_amps is None or not isinstance(factors, dict):
        return "INCOMPLETE"
    if not isinstance(required_factors, (tuple, list)) or not all(
            isinstance(name, str) and name for name in required_factors):
        return "INCOMPLETE"
    if len(set(required_factors)) != len(required_factors) or set(required_factors) != factors.keys():
        return "INCOMPLETE"
    if not finite_measure(rated_amps) or rated_amps <= 0 or not finite_measure(required_amps) or required_amps < 0:
        return "FAIL"
    capacity = Fraction(str(rated_amps))
    for factor in factors.values():
        if factor is None:
            return "INCOMPLETE"
        if not finite_measure(factor) or not 0 < factor <= 1:
            return "FAIL"
        capacity *= Fraction(str(factor))
    return "PASS" if capacity >= Fraction(str(required_amps)) else "FAIL"


def dc_drop_quality(round_trip_length, resistance_per_length, current, max_drop, *, units=None):
    """Use the full conductor loop length once. The drop limit is in volts."""
    if not isinstance(units, dict) or any(units.get(k) is None for k in ("length", "resistance", "current", "drop")):
        return "INCOMPLETE"
    lengths = {"m": 1, "km": 1000, "ft": Fraction(381, 1250)}
    resistance_lengths = {"ohm/" + unit: scale for unit, scale in lengths.items()}
    if (not all(isinstance(v, str) for v in units.values()) or units["length"] not in lengths
            or units["resistance"] not in resistance_lengths or units["current"] != "A" or units["drop"] != "V"):
        return "FAIL"
    values = (round_trip_length, resistance_per_length, current, max_drop)
    if None in values:
        return "INCOMPLETE"
    if not all(finite_measure(v) and v >= 0 for v in values):
        return "FAIL"
    length, resistance, amps, limit = (Fraction(str(v)) for v in values)
    drop = amps * length * lengths[units["length"]] * resistance / resistance_lengths[units["resistance"]]
    return "PASS" if drop <= limit else "FAIL"


def bend_radius_quality(segments, min_radius):
    """Grade directed planar line and circular arc centerlines, including their joins."""
    from math import dist, hypot, isclose
    if not isinstance(segments, (tuple, list)) or not segments or min_radius is None:
        return "INCOMPLETE"
    if not finite_measure(min_radius) or min_radius <= 0:
        return "FAIL"
    previous_end = previous_tangent = None
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("kind") not in ("line", "arc"):
            return "INCOMPLETE"
        arc = segment["kind"] == "arc"
        keys = ("start", "end", "center") if arc else ("start", "end")
        if any(not isinstance(segment.get(k), (tuple, list)) or len(segment[k]) != 2
               or not all(map(finite_measure, segment[k])) for k in keys):
            return "INCOMPLETE"
        start, end = tuple(segment["start"]), tuple(segment["end"])
        if start == end or (previous_end is not None and start != previous_end):
            return "FAIL"
        if arc:
            center = segment["center"]
            radius = dist(start, center)
            if not finite_measure(radius) or radius < min_radius or not isclose(dist(end, center), radius, rel_tol=1e-12):
                return "FAIL"
            if segment.get("direction") not in ("cw", "ccw"):
                return "INCOMPLETE"
            sign = 1 if segment["direction"] == "ccw" else -1
            tangents = [(sign*(center[1]-p[1]), sign*(p[0]-center[0])) for p in (start, end)]
        else:
            tangents = [(end[0]-start[0], end[1]-start[1])] * 2
        normalized = []
        for dx, dy in tangents:
            norm = hypot(dx, dy)
            if not finite_measure(norm) or norm == 0:
                return "FAIL"
            normalized.append((dx/norm, dy/norm))
        if previous_tangent is not None and dist(previous_tangent, normalized[0]) > 1e-9:
            return "FAIL"
        previous_end, previous_tangent = end, normalized[1]
    return "PASS"


def terminal_polarity_quality(modules, terminals, joins, leads):
    """Trace one series string from the negative lead to the positive lead."""
    if not isinstance(modules, (list, tuple)) or not modules or not isinstance(terminals, dict):
        return "INCOMPLETE"
    if not isinstance(joins, (list, tuple)) or not isinstance(leads, dict):
        return "INCOMPLETE"
    pins = {}
    for module in modules:
        pair = terminals.get(module)
        if not isinstance(pair, dict) or any(not isinstance(pair.get(p), str) or not pair[p] for p in ("-", "+")):
            return "INCOMPLETE"
        for polarity in ("-", "+"):
            pin = pair[polarity]
            if pin in pins:
                return "FAIL"
            pins[pin] = (module, polarity)
    if any(not isinstance(leads.get(p), str) or not leads[p] for p in ("-", "+")):
        return "INCOMPLETE"
    start, end = leads["-"], leads["+"]
    if start not in pins or end not in pins or pins[start][1] != "-" or pins[end][1] != "+":
        return "FAIL"
    links = {}
    for join in joins:
        if not isinstance(join, (tuple, list)) or len(join) != 2 or not all(isinstance(p, str) for p in join):
            return "INCOMPLETE"
        a, b = join
        if (a not in pins or b not in pins or pins[a][1] == pins[b][1] or a in links or b in links
                or start in join or end in join):
            return "FAIL"
        links[a], links[b] = b, a
    visited = set()
    module = pins[start][0]
    while module not in visited:
        visited.add(module)
        positive = terminals[module]["+"]
        if positive == end:
            return "PASS" if len(visited) == len(modules) else "FAIL"
        if positive not in links:
            break
        module = pins[links[positive]][0]
    return "FAIL"


def mppt_window_quality(series_count, vmp_25, coefficient_per_c, cell_temperatures, voltage_window):
    """Both endpoints of a linear Vmp model must lie in the inclusive tracker window."""
    if None in (series_count, vmp_25, coefficient_per_c, cell_temperatures, voltage_window):
        return "INCOMPLETE"
    if any(not isinstance(pair, (list, tuple)) or len(pair) != 2 or None in pair
           for pair in (cell_temperatures, voltage_window)):
        return "INCOMPLETE"
    if type(series_count) is not int or series_count <= 0 or not all(
            finite_measure(v) for v in (vmp_25, coefficient_per_c, *cell_temperatures, *voltage_window)):
        return "FAIL"
    reference, coefficient, cold, hot, low, high = (
        Fraction(str(v)) for v in (vmp_25, coefficient_per_c, *cell_temperatures, *voltage_window))
    if reference <= 0 or hot < cold or low <= 0 or high < low:
        return "FAIL"
    for temperature in (cold, hot):
        voltage = series_count * reference * (1 + coefficient * (temperature - 25))
        if not low <= voltage <= high:
            return "FAIL"
    return "PASS"


def circuit_separation_quality(routes, circuits, rules, junctions):
    """A permitted crossing waives clearance on the two crossing segments only."""
    from itertools import combinations, pairwise
    from shapely.geometry import LineString
    if not all(isinstance(x, dict) for x in (routes, circuits, rules, junctions)) or not routes:
        return "INCOMPLETE"
    for name, path in routes.items():
        if not isinstance(circuits.get(name), str) or not circuits[name] or path is None:
            return "INCOMPLETE"
        if path.geom_type != "LineString" or path.is_empty or not path.is_valid:
            return "FAIL"
    for a, b in combinations(routes, 2):
        key = frozenset((circuits[a], circuits[b]))
        if key not in rules:
            return "INCOMPLETE"
        limit = rules[key]
        if not finite_measure(limit) or limit < 0:
            return "FAIL"
        allowed = junctions.get(frozenset((a, b)), ())
        for coords_a in pairwise(routes[a].coords):
            first = LineString(coords_a)
            for coords_b in pairwise(routes[b].coords):
                second = LineString(coords_b)
                if first.distance(second) >= limit:
                    continue
                crossing = first.intersection(second)
                if crossing.geom_type != "Point" or crossing.is_empty or not any(crossing.equals(p) for p in allowed):
                    return "FAIL"
    return "PASS"


def conduit_fill_quality(internal_area, conductor_areas, allowed_fraction):
    """Compare occupied and permitted cross-sectional area in the same unit."""
    if internal_area is None or allowed_fraction is None or not isinstance(conductor_areas, (list, tuple)):
        return "INCOMPLETE"
    if any(area is None for area in conductor_areas):
        return "INCOMPLETE"
    if (not finite_measure(allowed_fraction) or not 0 < allowed_fraction <= 1
            or not all(finite_measure(a) and a > 0 for a in (internal_area, *conductor_areas))):
        return "FAIL"
    occupied = sum(Fraction(str(a)) for a in conductor_areas)
    permitted = Fraction(str(internal_area)) * Fraction(str(allowed_fraction))
    return "PASS" if occupied <= permitted else "FAIL"


def inverter_ratio_quality(ac_watts, dc_watts, assignments, bounds):
    """Reconcile rated DC watts to each inverter's own inclusive ratio bounds."""
    if not all(isinstance(m, dict) and m for m in (ac_watts, dc_watts, assignments, bounds)):
        return "INCOMPLETE"
    if assignments.keys() != dc_watts.keys() or bounds.keys() != ac_watts.keys():
        return "INCOMPLETE"
    totals = dict.fromkeys(ac_watts, Fraction(0))
    for source, inverter in assignments.items():
        if not isinstance(inverter, str) or inverter not in totals:
            return "FAIL"
        rating = dc_watts[source]
        if rating is None:
            return "INCOMPLETE"
        if not finite_measure(rating) or rating <= 0:
            return "FAIL"
        totals[inverter] += Fraction(str(rating))
    for inverter, ac in ac_watts.items():
        limits = bounds[inverter]
        if ac is None or not isinstance(limits, (tuple, list)) or len(limits) != 2 or None in limits:
            return "INCOMPLETE"
        if not finite_measure(ac) or ac <= 0 or not all(finite_measure(v) and v >= 0 for v in limits):
            return "FAIL"
        low, high = map(lambda x: Fraction(str(x)), limits)
        if not low <= totals[inverter] / Fraction(str(ac)) <= high:
            return "FAIL"
    return "PASS"


def parallel_string_quality(strings, modules, tracker_limits):
    """Compare series operating voltages within each tracker using its declared voltage tolerance."""
    if not all(isinstance(x, dict) and x for x in (strings, modules, tracker_limits)):
        return "INCOMPLETE"
    voltages = {}
    for record in strings.values():
        if not isinstance(record, dict) or any(record.get(k) is None for k in ("tracker", "module", "series_count")):
            return "INCOMPLETE"
        tracker, model, count = record["tracker"], record["module"], record["series_count"]
        if not isinstance(tracker, str) or not isinstance(model, str):
            return "INCOMPLETE"
        rating = modules.get(model)
        if rating is None or tracker_limits.get(tracker) is None:
            return "INCOMPLETE"
        if not finite_measure(rating) or rating <= 0 or type(count) is not int or count < 1:
            return "FAIL"
        voltages.setdefault(tracker, []).append(count * Fraction(str(rating)))
    for tracker, values in voltages.items():
        delta = tracker_limits[tracker]
        if not finite_measure(delta) or delta < 0 or max(values)-min(values) > Fraction(str(delta)):
            return "FAIL"
    return "PASS"


def overcurrent_quality(design_amps, device_amps, conductor_ampacity, equipment_limit):
    """All inputs are recorded design-point ratings in amperes."""
    values = (design_amps, device_amps, conductor_ampacity, equipment_limit)
    if None in values:
        return "INCOMPLETE"
    if not all(finite_measure(a) and a > 0 for a in values):
        return "FAIL"
    return "PASS" if design_amps <= device_amps <= min(conductor_ampacity, equipment_limit) else "FAIL"


def tray_capacity_quality(ratings, occupancy):
    """Compare the sum of recorded cable loads on each segment to its rating."""
    if not isinstance(ratings, dict) or not ratings or not isinstance(occupancy, dict):
        return "INCOMPLETE"
    if ratings.keys() - occupancy.keys():
        return "INCOMPLETE"
    if occupancy.keys() - ratings.keys():
        return "FAIL"
    for segment, rating in ratings.items():
        loads = occupancy[segment]
        if rating is None or not isinstance(loads, dict) or None in loads.values():
            return "INCOMPLETE"
        if not finite_measure(rating) or rating <= 0 or not all(finite_measure(v) and v > 0 for v in loads.values()):
            return "FAIL"
        if sum(Fraction(str(v)) for v in loads.values()) > Fraction(str(rating)):
            return "FAIL"
    return "PASS"


def bonding_quality(equipment, reference, bonds):
    """Trace recorded intact bonds from the reference to every inventoried item."""
    if not isinstance(equipment, list) or not equipment or not isinstance(reference, str) or not isinstance(bonds, list):
        return "INCOMPLETE"
    if not all(isinstance(item, str) and item for item in equipment) or not reference:
        return "INCOMPLETE"
    network = {node: set() for node in [reference, *equipment]}
    for bond in bonds:
        if not isinstance(bond, dict) or type(bond.get("intact")) is not bool:
            return "INCOMPLETE"
        a, b = bond.get("a"), bond.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            return "INCOMPLETE"
        if a not in network or b not in network or a == b:
            return "FAIL"
        if bond["intact"]:
            network[a].add(b)
            network[b].add(a)
    reached, pending = {reference}, [reference]
    while pending:
        unseen = network[pending.pop()] - reached
        reached.update(unseen)
        pending.extend(unseen)
    return "PASS" if reached == network.keys() else "FAIL"


def phase_balance_quality(assignments, nameplate_amps, phase_limits, max_spread):
    """Nameplate amps are per connected phase. Limits and spread are in amps."""
    if not all(isinstance(x, dict) and x for x in (assignments, nameplate_amps, phase_limits)):
        return "INCOMPLETE"
    if nameplate_amps.keys() != assignments.keys() or max_spread is None:
        return "INCOMPLETE"
    if None in phase_limits.values() or None in nameplate_amps.values():
        return "INCOMPLETE"
    if not finite_measure(max_spread) or max_spread < 0:
        return "FAIL"
    if not all(finite_measure(v) and v > 0 for v in (*nameplate_amps.values(), *phase_limits.values())):
        return "FAIL"
    loads = dict.fromkeys(phase_limits, Fraction(0))
    for inverter, phases in assignments.items():
        if not isinstance(phases, list) or not phases or None in phases:
            return "INCOMPLETE"
        if not all(isinstance(p, str) and p in loads for p in phases) or len(set(phases)) != len(phases):
            return "FAIL"
        for phase in phases:
            loads[phase] += Fraction(str(nameplate_amps[inverter]))
    if any(loads[p] > Fraction(str(limit)) for p, limit in phase_limits.items()):
        return "FAIL"
    return "PASS" if max(loads.values()) - min(loads.values()) <= Fraction(str(max_spread)) else "FAIL"


def reverse_current_quality(sources, withstand, protection):
    """Other strings feed the fault. A numeric cap is a recorded let-through bound."""
    if not all(isinstance(x, dict) and x for x in (sources, withstand, protection)):
        return "INCOMPLETE"
    if sources.keys() != withstand.keys() or sources.keys() != protection.keys():
        return "INCOMPLETE"
    if any(v is None for v in (*sources.values(), *withstand.values(), *protection.values())):
        return "INCOMPLETE"
    if not all(finite_measure(v) and v > 0 for v in (*sources.values(), *withstand.values())):
        return "FAIL"
    total = sum(Fraction(str(v)) for v in sources.values())
    for string, cap in protection.items():
        reverse = total - Fraction(str(sources[string]))
        if cap != "unprotected":
            if not finite_measure(cap) or cap <= 0:
                return "FAIL"
            reverse = min(reverse, Fraction(str(cap)))
        if reverse > Fraction(str(withstand[string])):
            return "FAIL"
    return "PASS"


def interrupting_rating_quality(fault_amps, interrupting_amps):
    """Compare available fault current and interrupting rating at each device, in amps."""
    if not all(isinstance(x, dict) and x for x in (fault_amps, interrupting_amps)):
        return "INCOMPLETE"
    if fault_amps.keys() != interrupting_amps.keys() or None in (*fault_amps.values(), *interrupting_amps.values()):
        return "INCOMPLETE"
    for device, fault in fault_amps.items():
        rating = interrupting_amps[device]
        if not finite_measure(fault) or fault < 0 or not finite_measure(rating) or rating <= 0 or fault > rating:
            return "FAIL"
    return "PASS"


def connector_pair_quality(mated, approvals, volts, amps):
    """Model IDs identify full manufacturer parts. Ratings belong to the approved pair."""
    if not isinstance(mated, (list, tuple)) or len(mated) != 2 or not isinstance(approvals, list):
        return "INCOMPLETE"
    if not all(isinstance(p, str) and p for p in mated) or None in (volts, amps):
        return "INCOMPLETE"
    if not all(finite_measure(v) and v >= 0 for v in (volts, amps)):
        return "FAIL"
    matched = False
    for row in approvals:
        if not isinstance(row, dict) or not isinstance(row.get("parts"), (list, tuple)):
            return "INCOMPLETE"
        if tuple(row["parts"]) not in (tuple(mated), tuple(reversed(mated))):
            continue
        if row.get("approved") is None:
            return "INCOMPLETE"
        if row["approved"] is not True:
            return "FAIL"
        limits = (row.get("max_volts"), row.get("max_amps"))
        if None in limits:
            return "INCOMPLETE"
        if not all(finite_measure(v) and v > 0 for v in limits) or volts > limits[0] or amps > limits[1]:
            return "FAIL"
        matched = True
    return "PASS" if matched else "INCOMPLETE"


def apparent_power_quality(kw, kvar, kva):
    """Compare squared real and reactive power with squared capacity per inverter using recorded kW, kvar and kVA."""
    if not all(isinstance(v, dict) and v for v in (kw, kvar, kva)) or not kw.keys() == kvar.keys() == kva.keys():
        return "INCOMPLETE"
    for inverter in kw:
        values = (kw[inverter], kvar[inverter], kva[inverter])
        if None in values:
            return "INCOMPLETE"
        if not all(map(finite_measure, values)):
            return "FAIL"
        p, q, rating = (Fraction(str(v)) for v in values)
        if rating <= 0 or p*p + q*q > rating*rating:
            return "FAIL"
    return "PASS"


def short_circuit_withstand_quality(fault_amps, clearing_seconds, withstand_a2s):
    """Use constant RMS fault current and the recorded total clearing time per conductor."""
    if not all(isinstance(group, dict) and group for group in (fault_amps, clearing_seconds, withstand_a2s)):
        return "INCOMPLETE"
    if not fault_amps.keys() == clearing_seconds.keys() == withstand_a2s.keys():
        return "INCOMPLETE"
    for key, amps in fault_amps.items():
        values = (amps, clearing_seconds[key], withstand_a2s[key])
        if None in values:
            return "INCOMPLETE"
        if not all(finite_measure(value) and value > 0 for value in values):
            return "FAIL"
        current, seconds, rating = (Fraction(str(value)) for value in values)
        if current**2 * seconds > rating:
            return "FAIL"
    return "PASS"


def ac_voltage_drop_quality(phases, amps, length_m, pf, load, resistance, reactance, impedance_unit, reported_v):
    """Signed longitudinal drop on a balanced line using one-way conductor length."""
    from math import isclose, sqrt
    values = (amps, length_m, pf, resistance, reactance, reported_v)
    if None in (*values, phases, load, impedance_unit):
        return "INCOMPLETE"
    if not all(map(finite_measure, values)) or type(phases) is not int or phases not in (1, 3):
        return "FAIL"
    if load not in ("leading", "lagging") or not 0 <= pf <= 1 or min(amps, length_m, resistance, reactance) < 0:
        return "FAIL"
    if impedance_unit not in ("ohm/m", "ohm/km"):
        return "INCOMPLETE"
    length = length_m if impedance_unit == "ohm/m" else length_m / 1000
    sine = sqrt(1 - pf**2) * (-1 if load == "leading" else 1)
    multiplier = 2 if phases == 1 else sqrt(3)
    drop = multiplier * amps * length * (resistance * pf + reactance * sine)
    return "PASS" if isclose(reported_v, drop, abs_tol=1e-9, rel_tol=1e-9) else "FAIL"
