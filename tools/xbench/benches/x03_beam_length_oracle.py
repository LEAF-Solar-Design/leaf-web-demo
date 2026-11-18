"""Compare grouped model-space DXF LINE lengths in metres."""
from collections import defaultdict
from math import dist, isclose, isfinite

METRES = {"m": 1, "mm": 0.001, "ft": 0.3048}


def beam_length_quality(entities, units, reported):
    if units not in METRES:
        return "INCOMPLETE"
    totals = defaultdict(float)
    for entity in entities:
        if entity.dxftype() == "LINE":
            totals[entity.dxf.layer] += dist(entity.dxf.start, entity.dxf.end) * METRES[units]
    if totals.keys() != reported.keys():
        return "FAIL"
    for layer, length in totals.items():
        value = reported[layer]
        if not isfinite(length):
            return "INCOMPLETE"
        if type(value) not in (float, int) or not isclose(value, length, rel_tol=1e-9, abs_tol=1e-9):
            return "FAIL"
    return "PASS"
