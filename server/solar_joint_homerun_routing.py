"""Joint selection of terminal lead paths from explicitly supplied CAD geometry."""
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, pairwise
from math import dist, fsum, isfinite

from solar_design_graph import GraphValidationError


MAX_DRAWING_LEADS = 512
MAX_AISLE_LEADS = 64
MAX_KEEP_OUTS = 16


class HomerunRoutingInfeasible(GraphValidationError):
    def __init__(self, affected_leads):
        super().__init__("JOINT_ROUTING_INFEASIBLE", ", ".join(affected_leads))
        self.user_message = f"Route these homerun leads manually. {self.path}"


@dataclass(frozen=True)
class Aisle:
    name: str
    points: tuple
    capacity: int


@dataclass(frozen=True)
class Path:
    aisle: str | None
    points: tuple
    length_m: float
    cost: int


def _invalid():
    return GraphValidationError("INVALID_ROUTING_CONSTRAINTS", "routing_constraints")


def _point(value, dimensions):
    if (type(value) is not list or len(value) != dimensions
            or any(type(n) not in (int, float) or abs(n) > 1e12 or not isfinite(n) for n in value)):
        raise _invalid()
    return tuple(value)


def _xy(point):
    # Decimal coordinates are exact for planar topology, including tangencies.
    return tuple(Fraction(str(value)) for value in point[:2])


def _overlaps(first, second):
    """Whether two projected segments share a positive-length collinear portion."""
    a, b = first
    c, d = second
    if a == b or c == d:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    if any(dx * (p[1] - a[1]) != dy * (p[0] - a[0]) for p in (c, d)):
        return False
    axis = 0 if dx else 1
    low, high = sorted((a[axis], b[axis]))
    other_low, other_high = sorted((c[axis], d[axis]))
    return max(low, other_low) < min(high, other_high)


def _touches_box(segment, box):
    start, end = segment
    lower, upper = box
    enter, leave = 0, 1
    for a, b, low, high in zip(start, end, lower, upper):
        delta = b - a
        if delta == 0:
            if a < low or a > high:
                return False
            continue
        first, last = sorted(((low - a) / delta, (high - a) / delta))
        enter, leave = max(enter, first), min(leave, last)
        if enter > leave:
            return False
    return True


def _constraints(value):
    if (type(value) is not dict or set(value) != {"keep_outs", "aisles"}
            or type(value["keep_outs"]) is not list or len(value["keep_outs"]) > MAX_KEEP_OUTS
            or type(value["aisles"]) is not list or len(value["aisles"]) > 8):
        raise _invalid()
    names = set()

    def name_of(item, fields):
        if type(item) is not dict or set(item) != set(fields):
            raise _invalid()
        name = item["name"]
        if type(name) is not str or not name.strip() or len(name) > 64 or name in names:
            raise _invalid()
        names.add(name)
        return name

    boxes = {}
    for item in value["keep_outs"]:
        name = name_of(item, ("name", "min_xy", "max_xy"))
        low = _point(item["min_xy"], 2)
        high = _point(item["max_xy"], 2)
        if any(a >= b for a, b in zip(low, high)):
            raise _invalid()
        boxes[name] = _xy(low), _xy(high)

    aisles = []
    reserved = []
    for item in value["aisles"]:
        name = name_of(item, ("name", "points", "lead_capacity"))
        capacity, raw_points = item["lead_capacity"], item["points"]
        if (type(capacity) is not int or not 0 <= capacity <= MAX_AISLE_LEADS
                or type(raw_points) is not list or not 2 <= len(raw_points) <= 16):
            raise _invalid()
        points = tuple(_point(point, 3) for point in raw_points)
        segments = list(pairwise(map(_xy, points)))
        if any(a == b for a, b in segments):
            raise _invalid()
        if (any(_overlaps(segment, other) for segment in segments for other in reserved)
                or any(_overlaps(first, second) for first, second in combinations(segments, 2))):
            # The same physical centerline must not supply two independent capacities.
            raise _invalid()
        reserved.extend(segments)
        if not any(_touches_box(segment, box) for segment in segments for box in boxes.values()):
            aisles.append(Aisle(name, points, capacity))
    return dict(sorted(boxes.items())), sorted(aisles, key=lambda aisle: aisle.name), reserved


def _path(name, points):
    length = fsum(dist(a, b) for a, b in pairwise(points))
    return Path(name, points, length, round(length * 1_000_000))


def _options(route, boxes, aisles, reserved):
    start, end = map(tuple, route["points"])
    source_xy, target_xy = _xy(start), _xy(end)
    direct = source_xy, target_xy
    blocked = [name for name, box in boxes.items() if _touches_box(direct, box)]

    def reserved_overlap(segment):
        return any(_overlaps(segment, other) for other in reserved)

    def accessible(segment):
        return (not reserved_overlap(segment)
                and not any(_touches_box(segment, box) for box in boxes.values()))

    if not blocked and not reserved_overlap(direct):
        # The straight segment is shortest and uses no shared capacity.
        return [_path(None, (start, end))], blocked
    paths = []
    for aisle in aisles:
        candidates = []
        for centerline in (aisle.points, aisle.points[::-1]):
            if (accessible((source_xy, _xy(centerline[0])))
                    and accessible((_xy(centerline[-1]), target_xy))):
                points = ((start,) if start != centerline[0] else ()) + centerline
                if end != centerline[-1]:
                    points += (end,)
                candidates.append(_path(aisle.name, points))
        if candidates:
            paths.append(min(candidates, key=lambda path: (path.cost, path.points)))
    return sorted(paths, key=lambda path: (path.cost, path.aisle or "")), blocked


@dataclass
class Edge:
    target: int
    capacity: int
    cost: int
    reverse: int


def _allocate(options, capacities):
    """Send one unit per lead by cheapest residual augmenting path."""
    count = len(options)
    source = count + len(capacities)
    sink = source + 1
    edges = [[] for _ in range(sink + 1)]
    resource_nodes = {name: count + i for i, name in enumerate(capacities)}

    def connect(a, b, capacity, cost=0):
        edge = Edge(b, capacity, cost, len(edges[b]))
        edges[b].append(Edge(a, 0, -cost, len(edges[a])))
        edges[a].append(edge)
        return edge

    for name, node in resource_nodes.items():
        connect(node, sink, capacities[name])
    choices = []
    for lead, paths in enumerate(options):
        connect(source, lead, 1)
        choices.append([(path, connect(lead, resource_nodes[path.aisle], 1, path.cost))
                        for path in paths])

    for _ in range(count):
        costs = [None] * len(edges)
        previous = [None] * len(edges)
        costs[source] = 0
        for _ in range(len(edges) - 1):
            changed = False
            for node, outgoing in enumerate(edges):
                if costs[node] is None or node == sink:
                    continue
                for edge in outgoing:
                    if not edge.capacity or edge.target == source:
                        continue
                    cost = costs[node] + edge.cost
                    if costs[edge.target] is None or cost < costs[edge.target]:
                        costs[edge.target] = cost
                        previous[edge.target] = node, edge
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            break
        node = sink
        while node != source:
            prior, edge = previous[node]
            edge.capacity -= 1
            edges[node][edge.reverse].capacity += 1
            node = prior

    return [next((path for path, edge in paths if edge.capacity == 0), None) for paths in choices]


def route_together(routes, constraints):
    """Apply a complete minimum-cost allocation or refuse the entire mutation."""
    if len(routes) > MAX_DRAWING_LEADS:
        raise GraphValidationError("JOINT_ROUTING_LEAD_LIMIT", "routing_constraints")
    boxes, aisles, reserved = _constraints(constraints)
    candidates = [_options(route, boxes, aisles, reserved) for route in routes]
    allocation = [paths[0] if paths and paths[0].aisle is None else None for paths, _ in candidates]
    pending = [index for index, path in enumerate(allocation) if path is None]
    if len(pending) > MAX_AISLE_LEADS:
        raise GraphValidationError("JOINT_ROUTING_LEAD_LIMIT", "routing_constraints")
    capacities = {aisle.name: aisle.capacity for aisle in aisles}
    for index, selected in zip(pending, _allocate([candidates[i][0] for i in pending], capacities)):
        allocation[index] = selected
    if None in allocation:
        affected = [f"{route['from_ref']}/{route['route_kind']}" for route, path in zip(routes, allocation)
                    if path is None]
        raise HomerunRoutingInfeasible(affected)

    use = Counter(path.aisle for path in allocation)
    for route, (paths, blocked), selected in zip(routes, candidates, allocation):
        preferred = paths[0]
        explanation = {"aisle": selected.aisle, "direct_keep_outs": blocked}
        if selected.aisle is not None:
            explanation.update(aisle_leads=use[selected.aisle], aisle_capacity=capacities[selected.aisle])
        if preferred.cost < selected.cost:
            explanation["shorter_aisle_at_capacity"] = {
                "name": preferred.aisle, "length_m": preferred.length_m,
                "lead_capacity": capacities[preferred.aisle],
            }
        route["points"] = [list(point) for point in selected.points]
        route["length_ft"] = selected.length_m / .3048
        route["extra"].update(routing_policy="shared-aisle-terminal-lead-v1", routing=explanation)
    return routes
