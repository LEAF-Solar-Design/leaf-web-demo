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
