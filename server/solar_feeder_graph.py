"""Pure regeneration of L1-to-L2 feeders through the route-aware electrical bridge."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

import solar_electrical_route_bridge as rb
from solar_design_graph import validate_graph

cab = rb.cab
st = rb.st
CODES = ("FEEDER_L2_MODE_REQUIRED", "FEEDER_COMBINERS_REQUIRED", "FEEDER_COLLECTORS_REQUIRED",
         "FEEDER_CAPACITY_AMBIGUOUS", "FEEDER_OUTLINES_INVALID", "FEEDER_NOT_PORTED",
         "FEEDER_KERNEL_REFUSED", "FEEDER_POSTCONDITION_FAILED")
RECEIPT_FORMAT = "leaf.solar-feeder-routing.v1"
MAX_LINES = 64
MAX_LINE_CHARS = 512
_CREATED = re.compile(r"^(\d+) new combiner box assignment\(s\) saved\.$")


class FeederGraphError(ValueError):
    """A closed, payload-free refusal carrying its machine-readable code."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise FeederGraphError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _default_created_at(value):
    """Normalize the graph's RFC 3339 timestamp to the bridge's UTC format."""
    try:
        stamp = datetime.fromisoformat(value.upper())
        if stamp.tzinfo is None:
            _fail("FEEDER_POSTCONDITION_FAILED")
        stamp = stamp.astimezone(timezone.utc)
        fraction = f".{stamp.microsecond:06d}" if stamp.microsecond else ""
        return stamp.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"
    except (AttributeError, TypeError, ValueError, OverflowError):
        _fail("FEEDER_POSTCONDITION_FAILED")


def _feeders(graph):
    return [route for route in graph["routes"] if route["route_kind"] == "feeder"]


def _untouched(graph):
    return [route for route in graph["routes"] if route["route_kind"] != "feeder"]


def _prove(before, result, after):
    """Pin topology, kernel lengths, and every route outside the regenerated feeders."""
    try:
        l1 = {i["id"]: i for i in result["inverters"] if not i["is_l2"]}
        l2 = {i["id"] for i in result["inverters"] if i["is_l2"]}
        ok = set(l1) == {i["id"] for i in before["inverters"] if not i["is_l2"]}
        ok = ok and l2 == {i["id"] for i in before["inverters"] if i["is_l2"]}
        feeders = _feeders(result)
        counts = Counter(r["from_ref"] for r in feeders)
        ok = ok and set(counts) == set(l1) and all(n == 1 for n in counts.values())
        rows = [r for r in after["rows"]["cable"] if r.get("cable_kind") == "feeder"]
        by_number = {cab._feeder_ends(r)[0]: r for r in rows}
        ok = ok and len(rows) == len(by_number) == len(l1)
        old = {r["id"]: r for r in _feeders(before)}
        for route in feeders:
            source = l1[route["from_ref"]]
            row = by_number[source["number"]]
            ok = ok and route["to_ref"] == source["l2_ref"] and source["l2_ref"] in l2
            ok = ok and len(route["points"]) >= 2 and route["length_ft"] == row["length"]["value"]
            prior = old.get(route["id"])
            if prior and prior.get("pathway_ref") is not None and prior["points"] != route["points"]:
                ok = ok and route.get("pathway_ref") is None
        ok = ok and _canonical(_untouched(before)) == _canonical(_untouched(result))
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        _fail("FEEDER_POSTCONDITION_FAILED")
    if not ok:
        _fail("FEEDER_POSTCONDITION_FAILED")


def route_feeders(graph, panel_groups, *, new_id=None, created_at=None):
    """Strip adopted feeders, regenerate all of them, prove and return (graph, receipt)."""
    g = validate_graph(graph)
    # solar-design-graph.v1.schema.json $defs/settings and $defs/inverter:
    # use_l2_collectors selects the mode; is_l2 distinguishes L1/L2, l2_ref is
    # the L1's assignment, and each L2 carries collector_capacity and l1_assignments.
    if g["settings"]["use_l2_collectors"] is not True:
        _fail("FEEDER_L2_MODE_REQUIRED")
    if not any(not i["is_l2"] for i in g["inverters"]):
        _fail("FEEDER_COMBINERS_REQUIRED")
    l2 = [i for i in g["inverters"] if i["is_l2"]]
    if not l2:
        _fail("FEEDER_COLLECTORS_REQUIRED")
    capacities = {i["collector_capacity"] for i in l2}
    if len(capacities) != 1:
        _fail("FEEDER_CAPACITY_AMBIGUOUS")
    try:
        # The kernel accepts list subclasses, but this boundary accepts plain JSON only.
        if type(panel_groups) is not list:
            _fail("FEEDER_OUTLINES_INVALID")
        cab.validate_outlines(panel_groups)
        groups = deepcopy(panel_groups)
    except FeederGraphError:
        raise
    except Exception:
        # Includes validator overflow, malformed containers and recursive custom values.
        _fail("FEEDER_OUTLINES_INVALID")
    state, binding = rb.state_from_graph(g)
    state = deepcopy(state)
    state["rows"]["cable"] = [r for r in state["rows"]["cable"] if r.get("cable_kind") != "feeder"]
    host = {"UseL2Collectors": True, "L1CollectorsPerL2": next(iter(capacities))}
    try:
        after, lines = cab.route_l2_feeders(state, groups, host)
    except cab.InverterCablingNotPortedError:
        _fail("FEEDER_NOT_PORTED")
    except cab.InverterCablingError:
        _fail("FEEDER_KERNEL_REFUSED")
    # Refuse incomplete kernel output before the bridge can report a topology error.
    expected = {i["number"] for i in g["inverters"] if not i["is_l2"]}
    try:
        sources = Counter(cab._feeder_ends(r)[0] for r in after["rows"]["cable"]
                          if r.get("cable_kind") == "feeder")
        if set(sources) != expected or any(n != 1 for n in sources.values()):
            _fail("FEEDER_POSTCONDITION_FAILED")
    except (KeyError, TypeError, ValueError):
        _fail("FEEDER_POSTCONDITION_FAILED")
    # Missing routes also have deterministic defaults: no UUID randomness or clock.
    serial = 0
    seed = rb.canonical_sha256(g)

    def mint(kind):
        nonlocal serial
        serial += 1
        value = list(hashlib.sha256(
            "\0".join((seed, kind, str(serial))).encode("utf-8")).hexdigest()[:32])
        # Match seed_entity_id's schema-required version and variant layout.
        value[12] = "4"
        value[16] = "89ab"[int(value[16], 16) % 4]
        value = "".join(value)
        return "leaf:" + kind + ":" + "-".join(
            (value[:8], value[8:12], value[12:16], value[16:20], value[20:]))

    try:
        result, _ = rb.graph_from_state(g, after, binding, new_id=mint if new_id is None else new_id,
                                        created_at=_default_created_at(g["project"]["provenance"]["created_at"])
                                        if created_at is None else created_at)
        _prove(g, result, after)
        result = validate_graph(result)
    except FeederGraphError:
        raise
    except ValueError:
        _fail("FEEDER_POSTCONDITION_FAILED")
    old = {r["from_ref"]: r for r in _feeders(g)}
    feeders = _feeders(result)
    redrawn = []
    for route in feeders:
        prior = old.get(route["from_ref"])
        if prior is None or any(prior[k] != route[k] for k in ("points", "from_ref", "to_ref")):
            redrawn.append(route["id"])
    created = sum(int(match.group(1)) for line in lines if (match := _CREATED.fullmatch(str(line))))
    return result, {"format": RECEIPT_FORMAT, "feeders": len(feeders), "redrawn": redrawn,
                    "unchanged": len(feeders) - len(redrawn), "assignments_created": created,
                    "lines": [str(line)[:MAX_LINE_CHARS] for line in lines[:MAX_LINES]]}
