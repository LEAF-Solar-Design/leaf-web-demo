"""Author a plan-view cable crossing check and shortest drawn-aisle detour."""
import re

from authored_route_crossings import PLANAR_GEOMETRY_BODY


def matches(description):
    prompt = description.casefold()
    return all(re.search(pattern, prompt) for pattern in (
        r"\b(?:cables?|wires?|wiring|conduits?)\b",
        r"\b(?:arrays?|panels?|modules?)\b",
        r"\baisles?\b",
        r"\b(?:detours?|rerout(?:e|es|ing)|shortest|minimum[- ]length)\b",
    ))


def build_body(description):
    return PLANAR_GEOMETRY_BODY + _BODY


_BODY = r"""
import math
from heapq import heappop, heappush


def read_geometry(intake):
    arrays, aisles, centerlines, cables = [], [], [], {}
    for entity in intake.get("polylines", []):
        if entity.get("space") == "paper":
            continue
        words = set(re.findall(r"[a-z0-9]+", entity.get("layer", "").casefold()))
        if words & {"aisle", "aisles"}:
            role = "aisle"
        elif words & {"cable", "cables", "wire", "wires", "wiring", "conduit", "conduits", "route", "routes"}:
            role = "cable"
        elif words & {"array", "arrays", "panel", "panels", "module", "modules"}:
            role = "array"
        else:
            continue
        handle = entity.get("handle")
        closed = entity.get("closed", False)
        if any(entity.get("bulges", [])):
            raise ValueError(f"Detours require straight segments on {handle}.")
        points = [(Fraction(p[0]), Fraction(p[1])) for p in entity["pts"]]
        if closed and len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if len(points) < (3 if closed else 2):
            raise ValueError(f"Insufficient vertices on {handle}.")
        edges = list(zip(points, points[1:] + (points[:1] if closed else [])))
        if any(a == b for a, b in edges):
            raise ValueError(f"Zero-length segment on {handle}.")
        if role == "cable":
            if not handle or handle in cables:
                raise ValueError("Cable detours require unique drawing handles.")
            cables[handle] = edges
        elif closed:
            collection = arrays if role == "array" else aisles
            collection.append((bounds(points), edges))
        elif role == "aisle":
            centerlines.extend(edges)
        else:
            raise ValueError(f"Array footprint {handle} must be closed.")
    return arrays, aisles, centerlines, cables


def at(a, direction, t):
    return a[0] + direction[0]*t, a[1] + direction[1]*t


def intervals(a, b, outlines):
    direction = difference(b, a)
    cuts = {Fraction(0), Fraction(1)}
    for box, edges in outlines:
        split_at_edges(a, direction, edges, cuts)
    ordered = sorted(cuts)
    for lo, hi in zip(ordered, ordered[1:]):
        yield at(a, direction, (lo + hi)/2)


def nearby(a, b, outlines):
    extent = bounds((a, b))
    return [outline for outline in outlines if overlaps(extent, outline[0])]


def enters_array(a, b, arrays):
    hit = nearby(a, b, arrays)
    return any(location(p, edges) == 1 for p in intervals(a, b, hit) for box, edges in hit)


def inside_drawn_aisles(a, b, aisles):
    hit = nearby(a, b, aisles)
    return bool(hit) and all(any(location(p, edges) >= 0 for box, edges in hit)
                             for p in intervals(a, b, hit))


def add_edge(graph, a, b):
    weight = math.dist(a, b)
    graph.setdefault(a, {})[b] = weight
    graph.setdefault(b, {})[a] = weight


def build_graph(arrays, aisles, centerlines, endpoints):
    # Split the planar arrangement at every crossing, including T junctions
    # and overlaps. Inside closed aisle footprints a shortest path can turn
    # at boundary vertices; on open centerlines it must follow drawn edges.
    lines = set(tuple(sorted(edge)) for edge in centerlines)
    segments = set(lines)
    for _, edges in arrays + aisles:
        segments.update(tuple(sorted(edge)) for edge in edges)
    indexed = [(edge, bounds(edge)) for edge in sorted(segments)]
    vertices = set(endpoints)
    graph = {}
    for (a, b), extent in indexed:
        direction = difference(b, a)
        cuts = {Fraction(0), Fraction(1)}
        others = [edge for edge, box in indexed if overlaps(extent, box)]
        split_at_edges(a, direction, others, cuts)
        if (a, b) in lines:
            split_at_edges(a, direction, [(p, p) for p in endpoints], cuts)
        pieces = [at(a, direction, t) for t in sorted(cuts)]
        vertices.update(pieces)
        if (a, b) in lines:
            for start, end in zip(pieces, pieces[1:]):
                if not enters_array(start, end, arrays):
                    add_edge(graph, start, end)
    if aisles:
        # Connect mutually visible vertices in the legal polygonal domain.
        candidates = sorted(p for p in vertices if any(
            location(p, edges) >= 0 for _, edges in aisles))
        for i, a in enumerate(candidates):
            for b in candidates[i+1:]:
                if inside_drawn_aisles(a, b, aisles) and not enters_array(a, b, arrays):
                    add_edge(graph, a, b)
    return graph


def shortest(graph, start, end):
    distances = {start: 0.0}
    previous = {}
    queue = [(0.0, start)]
    while queue:
        length, node = heappop(queue)
        if length != distances[node]:
            continue
        if node == end:
            path = [end]
            while path[-1] != start:
                path.append(previous[path[-1]])
            return {"status": "found", "length": length,
                    "points": [[float(x), float(y)] for x, y in reversed(path)]}
        for neighbor, weight in graph.get(node, {}).items():
            candidate = length + weight
            if candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                previous[neighbor] = node
                heappush(queue, (candidate, neighbor))
    return {"status": "no_legal_detour"}


def run(intake, params):
    arrays, aisles, centerlines, cables = read_geometry(intake)
    crossings = [h for h, edges in cables.items() if any(enters_array(a, b, arrays) for a, b in edges)]
    endpoints = {h: (cables[h][0][0], cables[h][-1][1]) for h in crossings}
    if any(start == end for start, end in endpoints.values()):
        raise ValueError("A closed crossing cable needs distinct endpoints for an aisle detour.")
    nodes = {p for pair in endpoints.values() for p in pair}
    graph = build_graph(arrays, aisles, centerlines, nodes) if crossings else {}
    detours = {h: shortest(graph, start, end) for h, (start, end) in endpoints.items()}
    shapes = [{"pts": row["points"]} for row in detours.values() if row["status"] == "found"]
    return ({"crosses_array": crossings, "detours": detours, "units": "drawing_units"},
            {"highlight_handles": crossings, "polylines": shapes, "markers": []})
"""
