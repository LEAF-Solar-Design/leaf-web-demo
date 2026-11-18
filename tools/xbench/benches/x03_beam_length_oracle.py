"""Grade a supplied set of beam entities. DXF LINEs and circular ARCs are supported."""
from collections import defaultdict
from math import dist, isclose, isfinite, radians

METRES = {"m": 1, "mm": 0.001, "ft": 0.3048}


def beam_length_quality(entities, units, reported):
    if units not in METRES:
        return "INCOMPLETE"
    totals = defaultdict(float)
    for entity in entities:
        if entity.dxftype() == "ARC":
            radius, start, end = (entity.dxf.get(k) for k in ("radius","start_angle","end_angle"))
            if not all(type(x) in (int,float) and isfinite(x) for x in (radius,start,end)) or radius <= 0:
                return "INCOMPLETE"
            sweep = (end-start) % 360
            if sweep == 0 and start != end:
                sweep = 360
            totals[entity.dxf.layer] += radius * radians(sweep) * METRES[units]
            continue
        if entity.dxftype() != "LINE":
            return "INCOMPLETE"
        try:
            points = [tuple(entity.dxf.get(key)) for key in ("start", "end")]
            if any(len(p) not in (2, 3) or not all(map(isfinite, p)) for p in points):
                return "INCOMPLETE"
            totals[entity.dxf.layer] += dist(*points) * METRES[units]
        except (TypeError, ValueError, AttributeError, OverflowError):
            return "INCOMPLETE"
    if not isinstance(reported, dict) or totals.keys() != reported.keys():
        return "FAIL"
    for layer, length in totals.items():
        if not isfinite(length):
            return "INCOMPLETE"
        value = reported[layer]
        if type(value) not in (float, int) or not isclose(value, length, rel_tol=1e-9, abs_tol=1e-9):
            return "FAIL"
    return "PASS"
