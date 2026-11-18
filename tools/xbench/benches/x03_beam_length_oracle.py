"""Grade a supplied set of beam entities. DXF LINEs and circular ARCs are supported."""
from collections import defaultdict
from math import atan, dist, isclose, isfinite, radians

METRES = {"m": 1, "mm": 0.001, "ft": 0.3048}


def _lengths(entities, units, inherited, active, remaining):
    totals = defaultdict(float)
    for entity in entities:
        remaining[0] -= 1
        if remaining[0] < 0:
            return None
        layer = inherited if entity.dxf.layer == "0" else entity.dxf.layer
        if entity.dxftype() == "INSERT":
            attrs = entity.dxf
            name = attrs.name.casefold()
            scales = [abs(attrs.get(axis+"scale", 1)) for axis in "xyz"]
            if (len(active) >= 64 or name in active or len(set(scales)) != 1 or not isfinite(scales[0]) or scales[0] == 0
                    or not all(isfinite(v) for v in (*attrs.insert,attrs.rotation))):
                return None
            definition = entity.doc.blocks.get(name) if entity.doc else None
            if definition is None:
                return None
            nested = _lengths(definition, units, layer, active | {name}, remaining)
            if nested is None:
                return None
            for target, length in nested.items():
                totals[target] += length * scales[0] * entity.mcount
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
            totals[layer] += length * METRES[units]
            continue
        if entity.dxftype() == "ARC":
            radius, start, end = (entity.dxf.get(k) for k in ("radius","start_angle","end_angle"))
            if not all(type(x) in (int,float) and isfinite(x) for x in (radius,start,end)) or radius <= 0:
                return None
            sweep = (end-start) % 360
            if sweep == 0 and start != end:
                sweep = 360
            totals[layer] += radius * radians(sweep) * METRES[units]
            continue
        if entity.dxftype() != "LINE":
            return None
        try:
            points = [tuple(entity.dxf.get(key)) for key in ("start", "end")]
            if any(len(p) not in (2, 3) or not all(map(isfinite, p)) for p in points):
                return None
            totals[layer] += dist(*points) * METRES[units]
        except (TypeError, ValueError, AttributeError, OverflowError):
            return None
    return totals


def beam_length_quality(entities, units, reported, *, max_entities=100_000):
    if units not in METRES or type(max_entities) is not int or max_entities < 0:
        return "INCOMPLETE"
    totals = _lengths(entities, units, "0", set(), [max_entities])
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
