"""Grade a supplied set of beam entities. DXF LINEs and circular ARCs are supported."""
from collections import defaultdict
from math import atan, dist, isclose, isfinite, radians

METRES = {"m": 1, "mm": 0.001, "ft": 0.3048}


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
