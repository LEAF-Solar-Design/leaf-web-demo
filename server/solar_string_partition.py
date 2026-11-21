"""Bounded, deterministic path partitions for geometric panel adjacency."""
from __future__ import annotations

import math


DEFAULT_STATE_BUDGET = 50_000


def validate_partition(ordered, points, paths, minimum, maximum, max_step):
    """Validate a complete plan independently of the search's adjacency table."""
    expected = set(ordered)
    flat = [index for path in paths for index in path]
    if (any(type(index) is not int for index in flat)
            or len(flat) != len(ordered) or len(set(flat)) != len(flat)
            or set(flat) != expected):
        raise ValueError("invalid string partition coverage")
    for path in paths:
        if not minimum <= len(path) <= maximum:
            raise ValueError("invalid string partition size")
        if any(math.dist(points[left], points[right]) > max_step
               for left, right in zip(path, path[1:])):
            raise ValueError("invalid string partition adjacency")


def _count_feasible(count, minimum, maximum):
    return (count + maximum - 1) // maximum <= count // minimum


def _components(ordered, neighbors):
    unseen = set(ordered)
    for start in ordered:
        if start not in unseen:
            continue
        unseen.remove(start)
        component, pending = [], [start]
        while pending:
            node = pending.pop()
            component.append(node)
            following = neighbors[node] & unseen
            unseen.difference_update(following)
            pending.extend(sorted(following, reverse=True))
        yield component


def _anchor_reachable(remaining, last, anchor, slots, neighbors):
    """Can an unused-panel suffix reach the required anchor within its hop limit?"""
    if last == anchor:
        return True
    reached = {last}
    frontier = {last}
    for _ in range(slots):
        following = set()
        for node in frontier:
            following.update(neighbors[node] & remaining)
        following.difference_update(reached)
        if anchor in following:
            return True
        if not following:
            return False
        reached.update(following)
        frontier = following
    return False


def _search(component, neighbors, rank, minimum, maximum, budget):
    """Enumerate path partitions with a lazy stack, without Python recursion.

    Each next string must contain the first remaining panel. Its start can be
    any panel, so this removes string permutations without losing partitions
    where that panel lies in the interior of a path.
    """
    def successors(state):
        remaining, path, completed, anchor = state
        if not path:
            anchor = min(remaining, key=rank.__getitem__)
            starts = sorted(remaining, key=lambda node: (
                len(neighbors[node] & remaining), rank[node]))
            for node in starts:
                yield remaining - {node}, (node,), completed, anchor
            return
        # Every completion of this prefix must visit the anchor without
        # reusing a panel or exceeding the current string's maximum size.
        if anchor not in path and not _anchor_reachable(
                remaining, path[-1], anchor, maximum - len(path), neighbors):
            return
        total = len(remaining) + len(path)
        string_count = (total + maximum - 1) // maximum
        preferred_length = (total + string_count - 1) // string_count
        can_close = (anchor in path and len(path) >= minimum
                     and _count_feasible(len(remaining), minimum, maximum))
        if can_close and len(path) == preferred_length and remaining:
            yield remaining, (), completed + (path,), None
        if len(path) < maximum:
            following = sorted(neighbors[path[-1]] & remaining, key=lambda node: (
                len(neighbors[node] & remaining), rank[node]))
            for node in following:
                yield remaining - {node}, path + (node,), completed, anchor
        if can_close and len(path) != preferred_length and remaining:
            yield remaining, (), completed + (path,), None

    initial = frozenset(component), (), (), None
    stack = [iter((initial,))]
    searched = 0
    while stack:
        try:
            state = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if searched >= budget:
            return "unresolved", [], searched
        searched += 1
        remaining, path, completed, anchor = state
        if not remaining:
            if minimum <= len(path) <= maximum and anchor in path:
                return "complete", [list(item) for item in (*completed, path)], searched
            continue
        stack.append(iter(successors(state)))
    return "infeasible", [], searched


def plan_partition(ordered, points, minimum, maximum, max_step,
                   *, state_budget=DEFAULT_STATE_BUDGET):
    """Return a complete validated plan, a proof of infeasibility, or unresolved."""
    if (type(minimum) is not int or type(maximum) is not int
            or minimum < 1 or maximum < minimum
            or type(state_budget) is not int or state_budget < 0):
        raise ValueError("invalid string partition bounds or budget")
    if (any(type(index) is not int or not 0 <= index < len(points) for index in ordered)
            or len(set(ordered)) != len(ordered)
            or not math.isfinite(max_step) or max_step <= 0
            or any(not all(math.isfinite(value) for value in points[index]) for index in ordered)):
        raise ValueError("invalid string partition geometry")
    neighbors = {index: set() for index in ordered}
    for offset, left in enumerate(ordered):
        for right in ordered[:offset]:
            if math.dist(points[left], points[right]) <= max_step:
                neighbors[left].add(right)
                neighbors[right].add(left)
    rank = {index: offset for offset, index in enumerate(ordered)}
    components = list(_components(ordered, neighbors))
    for component in components:
        if not _count_feasible(len(component), minimum, maximum):
            return {"status": "infeasible", "reason": "component_cardinality",
                    "paths": [], "searched_states": 0,
                    "witness": {"panel_count": len(component),
                                "minimum_strings": (len(component) + maximum - 1) // maximum,
                                "maximum_strings": len(component) // minimum}}
    paths, searched = [], 0
    for component in components:
        status, found, used = _search(component, neighbors, rank, minimum, maximum,
                                     max(0, state_budget - searched))
        searched += used
        if status != "complete":
            reason = "search_budget" if status == "unresolved" else "exhaustive_path_search"
            return {"status": status, "reason": reason, "paths": [],
                    "searched_states": searched, "witness": {"panel_count": len(component)}}
        paths.extend(found)
    validate_partition(ordered, points, paths, minimum, maximum, max_step)
    return {"status": "complete", "reason": "validated_partition", "paths": paths,
            "searched_states": searched, "witness": None}
