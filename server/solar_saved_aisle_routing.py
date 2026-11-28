"""Route terminal leads along connected centerlines in the saved drawing."""
from collections import defaultdict
from fractions import Fraction
from heapq import heappop, heappush
from itertools import combinations, pairwise
from math import dist, fsum, isfinite

from solar_design_graph import GraphValidationError
from solar_joint_homerun_routing import (
    JointRoutingInfeasible, MAX_AISLE_LEADS, _constraints, _touches_box, _xy,
)

MAX_SEGMENTS = 256


def _invalid():
    return GraphValidationError("INVALID_SAVED_AISLE_GEOMETRY", "saved_aisle_layer")


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _delta(a, b):
    return a[0] - b[0], a[1] - b[1]


def _contains(segment, point):
    a, b = segment
    return (_cross(_delta(b, a), _delta(point, a)) == 0
            and all(min(lo, hi) <= value <= max(lo, hi)
                    for lo, hi, value in zip(a, b, point)))


def _intersections(first, second):
    a, b = first
    c, d = second
    r, s = _delta(b, a), _delta(d, c)
    denominator = _cross(r, s)
    if not denominator:
        return {p for p in first if _contains(second, p)} | {
            p for p in second if _contains(first, p)}
    offset = _delta(c, a)
    t, u = _cross(offset, s) / denominator, _cross(offset, r) / denominator
    if 0 <= t <= 1 and 0 <= u <= 1:
        return {(a[0] + t * r[0], a[1] + t * r[1])}
    return set()


def _coordinates(point):
    # Normalize unit-conversion noise before performing exact planar topology.
    return _xy([round(point[0], 9), round(point[1], 9)]), round(point[2], 9)


def _segments(intake, layer, scale):
    if not isinstance(intake, dict) or not isinstance(intake.get("polylines"), list):
        raise _invalid()
    segments = defaultdict(set)
    segment_count = 0
    for entity in intake["polylines"]:
        if (not isinstance(entity, dict) or entity.get("space") == "paper"
                or not isinstance(entity.get("layer"), str)
                or entity["layer"].casefold() != layer.casefold()):
            continue
        points = entity.get("pts")
        bulges = entity.get("bulges", [])
        if (entity.get("closed", False) is not False or entity.get("width")
                or not isinstance(bulges, list) or any(bulges)
                or not isinstance(points, list) or len(points) < 2):
            raise _invalid()
        segment_count += len(points) - 1
        if segment_count > MAX_SEGMENTS:
            raise _invalid()
        vertices = []
        for raw in points:
            if (not isinstance(raw, list) or len(raw) not in (2, 3)
                    or any(type(value) not in (int, float) or abs(value) > 1e15
                           or not isfinite(value) for value in raw)):
                raise _invalid()
            scaled = [value * scale for value in raw]
            if len(scaled) == 2:
                scaled.append(0.0)
            if any(abs(value) > 1e12 or not isfinite(value) for value in scaled):
                raise _invalid()
            vertices.append(_coordinates(scaled))
        elevation = vertices[0][1]
        if any(z != elevation for _, z in vertices):
            raise _invalid()
        for (a, _), (b, _) in pairwise(vertices):
            if a == b:
                raise _invalid()
            segments[elevation].add(tuple(sorted((a, b))))
    if not segments:
        raise GraphValidationError("SAVED_AISLE_LAYER_EMPTY", "saved_aisle_layer")
    return segments


def _network(segments, terminals, boxes, elevation):
    cuts = {segment: set(segment) for segment in segments}
    for first, second in combinations(cuts, 2):
        shared = _intersections(first, second)
        cuts[first].update(shared)
        cuts[second].update(shared)
    adj = defaultdict(list)
    edges = set()
    for segment, split_points in cuts.items():
        split_points.update(p for p in terminals if _contains(segment, p))
        edges.update(pairwise(sorted(split_points)))
    # Retain exact XY intersections and attach the plane's exact elevation.
    z = Fraction(str(elevation))
    for a, b in sorted(edges):
        if any(_touches_box(((*a, z), (*b, z)), box) for box in boxes):
            continue
        length = dist(a, b)
        adj[a].append((b, length))
        adj[b].append((a, length))
    return adj


def _toward_destination(network, target):
    distance, successor, pending = {target: 0}, {}, [(0, target)]
    while pending:
        cost, vertex = heappop(pending)
        if cost != distance[vertex]:
            continue
        for neighbor, length in network.get(vertex, ()):
            candidate = cost + length
            if candidate < distance.get(neighbor, float("inf")):
                distance[neighbor] = candidate
                successor[neighbor] = vertex
                heappush(pending, (candidate, neighbor))
    return successor


def route_saved_aisles(routes, intake, layer, scale, drawing_keepouts):
    if len(routes) > MAX_AISLE_LEADS:
        raise _invalid()
    segments = _segments(intake, layer, scale)
    boxes = _constraints(None, drawing_keepouts)[0].values() if drawing_keepouts else ()
    terminals = defaultdict(set)
    for route in routes:
        for terminal in route["points"]:
            xy, elevation = _coordinates(terminal)
            terminals[elevation].add(xy)
    networks = {z: _network(lines, terminals[z], boxes, z) for z, lines in segments.items()
                if z in terminals}
    missing, paths, destinations = [], [], {}
    for route in routes:
        (start, z), (target, end_z) = map(_coordinates, route["points"])
        if z == end_z and z in networks and (z, target) not in destinations:
            destinations[z, target] = _toward_destination(networks[z], target)
        successors = destinations.get((z, target), {})
        if z != end_z or start not in successors:
            missing.append(route)
            continue
        points = [route["points"][0]]
        current = successors[start]
        while current != target:
            points.append([float(current[0]), float(current[1]), z])
            current = successors[current]
        points.append(route["points"][-1])
        paths.append((route, points))
    if missing:
        raise JointRoutingInfeasible(missing)
    for route, points in paths:
        route["points"] = points
        route["length_ft"] = fsum(dist(a, b) for a, b in pairwise(points)) / .3048
        route["extra"].update(routing_policy="saved-aisle-centerline-v1", saved_aisle_layer=layer)
    return routes
