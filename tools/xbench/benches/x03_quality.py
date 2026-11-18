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


def inverter_quality(strings, capacities, groups):
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
