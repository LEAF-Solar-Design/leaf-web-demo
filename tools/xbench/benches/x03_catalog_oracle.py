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


def paginated_catalog_quality(pages, version, expected_parts):
    """Follow a recorded cursor chain from the initial request to the terminal page."""
    if not isinstance(pages, list) or not pages or not isinstance(version, str) or not version:
        return "INCOMPLETE"
    if not isinstance(expected_parts, list) or not all(isinstance(p, str) and p for p in expected_parts):
        return "INCOMPLETE"
    if len(set(expected_parts)) != len(expected_parts):
        return "FAIL"
    indexed = {}
    for page in pages:
        if not isinstance(page, dict) or any(k not in page for k in ("cursor", "next", "version", "rows")):
            return "INCOMPLETE"
        if page["version"] != version or not isinstance(page["rows"], list):
            return "FAIL"
        if any(c is not None and (not isinstance(c, str) or not c) for c in (page["cursor"], page["next"])):
            return "FAIL"
        if page["cursor"] in indexed:
            return "FAIL"
        indexed[page["cursor"]] = page
    cursor, visited, parts = None, set(), set()
    while cursor not in visited:
        if cursor not in indexed:
            return "INCOMPLETE"
        visited.add(cursor)
        page = indexed[cursor]
        for row in page["rows"]:
            if not isinstance(row, dict) or not isinstance(row.get("part"), str) or not row["part"]:
                return "INCOMPLETE"
            if row.get("version", version) != version or row["part"] in parts:
                return "FAIL"
            parts.add(row["part"])
        cursor = page["next"]
        if cursor is None:
            return "PASS" if len(visited) == len(pages) and parts == set(expected_parts) else "FAIL"
    return "FAIL"


def replacement_quality(parts, requested, selected, approvals, rating_bounds):
    """Follow the declared supersession chain, with approval for every substitution."""
    if (not isinstance(parts, dict) or not isinstance(approvals, (list, tuple, set))
            or not isinstance(rating_bounds, dict) or not rating_bounds
            or not all(isinstance(p, str) and p for p in (requested, selected))):
        return "INCOMPLETE"
    approved = set()
    for pair in approvals:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not all(isinstance(p, str) for p in pair):
            return "INCOMPLETE"
        approved.add(tuple(pair))
    current, seen = requested, set()
    while current not in seen:
        seen.add(current)
        part = parts.get(current)
        if not isinstance(part, dict) or part.get("status") is None:
            return "INCOMPLETE"
        if part["status"] == "active":
            break
        if part["status"] not in ("withdrawn", "superseded"):
            return "FAIL"
        successor = part.get("replacement")
        if not isinstance(successor, str) or not successor:
            return "FAIL" if current == selected else "INCOMPLETE"
        if (current, successor) not in approved:
            return "FAIL"
        current = successor
    else:
        return "FAIL"
    if current != selected:
        return "FAIL"
    ratings = part.get("ratings")
    if not isinstance(ratings, dict):
        return "INCOMPLETE"
    from benches.x03_quality import finite_measure
    for field, bounds in rating_bounds.items():
        if ratings.get(field) is None or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            return "INCOMPLETE"
        if not all(map(finite_measure, (*bounds, ratings[field]))) or not bounds[0] <= ratings[field] <= bounds[1]:
            return "FAIL"
    return "PASS"


def effective_catalog_quality(rows, design_time, part, version, unit, result):
    """Design time and the half-open validity intervals use the same integer time scale."""
    if not isinstance(rows, list) or type(design_time) is not int:
        return "INCOMPLETE"
    active = []
    for row in rows:
        if not isinstance(row, dict) or row.get("part") is None:
            return "INCOMPLETE"
        if row["part"] != part:
            continue
        start, end = row.get("valid_from"), row.get("valid_until")
        if not all(type(v) is int for v in (start, end)) or not row.get("version"):
            return "INCOMPLETE"
        if start >= end:
            return "FAIL"
        if start <= design_time < end:
            active.append(row)
    if len(active) != 1 or active[0]["version"] != version:
        return "FAIL"
    if active[0].get("rating") is None:
        return "INCOMPLETE"
    return catalog_quality(active, part, version, unit, result)


def temperature_derating_quality(curve, temperature, required, policy):
    """Compare duty with a piecewise-linear table of absolute ratings at recorded temperatures."""
    from benches.x03_quality import finite_measure
    if policy != "linear-no-extrapolation" or not isinstance(curve, list) or not curve or None in (temperature, required):
        return "INCOMPLETE"
    if not all(map(finite_measure, (temperature, required))) or required < 0:
        return "FAIL"
    points = []
    for row in curve:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or None in row:
            return "INCOMPLETE"
        if not all(map(finite_measure, row)) or row[1] < 0:
            return "FAIL"
        points.append(tuple(Fraction(str(v)) for v in row))
    points.sort()
    if len({t for t, rating in points}) != len(points):
        return "FAIL"
    t, duty = (Fraction(str(v)) for v in (temperature, required))
    for knot, rating in points:
        if t == knot:
            return "PASS" if duty <= rating else "FAIL"
    for (low, first), (high, last) in zip(points, points[1:]):
        if low < t < high:
            limit = first + (last-first) * (t-low) / (high-low)
            return "PASS" if duty <= limit else "FAIL"
    return "FAIL"
