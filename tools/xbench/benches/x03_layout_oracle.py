"""Grade recorded planar panel footprints in drawing units."""
from fractions import Fraction

from benches.x03_quality import finite_measure


def layout_quality(panels, usable_area, setback):
    if usable_area is None or setback is None or not panels:
        return "INCOMPLETE"
    for shape in (usable_area, *panels):
        if shape is None or getattr(shape, "is_empty", True):
            return "INCOMPLETE"
        if shape.geom_type not in ("Polygon", "MultiPolygon") or not shape.is_valid:
            return "FAIL"
    if not finite_measure(setback) or setback < 0:
        return "FAIL"
    for i, panel in enumerate(panels):
        if not usable_area.covers(panel) or panel.distance(usable_area.boundary) < setback:
            return "FAIL"
        if any(panel.relate_pattern(other, "T********") for other in panels[:i]):
            return "FAIL"
    return "PASS"


def row_layout_quality(rows, usable_area, setback, *, orientation=None, row_axis=None, min_pitch=None):
    """Angles are degrees from +X. Pitch is centerline distance normal to row_axis."""
    from math import atan2, cos, degrees, hypot, isclose, radians, sin
    if not rows or any(not row for row in rows) or None in (orientation, row_axis, min_pitch):
        return "INCOMPLETE"
    if not all(map(finite_measure, (orientation, row_axis, min_pitch))) or min_pitch <= 0:
        return "FAIL"
    panels = [panel for row in rows for panel in row]
    quality = layout_quality(panels, usable_area, setback)
    if quality != "PASS":
        return quality
    normal = (-sin(radians(row_axis)), cos(radians(row_axis)))
    offsets = []
    for row in rows:
        positions = []
        for panel in row:
            rectangle = panel.minimum_rotated_rectangle
            if not panel.equals(rectangle) and panel.symmetric_difference(rectangle).area > panel.area * 1e-9:
                return "INCOMPLETE"
            vertices = list(rectangle.exterior.coords)
            edges = [(b[0]-a[0], b[1]-a[1]) for a, b in zip(vertices, vertices[1:])]
            if isclose(hypot(*edges[0]), hypot(*edges[1])):
                return "INCOMPLETE"
            dx, dy = max(edges, key=lambda edge: hypot(*edge))
            difference = (degrees(atan2(dy, dx)) - orientation + 90) % 180 - 90
            if abs(difference) > 1e-7:
                return "FAIL"
            positions.append(panel.centroid.x * normal[0] + panel.centroid.y * normal[1])
        if max(positions) - min(positions) > 1e-8:
            return "FAIL"
        offsets.append(positions[0])
    offsets.sort()
    return "PASS" if all(b-a >= min_pitch or isclose(b-a, min_pitch, rel_tol=1e-12)
                         for a, b in zip(offsets, offsets[1:])) else "FAIL"


def measured_layout_quality(panels, usable_area, setback, *, panel_unit=None, area_unit=None, setback_unit=None):
    """Put panel, usable-area and setback records in the area coordinate system."""
    from shapely.affinity import scale
    metres = {"m": 1.0, "mm": 0.001, "ft": 0.3048}
    units = (panel_unit, area_unit, setback_unit)
    if None in units or setback is None or not isinstance(panels, (list, tuple)) or not panels:
        return "INCOMPLETE"
    if any(not isinstance(unit, str) or unit not in metres for unit in units) or not finite_measure(setback):
        return "FAIL"
    ratio = metres[panel_unit] / metres[area_unit]
    if any(panel is None or not hasattr(panel, "geom_type") for panel in panels):
        return "INCOMPLETE"
    converted = [scale(panel, xfact=ratio, yfact=ratio, origin=(0, 0)) for panel in panels]
    return layout_quality(converted, usable_area, setback * (metres[setback_unit] / metres[area_unit]))


def zone_load_quality(panel_loads, ballast_loads, supports, zones):
    """Mass and support shares are recorded per panel. Limits are mass per unit area."""
    from fractions import Fraction
    if not all(isinstance(x, dict) and x for x in (panel_loads, ballast_loads, supports, zones)):
        return "INCOMPLETE"
    if panel_loads.keys() != ballast_loads.keys() or panel_loads.keys() != supports.keys():
        return "INCOMPLETE"
    totals = dict.fromkeys(zones, Fraction(0))
    for panel, mass in panel_loads.items():
        ballast = ballast_loads[panel]
        shares = supports[panel]
        if mass is None or ballast is None or not isinstance(shares, dict) or not shares:
            return "INCOMPLETE"
        if not all(finite_measure(v) and v >= 0 for v in (mass, ballast, *shares.values())):
            return "FAIL"
        shares = {zone: Fraction(str(v)) for zone, v in shares.items()}
        if sum(shares.values()) != 1 or not shares.keys() <= zones.keys():
            return "FAIL"
        for zone, share in shares.items():
            totals[zone] += (Fraction(str(mass)) + Fraction(str(ballast))) * share
    for zone, record in zones.items():
        if not isinstance(record, dict) or any(record.get(k) is None for k in ("area", "limit")):
            return "INCOMPLETE"
        area, limit = record["area"], record["limit"]
        if not all(finite_measure(v) for v in (area, limit)) or area <= 0 or limit < 0:
            return "FAIL"
        if totals[zone] > Fraction(str(area)) * Fraction(str(limit)):
            return "FAIL"
    return "PASS"


def shade_exclusion_quality(panels, shade, *, drawing, revision, boundary_policy):
    """Check panel footprints against exclusions from one drawing revision."""
    if not drawing or not revision or not isinstance(shade, dict) or not panels:
        return "INCOMPLETE"
    if any(not shade.get(k) for k in ("drawing", "revision")) or boundary_policy is None:
        return "INCOMPLETE"
    if (shade["drawing"], shade["revision"]) != (drawing, revision) or boundary_policy not in ("allow-touch", "exclude-touch"):
        return "FAIL"
    polygons = shade.get("polygons")
    if not isinstance(polygons, list):
        return "INCOMPLETE"
    for geometry in (*panels, *polygons):
        if geometry is None or getattr(geometry, "is_empty", True):
            return "INCOMPLETE"
        if geometry.geom_type not in ("Polygon", "MultiPolygon") or not geometry.is_valid:
            return "FAIL"
    for panel in panels:
        for zone in polygons:
            if panel.intersects(zone) and (boundary_policy == "exclude-touch" or not panel.touches(zone)):
                return "FAIL"
    return "PASS"


def access_path_quality(paths, entrances, service_points):
    """Trace walkable, coplanar path centerlines from declared access points."""
    if not all(isinstance(items, (list, tuple)) and items for items in (paths, entrances, service_points)):
        return "INCOMPLETE"
    for items, kind in ((paths, "LineString"), (entrances, "Point"), (service_points, "Point")):
        for shape in items:
            if shape is None or getattr(shape, "is_empty", True):
                return "INCOMPLETE"
            if shape.geom_type != kind or not shape.is_valid or shape.has_z:
                return "FAIL"
    pending = [i for i, path in enumerate(paths) if any(path.covers(p) for p in entrances)]
    reached = set(pending)
    while pending:
        path = paths[pending.pop()]
        for i, other in enumerate(paths):
            if i not in reached and path.intersects(other):
                reached.add(i)
                pending.append(i)
    return "PASS" if all(any(paths[i].covers(p) for i in reached) for p in service_points) else "FAIL"


def overhead_quality(equipment, obstacles, minimum_gap):
    """Recorded footprints and vertical intervals use the same datum and length unit."""
    if not isinstance(equipment, list) or not equipment or not isinstance(obstacles, list) or minimum_gap is None:
        return "INCOMPLETE"
    if not finite_measure(minimum_gap) or minimum_gap < 0:
        return "FAIL"
    for record in [*equipment, *obstacles]:
        if not isinstance(record, dict) or any(record.get(k) is None for k in ("footprint", "bottom", "top")):
            return "INCOMPLETE"
        footprint = record["footprint"]
        if (getattr(footprint, "geom_type", None) not in ("Polygon", "MultiPolygon") or footprint.is_empty
                or not footprint.is_valid or not all(finite_measure(record[k]) for k in ("bottom", "top"))
                or record["bottom"] >= record["top"]):
            return "FAIL"
    for unit in equipment:
        for obstruction in obstacles:
            if unit["footprint"].intersects(obstruction["footprint"]):
                gap = max(Fraction(str(obstruction["bottom"])) - Fraction(str(unit["top"])),
                          Fraction(str(unit["bottom"])) - Fraction(str(obstruction["top"])))
                if gap < Fraction(str(minimum_gap)):
                    return "FAIL"
    return "PASS"


def roof_orientation_quality(normal, tilt, azimuth, tilt_tolerance, azimuth_tolerance):
    """Use an upward east/north/up normal and azimuth clockwise from north."""
    from math import atan2, degrees, hypot, isclose
    if not isinstance(normal, (tuple, list)) or len(normal) != 3:
        return "INCOMPLETE"
    fields = (*normal, tilt, azimuth, tilt_tolerance, azimuth_tolerance)
    if None in fields:
        return "INCOMPLETE"
    if not all(map(finite_measure, fields)) or not 0 <= tilt <= 90 or min(tilt_tolerance, azimuth_tolerance) < 0:
        return "FAIL"
    east, north, up = normal
    horizontal = hypot(east, north)
    if up < 0 or (horizontal == 0 and up == 0):
        return "FAIL"
    tilt_error = abs(degrees(atan2(horizontal, up)) - tilt)
    if tilt_error > tilt_tolerance and not isclose(tilt_error, tilt_tolerance, abs_tol=1e-10, rel_tol=0):
        return "FAIL"
    if horizontal == 0:
        return "PASS" if azimuth_tolerance >= 180 else "INCOMPLETE"
    bearing = degrees(atan2(east, north))
    error = abs((bearing - azimuth + 180) % 360 - 180)
    return "PASS" if error <= azimuth_tolerance or isclose(error, azimuth_tolerance, abs_tol=1e-10, rel_tol=0) else "FAIL"


def rail_support_quality(start, end, attachments, max_span, max_cantilever):
    """Use longitudinal stations measured along one straight rail in one drawing unit."""
    if not isinstance(attachments, (list, tuple)) or None in (start, end, max_span, max_cantilever, *attachments):
        return "INCOMPLETE"
    if not all(map(finite_measure, (start, end, max_span, max_cantilever, *attachments))):
        return "FAIL"
    start, end, span, overhang = map(lambda v: Fraction(str(v)), (start, end, max_span, max_cantilever))
    stations = sorted(Fraction(str(v)) for v in attachments)
    if end <= start or span <= 0 or overhang < 0 or len(set(stations)) < 2:
        return "FAIL"
    if stations[0] < start or stations[-1] > end:
        return "FAIL"
    if max(stations[0] - start, end - stations[-1]) > overhang:
        return "FAIL"
    return "PASS" if all(b - a <= span for a, b in zip(stations, stations[1:])) else "FAIL"
