"""Author a plan-view cable crossing check and shortest drawn-aisle detour."""
import re
from fractions import Fraction

from authored_route_crossings import PLANAR_GEOMETRY_BODY


_LENGTHS = {
    alias: Fraction(metres)
    for metres, aliases in (
        ("0.001", "mm millimetre millimetres millimeter millimeters"),
        ("0.01", "cm centimetre centimetres centimeter centimeters"),
        ("1", "m metre metres meter meters"),
        ("0.0254", "in inch inches"),
        ("0.3048", "ft foot feet"),
    )
    for alias in aliases.split()
}
_LENGTH_UNIT = "(?:" + "|".join(_LENGTHS) + ")"
_LENGTH = rf"([+-]?(?:\d{{1,12}}(?:\.\d{{1,12}})?|\.\d{{1,12}}))\s*({_LENGTH_UNIT})\b"


def requests_clearance(prompt):
    return bool(re.search(r"\btrays?\b", prompt)
                and re.search(r"\b(?:clearance|wide|width)\b", prompt)
                and re.search(r"\b(?:routes?|paths?|runs?|detours?)\b", prompt))


def tray_dimensions(prompt):
    def measure(patterns):
        values = {Fraction(value) * _LENGTHS[unit]
                  for pattern in patterns for value, unit in re.findall(pattern, prompt)}
        return values.pop() if len(values) == 1 else None

    width = measure((rf"\b(?:tray\s+)?width\s*(?:(?:of|is)\s+|[:=]\s*)?{_LENGTH}",
                     rf"(?<![\d.]){_LENGTH}(?:\s+|-)wide\b",
                     rf"(?<![\d.]){_LENGTH}(?:\s+|-)trays?\b"))
    side = measure((rf"\b(?:side\s+)?clearance\s*(?:(?:of|is)\s+|[:=]\s*)?{_LENGTH}",
                    rf"(?<![\d.]){_LENGTH}(?:\s+of)?\s+(?:side\s+)?clearance\b"))
    if width is None or width <= 0 or side is None or side < 0:
        return ()
    return str(width), str(side)


def compares_lengths(prompt):
    return bool(re.search(r"\b(?:avoidable|unnecessary|shorter|shorten|shortening|inefficient|excess)\b", prompt)
                and re.search(r"\b(?:detours?|routes?|paths?|runs?|length)\b", prompt))


def prefers_fewer_bends(prompt):
    return bool(re.search(r"\b(?:bends?|turns?|elbows?)\b", prompt)
                and re.search(r"\b(?:fewest|fewer|remove|minimi[sz]e|least|reduce)\b", prompt)
                and re.search(r"\b(?:same(?: total)?(?: cable)? length|equal(?: length)?|ties?|shortest)\b", prompt))


_HEADINGS = {
    "north": (0, 1), "northeast": (1, 1), "east": (1, 0),
    "southeast": (1, -1), "south": (0, -1), "southwest": (-1, -1),
    "west": (-1, 0), "northwest": (-1, 1),
}
_COMPASS = "(?:" + "|".join(_HEADINGS) + ")"


def equipment_headings(prompt):
    def heading(terms):
        directions = set(re.findall(rf"\b({_COMPASS})[- ]facing (?:{terms})\b", prompt))
        directions.update(re.findall(
            rf"\b(?:{terms})(?: faces?| heading| towards?| to the)? ({_COMPASS})\b", prompt))
        return _HEADINGS[directions.pop()] if len(directions) == 1 else None

    return heading("departure|depart|leave"), heading("arrival|arrive|enter")


def compares_ports(prompt):
    return bool(re.search(r"\b(?:cables?|wires?|conduits?)\b", prompt)
                and re.search(r"\b(?:paths?|routes?|runs?|detours?)\b", prompt)
                and re.search(r"\bports?\b", prompt)
                and (prefers_fewer_bends(prompt) or re.search(r"\b(?:equivalent|shortest)\b", prompt)))


def equipment_ports(description):
    """Read explicit named options at the two fixed routing endpoints."""
    sections = {}
    for clause in re.split(r"[.;\n]", description):
        match = re.fullmatch(r"\s*(departure|arrival) ports\s*:\s*(.+?)\s*", clause, re.I)
        if not match:
            continue
        side, entries = match.groups()
        side = side.lower()
        if side in sections:
            return {}
        options = {}
        for entry in re.split(r"\s*,\s*|\s+or\s+", entries, flags=re.I):
            port = re.fullmatch(rf"([a-z0-9][a-z0-9_-]{{0,31}})(?:\s*=\s*|\s+)({_COMPASS})", entry, re.I)
            if not port or len(options) >= 16:
                return {}
            name, heading = port.groups()
            if name.casefold() in options:
                return {}
            options[name.casefold()] = (name, _HEADINGS[heading.lower()])
        sections[side] = tuple(options.values())
    return sections if sections.keys() == {"departure", "arrival"} else {}


def matches(description):
    prompt = description.casefold()
    if requests_clearance(prompt):
        return True
    if compares_ports(prompt):
        return True
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
    dimensions = tray_dimensions(prompt) if requests_clearance(prompt) else None
    ports = equipment_ports(description) if compares_ports(prompt) else None
    fewer_bends = ports is not None or prefers_fewer_bends(prompt)
    headings = equipment_headings(prompt)
    equipment = fewer_bends and (any(headings) or bool(re.search(r"\bequipment\b", prompt)))
    body = _BODY.replace("__FEWER_BENDS__", str(fewer_bends))
    body = body.replace("__EQUIPMENT_PORTS__", repr(ports))
    body = body.replace("__EQUIPMENT_HEADINGS__", repr(headings if equipment else None))
    body = body.replace("__TRAY_DIMENSIONS__", repr(dimensions))
    compare = dimensions is not None or fewer_bends or compares_lengths(prompt)
    return PLANAR_GEOMETRY_BODY + body.replace("__COMPARE_LENGTHS__", str(compare))


_BODY = r"""
import math
from heapq import heappop, heappush

METRES_PER_UNIT = {1: "0.0254", 2: "0.3048", 3: "1609.344", 4: "0.001", 5: "0.01",
                   6: "1", 7: "1000", 10: "0.9144", 14: "0.1"}


def read_geometry(intake):
    arrays, aisles, centerlines, cables = [], [], [], {}
    obstacle_ids = []
    for entity in intake.get("polylines", []):
        if entity.get("space") == "paper":
            continue
        words = set(re.findall(r"[a-z0-9]+", entity.get("layer", "").casefold()))
        if words & {"aisle", "aisles"}:
            role = "aisle"
        elif words & {"cable", "cables", "wire", "wires", "wiring", "conduit", "conduits", "route", "routes"} or (
                __TRAY_DIMENSIONS__ is not None and words & {"tray", "trays"}):
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


def point_segment_distance_squared(point, a, b):
    dx, dy = difference(b, a)
    px, py = difference(point, a)
    t = max(0, min(1, (px*dx + py*dy) / (dx*dx + dy*dy)))
    return (px - t*dx)**2 + (py - t*dy)**2


def violates_clearance(a, b, arrays, radius):
    x0, y0, x1, y1 = bounds((a, b))
    extent = (x0-radius, y0-radius, x1+radius, y1+radius)
    limit = radius**2
    for box, edges in arrays:
        if not overlaps(extent, box):
            continue
        if location(a, edges) >= 0 or location(b, edges) >= 0:
            return True
        cuts = set()
        split_at_edges(a, difference(b, a), edges, cuts)
        if cuts:
            return True
        # For disjoint segments a nearest pair includes a segment endpoint.
        if any(min(point_segment_distance_squared(a, p, q),
                   point_segment_distance_squared(b, p, q),
                   point_segment_distance_squared(p, a, b),
                   point_segment_distance_squared(q, a, b)) < limit for p, q in edges):
            return True
    return False


def inside_drawn_aisles(a, b, aisles):
    hit = nearby(a, b, aisles)
    return bool(hit) and all(any(location(p, edges) >= 0 for box, edges in hit)
                             for p in intervals(a, b, hit))


def add_edge(graph, a, b):
    weight = math.dist(a, b)
    graph.setdefault(a, {})[b] = weight
    graph.setdefault(b, {})[a] = weight


def build_graph(arrays, aisles, centerlines, endpoints, radius=None):
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
                blocked = (enters_array(start, end, arrays) if radius is None
                           else violates_clearance(start, end, arrays, radius))
                if not blocked:
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


def bend_count(edges, departure, arrival):
    directions = [direction(a, b) for a, b in edges]
    if departure is not None:
        directions.insert(0, departure)
    if arrival is not None:
        directions.append(arrival)
    return sum(left != right for left, right in zip(directions, directions[1:]))


def best_port(options, heading):
    return min(options, key=lambda option: (option[1] != heading, option[0]))


def shortest(graph, start, end, departure=None, arrival=None, ports=None):
    if ports:
        # Each distinct initial heading is a zero-length source. The best
        # arrival port depends only on the final heading.
        initial = {(start, heading): best_port(ports["departure"], heading)[0]
                   for _, heading in ports["departure"]}
    else:
        initial = {(start, departure or (0, 0)): None}
    costs = {state: (0.0, 0) for state in initial}
    previous = {}
    queue = []
    for state in initial:
        heappush(queue, (0.0, 0, state))
    while queue:
        length, bends, state = heappop(queue)
        if (length, bends) != costs[state]:
            continue
        node, incoming = state
        if node == end:
            path = [node]
            terminal_heading = incoming
            while state not in initial:
                state = previous[state]
                path.append(state[0])
            result = {"status": "found", "length": length,
                      "points": [[float(x), float(y)] for x, y in reversed(path)]}
            if __FEWER_BENDS__:
                result["bends"] = bends
            if ports:
                result["ports"] = {"departure": initial[state],
                                   "arrival": best_port(ports["arrival"], terminal_heading)[0]}
            return result
        for neighbor, weight in graph.get(node, {}).items():
            heading = direction(node, neighbor) if __FEWER_BENDS__ else (0, 0)
            next_state = neighbor, heading
            turn = incoming != (0, 0) and incoming != heading
            arrival_heading = arrival
            if neighbor == end and ports:
                arrival_heading = best_port(ports["arrival"], heading)[1]
            entry_turn = neighbor == end and arrival_heading is not None and heading != arrival_heading
            candidate = length + weight, bends + turn + entry_turn
            if candidate < costs.get(next_state, (math.inf, 0)):
                costs[next_state] = candidate
                previous[next_state] = state
                heappush(queue, (*candidate, next_state))
    return {"status": "no_legal_detour"}


def run(intake, params):
    arrays, aisles, centerlines, cables, obstacle_ids = read_geometry(intake)
    if __COMPARE_LENGTHS__:
        dimensions = __TRAY_DIMENSIONS__
        radius = None
        if dimensions is not None:
            empty_overlay = {"highlight_handles": [], "markers": [], "polylines": []}
            if not dimensions:
                return ({"status": "needs_tray_clearance",
                         "message": "Specify one positive tray width and one nonnegative side clearance, each with a unit."},
                        empty_overlay)
            unit = METRES_PER_UNIT.get(intake.get("insunits"))
            if unit is None:
                return ({"status": "needs_drawing_units", "message": "Declare drawing insertion units."}, empty_overlay)
            if not centerlines or aisles:
                return ({"status": "needs_aisle_centerlines",
                         "message": "Swept-clearance routing requires an aisle network drawn as open centerlines."},
                        empty_overlay)
            width, side = map(Fraction, dimensions)
            radius = (width/2 + side) / Fraction(unit)
        ports = __EQUIPMENT_PORTS__
        headings = __EQUIPMENT_HEADINGS__ if ports is None else None
        if ports == {}:
            return ({"status": "needs_equipment_ports",
                     "message": "List allowed travel headings as Departure ports: name north, name east; Arrival ports: name east, name south."},
                    {"highlight_handles": [], "markers": [], "polylines": []})
        if headings is not None and None in headings:
            return ({"status": "needs_equipment_headings",
                     "message": "Specify the cable travel heading at departure and arrival."},
                    {"highlight_handles": [], "markers": [], "polylines": []})
        departure, arrival = headings or (None, None)
        endpoints = {point for edges in cables.values() for point in (edges[0][0], edges[-1][1])}
        graph = build_graph(arrays, aisles, centerlines, endpoints, radius)
        findings, unavailable, crossings, tied, choices = {}, {}, {}, {}, {}
        legal, violations = {}, {}
        for handle, edges in cables.items():
            crossed = [name for name, outline in zip(obstacle_ids, arrays)
                       if any(enters_array(a, b, [outline]) for a, b in edges)]
            if crossed:
                crossings[handle] = crossed
            if radius is not None:
                too_close = [name for name, outline in zip(obstacle_ids, arrays)
                             if any(violates_clearance(a, b, [outline], radius) for a, b in edges)]
                if too_close:
                    violations[handle] = too_close
            start, end = edges[0][0], edges[-1][1]
            if start == end:
                unavailable[handle] = "closed_route"
                continue
            route = shortest(graph, start, end, departure, arrival, ports)
            if route["status"] != "found":
                unavailable[handle] = route["status"]
                continue
            if ports:
                choices[handle] = route
            if radius is not None:
                legal[handle] = route
            drawn = math.fsum(math.dist(a, b) for a, b in edges)
            best = route["length"]
            if best < drawn and not math.isclose(best, drawn, rel_tol=1e-12):
                findings[handle] = dict(route, drawn_length=drawn, saving=drawn-best)
            elif __FEWER_BENDS__ and math.isclose(best, drawn, rel_tol=1e-12):
                if ports:
                    first = best_port(ports["departure"], direction(*edges[0]))[1]
                    last = best_port(ports["arrival"], direction(*edges[-1]))[1]
                else:
                    first, last = departure, arrival
                drawn_bends = bend_count(edges, first, last)
                if route["bends"] < drawn_bends:
                    tied[handle] = dict(route, drawn_bends=drawn_bends,
                                        bends_saved=drawn_bends-route["bends"])
        report = {"avoidable_detours": findings, "crossed_obstacles": crossings,
                  "unavailable": unavailable, "units": "drawing_units"}
        if __FEWER_BENDS__:
            report["equal_length_fewer_bends"] = tied
        if headings is not None:
            report["equipment_travel_headings"] = {"departure": list(departure), "arrival": list(arrival)}
        if ports:
            report["port_choices"] = choices
        if radius is not None:
            report["shortest_legal_routes"] = legal
            report["clearance_violations"] = violations
            report["swept_half_width"] = float(radius)
        alternatives = {**findings, **tied}
        alternatives.update({handle: legal[handle] for handle in violations if handle in legal})
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
