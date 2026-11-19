"""Independent X03 quality checks for recorded drawing outputs."""
from collections import Counter
from fractions import Fraction
from math import ceil


def battery_thermal_quality(initial_c, ambient_c, heat_capacity_j_per_k, maximum_c, intervals):
    """Lumped thermal store with constant loads per interval and cooling limited at ambient."""
    inputs = (initial_c, ambient_c, heat_capacity_j_per_k, maximum_c)
    if not all(map(finite_measure, inputs)) or not isinstance(intervals, list) or not intervals:
        return "INCOMPLETE"
    temperature, ambient, capacity, limit = (Fraction(str(v)) for v in inputs)
    if capacity <= 0 or temperature < ambient:
        return "INCOMPLETE"
    if temperature > limit:
        return "FAIL"
    for interval in intervals:
        if not isinstance(interval, (list, tuple)) or len(interval) != 3 or not all(map(finite_measure, interval)):
            return "INCOMPLETE"
        seconds, heat_w, cooling_w = (Fraction(str(v)) for v in interval)
        if seconds <= 0 or min(heat_w, cooling_w) < 0:
            return "INCOMPLETE"
        temperature = max(ambient, temperature + seconds * (heat_w - cooling_w) / capacity)
        if temperature > limit:
            return "FAIL"
    return "PASS"


def insulation_resistance_quality(test, equipment_rule):
    """Compare a recorded insulation test with the equipment's permitted voltage and resistance."""
    if not isinstance(test, dict) or not isinstance(equipment_rule, dict):
        return "INCOMPLETE"
    volts, resistance = test.get("volts"), test.get("megohms")
    low, high, minimum = (equipment_rule.get(k) for k in ("min_volts", "max_volts", "min_megohms"))
    if not all(map(finite_measure, (volts, resistance, low, high, minimum))) or not 0 < low <= high or minimum <= 0:
        return "INCOMPLETE"
    return "PASS" if low <= volts <= high and resistance >= minimum else "FAIL"


def insulation_quality(circuits, dielectric_volts):
    """Map circuit IDs to (maximum volts, cable IDs) and check every installed cable."""
    if not isinstance(circuits, dict) or not circuits or not isinstance(dielectric_volts, dict):
        return "INCOMPLETE"
    for circuit in circuits.values():
        if not isinstance(circuit, (list, tuple)) or len(circuit) != 2:
            return "INCOMPLETE"
        maximum, cables = circuit
        if not finite_measure(maximum) or not isinstance(cables, (list, tuple)) or not cables:
            return "INCOMPLETE"
        for cable in cables:
            if not isinstance(cable, str) or not finite_measure(dielectric_volts.get(cable)):
                return "INCOMPLETE"
            if maximum <= 0 or dielectric_volts[cable] < maximum:
                return "FAIL"
    return "PASS"


def charging_temperature_quality(readings, allowed_ranges, charging):
    """Every sensor named by the charging policy must be within its inclusive range."""
    if type(charging) is not bool or not isinstance(readings, dict) or not isinstance(allowed_ranges, dict) or not allowed_ranges:
        return "INCOMPLETE"
    for sensor, bounds in allowed_ranges.items():
        value = readings.get(sensor)
        if not finite_measure(value) or not isinstance(bounds, (tuple, list)) or len(bounds) != 2 or not all(map(finite_measure, bounds)):
            return "INCOMPLETE"
        low, high = bounds
        if low > high or (charging and not low <= value <= high):
            return "FAIL"
    return "PASS"


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


def mismatch_loss_quality(string_currents, max_loss_fraction, rule):
    """Equal module operating voltages, series current limited to the weakest module."""
    if rule != "equal-voltage-min-current" or not isinstance(string_currents, dict) or not string_currents:
        return "INCOMPLETE"
    if max_loss_fraction is None:
        return "INCOMPLETE"
    if not finite_measure(max_loss_fraction) or not 0 <= max_loss_fraction <= 1:
        return "FAIL"
    limit = Fraction(str(max_loss_fraction))
    for modules in string_currents.values():
        if not isinstance(modules, dict) or not modules or None in modules.values():
            return "INCOMPLETE"
        if not all(finite_measure(v) and v > 0 for v in modules.values()):
            return "FAIL"
        currents = [Fraction(str(v)) for v in modules.values()]
        available = sum(currents)
        lost = available - len(currents) * min(currents)
        if lost > limit * available:
            return "FAIL"
    return "PASS"


def disconnect_isolation_quality(nodes, sources, protected, links, switch_states):
    """Trace every energized source through bidirectional wires and closed disconnects."""
    from collections import defaultdict
    if not all(isinstance(v, list) and v for v in (nodes, sources, protected)):
        return "INCOMPLETE"
    if not isinstance(links, list) or not isinstance(switch_states, dict):
        return "INCOMPLETE"
    if not all(isinstance(v, str) and v for v in (*nodes, *sources, *protected)):
        return "INCOMPLETE"
    inventory = set(nodes)
    if not set(sources + protected) <= inventory:
        return "INCOMPLETE"
    unknown_switch = False
    adjacent = defaultdict(set)
    for link in links:
        if not isinstance(link, dict) or not all(isinstance(link.get(k), str) for k in ("a", "b", "kind")):
            return "INCOMPLETE"
        a, b = link["a"], link["b"]
        if a not in inventory or b not in inventory:
            return "INCOMPLETE"
        if link["kind"] == "switch":
            if not isinstance(link.get("id"), str) or switch_states.get(link["id"]) not in ("open", "closed"):
                unknown_switch = True
                continue
            if switch_states[link["id"]] == "open":
                continue
        elif link["kind"] != "wire":
            return "INCOMPLETE"
        adjacent[a].add(b)
        adjacent[b].add(a)
    energized = set(sources)
    pending = list(energized)
    while pending:
        unseen = adjacent[pending.pop()] - energized
        energized.update(unseen)
        pending.extend(unseen)
    if energized.intersection(protected):
        return "FAIL"
    return "INCOMPLETE" if unknown_switch else "PASS"


def startup_voltage_quality(series_start_volts, threshold_volts):
    """Each listed string must start independently at its recorded pre-start condition."""
    if not isinstance(series_start_volts, dict) or not series_start_volts or threshold_volts is None:
        return "INCOMPLETE"
    if not finite_measure(threshold_volts) or threshold_volts <= 0:
        return "FAIL"
    for module_volts in series_start_volts.values():
        if not isinstance(module_volts, list) or not module_volts or None in module_volts:
            return "INCOMPLETE"
        if not all(finite_measure(v) and v >= 0 for v in module_volts):
            return "FAIL"
        if sum(Fraction(str(v)) for v in module_volts) < Fraction(str(threshold_volts)):
            return "FAIL"
    return "PASS"


def parallel_current_quality(total_amps, resistances, ampacities):
    """Split total current by conductance between common terminals, then check each path."""
    if total_amps is None or not isinstance(resistances, dict) or not isinstance(ampacities, dict):
        return "INCOMPLETE"
    if len(resistances) < 2 or resistances.keys() != ampacities.keys():
        return "INCOMPLETE"
    if None in resistances.values() or None in ampacities.values():
        return "INCOMPLETE"
    if not finite_measure(total_amps) or total_amps < 0:
        return "FAIL"
    if not all(finite_measure(v) and v > 0 for v in (*resistances.values(), *ampacities.values())):
        return "FAIL"
    conductances = {path: 1 / Fraction(str(r)) for path, r in resistances.items()}
    total_g = sum(conductances.values())
    for path, g in conductances.items():
        if Fraction(str(total_amps)) * g > Fraction(str(ampacities[path])) * total_g:
            return "FAIL"
    return "PASS"


def coordination_quality(downstream, upstream, current_range):
    """Strict separation of piecewise-linear time envelopes on linear current and time axes."""
    from bisect import bisect_right
    if not isinstance(current_range, (list, tuple)) or len(current_range) != 2 or None in current_range:
        return "INCOMPLETE"
    if not all(map(finite_measure, current_range)) or not 0 < current_range[0] <= current_range[1]:
        return "FAIL"
    low, high = map(lambda value: Fraction(str(value)), current_range)
    curves = []
    probes = {low, high}
    for envelope, column in ((downstream, 2), (upstream, 1)):
        if not isinstance(envelope, list) or len(envelope) < 2:
            return "INCOMPLETE"
        currents, times = [], []
        for row in envelope:
            if not isinstance(row, (list, tuple)) or len(row) != 3 or None in row:
                return "INCOMPLETE"
            if not all(map(finite_measure, row)) or not (row[0] > 0 and 0 <= row[1] <= row[2]):
                return "FAIL"
            current = Fraction(str(row[0]))
            if currents and current <= currents[-1]:
                return "FAIL"
            currents.append(current)
            times.append(Fraction(str(row[column])))
            if low <= current <= high:
                probes.add(current)
        if low < currents[0] or high > currents[-1]:
            return "INCOMPLETE"
        curves.append((currents, times))

    def at(curve, current):
        currents, times = curve
        i = min(bisect_right(currents, current) - 1, len(times) - 2)
        weight = (current - currents[i]) / (currents[i+1] - currents[i])
        return times[i] + weight * (times[i+1] - times[i])

    return "PASS" if all(at(curves[0], i) < at(curves[1], i) for i in probes) else "FAIL"


def pulling_tension_quality(initial_n, segments):
    """In pull order, add straight-run friction then apply the capstan factor at the bend."""
    from math import exp, radians
    if initial_n is None or not isinstance(segments, list) or not segments:
        return "INCOMPLETE"
    if not finite_measure(initial_n) or initial_n < 0:
        return "FAIL"
    tension = initial_n
    for segment in segments:
        fields = ("friction", "normal_force_n", "bend_degrees", "limit_n")
        if not isinstance(segment, dict) or any(segment.get(k) is None for k in fields):
            return "INCOMPLETE"
        mu, normal, angle, limit = (segment[k] for k in fields)
        if not all(map(finite_measure, (mu, normal, angle, limit))) or min(mu, normal, angle) < 0 or limit <= 0:
            return "FAIL"
        try:
            tension = (tension + mu * normal) * exp(mu * radians(angle))
        except OverflowError:
            return "FAIL"
        if not finite_measure(tension) or tension > limit:
            return "FAIL"
    return "PASS"


def battery_energy_quality(initial_kwh, capacity_kwh, reserve_kwh, charge_efficiency, discharge_efficiency, steps):
    """Positive external kW discharges the battery. Check stored energy after every step."""
    fields = (initial_kwh, capacity_kwh, reserve_kwh, charge_efficiency, discharge_efficiency)
    if None in fields or not isinstance(steps, list) or not steps:
        return "INCOMPLETE"
    if not all(map(finite_measure, fields)):
        return "FAIL"
    energy, capacity, reserve, charge, discharge = (Fraction(str(v)) for v in fields)
    if capacity <= 0 or not 0 <= reserve <= energy <= capacity or not (0 < charge <= 1 and 0 < discharge <= 1):
        return "FAIL"
    for step in steps:
        if not isinstance(step, dict) or any(step.get(k) is None for k in ("load_kw", "hours", "stored_kwh")):
            return "INCOMPLETE"
        values = (step["load_kw"], step["hours"], step["stored_kwh"])
        if not all(map(finite_measure, values)) or step["hours"] <= 0:
            return "FAIL"
        power, hours, recorded = (Fraction(str(v)) for v in values)
        energy -= power * hours * (1 / discharge if power >= 0 else charge)
        if energy != recorded or not reserve <= energy <= capacity:
            return "FAIL"
    return "PASS"


def battery_power_quality(power_kw, inverter, battery):
    """Both directional nameplates and signed step powers use the same reference terminal."""
    if not isinstance(power_kw, list) or not power_kw or None in power_kw:
        return "INCOMPLETE"
    for ratings in (inverter, battery):
        if not isinstance(ratings, dict) or any(ratings.get(k) is None for k in ("charge_kw", "discharge_kw")):
            return "INCOMPLETE"
        if not all(finite_measure(ratings[k]) and ratings[k] >= 0 for k in ("charge_kw", "discharge_kw")):
            return "FAIL"
    for power in power_kw:
        if not finite_measure(power):
            return "FAIL"
        direction = "charge_kw" if power < 0 else "discharge_kw"
        if abs(power) > min(inverter[direction], battery[direction]):
            return "FAIL"
    return "PASS"


def sidewall_pressure_quality(bends):
    """Single cable. Tension in N divided by bend radius in m gives lateral load in N/m."""
    if not isinstance(bends, list) or not bends:
        return "INCOMPLETE"
    for bend in bends:
        fields = ("tension_n", "radius_m", "limit_n_per_m")
        if not isinstance(bend, dict) or any(bend.get(k) is None for k in fields):
            return "INCOMPLETE"
        values = tuple(bend[k] for k in fields)
        if not all(map(finite_measure, values)):
            return "FAIL"
        tension, radius, limit = (Fraction(str(v)) for v in values)
        if tension < 0 or radius <= 0 or limit <= 0 or tension > radius * limit:
            return "FAIL"
    return "PASS"


def retained_energy_quality(new_kwh, required_kwh, age_years, cycles, age_curve, cycle_curve, combination):
    """Interpolate each retention fraction within its recorded range using the declared combination."""
    if combination not in ("minimum", "product") or None in (new_kwh, required_kwh, age_years, cycles):
        return "INCOMPLETE"
    if not all(map(finite_measure, (new_kwh, required_kwh, age_years))) or type(cycles) is not int:
        return "FAIL"
    if new_kwh <= 0 or min(required_kwh, age_years, cycles) < 0:
        return "FAIL"
    factors = []
    for curve, value in ((age_curve, age_years), (cycle_curve, cycles)):
        if not isinstance(curve, list) or not curve:
            return "INCOMPLETE"
        points = []
        for point in curve:
            if not isinstance(point, (tuple, list)) or len(point) != 2 or None in point:
                return "INCOMPLETE"
            if not all(map(finite_measure, point)) or point[0] < 0 or not 0 <= point[1] <= 1:
                return "FAIL"
            x, y = (Fraction(str(v)) for v in point)
            if points and (x <= points[-1][0] or y > points[-1][1]):
                return "FAIL"
            points.append((x,y))
        v = Fraction(str(value))
        if not points[0][0] <= v <= points[-1][0]:
            return "INCOMPLETE"
        factor = points[0][1]
        for (a, left), (b, right) in zip(points, points[1:]):
            if a <= v <= b:
                factor = left + (right-left) * (v-a) / (b-a)
                break
        factors.append(factor)
    retained = min(factors) if combination == "minimum" else factors[0] * factors[1]
    return "PASS" if Fraction(str(new_kwh)) * retained >= Fraction(str(required_kwh)) else "FAIL"


def ground_fault_quality(leakage_ma, sensors):
    """Each branch is a separate single-fault scenario. Any covering sensor may trip."""
    if not isinstance(leakage_ma, dict) or not leakage_ma or not isinstance(sensors, list):
        return "INCOMPLETE"
    if None in leakage_ma.values():
        return "INCOMPLETE"
    if not all(finite_measure(v) and v > 0 for v in leakage_ma.values()):
        return "FAIL"
    detected = set()
    for sensor in sensors:
        if not isinstance(sensor, dict) or not isinstance(sensor.get("branches"), list) or sensor.get("trip_ma") is None:
            return "INCOMPLETE"
        if not all(isinstance(k, str) for k in sensor["branches"]):
            return "INCOMPLETE"
        threshold = sensor["trip_ma"]
        if not finite_measure(threshold) or threshold <= 0:
            return "FAIL"
        for branch in sensor["branches"]:
            if branch in leakage_ma and leakage_ma[branch] >= threshold:
                detected.add(branch)
    return "PASS" if detected == leakage_ma.keys() else "FAIL"


def battery_bank_quality(cells, connections, terminals, bank_voltage, branch_currents):
    """Trace disjoint series strings between common buses using measured branch currents.

    Cell IDs map to (nominal volts, maximum amps). Directed edges map each
    cell ID to its two nodes. Current measurements are keyed by first cell.
    """
    if not all(isinstance(x, dict) for x in (cells, connections, branch_currents)):
        return "INCOMPLETE"
    if not cells or cells.keys() - connections.keys() or not finite_measure(bank_voltage):
        return "INCOMPLETE"
    if not isinstance(terminals, (tuple, list)) or len(terminals) != 2:
        return "INCOMPLETE"
    start, end = terminals
    if not all(isinstance(n, str) and n for n in terminals) or start == end:
        return "FAIL"
    outgoing = {}
    for cell, edge in connections.items():
        if not isinstance(edge, (list, tuple)) or len(edge) != 2 or None in edge:
            return "INCOMPLETE"
        if cell not in cells or not all(isinstance(n, str) and n for n in edge):
            return "FAIL"
        outgoing.setdefault(edge[0], []).append((cell, edge[1]))
    visited = set()
    branches = outgoing.get(start, [])
    if not branches or end in outgoing:
        return "FAIL"
    first_cells = {cell for cell, _ in branches}
    if first_cells - branch_currents.keys():
        return "INCOMPLETE"
    if first_cells != branch_currents.keys():
        return "FAIL"
    for first, next_node in branches:
        amps = branch_currents[first]
        if not finite_measure(amps):
            return "INCOMPLETE"
        voltage = Fraction(0)
        cell = first
        while True:
            if cell in visited:
                return "FAIL"
            visited.add(cell)
            rating = cells[cell]
            if not isinstance(rating, (tuple, list)) or len(rating) != 2 or not all(map(finite_measure, rating)):
                return "INCOMPLETE"
            volts, limit = rating
            if min(volts, limit) <= 0 or abs(amps) > limit:
                return "FAIL"
            voltage += Fraction(str(volts))
            if next_node == end:
                break
            choices = outgoing.get(next_node, [])
            if next_node == start or len(choices) != 1:
                return "FAIL"
            cell, next_node = choices[0]
        if voltage != Fraction(str(bank_voltage)):
            return "FAIL"
    return "PASS" if visited == cells.keys() else "FAIL"


def neutral_harmonic_quality(spectra, neutral_ampacity):
    """Use RMS magnitudes and common-reference angles, in degrees, for A/B/C."""
    from cmath import rect
    from math import hypot, isclose, radians
    if not isinstance(spectra, dict) or set(spectra) != {"A", "B", "C"} or not finite_measure(neutral_ampacity):
        return "INCOMPLETE"
    if not all(isinstance(s, dict) and 1 in s for s in spectra.values()):
        return "INCOMPLETE"
    orders = spectra["A"].keys()
    if any(s.keys() != orders for s in spectra.values()):
        return "INCOMPLETE"
    rms_components = []
    for order in orders:
        if type(order) is not int or order < 1:
            return "FAIL"
        total = 0j
        for phase in spectra.values():
            sample = phase[order]
            if not isinstance(sample, (list, tuple)) or len(sample) != 2 or not all(map(finite_measure, sample)):
                return "INCOMPLETE"
            magnitude, angle = sample
            if magnitude < 0:
                return "FAIL"
            total += rect(magnitude, radians(angle % 360))
        rms_components.append(abs(total))
    current = hypot(*rms_components)
    return "PASS" if neutral_ampacity > 0 and (current <= neutral_ampacity or isclose(current, neutral_ampacity, rel_tol=1e-12)) else "FAIL"


def inverter_ramp_quality(samples, rise_kw_per_s, fall_kw_per_s):
    """Grade each adjacent pair in acquisition order, including downward ramps."""
    if not isinstance(samples, (list, tuple)) or len(samples) < 2:
        return "INCOMPLETE"
    if not all(map(finite_measure, (rise_kw_per_s, fall_kw_per_s))):
        return "INCOMPLETE"
    rise, fall = (Fraction(str(n)) for n in (rise_kw_per_s, fall_kw_per_s))
    if min(rise, fall) < 0:
        return "FAIL"
    previous = None
    for sample in samples:
        if not isinstance(sample, (tuple, list)) or len(sample) != 2 or not all(map(finite_measure, sample)):
            return "INCOMPLETE"
        timestamp, power = (Fraction(str(n)) for n in sample)
        if previous is not None:
            elapsed, delta = timestamp - previous[0], power - previous[1]
            if elapsed <= 0:
                return "INCOMPLETE"
            if not -fall * elapsed <= delta <= rise * elapsed:
                return "FAIL"
        previous = timestamp, power
    return "PASS"


def idle_reserve_quality(initial_kwh, start_hour, end_hour, retention_per_hour, claimed_kwh):
    """Compound the recorded hourly retention fraction over uninterrupted idle time."""
    from math import isclose
    if not all(map(finite_measure, (initial_kwh, start_hour, end_hour, retention_per_hour, claimed_kwh))):
        return "INCOMPLETE"
    if end_hour < start_hour or not 0 <= retention_per_hour <= 1 or min(initial_kwh, claimed_kwh) < 0:
        return "FAIL"
    retained = initial_kwh * retention_per_hour ** (end_hour - start_hour)
    return "PASS" if claimed_kwh <= retained or isclose(claimed_kwh, retained, rel_tol=1e-12) else "FAIL"


def altitude_rating_quality(model, revision, curve, elevation_m, required_kw):
    """Compare site demand to the pinned table of absolute kW by elevation."""
    from benches.x03_catalog_oracle import temperature_derating_quality
    if not model or not revision or not isinstance(curve, dict) or not curve.get("model") or not curve.get("revision"):
        return "INCOMPLETE"
    if (curve["model"], curve["revision"]) != (model, revision):
        return "FAIL"
    return temperature_derating_quality(curve.get("elevation_kw"), elevation_m, required_kw, curve.get("interpolation"))


def rapid_shutdown_quality(opened_at, seconds_allowed, limit_volts, trace):
    """Verify measured voltage magnitude by the deadline and through the last sample."""
    if not all(map(finite_measure, (opened_at, seconds_allowed, limit_volts))) or not isinstance(trace, list):
        return "INCOMPLETE"
    if seconds_allowed < 0 or limit_volts < 0:
        return "FAIL"
    deadline = Fraction(str(opened_at)) + Fraction(str(seconds_allowed))
    last = None
    safe_at_deadline = None
    for pair in trace:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2 or not all(map(finite_measure, pair)):
            return "INCOMPLETE"
        stamp, volts = pair
        when = Fraction(str(stamp))
        if last is not None and when <= last:
            return "INCOMPLETE"
        last = when
        if when < Fraction(str(opened_at)):
            continue
        if when <= deadline:
            safe_at_deadline = abs(volts) <= limit_volts
        if when >= deadline and abs(volts) > limit_volts:
            return "FAIL"
    if last is None or last < deadline or safe_at_deadline is None:
        return "INCOMPLETE"
    if safe_at_deadline:
        return "PASS"
    # Samples bracketing the deadline cannot establish the time of crossing.
    return "INCOMPLETE"


def cell_spread_quality(inventory, readings, max_spread):
    if not isinstance(inventory, (tuple, list)) or not inventory or not isinstance(readings, dict):
        return "INCOMPLETE"
    if not all(isinstance(cell, str) and cell for cell in inventory) or len(set(inventory)) != len(inventory):
        return "INCOMPLETE"
    if set(inventory) != readings.keys() or not finite_measure(max_spread) or not all(map(finite_measure, readings.values())):
        return "INCOMPLETE"
    volts = [Fraction(str(v)) for v in readings.values()]
    if min(volts) <= 0 or max_spread < 0:
        return "FAIL"
    return "PASS" if max(volts) - min(volts) <= Fraction(str(max_spread)) else "FAIL"


def relay_trace_quality(trace, trip, reset, reset_delay, initially_tripped=False):
    """Replay a sampled high-trip relay with inclusive pickup and dropout thresholds."""
    if not isinstance(trace, list) or not trace or type(initially_tripped) is not bool:
        return "INCOMPLETE"
    if not all(map(finite_measure, (trip, reset, reset_delay))) or reset >= trip or reset_delay < 0:
        return "INCOMPLETE"
    previous = below_since = None
    active = initially_tripped
    for row in trace:
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            return "INCOMPLETE"
        stamp, signal, recorded = row
        if not all(map(finite_measure, (stamp, signal))) or type(recorded) is not bool:
            return "INCOMPLETE"
        when = Fraction(str(stamp))
        if previous is not None and when <= previous:
            return "INCOMPLETE"
        previous = when
        if signal >= trip:
            active = True
        if active and signal <= reset:
            if below_since is None:
                below_since = when
            if when - below_since >= Fraction(str(reset_delay)):
                active = False
        else:
            below_since = None
        if recorded != active:
            return "FAIL"
    return "PASS"


def bypass_string_quality(substrings, current, revision, curves, reported_watts):
    """At one common series current clamp each substring to its diode's forward drop."""
    from math import isclose
    if not isinstance(substrings, dict) or not substrings or not isinstance(curves, dict) or not revision:
        return "INCOMPLETE"
    if not all(map(finite_measure, (current, reported_watts))) or current < 0 or not curves.get("revision"):
        return "INCOMPLETE"
    if revision != curves["revision"]:
        return "FAIL"
    tables = curves.get("volts_by_current")
    if not isinstance(tables, dict):
        return "INCOMPLETE"
    voltage = 0
    for record in substrings.values():
        if not isinstance(record, dict) or not isinstance(record.get("shading"), str):
            return "INCOMPLETE"
        drop = record.get("diode_drop")
        table = tables.get(record["shading"])
        if not finite_measure(drop) or drop < 0 or not isinstance(table, list) or len(table) < 2:
            return "INCOMPLETE"
        if not all(isinstance(p, (tuple, list)) and len(p) == 2 and all(map(finite_measure, p)) for p in table):
            return "INCOMPLETE"
        if any(a[0] >= b[0] for a,b in zip(table, table[1:])):
            return "INCOMPLETE"
        for (lo, vl), (hi, vh) in zip(table, table[1:]):
            if lo <= current <= hi:
                voltage += max(-drop, vl + (vh-vl) * (current-lo)/(hi-lo))
                break
        else:
            return "INCOMPLETE"
    return "PASS" if isclose(reported_watts, current * voltage, rel_tol=1e-9, abs_tol=1e-9) else "FAIL"


def frequency_ride_quality(window, intervals, envelope):
    return _band_duration_quality(window, intervals, envelope)


def _band_duration_quality(window, intervals, envelope):
    """Grade contiguous [start,end) measurement and connection records from excursion onset.

    Each half-open measurement band specifies minimum and maximum connected seconds
    per continuous visit. A trip at either time boundary is allowed.
    """
    if not isinstance(window, (tuple, list)) or len(window) != 2 or not all(map(finite_measure, window)):
        return "INCOMPLETE"
    if not isinstance(intervals, list) or not intervals or not isinstance(envelope, list) or not envelope:
        return "INCOMPLETE"
    for band in envelope:
        if not isinstance(band, (tuple, list)) or len(band) != 4 or not all(map(finite_measure, band)):
            return "INCOMPLETE"
        low, high, minimum, maximum = band
        if low >= high or not 0 <= minimum <= maximum:
            return "INCOMPLETE"
    cursor, end = window
    previous_band = None
    for row in intervals:
        if not isinstance(row, (tuple, list)) or len(row) != 4:
            return "INCOMPLETE"
        start, stop, value, connected = row
        if not all(map(finite_measure, (start, stop, value))) or type(connected) is not bool:
            return "INCOMPLETE"
        if start != cursor or not start < stop <= end:
            return "INCOMPLETE"
        matches = [b for b in envelope if b[0] <= value < b[1]]
        if len(matches) != 1:
            return "INCOMPLETE"
        band = matches[0]
        if band != previous_band:
            onset = Fraction(str(start))
            disconnected = False
        if connected:
            if disconnected or Fraction(str(stop)) - onset > Fraction(str(band[3])):
                return "FAIL"
        else:
            if Fraction(str(start)) - onset < Fraction(str(band[2])):
                return "FAIL"
            disconnected = True
        cursor, previous_band = stop, band
    return "PASS" if cursor == end else "INCOMPLETE"


def precharge_quality(events, max_delta_v):
    """Grade one initially open startup sequence with simultaneous source and bus readings."""
    if not finite_measure(max_delta_v) or max_delta_v < 0 or not isinstance(events, list):
        return "INCOMPLETE"
    previous = None
    precharging = main_closed = False
    for event in events:
        if not isinstance(event, dict) or not finite_measure(event.get("time")):
            return "INCOMPLETE"
        time, action = event["time"], event.get("action")
        if previous is not None and time <= previous:
            return "INCOMPLETE"
        previous = time
        if action == "close_precharge":
            if precharging or main_closed:
                return "FAIL"
            precharging = True
        elif action == "close_main":
            source, bus = event.get("source_v"), event.get("bus_v")
            if not all(map(finite_measure, (source, bus))):
                return "INCOMPLETE"
            if main_closed or not precharging or abs(Fraction(str(source))-Fraction(str(bus))) > Fraction(str(max_delta_v)):
                return "FAIL"
            main_closed = True
        elif action == "open_precharge":
            if not precharging or not main_closed:
                return "FAIL"
            precharging = False
        else:
            return "INCOMPLETE"
    return "PASS" if main_closed and not precharging else "INCOMPLETE"


def islanding_quality(loss_at, observed_until, clearing_seconds, energized_intervals):
    """Grade contiguous half-open intervals during one recorded grid outage."""
    if not all(map(finite_measure, (loss_at, observed_until, clearing_seconds))):
        return "INCOMPLETE"
    if observed_until <= loss_at or clearing_seconds < 0 or not isinstance(energized_intervals, list):
        return "INCOMPLETE"
    deadline = Fraction(str(loss_at)) + Fraction(str(clearing_seconds))
    cursor = loss_at
    for row in energized_intervals:
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            return "INCOMPLETE"
        start, stop, energized = row
        if not all(map(finite_measure, (start, stop))) or type(energized) is not bool:
            return "INCOMPLETE"
        if start != cursor or not start < stop <= observed_until:
            return "INCOMPLETE"
        if energized and Fraction(str(stop)) > deadline:
            return "FAIL"
        cursor = stop
    if cursor != observed_until or Fraction(str(cursor)) <= deadline:
        return "INCOMPLETE"
    return "PASS"


def surge_coordination_quality(residual_v, lead_rise_v, impulse_rating_v):
    """Compare installed protective voltage, including the leads, to equipment withstand."""
    if not all(map(finite_measure, (residual_v, lead_rise_v, impulse_rating_v))):
        return "INCOMPLETE"
    if min(residual_v, lead_rise_v) < 0 or impulse_rating_v <= 0:
        return "FAIL"
    installed = Fraction(str(residual_v)) + Fraction(str(lead_rise_v))
    return "PASS" if installed <= Fraction(str(impulse_rating_v)) else "FAIL"


def voltage_thd_quality(rms_volts, required_orders, limit_percent):
    """Compare squared harmonic RMS with the squared fraction of the fundamental."""
    if not isinstance(rms_volts, dict) or not isinstance(required_orders, (list, tuple)) or not required_orders:
        return "INCOMPLETE"
    if not all(type(k) is int and k >= 2 for k in required_orders) or not finite_measure(limit_percent):
        return "INCOMPLETE"
    if not all(type(k) is int and k >= 1 for k in rms_volts) or not {1, *required_orders} <= rms_volts.keys():
        return "INCOMPLETE"
    if not all(map(finite_measure, rms_volts.values())) or min(rms_volts.values()) < 0 or rms_volts[1] <= 0 or limit_percent < 0:
        return "INCOMPLETE"
    distortion = sum(Fraction(str(v))**2 for k,v in rms_volts.items() if k > 1)
    allowed = Fraction(str(rms_volts[1])) * Fraction(str(limit_percent)) / 100
    return "PASS" if distortion <= allowed**2 else "FAIL"


def inrush_quality(trace, protection):
    """Compare interval RMS currents with a recorded piecewise no-trip ceiling since energization."""
    if not isinstance(protection, list) or not protection or not isinstance(trace, list) or not trace:
        return "INCOMPLETE"
    bands = []
    start = 0
    for band in protection:
        if not isinstance(band, (list, tuple)) or len(band) != 2 or not all(map(finite_measure, band)):
            return "INCOMPLETE"
        end, ceiling = band
        if end <= start or ceiling < 0:
            return "INCOMPLETE"
        bands.append((start, end, ceiling))
        start = end
    cursor = 0
    for record in trace:
        if not isinstance(record, (tuple, list)) or len(record) != 4:
            return "INCOMPLETE"
        left, right, amps, tripped = record
        if not all(map(finite_measure, (left,right,amps))) or type(tripped) is not bool:
            return "INCOMPLETE"
        if left != cursor or not left < right <= start or amps < 0:
            return "INCOMPLETE"
        if tripped or any(amps > ceiling for a,b,ceiling in bands if a < right and left < b):
            return "FAIL"
        cursor = right
    return "PASS" if cursor == start else "INCOMPLETE"


def ripple_current_quality(expected_hz, spectrum_rms_a, rating_a):
    """Compare orthogonal AC components against a recorded broadband RMS rating."""
    if not isinstance(expected_hz, (set, frozenset)) or not expected_hz:
        return "INCOMPLETE"
    if not isinstance(spectrum_rms_a, dict) or spectrum_rms_a.keys() != expected_hz:
        return "INCOMPLETE"
    if not all(finite_measure(hz) and hz > 0 for hz in expected_hz):
        return "INCOMPLETE"
    if not finite_measure(rating_a) or rating_a <= 0:
        return "INCOMPLETE"
    if not all(finite_measure(amps) and amps >= 0 for amps in spectrum_rms_a.values()):
        return "INCOMPLETE"
    heating = sum(Fraction(str(amps)) ** 2 for amps in spectrum_rms_a.values())
    return "PASS" if heating <= Fraction(str(rating_a)) ** 2 else "FAIL"


def series_energy_quality(cell_ids, capacities_ah, cutoff_rule, bank_volts, required_wh):
    """Recorded constant bank voltage, series discharge until the first cell hits cutoff."""
    if cutoff_rule != "first-cell-cutoff" or not isinstance(cell_ids, (set, frozenset)) or not cell_ids:
        return "INCOMPLETE"
    if not isinstance(capacities_ah, dict) or capacities_ah.keys() != cell_ids:
        return "INCOMPLETE"
    if not all(finite_measure(v) and v >= 0 for v in capacities_ah.values()):
        return "INCOMPLETE"
    if not all(map(finite_measure, (bank_volts, required_wh))) or bank_volts <= 0 or required_wh < 0:
        return "INCOMPLETE"
    available = Fraction(str(min(capacities_ah.values()))) * Fraction(str(bank_volts))
    return "PASS" if available >= Fraction(str(required_wh)) else "FAIL"


def arc_interruption_quality(events, coverage, traces, limit_s):
    """Each arc has its own observation window and detector/interrupt timestamps."""
    if not isinstance(events, dict) or not events or not isinstance(coverage, dict) or not isinstance(traces, dict):
        return "INCOMPLETE"
    if not finite_measure(limit_s) or limit_s < 0:
        return "INCOMPLETE"
    for event_id, event in events.items():
        if not isinstance(event, tuple) or len(event) != 2:
            return "INCOMPLETE"
        segment, start = event
        if not isinstance(segment, str) or not finite_measure(start):
            return "INCOMPLETE"
        sensor = coverage.get(segment)
        trace = traces.get(event_id)
        if sensor is False:
            return "FAIL"
        if not isinstance(sensor, str) or not sensor or not isinstance(trace, dict):
            return "INCOMPLETE"
        if trace.get("sensor") != sensor or not {"detected", "interrupted", "through"} <= trace.keys():
            return "INCOMPLETE"
        through, detected, cleared = (trace[key] for key in ("through", "detected", "interrupted"))
        if not finite_measure(through) or through < start:
            return "INCOMPLETE"
        observed = [t for t in (detected, cleared) if t is not None]
        if any(not finite_measure(t) or not start <= t <= through for t in observed):
            return "INCOMPLETE"
        deadline = Fraction(str(start)) + Fraction(str(limit_s))
        if detected is None or cleared is None:
            return "FAIL" if Fraction(str(through)) >= deadline else "INCOMPLETE"
        if cleared < detected or Fraction(str(cleared)) > deadline:
            return "FAIL"
    return "PASS"


def reactive_envelope_quality(revision, curves, condition, p_kw, q_kvar):
    """Linearly interpolate signed reactive limits at the recorded active power."""
    if not revision or not isinstance(curves, dict) or curves.get("revision") != revision:
        return "INCOMPLETE"
    envelopes = curves.get("conditions")
    if not isinstance(envelopes, dict) or not isinstance(condition, str):
        return "INCOMPLETE"
    rows = envelopes.get(condition)
    if not isinstance(rows, list) or len(rows) < 2 or not all(map(finite_measure, (p_kw, q_kvar))):
        return "INCOMPLETE"
    points = []
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 3 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        power, low, high = map(lambda n: Fraction(str(n)), row)
        if low > high or (points and power <= points[-1][0]):
            return "INCOMPLETE"
        points.append((power, low, high))
    power, q = Fraction(str(p_kw)), Fraction(str(q_kvar))
    for (left, lo, hi), (right, lo2, hi2) in zip(points, points[1:]):
        if left <= power <= right:
            t = (power - left) / (right - left)
            return "PASS" if lo + t * (lo2 - lo) <= q <= hi + t * (hi2 - hi) else "FAIL"
    return "FAIL"


def voltage_ride_quality(window, intervals, revision, profile):
    """Grade a voltage excursion in the same units as its pinned duration bands."""
    if not revision or not isinstance(profile, dict) or profile.get("revision") != revision:
        return "INCOMPLETE"
    return _band_duration_quality(window, intervals, profile.get("bands"))


def dc_load_break_quality(revision, device, category, voltage_v, load_a):
    """Grade an interruption under the circuit's specified DC utilization category."""
    if not revision or not isinstance(device, dict) or device.get("revision") != revision:
        return "INCOMPLETE"
    if not all(map(finite_measure, (voltage_v, load_a))) or voltage_v <= 0 or load_a <= 0:
        return "INCOMPLETE"
    if device.get("load_break") is False:
        return "FAIL"
    ratings = device.get("dc_ratings")
    if device.get("load_break") is not True or not isinstance(ratings, dict) or not isinstance(category, str):
        return "INCOMPLETE"
    rating = ratings.get(category)
    if rating is False:
        return "FAIL"
    if not isinstance(rating, (tuple, list)) or len(rating) != 2 or not all(map(finite_measure, rating)) or min(rating) <= 0:
        return "INCOMPLETE"
    return "PASS" if voltage_v <= rating[0] and load_a <= rating[1] else "FAIL"


def conversion_loss_quality(revision, datasheet, intervals, reported_loss_kwh):
    """Integrate DC input energy times loss fraction, interpolating only between recorded loads."""
    from math import isclose
    if not revision or not isinstance(datasheet, dict) or datasheet.get("revision") != revision:
        return "INCOMPLETE"
    points = datasheet.get("dc_kw_efficiency")
    if not isinstance(points, list) or len(points) < 2 or not isinstance(intervals, list) or not intervals:
        return "INCOMPLETE"
    if not finite_measure(reported_loss_kwh):
        return "INCOMPLETE"
    curve = []
    for row in points:
        if not isinstance(row, (tuple, list)) or len(row) != 2 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        load, efficiency = (Fraction(str(v)) for v in row)
        if load < 0 or not 0 <= efficiency <= 1 or (curve and load <= curve[-1][0]):
            return "INCOMPLETE"
        curve.append((load, efficiency))
    loss = Fraction(0)
    for interval in intervals:
        if not isinstance(interval, (tuple, list)) or len(interval) != 2 or not all(map(finite_measure, interval)):
            return "INCOMPLETE"
        load, hours = (Fraction(str(v)) for v in interval)
        if hours <= 0:
            return "INCOMPLETE"
        for (low, eta_low), (high, eta_high) in zip(curve, curve[1:]):
            if low <= load <= high:
                efficiency = eta_low + (eta_high - eta_low) * (load - low) / (high - low)
                loss += hours * load * (1 - efficiency)
                break
        else:
            return "INCOMPLETE"
    return "PASS" if isclose(reported_loss_kwh, loss, rel_tol=1e-9, abs_tol=1e-12) else "FAIL"


def black_start_quality(steps, continuous_kw, surge_kw, surge_seconds, available_kwh):
    """Test contiguous auxiliary-load stages using available pre-energization power and energy."""
    ratings = (continuous_kw, surge_kw, surge_seconds, available_kwh)
    if not all(map(finite_measure, ratings)) or min(ratings) < 0 or surge_kw < continuous_kw:
        return "INCOMPLETE"
    if not isinstance(steps, list) or not steps:
        return "INCOMPLETE"
    energy_kws = surge_time = Fraction(0)
    for step in steps:
        if not isinstance(step, (list, tuple)) or len(step) != 2 or not all(map(finite_measure, step)):
            return "INCOMPLETE"
        load, seconds = (Fraction(str(v)) for v in step)
        if load < 0 or seconds <= 0:
            return "INCOMPLETE"
        surge_time = surge_time + seconds if load > Fraction(str(continuous_kw)) else 0
        if load > Fraction(str(surge_kw)) or surge_time > Fraction(str(surge_seconds)):
            return "FAIL"
        energy_kws += load * seconds
    return "PASS" if energy_kws <= Fraction(str(available_kwh)) * 3600 else "FAIL"


def phase_sequence_quality(source_order, terminal_order, mapping, required_rotation):
    """Read the source phases at equipment terminals in the maker's declared order."""
    for order in (source_order, terminal_order):
        if not isinstance(order, (tuple, list)) or len(order) != 3:
            return "INCOMPLETE"
        if not all(isinstance(name, str) and name for name in order) or len(set(order)) != 3:
            return "INCOMPLETE"
    if required_rotation not in ("forward", "reverse") or not isinstance(mapping, dict):
        return "INCOMPLETE"
    if any(not isinstance(mapping.get(t), str) or not mapping[t] for t in terminal_order):
        return "INCOMPLETE"
    actual = [mapping[t] for t in terminal_order]
    if set(actual) != set(source_order):
        return "FAIL"
    order = list(source_order)
    if required_rotation == "reverse":
        order.reverse()
    offset = order.index(actual[0])
    return "PASS" if actual == order[offset:] + order[:offset] else "FAIL"


def recharge_deadline_quality(revision, curve, capacity_kwh, initial_soc, target_soc, available_kw, hours):
    """Integrate a pinned stepwise charge-acceptance curve over the requested SOC range."""
    numbers = (capacity_kwh, initial_soc, target_soc, available_kw, hours)
    if not all(map(finite_measure, numbers)) or not revision or not isinstance(curve, dict):
        return "INCOMPLETE"
    if curve.get("revision") != revision or curve.get("method") != "step-kw":
        return "INCOMPLETE"
    bands = curve.get("soc_bands")
    if not isinstance(bands, list) or not 0 <= initial_soc <= target_soc <= 1 or capacity_kwh <= 0 or min(available_kw, hours) < 0:
        return "INCOMPLETE"
    cursor = Fraction(str(initial_soc))
    target = Fraction(str(target_soc))
    elapsed = Fraction(0)
    previous = None
    stalled = False
    for row in bands:
        if not isinstance(row, (list, tuple)) or len(row) != 3 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        low, high, kw = (Fraction(str(value)) for value in row)
        if not 0 <= low < high <= 1 or kw < 0 or (previous is not None and low < previous):
            return "INCOMPLETE"
        previous = high
        if cursor >= target or high <= cursor:
            continue
        if low > cursor:
            return "INCOMPLETE"
        end = min(high, target)
        power = min(kw, Fraction(str(available_kw)))
        if power:
            elapsed += Fraction(str(capacity_kwh)) * (end - cursor) / power
        else:
            stalled = True
        cursor = end
    if cursor != target:
        return "INCOMPLETE"
    return "PASS" if not stalled and elapsed <= Fraction(str(hours)) else "FAIL"


def grid_closure_quality(differences, limits):
    """Check simultaneous signed differences at a recorded grid closure."""
    if not isinstance(differences, dict) or not isinstance(limits, dict):
        return "INCOMPLETE"
    fields = ("angle_deg", "frequency_hz", "voltage_v")
    if any(not finite_measure(record.get(key)) for record in (differences, limits) for key in fields):
        return "INCOMPLETE"
    if any(limits[key] < 0 for key in fields) or limits["angle_deg"] > 180:
        return "INCOMPLETE"
    angle = Fraction(str(differences["angle_deg"])) % 360
    within = min(angle, 360-angle) <= Fraction(str(limits["angle_deg"]))
    for key in ("frequency_hz", "voltage_v"):
        within = within and abs(differences[key]) <= limits[key]
    return "PASS" if within else "FAIL"


def insulation_spacing_quality(surface_path_mm, pollution_degree, revision, rules):
    """Grade a recorded shortest surface polyline and unobstructed air gap between its endpoints."""
    from math import dist, fsum, isclose
    if not revision or not isinstance(rules, dict) or revision != rules.get("revision"):
        return "INCOMPLETE"
    degrees = rules.get("degrees")
    if not isinstance(degrees, dict) or type(pollution_degree) is not int:
        return "INCOMPLETE"
    thresholds = degrees.get(pollution_degree)
    if not isinstance(thresholds, dict) or not isinstance(surface_path_mm, list) or len(surface_path_mm) < 2:
        return "INCOMPLETE"
    minima = [thresholds.get(key) for key in ("surface_mm", "air_mm")]
    if not all(finite_measure(v) and v > 0 for v in minima):
        return "INCOMPLETE"
    for point in surface_path_mm:
        if not isinstance(point, (tuple, list)) or len(point) != 3 or not all(map(finite_measure, point)):
            return "INCOMPLETE"
    surface = fsum(dist(a, b) for a, b in zip(surface_path_mm, surface_path_mm[1:]))
    air = dist(surface_path_mm[0], surface_path_mm[-1])
    return "PASS" if all(v >= m or isclose(v, m, rel_tol=1e-14) for v, m in zip((surface, air), minima)) else "FAIL"


def voltage_unbalance_quality(phasors, percent_limit):
    """ABC phase-to-neutral RMS phasors. Compare the negative to positive sequence amplitude."""
    from cmath import rect
    from math import radians, isclose
    if not isinstance(phasors, dict) or set(phasors) != {"A", "B", "C"}:
        return "INCOMPLETE"
    if not finite_measure(percent_limit) or percent_limit < 0:
        return "INCOMPLETE"
    measurements = [phasors[p] for p in "ABC"]
    for row in measurements:
        if not isinstance(row, (tuple, list)) or len(row) != 2 or not all(map(finite_measure, row)) or row[0] < 0:
            return "INCOMPLETE"
    scale = max(magnitude for magnitude, _ in measurements)
    if scale == 0:
        return "INCOMPLETE"
    va, vb, vc = (rect(magnitude / scale, radians(angle % 360)) for magnitude, angle in measurements)
    a = rect(1, radians(120))
    positive = abs(va + a * vb + a.conjugate() * vc)
    negative = abs(va + a.conjugate() * vb + a * vc)
    if positive < 1e-14:
        return "FAIL" if negative >= 1e-14 else "INCOMPLETE"
    percent = 100 * negative / positive
    return "PASS" if percent <= percent_limit or isclose(percent, percent_limit, rel_tol=1e-12, abs_tol=1e-12) else "FAIL"


def touch_voltage_quality(fault_a, bonding_ohm, duration_s, duration_limits):
    """Use RMS current through the recorded bonding impedance and the first inclusive time ceiling."""
    if not all(map(finite_measure, (fault_a, bonding_ohm, duration_s))) or min(fault_a, bonding_ohm) < 0 or duration_s <= 0:
        return "INCOMPLETE"
    if not isinstance(duration_limits, list):
        return "INCOMPLETE"
    previous = 0
    applicable = None
    for band in duration_limits:
        if not isinstance(band, (tuple, list)) or len(band) != 2 or not all(map(finite_measure, band)):
            return "INCOMPLETE"
        through, volts = band
        if through <= previous or volts < 0:
            return "INCOMPLETE"
        if previous < duration_s <= through:
            applicable = volts
        previous = through
    if applicable is None:
        return "INCOMPLETE"
    potential = Fraction(str(fault_a)) * Fraction(str(bonding_ohm))
    return "PASS" if potential <= Fraction(str(applicable)) else "FAIL"


def capacitor_hold_up_quality(capacitance_f, initial_v, cutoff_v, load_w, required_s):
    """Ideal capacitor supplying a constant-power load down to the stated cutoff."""
    values = (capacitance_f, initial_v, cutoff_v, load_w, required_s)
    if not all(map(finite_measure, values)):
        return "INCOMPLETE"
    capacitance, initial, cutoff, load, duration = (Fraction(str(v)) for v in values)
    if capacitance <= 0 or load <= 0 or min(initial, cutoff, duration) < 0:
        return "INCOMPLETE"
    usable_joules = capacitance * max(0, initial**2 - cutoff**2) / 2
    return "PASS" if usable_joules >= load * duration else "FAIL"


def bank_resonance_quality(revision, source, capacitance_f, spectrum, exclusion_hz):
    """Parallel LC model with inductive source reactance measured at the reference frequency."""
    from math import pi, sqrt
    if not revision or not isinstance(source, dict) or revision != source.get("revision"):
        return "INCOMPLETE"
    hz, reactance = source.get("reference_hz"), source.get("inductive_reactance_ohm")
    if not all(map(finite_measure, (hz, reactance, capacitance_f, exclusion_hz))):
        return "INCOMPLETE"
    if min(hz, reactance, capacitance_f) <= 0 or exclusion_hz < 0 or not isinstance(spectrum, dict) or not spectrum:
        return "INCOMPLETE"
    if any(type(order) is not int or order < 1 or not finite_measure(amps) or amps < 0 for order, amps in spectrum.items()):
        return "INCOMPLETE"
    resonant_hz = sqrt(hz / (2*pi*reactance*capacitance_f))
    return "FAIL" if any(amps > 0 and abs(order*hz - resonant_hz) <= exclusion_hz
                         for order, amps in spectrum.items()) else "PASS"


def _current_integral(window, waveform):
    """Ampere-seconds from interval-mean amperes, clipped to a fully covered time window."""
    if not isinstance(window, (tuple, list)) or len(window) != 2 or not all(map(finite_measure, window)):
        return None
    start, end = map(lambda v: Fraction(str(v)), window)
    if start >= end or not isinstance(waveform, list):
        return None
    spans = []
    for row in waveform:
        if not isinstance(row, (tuple, list)) or len(row) != 3 or not all(map(finite_measure, row)):
            return None
        left, right, amps = (Fraction(str(v)) for v in row)
        if right <= left:
            return None
        if left < end and start < right:
            spans.append((max(start, left), min(end, right), amps))
    charge = Fraction(0)
    cursor = start
    for left, right, amps in sorted(spans):
        if left != cursor:
            return None
        charge += (right - left) * amps
        cursor = right
    return charge if cursor == end else None


def dc_injection_quality(waveform, windows, max_dc_a):
    if not isinstance(windows, list) or not windows or not finite_measure(max_dc_a) or max_dc_a < 0:
        return "INCOMPLETE"
    exceeded = False
    for window in windows:
        charge = _current_integral(window, waveform)
        if charge is None:
            return "INCOMPLETE"
        left, right = (Fraction(str(v)) for v in window)
        exceeded |= abs(charge) > Fraction(str(max_dc_a)) * (right-left)
    return "FAIL" if exceeded else "PASS"




def rocof_quality(samples, window_s, threshold_hz_s, delay_s, trip_s):
    """Sampled trailing difference; consecutive pickup starts a definite-time timer.

    Samples before pickup are pre-event history. A null trip records no trip
    through the final sample. Grade one event with a latched first trip.
    """
    if not all(map(finite_measure, (window_s, threshold_hz_s, delay_s))):
        return "INCOMPLETE"
    if window_s <= 0 or threshold_hz_s <= 0 or delay_s < 0:
        return "INCOMPLETE"
    if not isinstance(samples, list) or len(samples) < 2 or (trip_s is not None and not finite_measure(trip_s)):
        return "INCOMPLETE"
    window, threshold, delay = (Fraction(str(x)) for x in (window_s, threshold_hz_s, delay_s))
    history = {}
    pickup = deadline = None
    for row in samples:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        t, hz = (Fraction(str(x)) for x in row)
        if hz <= 0 or (history and t <= last):
            return "INCOMPLETE"
        if not history:
            first = t
        last = t
        history[t] = hz
        if t - first < window:
            continue
        if t - window not in history:
            return "INCOMPLETE"
        if pickup is not None and t >= pickup + delay:
            deadline = pickup + delay if deadline is None else deadline
        if abs(hz - history[t-window]) >= window * threshold:
            if pickup is None:
                pickup = t
            if delay == 0 and deadline is None:
                deadline = t
        else:
            pickup = None
    if last - first < window:
        return "INCOMPLETE"
    if deadline is None and pickup is not None and trip_s is None:
        return "INCOMPLETE"
    observed = None if trip_s is None else Fraction(str(trip_s))
    return "PASS" if observed == deadline else "FAIL"


def droop_quality(hz, nominal_hz, deadband_hz, base_kw, gains, limits_kw, reported_kw):
    """Two pinned linear slopes outside a symmetric deadband, clamped at output limits."""
    if not all(isinstance(pair, (tuple, list)) and len(pair) == 2 for pair in (gains, limits_kw)):
        return "INCOMPLETE"
    values = (hz, nominal_hz, deadband_hz, base_kw, *gains, *limits_kw, reported_kw)
    if not all(map(finite_measure, values)):
        return "INCOMPLETE"
    hz, nominal, band, base, up, down, low, high, actual = map(lambda x: Fraction(str(x)), values)
    if min(hz, nominal) <= 0 or min(band, up, down) < 0 or not low <= base <= high:
        return "INCOMPLETE"
    target = base + up * max(0, nominal-band-hz) - down * max(0, hz-nominal-band)
    return "PASS" if actual == min(high, max(low, target)) else "FAIL"


def coulomb_count_quality(window, intervals, initial_ah, reported_ah, polarity):
    if polarity not in ("charging-positive", "discharging-positive") or not all(map(finite_measure, (initial_ah, reported_ah))):
        return "INCOMPLETE"
    charge = _current_integral(window, intervals)
    if charge is None or initial_ah < 0:
        return "INCOMPLETE"
    if polarity == "discharging-positive":
        charge = -charge
    expected = Fraction(str(initial_ah)) + charge / 3600
    return "PASS" if expected >= 0 and expected == Fraction(str(reported_ah)) else "FAIL"


def transformer_flux_quality(window, winding_trace, turns, initial_wb, limit_wb):
    """Integrate piecewise-constant induced winding voltage and check both ends of every segment."""
    if type(turns) is not int or turns <= 0 or not all(map(finite_measure, (initial_wb, limit_wb))) or limit_wb <= 0:
        return "INCOMPLETE"
    if not isinstance(window, (tuple, list)) or len(window) != 2 or not all(map(finite_measure, window)):
        return "INCOMPLETE"
    if window[0] >= window[1] or not isinstance(winding_trace, list):
        return "INCOMPLETE"
    flux, limit = Fraction(str(initial_wb)), Fraction(str(limit_wb))
    peak = abs(flux)
    cursor, end = window
    for row in winding_trace:
        if not isinstance(row, (tuple, list)) or len(row) != 3 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        left, right, volts = row
        if left != cursor or not left < right <= end:
            return "INCOMPLETE"
        flux += Fraction(str(volts)) * (Fraction(str(right))-Fraction(str(left))) / turns
        peak = max(peak, abs(flux))
        cursor = right
    if cursor != end:
        return "INCOMPLETE"
    return "PASS" if peak <= limit else "FAIL"


def contactor_open_quality(command_s, settle_s, observation_end_s, safe_v, feedback):
    """After the settling deadline the auxiliary contact must stay open and the bus de-energized."""
    if not all(map(finite_measure, (command_s, settle_s, observation_end_s, safe_v))) or min(settle_s, safe_v) < 0:
        return "INCOMPLETE"
    deadline = Fraction(str(command_s)) + Fraction(str(settle_s))
    if Fraction(str(observation_end_s)) <= deadline or not isinstance(feedback, list):
        return "INCOMPLETE"
    isolated = True
    cursor = command_s
    for row in feedback:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            return "INCOMPLETE"
        start, end, aux_open, bus_v = row
        if type(aux_open) is not bool or not all(map(finite_measure, (start, end, bus_v))):
            return "INCOMPLETE"
        if start != cursor or not start < end <= observation_end_s:
            return "INCOMPLETE"
        if Fraction(str(end)) > deadline and (not aux_open or abs(bus_v) > safe_v):
            isolated = False
        cursor = end
    if cursor != observation_end_s:
        return "INCOMPLETE"
    return "PASS" if isolated else "FAIL"


def junction_temperature_quality(ambient_c, losses_w, path, max_junction_c):
    """Steady-state series path carrying the same device losses from junction to ambient."""
    if not all(map(finite_measure, (ambient_c, max_junction_c))):
        return "INCOMPLETE"
    if not isinstance(losses_w, list) or not losses_w or not all(finite_measure(w) and w >= 0 for w in losses_w):
        return "INCOMPLETE"
    if not isinstance(path, list) or not path:
        return "INCOMPLETE"
    node = "junction"
    visited = {node}
    resistance = Fraction(0)
    for segment in path:
        if not isinstance(segment, (tuple, list)) or len(segment) != 3:
            return "INCOMPLETE"
        start, end, kelvin_per_watt = segment
        if start != node or not isinstance(end, str) or not end or end in visited:
            return "INCOMPLETE"
        if not finite_measure(kelvin_per_watt) or kelvin_per_watt < 0:
            return "INCOMPLETE"
        resistance += Fraction(str(kelvin_per_watt))
        visited.add(end)
        node = end
    if node != "ambient":
        return "INCOMPLETE"
    power = sum(Fraction(str(w)) for w in losses_w)
    temperature = Fraction(str(ambient_c)) + power * resistance
    return "PASS" if temperature <= Fraction(str(max_junction_c)) else "FAIL"


def discharge_resistor_quality(capacitance_f, initial_v, safe_v, deadline_s, resistance_ohm, pulse_j):
    """A permanently connected ideal bleeder dissipates the full initial stored energy."""
    from math import log
    values = (capacitance_f, initial_v, safe_v, deadline_s, resistance_ohm, pulse_j)
    if not all(finite_measure(v) and v > 0 for v in values) or safe_v >= initial_v:
        return "INCOMPLETE"
    time_s = resistance_ohm * capacitance_f * (log(initial_v) - log(safe_v))
    energy_j = Fraction(str(capacitance_f)) * Fraction(str(initial_v)) ** 2 / 2
    return "PASS" if time_s <= deadline_s and energy_j <= Fraction(str(pulse_j)) else "FAIL"


def battery_fault_quality(cells, strings, common_ohm, rated_fault_a):
    """Series cells and links in parallel strings feeding one common bolted fault."""
    if not isinstance(cells, dict) or not cells or not isinstance(strings, list) or not strings:
        return "INCOMPLETE"
    if not all(finite_measure(x) and x >= 0 for x in (common_ohm, rated_fault_a)):
        return "INCOMPLETE"
    used = set()
    conductance = norton_a = Fraction(0)
    for row in strings:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return "INCOMPLETE"
        members, links_ohm = row
        if not isinstance(members, list) or not members or not finite_measure(links_ohm) or links_ohm < 0:
            return "INCOMPLETE"
        resistance = Fraction(str(links_ohm))
        voltage = Fraction(0)
        for member in members:
            if not isinstance(member, str) or member in used:
                return "INCOMPLETE"
            cell = cells.get(member)
            if not isinstance(cell, (list, tuple)) or len(cell) != 2 or not all(finite_measure(x) and x > 0 for x in cell):
                return "INCOMPLETE"
            voltage += Fraction(str(cell[0]))
            resistance += Fraction(str(cell[1]))
            used.add(member)
        norton_a += voltage / resistance
        conductance += 1 / resistance
    if used != cells.keys():
        return "INCOMPLETE"
    fault_a = norton_a / (1 + Fraction(str(common_ohm)) * conductance)
    return "PASS" if Fraction(str(rated_fault_a)) >= fault_a else "FAIL"
