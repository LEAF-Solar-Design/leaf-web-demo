"""Author a plan-view cable crossing check and shortest drawn-aisle detour."""
import re

from authored_route_crossings import PLANAR_GEOMETRY_BODY


def compares_lengths(prompt):
    return bool(re.search(r"\b(?:avoidable|unnecessary|shorter|shorten|shortening|inefficient|excess)\b", prompt)
                and re.search(r"\b(?:detours?|routes?|paths?|runs?|length)\b", prompt))


def prefers_fewer_bends(prompt):
    return bool(re.search(r"\b(?:bends?|turns?)\b", prompt)
                and re.search(r"\b(?:fewest|fewer|remove|minimi[sz]e|least|reduce)\b", prompt)
                and re.search(r"\b(?:same length|equal(?: length)?|ties?|shortest)\b", prompt))


def matches(description):
    prompt = description.casefold()
    if (prefers_fewer_bends(prompt)
            and re.search(r"\b(?:cables?|wires?|conduits?|aisles?)\b", prompt)
            and re.search(r"\b(?:paths?|routes?|runs?|detours?)\b", prompt)):
        return True
    if (compares_lengths(prompt) and re.search(r"\b(?:cables?|wires?|wiring|conduits?)\b", prompt)
            and re.search(r"\b(?:aisles?|obstacles?|keepouts?|arrays?|panels?|modules?)\b", prompt)):
        return True
    return all(re.search(pattern, prompt) for pattern in (
        r"\b(?:cables?|wires?|wiring|conduits?)\b",
        r"\b(?:arrays?|panels?|modules?)\b",
        r"\baisles?\b",
        r"\b(?:detours?|rerout(?:e|es|ing)|shortest|minimum[- ]length)\b",
    ))


def build_body(description):
    prompt = description.casefold()
    fewer_bends = prefers_fewer_bends(prompt)
    body = _BODY.replace("__FEWER_BENDS__", str(fewer_bends))
    return PLANAR_GEOMETRY_BODY + body.replace("__COMPARE_LENGTHS__", str(fewer_bends or compares_lengths(prompt)))


_BODY = r"""
import math
from heapq import heappop, heappush


def read_geometry(intake):
    arrays, aisles, centerlines, cables = [], [], [], {}
    obstacle_ids = []
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
        elif __COMPARE_LENGTHS__ and words & {"obstacle", "obstacles", "keepout", "keepouts"}:
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
            if role == "array":
                obstacle_ids.append(handle)
        elif role == "aisle":
            centerlines.extend(edges)
        else:
            raise ValueError(f"Array footprint {handle} must be closed.")
    return arrays, aisles, centerlines, cables, obstacle_ids


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


def direction(a, b):
    dx, dy = difference(b, a)
    scale = abs(dx) or abs(dy)
    return dx / scale, dy / scale


def bend_count(edges):
    directions = [direction(a, b) for a, b in edges]
    return sum(left != right for left, right in zip(directions, directions[1:]))


def shortest(graph, start, end):
    initial = (start, (0, 0))
    costs = {initial: (0.0, 0)}
    previous = {}
    queue = [(0.0, 0, initial)]
    while queue:
        length, bends, state = heappop(queue)
        if (length, bends) != costs[state]:
            continue
        node, incoming = state
        if node == end:
            path = [node]
            while state != initial:
                state = previous[state]
                path.append(state[0])
            result = {"status": "found", "length": length,
                      "points": [[float(x), float(y)] for x, y in reversed(path)]}
            if __FEWER_BENDS__:
                result["bends"] = bends
            return result
        for neighbor, weight in graph.get(node, {}).items():
            heading = direction(node, neighbor) if __FEWER_BENDS__ else (0, 0)
            next_state = neighbor, heading
            turn = incoming != (0, 0) and incoming != heading
            candidate = length + weight, bends + turn
            if candidate < costs.get(next_state, (math.inf, 0)):
                costs[next_state] = candidate
                previous[next_state] = state
                heappush(queue, (*candidate, next_state))
    return {"status": "no_legal_detour"}


def run(intake, params):
    arrays, aisles, centerlines, cables, obstacle_ids = read_geometry(intake)
    if __COMPARE_LENGTHS__:
        endpoints = {point for edges in cables.values() for point in (edges[0][0], edges[-1][1])}
        graph = build_graph(arrays, aisles, centerlines, endpoints)
        findings, unavailable, crossings, tied = {}, {}, {}, {}
        for handle, edges in cables.items():
            crossed = [name for name, outline in zip(obstacle_ids, arrays)
                       if any(enters_array(a, b, [outline]) for a, b in edges)]
            if crossed:
                crossings[handle] = crossed
            start, end = edges[0][0], edges[-1][1]
            if start == end:
                unavailable[handle] = "closed_route"
                continue
            route = shortest(graph, start, end)
            if route["status"] != "found":
                unavailable[handle] = route["status"]
                continue
            drawn = math.fsum(math.dist(a, b) for a, b in edges)
            best = route["length"]
            if best < drawn and not math.isclose(best, drawn, rel_tol=1e-12):
                findings[handle] = dict(route, drawn_length=drawn, saving=drawn-best)
            if __FEWER_BENDS__ and math.isclose(best, drawn, rel_tol=1e-12):
                drawn_bends = bend_count(edges)
                if route["bends"] < drawn_bends:
                    tied[handle] = dict(route, drawn_bends=drawn_bends,
                                        bends_saved=drawn_bends-route["bends"])
        report = {"avoidable_detours": findings, "crossed_obstacles": crossings,
                  "unavailable": unavailable, "units": "drawing_units"}
        if __FEWER_BENDS__:
            report["equal_length_fewer_bends"] = tied
        alternatives = {**findings, **tied}
        return (report, {"highlight_handles": list(alternatives), "markers": [],
                         "polylines": [{"pts": row["points"]} for row in alternatives.values()]})
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
