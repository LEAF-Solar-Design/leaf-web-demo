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
