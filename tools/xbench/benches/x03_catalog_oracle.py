"""Grade one catalog result against a recorded, versioned catalog."""
from fractions import Fraction
from math import isfinite

# Exact SI scale factors within each supported dimension.
UNITS = {
    'W': ('power', 1), 'kW': ('power', 1000),
    'V': ('voltage', 1), 'kV': ('voltage', 1000),
    'A': ('current', 1), 'mA': ('current', Fraction(1, 1000)),
}


def catalog_quality(rows, part, version, unit, result):
    if not isinstance(rows, list) or not isinstance(result, dict):
        return "INCOMPLETE"
    if not all(isinstance(value, str) and value.strip() for value in (part, version, unit)):
        return "INCOMPLETE"
    matches = [r for r in rows if isinstance(r, dict) and
               (r.get("part"), r.get("version")) == (part, version)]
    if len(matches) != 1:
        return "INCOMPLETE"
    expected = matches[0]
    for row in (expected, result):
        n = row.get("rating")
        if type(n) is not int and (type(n) is not float or not isfinite(n)):
            return "FAIL"
        if (row.get("part"), row.get("version")) != (part, version):
            return "FAIL"
    source = expected.get("unit")
    if not isinstance(source, str) or not source:
        return "INCOMPLETE"
    if result.get("unit") != unit:
        return "FAIL"
    if source == unit:
        return "PASS" if result["rating"] == expected["rating"] else "FAIL"
    if source not in UNITS or unit not in UNITS:
        return "INCOMPLETE"
    dimension, scale = UNITS[source]
    target_dimension, target_scale = UNITS[unit]
    if dimension != target_dimension:
        return "FAIL"
    matches = Fraction(str(expected["rating"])) * scale == Fraction(str(result["rating"])) * target_scale
    return "PASS" if matches else "FAIL"
