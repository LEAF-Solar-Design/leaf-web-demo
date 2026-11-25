"""Grade a supplied set of beam entities. DXF LINEs and circular ARCs are supported."""
from collections import defaultdict
from math import atan, dist, isclose, isfinite, radians

METRES = {"m": 1, "mm": 0.001, "ft": 0.3048}


def _rational_spline_length(entity):
    """Bound each clamped positive-weight Bezier span by its chord and control polygon."""
    from collections import Counter
    from ezdxf.math import BSpline
    degree = entity.dxf.degree
    points, weights, knots = list(entity.control_points), list(entity.weights), list(entity.knots)
    if not 1 <= degree <= 10 or not degree < len(points) <= 256 or len(points) != len(weights):
        return None
    if len(knots) != len(points) + degree + 1 or not all(map(isfinite, knots)):
        return None
    if not all(len(p) == 3 and all(map(isfinite, p)) for p in points):
        return None
    if not all(isfinite(w) and w > 0 for w in weights) or knots != sorted(knots):
        return None
    counts = Counter(knots)
    if len(counts) < 2 or counts.pop(knots[0]) != degree + 1 or counts.pop(knots[-1]) != degree + 1:
        return None
    if any(count > degree for count in counts.values()):
        return None
    width = knots[-1] - knots[0]
    try:
        curve = BSpline(points, degree + 1, [(t - knots[0]) / width for t in knots], weights)
        for knot, count in counts.items():
            for _ in range(degree - count):
                curve = curve.insert_knot((knot - knots[0]) / width)
        controls = [(*[v*w for v in p], w) for p, w in zip(curve.control_points, curve.weights())]
        spans = [controls[i:i+degree+1] for i in range(0, len(controls)-1, degree)]
        total = 0.0
        subdivisions = 0
        while spans:
            poly = spans.pop()
            xyz = [tuple(v / p[3] for v in p[:3]) for p in poly]
            upper = sum(dist(a, b) for a, b in zip(xyz, xyz[1:]))
            lower = dist(xyz[0], xyz[-1])
            if not isfinite(upper):
                return None
            if upper - lower <= upper * 1e-9:
                total += (upper + lower) / 2
                continue
            subdivisions += 1
            if subdivisions > 200000:
                return None
            left, right = [poly[0]], [poly[-1]]
            while len(poly) > 1:
                poly = [tuple(a/2 + b/2 for a, b in zip(p, q)) for p, q in zip(poly, poly[1:])]
                left.append(poly[0])
                right.append(poly[-1])
            spans.extend((left, right[::-1]))
        return total
    except (ArithmeticError, ValueError, IndexError):
        return None


def _lengths(entities, units, inherited, active, remaining, mode, transforms=(), curve_scale=1):
    totals = defaultdict(float)
    for entity in entities:
        remaining[0] -= 1
        if remaining[0] < 0:
            return None
        layer = inherited if entity.dxf.layer == "0" else entity.dxf.layer
        if mode is not None and entity.dxftype() not in ("LINE", "INSERT"):
            return None
        if entity.dxftype() == "INSERT":
            attrs = entity.dxf
            name = attrs.name.casefold()
            scales = [abs(attrs.get(axis+"scale", 1)) for axis in "xyz"]
            if (len(active) >= 64 or name in active or not all(isfinite(v) and v > 0 for v in scales)
                    or not all(isfinite(v) for v in (*attrs.insert,attrs.rotation))):
                return None
            definition = entity.doc.blocks.get(name) if entity.doc else None
            if definition is None:
                return None
            scale = curve_scale * scales[0] if curve_scale is not None and len(set(scales)) == 1 else None
            nested = _lengths(definition, units, layer, active | {name}, remaining, mode,
                              (entity.matrix44(), *transforms), scale)
            if nested is None:
                return None
            for target, length in nested.items():
                totals[target] += length * entity.mcount
            continue
        if curve_scale is None and entity.dxftype() != "LINE":
            return None
        if entity.dxftype() == "HELIX":
            length = _helix_length(entity)
            if length is None:
                return None
            totals[layer] += length * curve_scale * METRES[units]
            continue
        if entity.dxftype() == "SPLINE":
            length = _rational_spline_length(entity)
            if length is None:
                return None
            totals[layer] += length * curve_scale * METRES[units]
            continue
        if entity.dxftype() == "LWPOLYLINE":
            vertices = list(entity.get_points("xyb"))
            if len(vertices) < 2 or not all(all(map(isfinite, v)) for v in vertices):
                return None
            ends = vertices[1:] + (vertices[:1] if entity.closed else [])
            length = 0.0
            for a, b in zip(vertices, ends):
                chord = dist(a[:2], b[:2])
                bulge = abs(a[2])
                if bulge and not chord:
                    return None
                # theta=4*atan(bulge), radius=chord*(1+b*b)/(4*b).
                factor = (atan(bulge)/bulge + atan(bulge)*bulge) if bulge else 1
                length += chord * factor
            totals[layer] += length * curve_scale * METRES[units]
            continue
        if entity.dxftype() == "ELLIPSE":
            length = _ellipse_length(entity.dxf)
            if length is None:
                return None
            totals[layer] += length * curve_scale * METRES[units]
            continue
        if entity.dxftype() == "ARC":
            # OCS maps the arc plane to world coordinates by an orthonormal
            # basis. Spatial length therefore uses the same radius and sweep.
            # An omitted extrusion uses the DXF default world +Z axis.
            normal = entity.dxf.get("extrusion", (0, 0, 1))
            try:
                if len(normal) != 3 or not all(map(isfinite, normal)) or not any(normal):
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
            radius, start, end = (entity.dxf.get(k) for k in ("radius","start_angle","end_angle"))
            if not all(type(x) in (int,float) and isfinite(x) for x in (radius,start,end)) or radius <= 0:
                return None
            sweep = (end-start) % 360
            if sweep == 0 and start != end:
                sweep = 360
            totals[layer] += radius * radians(sweep) * curve_scale * METRES[units]
            continue
        if entity.dxftype() != "LINE":
            return None
        try:
            points = [tuple(entity.dxf.get(key)) for key in ("start", "end")]
            if any(len(p) not in (2, 3) or not all(map(isfinite, p)) for p in points):
                return None
            if mode == "spatial" and any(len(p) != 3 for p in points):
                return None
            for transform in transforms:
                points = [tuple(transform.transform(p)) for p in points]
            if mode == "plan":
                points = [p[:2] for p in points]
            totals[layer] += dist(*points) * METRES[units]
        except (TypeError, ValueError, AttributeError, OverflowError):
            return None
    return totals


def beam_length_quality(entities, units, reported, *, max_entities=100_000, _line_mode=None):
    if units not in METRES or type(max_entities) is not int or max_entities < 0:
        return "INCOMPLETE"
    totals = _lengths(entities, units, "0", set(), [max_entities], _line_mode)
    if totals is None:
        return "INCOMPLETE"
    if not isinstance(reported, dict) or totals.keys() != reported.keys():
        return "FAIL"
    for layer, length in totals.items():
        if not isfinite(length):
            return "INCOMPLETE"
        value = reported[layer]
        try:
            matches = type(value) in (float, int) and isfinite(value) and isclose(value, length, rel_tol=1e-9, abs_tol=1e-9)
        except OverflowError:
            matches = False
        if not matches:
            return "FAIL"
    return "PASS"


def line_measurement_quality(entities, units, reported, *, mode=None):
    if mode not in ("spatial", "plan"):
        return "INCOMPLETE"
    return beam_length_quality(entities, units, reported, _line_mode=mode)


def _ellipse_length(attrs):
    from math import cos, hypot, pi, sin, tau
    try:
        major = tuple(attrs.get("major_axis"))
        ratio, start, end = (attrs.get(k) for k in ("ratio", "start_param", "end_param"))
        if len(major) != 3 or not all(map(isfinite, (*major, ratio, start, end))):
            return None
        a = hypot(*major)
        if not isfinite(a) or a <= 0 or not 0 < ratio <= 1 or not (0 <= start <= tau and 0 <= end <= tau):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    span = (end - start) % tau
    if not span and end != start:
        span = tau
    def speed(t):
        return hypot(sin(t), ratio * cos(t))
    def integrate(left, right, fl, fm, fr, estimate, tolerance, depth):
        mid = (left + right) / 2
        fq, ft = speed((left + mid) / 2), speed((mid + right) / 2)
        first = (mid - left) * (fl + 4 * fq + fm) / 6
        last = (right - mid) * (fm + 4 * ft + fr) / 6
        delta = first + last - estimate
        if abs(delta) <= 15 * tolerance:
            return first + last + delta / 15
        if depth == 0:
            raise ArithmeticError
        return (integrate(left, mid, fl, fq, fm, first, tolerance / 2, depth - 1)
                + integrate(mid, right, fm, ft, fr, last, tolerance / 2, depth - 1))
    total = 0.0
    # Split at the axis extrema before adaptive Simpson integration.
    points = [start] + [n * pi / 2 for n in range(1, 9) if start < n * pi / 2 < start + span] + [start + span]
    try:
        for left, right in zip(points, points[1:]):
            fl, fm, fr = speed(left), speed((left + right) / 2), speed(right)
            estimate = (right - left) * (fl + 4 * fm + fr) / 6
            total += integrate(left, right, fl, fm, fr, estimate, (right - left) * 1e-13, 28)
    except ArithmeticError:
        return None
    return a * total


def _helix_length(entity):
    """Use constant-radius DXF parameters; check the accompanying spline for taper."""
    from math import hypot, tau
    from ezdxf.math import Vec3
    attrs = entity.dxf
    radius, pitch, turns, hand = (attrs.get(k) for k in ("radius", "turn_height", "turns", "handedness"))
    if not all(type(v) in (int, float) and isfinite(v) for v in (radius, pitch, turns)):
        return None
    if radius <= 0 or turns <= 0 or hand not in (0, 1) or not all(attrs.hasattr(k) for k in ("axis_base_point", "axis_vector", "start_point")):
        return None
    try:
        axis = Vec3(attrs.axis_vector).normalize()
        base = Vec3(attrs.axis_base_point)
        curve = entity.construction_tool()
        if len(curve.control_points) > 4096:
            return None
        knots = sorted(set(curve.knots()))
        if not curve.is_clamped:
            return None
        params = knots + [(a+b)/2 for a,b in zip(knots, knots[1:])]
        for point in [attrs.start_point, *curve.points(params)]:
            measured = (Vec3(point) - base).cross(axis).magnitude
            # The DXF cubic approximation to a circular quadrant has radial error < 0.03%.
            if not isclose(measured, radius, rel_tol=0.0003):
                return None
    except (ArithmeticError, ValueError, TypeError, IndexError):
        return None
    return turns * hypot(tau * radius, pitch)
