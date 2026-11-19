"""Build a standalone tool for vertical clearance over explicit panel footprints."""
import re

from authored_route_crossings import PLANAR_GEOMETRY_BODY


def measures_minimum(description):
    words = description.casefold()
    return bool(re.search(r"\b(?:minimum|smallest|closest|least)\b", words)
                and re.search(r"\b(?:along|station|chainage|where)\b", words))


def matches(description):
    words = description.casefold()
    return all(re.search(pattern, words) for pattern in (
        r"\b(?:cables?|wires?|wiring|conduits?|routes?)\b",
        r"\b(?:arrays?|panels?|modules?)\b",
        r"\b(?:clearances?|separation|gap|closest approach)\b",
        r"\b(?:above|below|vertical|overhead|underneath|elevations?|3d|envelope|stepped)\b",
    ))


def build_body(description):
    return PLANAR_GEOMETRY_BODY + _BODY.replace("__MEASURE_MINIMUM__", str(measures_minimum(description)))


_BODY = r"""
import math

def role_of(label):
    words = set(re.findall(r"[a-z0-9]+", label.casefold()))
    if words & {"cable", "cables", "wire", "wires", "wiring", "conduit", "conduits", "route", "routes"}:
        return "route"
    if words & {"panel", "panels", "module", "modules", "array", "arrays"}:
        return "panel"


def panel_surface(points, handle):
    # A plane over x,y has height z0 + sx*(x-x0) + sy*(y-y0).
    origin = points[0]
    base = points[1]
    u = difference(base, origin)
    for corner in points[2:]:
        v = difference(corner, origin)
        det = cross(u, v)
        if det:
            rise_u = base[2] - origin[2]
            rise_v = corner[2] - origin[2]
            sx = (rise_u*v[1] - rise_v*u[1])/det
            sy = (u[0]*rise_v - v[0]*rise_u)/det
            break
    else:
        raise ValueError(f"Panel {handle} needs a footprint with positive projected area.")
    if any(p[2] - origin[2] != sx*(p[0]-origin[0]) + sy*(p[1]-origin[1]) for p in points):
        raise ValueError(f"Panel {handle} needs one planar surface.")
    xy = [p[:2] for p in points]
    edges = list(zip(xy, xy[1:] + xy[:1]))
    # A self-crossing footprint has no unique panel interior.
    for i, (a, b) in enumerate(edges):
        if a == b:
            raise ValueError(f"Panel {handle} has a repeated footprint vertex.")
        for j in range(i+2, len(edges)):
            if i == 0 and j == len(edges)-1:
                continue
            c, d = edges[j]
            if not overlaps(bounds((a, b)), bounds((c, d))):
                continue
            if (cross(difference(b, a), difference(c, a))*cross(difference(b, a), difference(d, a)) <= 0
                    and cross(difference(d, c), difference(a, c))*cross(difference(d, c), difference(b, c)) <= 0):
                raise ValueError(f"Panel {handle} has a self-intersecting footprint.")
    return bounds(xy), edges, origin, sx, sy


def segment_minimum(a, b, panel):
    box, edges, origin, sx, sy = panel
    if not overlaps(bounds((a[:2], b[:2])), box):
        return None
    direction = difference(b, a)
    gap = a[2] - origin[2] - sx*(a[0]-origin[0]) - sy*(a[1]-origin[1])
    change = b[2] - a[2] - sx*direction[0] - sy*direction[1]
    cuts = {Fraction(0), Fraction(1)}
    if any(direction):
        split_at_edges(a[:2], direction, edges, cuts)
    if change:
        contact = -gap/change
        if 0 < contact < 1:
            cuts.add(contact)
    # On each closed interval inside the footprint the gap is affine: its
    # absolute minimum occurs at an end or at an interior zero.
    candidates = ((abs(gap + t*change), t) for t in cuts
                  if location((a[0] + t*direction[0], a[1] + t*direction[1]), edges) >= 0)
    return min(candidates, default=None)


def route_minimum(points, panels):
    best = None
    station = 0.0
    best_station = None
    for i, (a, b) in enumerate(zip(points, points[1:])):
        length = math.dist(a, b)
        for handle, panel in panels:
            match = segment_minimum(a, b, panel)
            if match is None:
                continue
            separation, t = match
            candidate = (separation, i, t, handle)
            if best is None or candidate < best:
                best = candidate
                best_station = station + float(t)*length
        station += length
    if best is None:
        return None
    return best[0], {"separation": float(best[0]), "station": best_station, "panel_handle": best[3]}


def run(intake, params):
    limit = params.get("clearance")
    measure = __MEASURE_MINIMUM__
    if not (measure and "clearance" not in params) and (
            type(limit) not in (int, float) or not 0 < limit < float("inf")):
        raise ValueError("Set a positive minimum vertical clearance in drawing units.")
    clearance = Fraction(str(limit)) if limit is not None else None
    evidence = set(intake.get("elevation_evidence", ()))
    panels = []
    routes = []
    seen = set()
    for group in ("circles", "arcs", "inserts"):
        for entity in intake.get(group, []):
            if entity.get("space") != "paper" and (role_of(entity.get("layer", "")) or role_of(entity.get("name", ""))):
                raise ValueError("Clearance requires straight model-space routes and planar panel footprints.")
    for entity in intake.get("polylines", []):
        role = role_of(entity.get("layer", ""))
        if not role or entity.get("space") == "paper":
            continue
        handle = entity.get("handle")
        if not handle or handle in seen:
            raise ValueError("Clearance requires unique entity handles.")
        seen.add(handle)
        if handle not in evidence or any(len(p) != 3 for p in entity["pts"]):
            raise ValueError(f"Missing explicit elevation for {role} {handle}.")
        if any(entity.get("bulges", ())):
            raise ValueError(f"Curved {role} {handle} is unsupported for clearance.")
        points = [tuple(Fraction(str(value)) for value in point) for point in entity["pts"]]
        if role == "panel":
            if points and points[-1] == points[0]:
                points.pop()
            if not entity.get("closed") or len(points) < 3:
                raise ValueError(f"Panel {handle} needs a closed footprint.")
            panels.append((handle, panel_surface(points, handle)))
        else:
            if len(points) < 2:
                raise ValueError(f"Route {handle} requires at least two vertices.")
            if entity.get("closed") and points[0] != points[-1]:
                points.append(points[0])
            routes.append((handle, points))
    if not panels or not routes:
        raise ValueError("Clearance requires model-space cable routes and closed panel footprints.")
    minimum_by_route = {}
    violating, clear = [], []
    for handle, points in routes:
        minimum = route_minimum(points, panels)
        if measure:
            minimum_by_route[handle] = None if minimum is None else minimum[1]
        if clearance is not None:
            hit = minimum is not None and minimum[0] < clearance
            (violating if hit else clear).append(handle)
    result = {"units": "drawing_units",
              "scope": "Vertical centerline separation from top-level planar panel footprints on named model-space layers."}
    if measure:
        result.update(minimum_by_route=minimum_by_route,
                      station_basis="3D distance along the cable from its first vertex; earliest minimum")
    if clearance is not None:
        result.update(violating_routes=violating, clear_routes=clear, minimum_vertical_clearance=limit)
    return result, {"highlight_handles": violating, "markers": []}
"""
