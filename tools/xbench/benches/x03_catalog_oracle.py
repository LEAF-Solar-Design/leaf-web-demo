"""Grade one catalog result against a recorded, versioned catalog."""
from fractions import Fraction
from math import isfinite

# Exact SI scale factors within each supported dimension.
UNITS = {
    'W': ('power', 1), 'kW': ('power', 1000),
    'V': ('voltage', 1), 'kV': ('voltage', 1000),
    'A': ('current', 1), 'mA': ('current', Fraction(1, 1000)),
}


def pack_order_quality(catalog, revision, part, demand, ordered_units):
    """Buy the smallest whole-pack quantity that meets demand and the unit minimum."""
    if not isinstance(catalog, dict) or not revision or not catalog.get("revision"):
        return "INCOMPLETE"
    if catalog["revision"] != revision:
        return "FAIL"
    parts = catalog.get("parts")
    row = parts.get(part) if isinstance(parts, dict) else None
    if not isinstance(row, dict):
        return "INCOMPLETE"
    pack, minimum, stock = (row.get(k) for k in ("pack_units", "minimum_units", "stock_units"))
    if None in (pack, minimum, stock, demand, ordered_units):
        return "INCOMPLETE"
    if any(type(x) is not int for x in (pack, minimum, stock, demand, ordered_units)):
        return "FAIL"
    if pack <= 0 or demand <= 0 or min(minimum, stock) < 0:
        return "FAIL"
    needed = max(minimum, demand)
    units = ((needed + pack - 1) // pack) * pack
    return "PASS" if units == ordered_units and units <= stock else "FAIL"


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


def stock_allocation_quality(version, inventory, demand, allocations):
    """Allocate quantities from pinned lots to distinct order lines without double use."""
    from collections import Counter
    if not isinstance(version, str) or not version or not isinstance(inventory, dict):
        return "INCOMPLETE"
    if inventory.get("version") is None:
        return "INCOMPLETE"
    if inventory["version"] != version:
        return "FAIL"
    lots = inventory.get("lots")
    if not isinstance(lots, dict) or not isinstance(demand, dict) or not demand or not isinstance(allocations, list):
        return "INCOMPLETE"
    for records, field in ((lots, "available"), (demand, "required")):
        for row in records.values():
            if not isinstance(row, dict) or not isinstance(row.get("part"), str) or row.get(field) is None:
                return "INCOMPLETE"
            if type(row[field]) is not int or row[field] < 0:
                return "FAIL"
    used, supplied, seen = Counter(), Counter(), set()
    for row in allocations:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ("id", "lot", "line")):
            return "INCOMPLETE"
        qty, lot, line = row.get("quantity"), row["lot"], row["line"]
        if qty is None:
            return "INCOMPLETE"
        if type(qty) is not int or qty <= 0 or lot not in lots or line not in demand or row["id"] in seen:
            return "FAIL"
        seen.add(row["id"])
        if lots[lot]["part"] != demand[line]["part"]:
            return "FAIL"
        used[lot] += qty
        supplied[line] += qty
        if used[lot] > lots[lot]["available"]:
            return "FAIL"
    return "PASS" if all(supplied[k] == row["required"] for k, row in demand.items()) else "FAIL"


def assembly_interface_quality(version, catalog, connections):
    """Each pinned port declares its connector, approved mates and supported protocols."""
    if not isinstance(catalog, dict) or not isinstance(connections, list) or not connections or not version:
        return "INCOMPLETE"
    if catalog.get("version") is None or not isinstance(catalog.get("parts"), dict):
        return "INCOMPLETE"
    if catalog["version"] != version:
        return "FAIL"
    for link in connections:
        if not isinstance(link, dict) or not isinstance(link.get("protocol"), str):
            return "INCOMPLETE"
        ports = []
        for side in ("from", "to"):
            endpoint = link.get(side)
            if not isinstance(endpoint, (list, tuple)) or len(endpoint) != 2 or not all(isinstance(k, str) for k in endpoint):
                return "INCOMPLETE"
            part = catalog["parts"].get(endpoint[0])
            port = part.get(endpoint[1]) if isinstance(part, dict) else None
            if not isinstance(port, dict) or not isinstance(port.get("connector"), str):
                return "INCOMPLETE"
            if not all(isinstance(port.get(k), list) and all(isinstance(v, str) for v in port[k]) for k in ("mates", "protocols")):
                return "INCOMPLETE"
            ports.append(port)
        a, b = ports
        if a["connector"] not in b["mates"] or b["connector"] not in a["mates"]:
            return "FAIL"
        if any(link["protocol"] not in port["protocols"] for port in ports):
            return "FAIL"
    return "PASS"


def reserved_stock_quality(version, inventory, demand, allocations, reservations, as_of, policy):
    """Subtract prior holds from gross lot stock during [start, expiry), then grade new allocations."""
    if policy != "start-inclusive-expiry-exclusive" or type(as_of) is not int or not isinstance(reservations, list):
        return "INCOMPLETE"
    if not isinstance(inventory, dict) or not isinstance(inventory.get("lots"), dict):
        return "INCOMPLETE"
    lots = {}
    for key, record in inventory["lots"].items():
        if not isinstance(record, dict) or record.get("available") is None:
            return "INCOMPLETE"
        if type(record["available"]) is not int or record["available"] < 0:
            return "FAIL"
        lots[key] = dict(record)
    seen = set()
    for reservation in reservations:
        fields = ("id", "lot", "start", "expiry", "quantity")
        if not isinstance(reservation, dict) or any(reservation.get(k) is None for k in fields):
            return "INCOMPLETE"
        identifier, lot, start, expiry, quantity = (reservation[k] for k in fields)
        if not all(isinstance(k, str) and k for k in (identifier, lot)) or not all(type(n) is int for n in (start, expiry, quantity)):
            return "FAIL"
        if identifier in seen or lot not in lots or expiry <= start or quantity <= 0:
            return "FAIL"
        seen.add(identifier)
        if start <= as_of < expiry:
            lots[lot]["available"] -= quantity
    return stock_allocation_quality(version, dict(inventory, lots=lots), demand, allocations)


def lot_recall_quality(selected, bulletin, revision):
    """Serial intervals in the pinned bulletin are inclusive and specific to a part and lot."""
    if not isinstance(selected, list) or not selected or not isinstance(bulletin, dict) or not revision:
        return "INCOMPLETE"
    if bulletin.get("revision") is None or not isinstance(bulletin.get("ranges"), list):
        return "INCOMPLETE"
    if bulletin["revision"] != revision:
        return "FAIL"
    recalls = {}
    for row in bulletin["ranges"]:
        if not isinstance(row, dict) or not all(isinstance(row.get(k),str) and row[k] for k in ("part","lot")):
            return "INCOMPLETE"
        lo, hi = row.get("first"), row.get("last")
        if lo is None or hi is None:
            return "INCOMPLETE"
        if type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi:
            return "FAIL"
        recalls.setdefault((row["part"],row["lot"]), []).append((lo,hi))
    for unit in selected:
        if not isinstance(unit,dict) or not all(isinstance(unit.get(k),str) and unit[k] for k in ("part","lot")):
            return "INCOMPLETE"
        serial = unit.get("serial")
        if serial is None:
            return "INCOMPLETE"
        if type(serial) is not int or serial < 0:
            return "FAIL"
        if any(lo <= serial <= hi for lo,hi in recalls.get((unit["part"],unit["lot"]), [])):
            return "FAIL"
    return "PASS"


def procurement_deadline_quality(stock, units, lead_days, order_date, deadline, delivery_days):
    """Lead time counts listed delivery days strictly after order. Zero permits same-day delivery."""
    from datetime import date
    if None in (stock, units, lead_days) or type(order_date) is not date or type(deadline) is not date:
        return "INCOMPLETE"
    if not isinstance(delivery_days, (tuple, list)) or any(type(day) is not date for day in delivery_days):
        return "INCOMPLETE"
    if any(type(n) is not int for n in (stock, units, lead_days)) or min(stock, lead_days) < 0 or units <= 0:
        return "FAIL"
    if stock < units:
        return "FAIL"
    slots = {day for day in delivery_days if order_date <= day <= deadline}
    if lead_days == 0:
        return "PASS" if slots else "FAIL"
    return "PASS" if len(slots - {order_date}) >= lead_days else "FAIL"


def certification_scope_quality(certificate, revision, model, conditions):
    """A single scope entry must cover the model and all installation conditions."""
    if not isinstance(certificate, dict) or not revision or not certificate.get("revision") or not model:
        return "INCOMPLETE"
    if certificate["revision"] != revision:
        return "FAIL"
    scopes = certificate.get("scopes")
    if not isinstance(scopes, list) or not isinstance(conditions, dict) or not conditions or None in conditions.values():
        return "INCOMPLETE"
    unknown = False
    for scope in scopes:
        if not isinstance(scope, dict) or not scope.get("model"):
            unknown = True
            continue
        if scope["model"] != model:
            continue
        allowed = scope.get("conditions")
        if not isinstance(allowed, dict):
            unknown = True
            continue
        missing = [key for key in conditions if not isinstance(allowed.get(key), list)]
        if any(value not in allowed[key] for key, value in conditions.items() if key not in missing):
            continue
        if not missing:
            return "PASS"
        unknown = True
    return "INCOMPLETE" if unknown or not scopes else "FAIL"


def accessory_quality(catalog, revision, primary, conditions, selected):
    """A matching requirement names one accessory slot and the compatible part IDs."""
    if not isinstance(catalog, dict) or not revision or not catalog.get("revision"):
        return "INCOMPLETE"
    if catalog["revision"] != revision:
        return "FAIL"
    rules = catalog.get("assemblies")
    if not isinstance(rules, dict) or not isinstance(rules.get(primary), list):
        return "INCOMPLETE"
    if not isinstance(conditions, dict) or not isinstance(selected, dict):
        return "INCOMPLETE"
    for rule in rules[primary]:
        if not isinstance(rule, dict) or not isinstance(rule.get("when"), dict):
            return "INCOMPLETE"
        when = rule["when"]
        if any(k not in conditions or conditions[k] is None for k in when):
            return "INCOMPLETE"
        if any(conditions[k] != v for k,v in when.items()):
            continue
        slot, compatible = rule.get("slot"), rule.get("compatible")
        if not isinstance(slot, str) or not isinstance(compatible, list) or not compatible:
            return "INCOMPLETE"
        if slot not in selected or selected[slot] not in compatible:
            return "FAIL"
    return "PASS"


def shelf_life_quality(manufactured, install_on, rule, revision):
    """Dates are civil days. The pinned policy specifies whether expiry day is usable."""
    from datetime import date
    if type(manufactured) is not date or type(install_on) is not date or not isinstance(rule, dict):
        return "INCOMPLETE"
    if not revision or not rule.get("revision"):
        return "INCOMPLETE"
    if rule["revision"] != revision:
        return "FAIL"
    days, inclusive = rule.get("life_days"), rule.get("expiry_day_usable")
    if type(days) is not int or days < 0 or type(inclusive) is not bool:
        return "INCOMPLETE"
    age = (install_on - manufactured).days
    if age < 0:
        return "FAIL"
    return "PASS" if age < days or (inclusive and age == days) else "FAIL"
