"""Grade one catalog result against a recorded, versioned catalog."""
from fractions import Fraction
from math import isfinite

# Exact SI scale factors within each supported dimension.
UNITS = {
    'W': ('power', 1), 'kW': ('power', 1000),
    'V': ('voltage', 1), 'kV': ('voltage', 1000),
    'A': ('current', 1), 'mA': ('current', Fraction(1, 1000)),
}


def ingress_quality(exposure, dust_code, water_code, catalog, revision):
    """Use the pinned exposure's explicitly qualified dust and water codes."""
    if not isinstance(catalog, dict) or not revision or not catalog.get("revision") or not isinstance(exposure,str):
        return "INCOMPLETE"
    if revision != catalog["revision"]:
        return "FAIL"
    rules = catalog.get("exposures")
    rule = rules.get(exposure) if isinstance(rules,dict) else None
    if not isinstance(rule,dict) or dust_code is None or water_code is None:
        return "INCOMPLETE"
    for axis, code in (("dust",dust_code), ("water",water_code)):
        allowed = rule.get(axis)
        if not isinstance(allowed,list) or not all(type(x) is int for x in allowed) or type(code) is not int:
            return "INCOMPLETE"
        if code not in allowed:
            return "FAIL"
    return "PASS"


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


def galvanic_quality(first, second, exposure, isolation, catalog, revision):
    """Look up an unordered material pair in the declared exposure and isolation policy."""
    if not all(isinstance(v,str) and v for v in (first,second,exposure,isolation,revision)):
        return "INCOMPLETE"
    if not isinstance(catalog,dict) or not catalog.get("revision") or not isinstance(catalog.get("rules"),list):
        return "INCOMPLETE"
    if catalog["revision"] != revision:
        return "FAIL"
    matches = []
    for row in catalog["rules"]:
        if not isinstance(row,dict) or not isinstance(row.get("materials"),(list,tuple)):
            return "INCOMPLETE"
        pair = row["materials"]
        if len(pair) != 2 or not all(isinstance(v,str) and v for v in pair):
            return "INCOMPLETE"
        if sorted(pair) == sorted((first,second)) and row.get("exposure") == exposure:
            matches.append(row.get("allowed_isolation"))
    if len(matches) != 1 or not isinstance(matches[0],list):
        return "INCOMPLETE"
    return "PASS" if isolation in matches[0] else "FAIL"


def accessory_compatibility_quality(revision, assembly, selected):
    """Require an explicit compatibility decision for each pair in the selected assembly."""
    from itertools import combinations
    if not revision or not isinstance(assembly, dict) or assembly.get("revision") != revision:
        return "INCOMPLETE"
    eligible = assembly.get("eligible")
    pairs = assembly.get("compatible")
    if not isinstance(eligible, set) or not isinstance(selected, set) or not selected or not isinstance(pairs, dict):
        return "INCOMPLETE"
    if not selected <= eligible:
        return "FAIL"
    decisions = [pairs.get(frozenset(pair)) for pair in combinations(selected, 2)]
    if any(value is False for value in decisions):
        return "FAIL"
    return "PASS" if all(value is True for value in decisions) else "INCOMPLETE"


def mating_clearance_quality(revision, drawing, required_mm):
    """One-dimensional fit with independent bilateral manufacturing tolerances."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(drawing, dict) or revision != drawing.get("revision"):
        return "INCOMPLETE"
    fields = ("opening_mm", "opening_minus_mm", "insert_mm", "insert_plus_mm")
    values = [drawing.get(key) for key in fields] + [required_mm]
    if not all(finite_measure(v) and v >= 0 for v in values):
        return "INCOMPLETE"
    opening, minus, insert, plus, minimum = [Fraction(str(v)) for v in values]
    if opening <= minus or insert <= 0:
        return "INCOMPLETE"
    return "PASS" if opening - minus - insert - plus >= minimum else "FAIL"


def chemical_exposure_quality(revision, catalog, material, agent, concentration_pct, temperature_c):
    """Use the pinned material/agent rating at the recorded concentration and temperature."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(catalog, dict) or catalog.get("revision") != revision:
        return "INCOMPLETE"
    if not all(isinstance(v, str) and v for v in (material, agent)):
        return "INCOMPLETE"
    if not all(map(finite_measure, (concentration_pct, temperature_c))) or not 0 <= concentration_pct <= 100:
        return "INCOMPLETE"
    rules = catalog.get("exposures")
    rows = rules.get((material, agent)) if isinstance(rules, dict) else None
    if not isinstance(rows, list):
        return "INCOMPLETE"
    decisions = []
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 5:
            return "INCOMPLETE"
        lo, hi, cold, hot, allowed = row
        if not all(map(finite_measure, row[:4])) or not 0 <= lo <= hi <= 100 or cold > hot or type(allowed) != bool:
            return "INCOMPLETE"
        if lo <= concentration_pct <= hi and cold <= temperature_c <= hot:
            decisions.append(allowed)
    if not decisions or len(set(decisions)) != 1:
        return "INCOMPLETE"
    return "PASS" if decisions[0] else "FAIL"


def mating_life_quality(revision, catalog, part, opening_cycles, history, planned_cycles):
    """Each consecutive history entry records completed mating cycles since the opening count."""
    if not revision or not isinstance(catalog, dict) or catalog.get("revision") != revision:
        return "INCOMPLETE"
    ratings = catalog.get("cycle_ratings")
    if not isinstance(ratings, dict) or not isinstance(part, str):
        return "INCOMPLETE"
    rating = ratings.get(part)
    if any(type(n) is not int or n < 0 for n in (rating, opening_cycles, planned_cycles)):
        return "INCOMPLETE"
    if not isinstance(history, list):
        return "INCOMPLETE"
    total = opening_cycles + planned_cycles
    for sequence, entry in enumerate(history, start=1):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return "INCOMPLETE"
        index, cycles = entry
        if type(index) is not int or index != sequence or type(cycles) is not int or cycles < 0:
            return "INCOMPLETE"
        total += cycles
    return "PASS" if total <= rating else "FAIL"


def vibration_qualification_quality(revision, sheet, mount, operating_band, acceleration_g):
    """Check an operating band against recorded continuous qualification bands at one mount."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    mounts = sheet.get("mounts")
    if not isinstance(mounts, dict) or not isinstance(mount, str) or not mount:
        return "INCOMPLETE"
    if mount not in mounts:
        return "FAIL" if sheet.get("complete_mount_list") is True else "INCOMPLETE"
    bands = mounts[mount]
    if not isinstance(bands, list) or not bands or not finite_measure(acceleration_g) or acceleration_g < 0:
        return "INCOMPLETE"
    if not isinstance(operating_band, (tuple, list)) or len(operating_band) != 2 or not all(map(finite_measure, operating_band)):
        return "INCOMPLETE"
    low, high = operating_band
    if not 0 < low < high:
        return "INCOMPLETE"
    cursor = low
    previous = None
    exceeded = False
    for band in bands:
        if not isinstance(band, (tuple, list)) or len(band) != 3 or not all(map(finite_measure, band)):
            return "INCOMPLETE"
        start, end, rating = band
        if not 0 < start < end or rating < 0 or (previous is not None and start < previous):
            return "INCOMPLETE"
        previous = end
        if end <= low or start >= high:
            continue
        if start > cursor:
            return "INCOMPLETE"
        exceeded = exceeded or acceleration_g > rating
        cursor = min(end, high)
    if cursor != high:
        return "INCOMPLETE"
    return "FAIL" if exceeded else "PASS"


def corrosion_allowance_quality(revision, catalog, material, exposures, thickness_mm, required_mm):
    """Accumulate pinned total wall-thickness loss per exposure-year across recorded intervals."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(catalog, dict) or catalog.get("revision") != revision:
        return "INCOMPLETE"
    rates = catalog.get("loss_mm_per_year")
    if not isinstance(rates, dict) or not isinstance(material, str) or not isinstance(exposures, list) or not exposures:
        return "INCOMPLETE"
    if not all(map(finite_measure, (thickness_mm, required_mm))) or min(thickness_mm, required_mm) <= 0:
        return "INCOMPLETE"
    residual = Fraction(str(thickness_mm))
    for interval in exposures:
        if not isinstance(interval, (tuple, list)) or len(interval) != 2:
            return "INCOMPLETE"
        environment, years = interval
        if not isinstance(environment, str) or not finite_measure(years) or years < 0:
            return "INCOMPLETE"
        rate = rates.get((material, environment))
        if not finite_measure(rate) or rate < 0:
            return "INCOMPLETE"
        residual -= Fraction(str(years)) * Fraction(str(rate))
    return "PASS" if residual >= Fraction(str(required_mm)) else "FAIL"


def duty_pressure_quality(revision, curve, duty):
    """Piecewise linear maximum pressure versus temperature. All recorded duty points must qualify."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(curve, dict) or curve.get("revision") != revision:
        return "INCOMPLETE"
    points = curve.get("celsius_max_bar")
    if not isinstance(points, list) or len(points) < 2 or not isinstance(duty, list) or not duty:
        return "INCOMPLETE"
    for row in points + duty:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or not all(map(finite_measure, row)) or row[1] < 0:
            return "INCOMPLETE"
    pairs = [(Fraction(str(t)), Fraction(str(p))) for t,p in points]
    segments = list(zip(pairs,pairs[1:]))
    if any(left[0] >= right[0] for left,right in segments):
        return "INCOMPLETE"
    for temperature, pressure in duty:
        t, p = Fraction(str(temperature)), Fraction(str(pressure))
        for (a, pa), (b, pb) in segments:
            if a <= t <= b:
                if (p-pa)*(b-a) > (pb-pa)*(t-a):
                    return "FAIL"
                break
        else:
            return "INCOMPLETE"
    return "PASS"




def acoustic_quality(sources, revision, model, reported_db, limit_db):
    """Incoherent isotropic point sources in free field, distances in metres."""
    from math import log10, pi, isclose
    from benches.x03_quality import finite_measure
    if not revision or model != "free-field-incoherent-v1" or not isinstance(sources, list) or not sources:
        return "INCOMPLETE"
    if not all(map(finite_measure, (reported_db, limit_db))):
        return "INCOMPLETE"
    levels = []
    for row in sources:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            return "INCOMPLETE"
        record_revision, power_db, distance_m = row
        if record_revision != revision or not all(map(finite_measure, (power_db, distance_m))) or distance_m <= 0:
            return "INCOMPLETE"
        levels.append(power_db - 10 * log10(4*pi) - 20 * log10(distance_m))
    peak = max(levels)
    total = peak + 10 * log10(sum(10 ** ((level-peak)/10) for level in levels))
    return "PASS" if total <= limit_db and isclose(total, reported_db, rel_tol=0, abs_tol=0.005) else "FAIL"


def transport_shock_quality(revision, sheet, package, shape, duration_ms, peak_g):
    """Use recorded qualified duration bands for the actual package and pulse shape."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    if not all(isinstance(v, str) and v for v in (package, shape)):
        return "INCOMPLETE"
    if not all(map(finite_measure, (duration_ms, peak_g))) or duration_ms <= 0 or peak_g < 0:
        return "INCOMPLETE"
    ratings = sheet.get("ratings")
    if not isinstance(ratings, dict):
        return "INCOMPLETE"
    key = (package, shape)
    if key not in ratings:
        return "FAIL" if sheet.get("exhaustive") is True else "INCOMPLETE"
    bands = ratings[key]
    if not isinstance(bands, list) or not bands:
        return "INCOMPLETE"
    applicable = set()
    for band in bands:
        if not isinstance(band, (list, tuple)) or len(band) != 3 or not all(map(finite_measure, band)):
            return "INCOMPLETE"
        low, high, maximum = band
        if not 0 < low <= high or maximum < 0:
            return "INCOMPLETE"
        if low <= duration_ms <= high:
            applicable.add(maximum)
    if not applicable:
        return "FAIL" if sheet.get("exhaustive") is True else "INCOMPLETE"
    if len(applicable) != 1:
        return "INCOMPLETE"
    return "PASS" if peak_g <= applicable.pop() else "FAIL"


def floor_life_quality(revision, sheet, window_h, opening_h, history):
    """Remaining floor-life at use. Sealed storage pauses exposure, approved completed bakes reset it."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    limit, recipes = sheet.get("floor_hours"), sheet.get("bake_min_hours")
    if not finite_measure(limit) or limit <= 0 or not isinstance(recipes, dict):
        return "INCOMPLETE"
    if not finite_measure(opening_h) or opening_h < 0 or not isinstance(history, list):
        return "INCOMPLETE"
    if not isinstance(window_h, (list, tuple)) or len(window_h) != 2 or not all(map(finite_measure, window_h)):
        return "INCOMPLETE"
    cursor, stop = window_h
    if cursor >= stop:
        return "INCOMPLETE"
    exposure = Fraction(str(opening_h))
    for entry in history:
        if not isinstance(entry, (list, tuple)) or len(entry) != 4:
            return "INCOMPLETE"
        start, end, state, recipe = entry
        if not all(map(finite_measure, (start,end))) or start != cursor or not start < end <= stop:
            return "INCOMPLETE"
        elapsed = Fraction(str(end)) - Fraction(str(start))
        if state == "exposed":
            exposure += elapsed
        elif state == "bake":
            minimum = recipes.get(recipe) if isinstance(recipe, str) else None
            if not finite_measure(minimum) or minimum <= 0:
                return "INCOMPLETE"
            if elapsed < Fraction(str(minimum)):
                return "FAIL"
            exposure = Fraction(0)
        elif state != "sealed":
            return "INCOMPLETE"
        cursor = end
    if cursor != stop:
        return "INCOMPLETE"
    return "PASS" if exposure <= Fraction(str(limit)) else "FAIL"


def dc_fuse_quality(revision, sheet, duty):
    """Each listed envelope qualifies one joint voltage, current and time-constant duty."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    rows = sheet.get("dc_envelopes_v_a_ms")
    if not isinstance(rows, list) or not rows or not isinstance(duty, (list, tuple)) or len(duty) != 3:
        return "INCOMPLETE"
    if not all(finite_measure(x) and x > 0 for x in duty):
        return "INCOMPLETE"
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 3 or not all(finite_measure(x) and x > 0 for x in row):
            return "INCOMPLETE"
    if any(all(value <= cap for value, cap in zip(duty, row)) for row in rows):
        return "PASS"
    return "FAIL" if sheet.get("exhaustive") is True else "INCOMPLETE"


def crimp_pull_quality(revision, sheet, terminal, conductor, tooling, forces_n):
    """Every recorded pull specimen must meet its qualified combination's minimum force."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    combinations = sheet.get("minimum_pull_n")
    key = (terminal, conductor, tooling)
    if not isinstance(combinations, dict) or not all(isinstance(x, str) and x for x in key):
        return "INCOMPLETE"
    if key not in combinations:
        return "FAIL" if sheet.get("exhaustive") is True else "INCOMPLETE"
    minimum = combinations[key]
    if not finite_measure(minimum) or minimum <= 0 or not isinstance(forces_n, list) or not forces_n:
        return "INCOMPLETE"
    if not all(finite_measure(x) and x >= 0 for x in forces_n):
        return "INCOMPLETE"
    return "PASS" if min(forces_n) >= minimum else "FAIL"


def flammability_quality(revision, sheet, grade, thickness_mm, required_class):
    """Qualification lists acceptable classes explicitly for each grade and tested thickness."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    if not all(isinstance(x, str) and x for x in (grade, required_class)) or not finite_measure(thickness_mm) or thickness_mm <= 0:
        return "INCOMPLETE"
    rows = sheet.get("qualified_classes")
    if not isinstance(rows, dict):
        return "INCOMPLETE"
    key = (grade, thickness_mm)
    if key not in rows:
        return "FAIL" if sheet.get("exhaustive") is True else "INCOMPLETE"
    classes = rows[key]
    if not isinstance(classes, list) or not all(isinstance(c, str) and c for c in classes):
        return "INCOMPLETE"
    return "PASS" if required_class in classes else "FAIL"


def gasket_squeeze_quality(revision, sheet, groove_mm):
    """Check both worst-case axial squeeze fractions of a compressed face seal."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    ranges = (sheet.get("seal_height_mm"), groove_mm, sheet.get("squeeze_fraction"))
    if not all(isinstance(r, (list, tuple)) and len(r) == 2 and all(map(finite_measure, r)) for r in ranges):
        return "INCOMPLETE"
    (short, tall), (shallow, deep), (low, high) = [tuple(Fraction(str(v)) for v in r) for r in ranges]
    if not (0 < short <= tall and 0 < shallow <= deep and 0 <= low <= high <= 1):
        return "INCOMPLETE"
    return "PASS" if low <= 1-deep/short and 1-shallow/tall <= high else "FAIL"


def insulation_spacing_quality(revision, sheet, pollution, altitude_m, creepage_mm, clearance_mm):
    """Use the pinned pollution class and first inclusive rated altitude ceiling."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(sheet, dict) or sheet.get("revision") != revision:
        return "INCOMPLETE"
    if not all(map(finite_measure, (altitude_m, creepage_mm, clearance_mm))) or min(creepage_mm, clearance_mm) < 0:
        return "INCOMPLETE"
    rules = sheet.get("spacing_by_pollution")
    if type(pollution) is not int or not isinstance(rules, dict) or not isinstance(rules.get(pollution), list):
        return "INCOMPLETE"
    selected = previous = None
    for row in rules[pollution]:
        if not isinstance(row, (list, tuple)) or len(row) != 3 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        ceiling, creep, clear = row
        if min(creep,clear) <= 0 or (previous is not None and ceiling <= previous):
            return "INCOMPLETE"
        if selected is None and altitude_m <= ceiling:
            selected = creep, clear
        previous = ceiling
    if selected is None:
        return "INCOMPLETE"
    return "PASS" if creepage_mm >= selected[0] and clearance_mm >= selected[1] else "FAIL"
