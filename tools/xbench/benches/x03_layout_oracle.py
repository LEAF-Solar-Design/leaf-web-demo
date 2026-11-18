"""Grade recorded planar panel footprints in drawing units."""
from fractions import Fraction

from benches.x03_quality import finite_measure


def rail_twist_quality(force_n, eccentricity_m, stiffness_nm_per_rad, max_rotation_rad):
    """Single elastic rail with a recorded torque per radian stiffness."""
    values = (force_n, eccentricity_m, stiffness_nm_per_rad, max_rotation_rad)
    if not all(map(finite_measure, values)):
        return "INCOMPLETE"
    force, arm, stiffness, limit = (Fraction(str(x)) for x in values)
    if stiffness <= 0 or limit < 0:
        return "INCOMPLETE"
    return "PASS" if abs(force * arm) <= stiffness * limit else "FAIL"


def rail_deflection_quality(rails):
    """Simply supported rails with uniform load. Use N and mm throughout."""
    if not isinstance(rails, list) or not rails:
        return "INCOMPLETE"
    for rail in rails:
        if not isinstance(rail, dict) or rail.get("support") != "simply_supported":
            return "INCOMPLETE"
        values = [rail.get(k) for k in ("span_mm", "load_n_per_mm", "modulus_mpa", "inertia_mm4", "limit_mm")]
        if not all(map(finite_measure, values)):
            return "INCOMPLETE"
        span, load, modulus, inertia, limit = map(lambda x: Fraction(str(x)), values)
        if min(span, modulus, inertia, limit) <= 0 or load < 0:
            return "FAIL"
        if 5 * load * span**4 > 384 * modulus * inertia * limit:
            return "FAIL"
    return "PASS"


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


def wind_uplift_quality(pressures, areas, resistance):
    """Positive uplift pressures in kPa times tributary areas in square metres give attachment loads in kN."""
    if not all(isinstance(v, dict) and v for v in (pressures, areas, resistance)):
        return "INCOMPLETE"
    if areas.keys() != resistance.keys():
        return "INCOMPLETE"
    for attachment, zones in areas.items():
        rating = resistance[attachment]
        if not isinstance(zones, dict) or not zones or rating is None:
            return "INCOMPLETE"
        if not finite_measure(rating) or rating <= 0:
            return "FAIL"
        load = Fraction(0)
        for zone, area in zones.items():
            pressure = pressures.get(zone)
            if pressure is None or area is None:
                return "INCOMPLETE"
            if not finite_measure(pressure) or pressure < 0 or not finite_measure(area) or area <= 0:
                return "FAIL"
            load += Fraction(str(pressure)) * Fraction(str(area))
        if load > Fraction(str(rating)):
            return "FAIL"
    return "PASS"


def rail_joint_quality(rails, cold_c, hot_c):
    """Adjacent rail stations and as-cold joint gaps use the same length unit."""
    if not isinstance(rails, list) or len(rails) < 2 or None in (cold_c, hot_c):
        return "INCOMPLETE"
    if not all(map(finite_measure, (cold_c, hot_c))) or cold_c > hot_c:
        return "FAIL"
    rise = Fraction(str(hot_c)) - Fraction(str(cold_c))
    previous_end = previous_growth = None
    for rail in rails:
        fields = ("start", "fixed", "end", "alpha_per_c")
        if not isinstance(rail, dict) or any(rail.get(k) is None for k in fields):
            return "INCOMPLETE"
        if not all(finite_measure(rail[k]) for k in fields):
            return "FAIL"
        start, fixed, end, alpha = (Fraction(str(rail[k])) for k in fields)
        if not start <= fixed <= end or start == end or alpha < 0:
            return "FAIL"
        if previous_end is not None:
            gap = start - previous_end
            if gap < previous_growth + (fixed - start) * alpha * rise:
                return "FAIL"
        previous_end = end
        previous_growth = (end - fixed) * alpha * rise
    return "PASS"


def member_edge_quality(members, attachments, minimum_distance):
    """Check point attachments against the full boundary of their assigned planar member."""
    if not isinstance(members, dict) or not isinstance(attachments, list) or not attachments or minimum_distance is None:
        return "INCOMPLETE"
    if not finite_measure(minimum_distance) or minimum_distance < 0:
        return "FAIL"
    for row in attachments:
        if not isinstance(row, dict) or not isinstance(row.get("member"), str):
            return "INCOMPLETE"
        shape, point = members.get(row["member"]), row.get("point")
        if shape is None or point is None:
            return "INCOMPLETE"
        if (getattr(shape, "geom_type", None) != "Polygon" or not shape.is_valid or shape.is_empty
                or getattr(point, "geom_type", None) != "Point" or point.is_empty or not point.is_valid):
            return "FAIL"
        if not shape.covers(point) or point.distance(shape.boundary) < minimum_distance:
            return "FAIL"
    return "PASS"


def drain_access_quality(footprints, drains, boundary_policy):
    """Each drain has a point, radial keepout and a connected, recorded access polygon."""
    if boundary_policy not in ("touch-allowed", "touch-forbidden") or not isinstance(drains, list) or not drains:
        return "INCOMPLETE"
    if not isinstance(footprints, list):
        return "INCOMPLETE"
    for shape in footprints:
        if shape is None:
            return "INCOMPLETE"
        if getattr(shape, "geom_type", None) != "Polygon" or not shape.is_valid or shape.is_empty:
            return "FAIL"
    for drain in drains:
        if not isinstance(drain, dict) or any(drain.get(k) is None for k in ("center", "radius", "access")):
            return "INCOMPLETE"
        point, radius, access = drain["center"], drain["radius"], drain["access"]
        if (getattr(point, "geom_type", None) != "Point" or point.is_empty or not point.is_valid
                or getattr(access, "geom_type", None) != "Polygon" or access.is_empty or not access.is_valid
                or not access.covers(point) or not finite_measure(radius) or radius <= 0):
            return "FAIL"
        for shape in footprints:
            distance = shape.distance(point)
            if distance < radius or shape.relate_pattern(access, "T********"):
                return "FAIL"
            if boundary_policy == "touch-forbidden" and (distance == radius or shape.intersects(access)):
                return "FAIL"
    return "PASS"


def sliding_resistance_quality(load_cases):
    """Weight and rated downslope restraint use the same force unit. Slope is in degrees."""
    from math import cos, radians, sin, ulp
    if not isinstance(load_cases, list) or not load_cases:
        return "INCOMPLETE"
    for case in load_cases:
        keys = ("slope_deg", "weight", "friction", "restraint")
        if not isinstance(case, dict) or any(case.get(k) is None for k in keys):
            return "INCOMPLETE"
        slope, weight, friction, restraint = (case[k] for k in keys)
        if not all(map(finite_measure, (slope, weight, friction, restraint))):
            return "FAIL"
        if not 0 <= slope <= 90 or weight <= 0 or min(friction, restraint) < 0:
            return "FAIL"
        angle = radians(slope)
        demand = weight * sin(angle)
        normal = 0 if slope == 90 else weight * cos(angle)
        capacity = normal * friction + restraint
        if demand - capacity > 2 * ulp(demand):
            return "FAIL"
    return "PASS"


def module_snow_quality(modules, zone_pressures, mount_ratings):
    """Compare the peak pressure over each module to its model and mounting rating in kPa."""
    if not isinstance(modules, list) or not modules or not all(isinstance(v, dict) for v in (zone_pressures, mount_ratings)):
        return "INCOMPLETE"
    for row in modules:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("model", "mount")):
            return "INCOMPLETE"
        zones = row.get("zones")
        if not isinstance(zones, list) or not zones or not all(isinstance(z, str) for z in zones):
            return "INCOMPLETE"
        ratings = mount_ratings.get(row["model"])
        rating = ratings.get(row["mount"]) if isinstance(ratings, dict) else None
        pressures = [zone_pressures.get(zone) for zone in zones]
        if rating is None or None in pressures:
            return "INCOMPLETE"
        if not finite_measure(rating) or rating <= 0 or not all(finite_measure(p) and p >= 0 for p in pressures):
            return "FAIL"
        if max(pressures) > rating:
            return "FAIL"
    return "PASS"


def overturning_quality(wind, ballast, pivot_m, restoring_side):
    """About one recorded tipping edge, compare applied wind moments with ballast moments."""
    if not isinstance(wind, list) or not wind or not isinstance(ballast, list) or pivot_m is None:
        return "INCOMPLETE"
    if restoring_side not in ("positive", "negative"):
        return "INCOMPLETE"
    if not finite_measure(pivot_m):
        return "FAIL"
    demand = resistance = Fraction(0)
    for rows, fields in ((wind, ("force_n", "arm_m")), (ballast, ("weight_n", "centroid_m"))):
        for row in rows:
            if not isinstance(row, dict) or any(row.get(k) is None for k in fields):
                return "INCOMPLETE"
            force, position = (row[k] for k in fields)
            if not all(map(finite_measure, (force, position))) or force < 0:
                return "FAIL"
            force, position = Fraction(str(force)), Fraction(str(position))
            if fields[0] == "force_n":
                if position < 0:
                    return "FAIL"
                demand += force * position
            else:
                arm = position - Fraction(str(pivot_m))
                resistance += force * (arm if restoring_side == "positive" else -arm)
    return "PASS" if resistance >= demand else "FAIL"


def ponding_quality(areas, residual_kn, water_kn_per_m3):
    """Sum depth times tributary area times water unit weight for each roof support."""
    if not isinstance(areas, dict) or not areas or not isinstance(residual_kn, dict) or water_kn_per_m3 is None:
        return "INCOMPLETE"
    if areas.keys() != residual_kn.keys() or None in residual_kn.values():
        return "INCOMPLETE"
    if not finite_measure(water_kn_per_m3) or water_kn_per_m3 <= 0:
        return "FAIL"
    unit_weight = Fraction(str(water_kn_per_m3))
    for support, patches in areas.items():
        if not isinstance(patches, list) or not patches:
            return "INCOMPLETE"
        capacity = residual_kn[support]
        if not finite_measure(capacity) or capacity < 0:
            return "FAIL"
        volume = Fraction(0)
        for patch in patches:
            if not isinstance(patch, dict) or patch.get("depth_m") is None or patch.get("area_m2") is None:
                return "INCOMPLETE"
            depth, area = patch["depth_m"], patch["area_m2"]
            if not all(map(finite_measure, (depth, area))) or depth < 0 or area <= 0:
                return "FAIL"
            volume += Fraction(str(depth)) * Fraction(str(area))
        if volume * unit_weight > Fraction(str(capacity)):
            return "FAIL"
    return "PASS"


def seismic_anchor_quality(equipment):
    """For each item distribute m*a shear by recorded fractions across its anchor group."""
    if not isinstance(equipment, list) or not equipment:
        return "INCOMPLETE"
    for item in equipment:
        if not isinstance(item, dict) or any(item.get(k) is None for k in ("mass_kg", "lateral_m_s2", "anchors")):
            return "INCOMPLETE"
        mass, acceleration, anchors = item["mass_kg"], item["lateral_m_s2"], item["anchors"]
        if not isinstance(anchors, dict) or not anchors:
            return "INCOMPLETE"
        if not all(map(finite_measure, (mass, acceleration))) or mass <= 0 or acceleration < 0:
            return "FAIL"
        force = Fraction(str(mass)) * Fraction(str(acceleration))
        total = Fraction(0)
        for anchor in anchors.values():
            if not isinstance(anchor, dict) or any(anchor.get(k) is None for k in ("fraction", "shear_n")):
                return "INCOMPLETE"
            share, rating = anchor["fraction"], anchor["shear_n"]
            if not all(map(finite_measure, (share,rating))) or not 0 <= share <= 1 or rating <= 0:
                return "FAIL"
            fraction = Fraction(str(share))
            if force * fraction > Fraction(str(rating)):
                return "FAIL"
            total += fraction
        if total != 1:
            return "FAIL"
    return "PASS"


def pad_pressure_quality(reactions, areas, roof_limits):
    """Check each support reaction in N against its contact area and N/m^2 limit."""
    if not all(isinstance(x, dict) and x for x in (reactions, areas, roof_limits)):
        return "INCOMPLETE"
    if reactions.keys() != areas.keys() or reactions.keys() != roof_limits.keys():
        return "INCOMPLETE"
    for pad, reaction in reactions.items():
        area, limit = areas[pad], roof_limits[pad]
        if not all(map(finite_measure, (reaction, area, limit))):
            return "INCOMPLETE"
        if reaction < 0 or min(area, limit) <= 0 or Fraction(str(reaction)) > Fraction(str(area)) * Fraction(str(limit)):
            return "FAIL"
    return "PASS"


def anchor_interaction_quality(anchors, equation):
    """Use recorded nonnegative force magnitudes and capacities in matching units."""
    if equation not in ("linear", "quadratic") or not isinstance(anchors, list) or not anchors:
        return "INCOMPLETE"
    exponent = 1 if equation == "linear" else 2
    for anchor in anchors:
        if not isinstance(anchor, dict):
            return "INCOMPLETE"
        values = [anchor.get(k) for k in ("tension", "shear", "tension_rating", "shear_rating")]
        if not all(map(finite_measure, values)):
            return "INCOMPLETE"
        tension, shear, tr, sr = (Fraction(str(v)) for v in values)
        if min(tension, shear) < 0 or min(tr, sr) <= 0:
            return "FAIL"
        if (tension / tr)**exponent + (shear / sr)**exponent > 1:
            return "FAIL"
    return "PASS"


def combined_load_quality(loads, combinations, capacity):
    """Apply every recorded combination to concurrent nonnegative loads in matching units."""
    if not isinstance(loads, dict) or not loads or not combinations or not isinstance(combinations, dict):
        return "INCOMPLETE"
    if not finite_measure(capacity) or not all(map(finite_measure, loads.values())):
        return "INCOMPLETE"
    if capacity <= 0 or min(loads.values()) < 0:
        return "FAIL"
    for factors in combinations.values():
        if not isinstance(factors, dict) or factors.keys() != loads.keys() or not all(map(finite_measure, factors.values())):
            return "INCOMPLETE"
        if min(factors.values()) < 0:
            return "FAIL"
        demand = sum(Fraction(str(factors[k])) * Fraction(str(v)) for k, v in loads.items())
        if demand > Fraction(str(capacity)):
            return "FAIL"
    return "PASS"


def door_sweep_quality(hinge, tip, opening_degrees, obstacles):
    """Intersect obstacles with angular wedges and compare exact radial distance."""
    from math import atan2, ceil, cos, dist, radians, sin
    from shapely.geometry import Point, Polygon
    from shapely.geometry.base import BaseGeometry
    if not all(isinstance(p, (list, tuple)) and len(p) == 2 and all(map(finite_measure, p)) for p in (hinge, tip)):
        return "INCOMPLETE"
    if not finite_measure(opening_degrees) or not 0 < abs(opening_degrees) <= 360 or not isinstance(obstacles, list):
        return "INCOMPLETE"
    radius = dist(hinge, tip)
    if radius <= 0 or not all(isinstance(g, BaseGeometry) and g.is_valid and not g.is_empty for g in obstacles):
        return "INCOMPLETE"
    origin = Point(hinge)
    angle = atan2(tip[1] - hinge[1], tip[0] - hinge[0])
    pieces = ceil(abs(opening_degrees) / 90)
    step = radians(opening_degrees) / pieces
    for _ in range(pieces):
        # The far chord stays outside the disc throughout this <=90 degree wedge.
        ends = [(hinge[0] + 2*radius*cos(a), hinge[1] + 2*radius*sin(a)) for a in (angle, angle + step)]
        wedge = Polygon([hinge, *ends])
        for obstacle in obstacles:
            clipped = wedge.intersection(obstacle)
            if not clipped.is_empty and origin.distance(clipped) <= radius:
                return "FAIL"
        angle += step
    return "PASS"


def buckling_quality(rails, end_factors):
    """Euler elastic buckling in N and mm with effective length K times unsupported length."""
    from math import pi
    if not isinstance(rails, list) or not rails or not isinstance(end_factors, dict):
        return "INCOMPLETE"
    for rail in rails:
        if not isinstance(rail, dict) or not isinstance(rail.get("end_condition"), str):
            return "INCOMPLETE"
        k = end_factors.get(rail["end_condition"])
        values = [rail.get(key) for key in ("length_mm", "modulus_mpa", "weak_inertia_mm4", "compression_n")]
        if not all(map(finite_measure, [k, *values])):
            return "INCOMPLETE"
        length, modulus, inertia, force = values
        if min(length, modulus, inertia, k) <= 0 or force < 0:
            return "FAIL"
        if force > pi**2 * modulus * inertia / (k * length)**2:
            return "FAIL"
    return "PASS"


def support_polygon_quality(components, supports):
    """Test the mass-weighted horizontal projection on the convex support footprint."""
    from shapely.geometry import MultiPoint, Point
    if not isinstance(components, list) or not components or not isinstance(supports, list) or len(supports) < 3:
        return "INCOMPLETE"
    if not all(isinstance(p, (tuple, list)) and len(p) == 2 and all(map(finite_measure, p)) for p in supports):
        return "INCOMPLETE"
    footprint = MultiPoint(supports).convex_hull
    if footprint.area <= 0:
        return "INCOMPLETE"
    mass = moment_x = moment_y = Fraction(0)
    for part in components:
        if not isinstance(part, dict) or not finite_measure(part.get("mass")) or part["mass"] <= 0:
            return "INCOMPLETE"
        cg = part.get("center")
        if not isinstance(cg, (tuple, list)) or len(cg) != 3 or not all(map(finite_measure, cg)):
            return "INCOMPLETE"
        weight = Fraction(str(part["mass"]))
        mass += weight
        moment_x += weight * Fraction(str(cg[0]))
        moment_y += weight * Fraction(str(cg[1]))
    return "PASS" if footprint.covers(Point(float(moment_x/mass), float(moment_y/mass))) else "FAIL"


def settlement_quality(displacements, spans):
    """For each span compare absolute differential displacement to its distortion limit."""
    if not isinstance(displacements, dict) or not isinstance(spans, list) or not spans:
        return "INCOMPLETE"
    for span in spans:
        if not isinstance(span, (tuple, list)) or len(span) != 4:
            return "INCOMPLETE"
        a, b, length, limit = span
        if not all(isinstance(k, str) and k for k in (a,b)) or a == b:
            return "INCOMPLETE"
        values = (displacements.get(a), displacements.get(b), length, limit)
        if not all(map(finite_measure, values)):
            return "INCOMPLETE"
        left, right, distance, ratio = (Fraction(str(v)) for v in values)
        if distance <= 0 or ratio < 0:
            return "INCOMPLETE"
        if abs(left-right) > distance * ratio:
            return "FAIL"
    return "PASS"
