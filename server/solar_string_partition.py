"""Bounded, deterministic path partitions for geometric panel adjacency."""
from __future__ import annotations

import math
from functools import lru_cache


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


def _spatial_partition(component, points, neighbors, rank, minimum, maximum, budget):
    """Try balanced spatial cuts to give the path search smaller regions.

    Failure here says nothing about feasibility: a valid string might cross
    every proposed cut. The caller keeps the remaining budget for _search.
    """
    spent = 0
    dimensions = range(len(points[component[0]]))

    @lru_cache(maxsize=512)
    def solve(nodes, depth):
        nonlocal spent
        if spent >= budget or depth == 0:
            return None
        spent += 1
        parts = list(_components(nodes, neighbors))
        if any(not _count_feasible(len(part), minimum, maximum) for part in parts):
            return None
        if len(parts) > 1:
            combined = []
            for part in parts:
                paths = solve(tuple(sorted(part)), depth - 1)
                if paths is None:
                    return None
                combined.extend(paths)
            return combined
        if len(nodes) <= 3 * maximum:
            status, paths, used = _search(nodes, neighbors, rank, minimum, maximum,
                                          min(80, budget - spent))
            spent += used
            if status == "complete":
                return paths
            if status == "infeasible":
                return None
        if len(nodes) < 2 * minimum:
            return None

        axes = sorted(dimensions, key=lambda axis:
                      max(points[node][axis] for node in nodes)
                      - min(points[node][axis] for node in nodes), reverse=True)
        sizes = sorted((size for size in range(minimum, len(nodes) - minimum + 1)
                        if _count_feasible(size, minimum, maximum)
                        and _count_feasible(len(nodes) - size, minimum, maximum)),
                       key=lambda size: abs(2 * size - len(nodes)))
        for axis in axes:
            order = sorted(nodes, key=lambda node: (
                points[node][axis], points[node], rank[node]))
            for size in sizes:
                if spent >= budget:
                    return None
                spent += 1
                left = solve(tuple(sorted(order[:size])), depth - 1)
                if left is None:
                    continue
                right = solve(tuple(sorted(order[size:])), depth - 1)
                if right is not None:
                    return left + right
        return None

    result = solve(tuple(sorted(component)), 16)
    return result, spent


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
        if len(component) > 8 * maximum:
            found, used = _spatial_partition(component, points, neighbors, rank, minimum,
                                             maximum, (state_budget - searched) // 2)
            searched += used
            if found is not None:
                paths.extend(found)
                continue
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
