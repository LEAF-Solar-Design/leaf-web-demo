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


def catalog_selection_quality(rows, version, constraints, result):
    """Bounds and ratings use the same field units, e.g. voltage_V."""
    if not version or not isinstance(rows, list) or not isinstance(result, dict) or not constraints:
        return "INCOMPLETE"
    part = result.get("part")
    if not part or not isinstance(constraints, dict):
        return "INCOMPLETE"
    if result.get("version") != version:
        return "FAIL"
    matches = [r for r in rows if isinstance(r, dict) and (r.get("part"), r.get("version")) == (part, version)]
    if len(matches) != 1:
        return "INCOMPLETE"
    ratings = matches[0].get("ratings")
    reported = result.get("ratings")
    if not isinstance(ratings, dict) or not isinstance(reported, dict):
        return "INCOMPLETE"
    for field in set(constraints) | set(reported):
        if field not in ratings or field not in reported:
            return "INCOMPLETE"
        values = [ratings[field], reported[field]]
        bounds = constraints.get(field, ())
        if field in constraints and (not isinstance(bounds, (tuple, list)) or len(bounds) != 2):
            return "INCOMPLETE"
        if any(type(v) not in (float, int) or (type(v) is float and not isfinite(v)) for v in (*values, *bounds)):
            return "FAIL"
        if values[0] != values[1] or (bounds and not bounds[0] <= values[0] <= bounds[1]):
            return "FAIL"
    return "PASS"
