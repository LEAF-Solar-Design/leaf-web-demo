"""Standalone offline tool for a plan-view cable route and footprint check."""
import re


def is_route_crossing_request(description):
    words = description.casefold()
    if not re.search(r"\b(?:cables?|wires?|wiring|conduits?|routes?)\b", words):
        return False
    if all(re.search(pattern, words) for pattern in (
        r"\b(?:arrays?|panels?|modules?)\b",
        r"\b(?:cross(?:es|ing)?|intersect(?:s|ing)?|enter(?:s|ing)?|cut(?:s|ting)? through)\b",
    )):
        return True
    return all(re.search(pattern, words) for pattern in (
        r"\baisles?\b",
        r"\b(?:check|verify|validate|audit|confirm|whether|which|flag|find|identify|report|show)\b",
        r"\b(?:stay(?:s|ing)?|remain(?:s|ing)?|follow(?:s|ing)?|leav(?:e|es|ing)|"
        r"inside|within|outside|contain(?:ed|ment)?|compliance|compliant|violations?)\b",
    ))


PLANAR_GEOMETRY_BODY = r"""
import re
from fractions import Fraction


def cross(u, v):
    return u[0]*v[1] - u[1]*v[0]


def difference(b, a):
    return b[0]-a[0], b[1]-a[1]


def bounds(points):
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def overlaps(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def split_at_edges(a, direction, edges, cuts):
    # Parameterize a route segment as a + t*direction. Include both ends of a
    # collinear overlap so boundary travel is classified as a separate interval.
    axis = 0 if direction[0] else 1
    for p, q in edges:
        edge = difference(q, p)
        delta = difference(p, a)
        det = cross(direction, edge)
        if det:
            t = cross(delta, edge)/det
            s = cross(delta, direction)/det
            if 0 <= s <= 1 and 0 < t < 1:
                cuts.add(t)
        elif cross(delta, direction) == 0:
            for point in (p, q):
                t = (point[axis] - a[axis])/direction[axis]
                if 0 < t < 1:
                    cuts.add(t)


def location(point, edges):
    # 1 = interior, 0 = boundary, -1 = exterior.
    x, y = point
    inside = False
    for a, b in edges:
        side = cross(difference(b, a), difference(point, a))
        if side == 0 and min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1]):
            return 0
        if (a[1] > y) != (b[1] > y) and (side > 0) == (b[1] > a[1]):
            inside = not inside
    return 1 if inside else -1


"""


ROUTE_CROSSINGS_BODY = PLANAR_GEOMETRY_BODY + r"""
def classify(points, polygons):
    in_aisles = True
    for a, b in zip(points, points[1:]):
        extent = bounds((a, b))
        nearby = [(role, edges) for role, box, edges in polygons if overlaps(extent, box)]
        direction = difference(b, a)
        cuts = {Fraction(0), Fraction(1)}
        for _, edges in nearby:
            split_at_edges(a, direction, edges, cuts)
        ordered = sorted(cuts)
        # Location is constant between consecutive edge intersections. The union
        # of the closed aisles covers a segment iff it covers every interval.
        for start, end in zip(ordered, ordered[1:]):
            t = (start + end)/2
            sample = (a[0] + t*direction[0], a[1] + t*direction[1])
            covered = False
            inside_array = False
            for role, edges in nearby:
                relation = location(sample, edges)
                if role == "array" and relation == 1:
                    inside_array = True
                if role == "aisle" and relation >= 0:
                    covered = True
            if inside_array and not covered:
                return "crosses_array"
            in_aisles = in_aisles and covered
    return "stays_in_aisle" if in_aisles else "outside_aisle"


def run(intake, params):
    polygons, routes = [], []
    for entity in intake.get("polylines", []):
        if entity.get("space") == "paper":
            continue
        words = set(re.findall(r"[a-z0-9]+", entity.get("layer", "").casefold()))
        if words & {"cable", "cables", "wire", "wires", "wiring", "conduit", "conduits", "route", "routes"}:
            role = "route"
        elif words & {"aisle", "aisles"}:
            role = "aisle"
        elif words & {"array", "arrays", "panel", "panels", "module", "modules"}:
            role = "array"
        else:
            continue
        handle = entity.get("handle")
        if any(entity.get("bulges", [])):
            raise ValueError(f"Curved geometry is unsupported on {handle}.")
        points = [(Fraction(pt[0]), Fraction(pt[1])) for pt in entity["pts"]]
        closed = entity.get("closed", False)
        if closed and len(points) > 1 and points[-1] == points[0]:
            points.pop()
        if len(points) < (3 if closed else 2) or any(a == b for a, b in zip(points, points[1:])):
            raise ValueError(f"Degenerate geometry on {handle}.")
        if role == "route":
            if not handle:
                raise ValueError("Cable route is missing its drawing handle.")
            routes.append((handle, points + points[:1] if closed else points))
        elif closed:
            polygons.append((role, bounds(points), list(zip(points, points[1:] + points[:1]))))
        else:
            raise ValueError(f"The {role} footprint on {handle} must be closed.")
    result = {"crosses_array": [], "stays_in_aisle": [], "outside_aisle": []}
    for handle, points in routes:
        result[classify(points, polygons)].append(handle)
    return result, {"highlight_handles": result["crosses_array"], "markers": []}
"""
