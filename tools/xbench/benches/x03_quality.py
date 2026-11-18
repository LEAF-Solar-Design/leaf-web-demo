"""Reference checks for small frozen X03 quality examples."""
from collections import Counter
from fractions import Fraction
from itertools import product


def route_quality(route, aisle, arrays):
    if route is None or aisle is None or arrays is None:
        return "INCOMPLETE"
    if route.is_empty or aisle.is_empty or any(a is None or a.is_empty for a in arrays):
        return "INCOMPLETE"
    if not aisle.covers(route) or any(route.relate_pattern(a, "T********") for a in arrays):
        return "FAIL"
    return "PASS"


def inverter_quality(strings, capacities, groups):
    """Tiny exhaustive oracle. Each string occupies one inverter input."""
    if capacities is None or groups is None or strings is None:
        return "INCOMPLETE"
    if (not capacities or len(groups) != len(capacities) or
            any(type(c) is not int or c <= 0 for c in capacities)):
        return "FAIL"
    loads = tuple(map(len, groups))
    if (len(set(strings)) != len(strings) or Counter(strings) != Counter(s for g in groups for s in g)
            or any(n > c for n, c in zip(loads, capacities))):
        return "FAIL"

    def spread(counts):
        fills = [Fraction(n, c) for n, c in zip(counts, capacities)]
        return max(fills) - min(fills)

    best = min(spread(counts) for counts in product(*(range(c + 1) for c in capacities))
               if sum(counts) == len(strings))
    return "PASS" if spread(loads) == best else "FAIL"


def string_quality(members, adjacency):
    if not members or adjacency is None or any(m not in adjacency for m in members):
        return "INCOMPLETE"
    selected = set(members)
    if len(selected) != len(members):
        return "FAIL"
    pending = [members[0]]
    reached = set()
    while pending:
        member = pending.pop()
        if member not in reached:
            reached.add(member)
            pending.extend(selected.intersection(adjacency[member]) - reached)
    return "PASS" if reached == selected else "FAIL"
