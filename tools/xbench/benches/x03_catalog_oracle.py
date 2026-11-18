"""Grade one catalog result against a recorded, versioned catalog."""
from math import isfinite


def catalog_quality(rows, part, version, unit, result):
    if not isinstance(rows, list) or not isinstance(result, dict):
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
        if (row.get("part"), row.get("version"), row.get("unit")) != (part, version, unit):
            return "FAIL"
    return "PASS" if result["rating"] == expected["rating"] else "FAIL"
