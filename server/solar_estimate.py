"""Exact-cent TEST RATES material; no persistence or takeoff computation."""
import copy
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
import re
import uuid

from solar_design_graph import GraphValidationError


MAX_BYTES = 262144
SCHEMA = "leaf.solar-estimate.v1"
RATE_SCHEMA = "leaf.solar-estimate-rate-card.v1"


def _fail(code):
    raise GraphValidationError(code)


def _canonical(value):
    # Check before serialization/copying, including cyclic and overly deep inputs.
    stack = [(value, 0)]
    nodes = 0
    size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 32768 or depth > 24:
            _fail("ESTIMATE_LIMIT_EXCEEDED")
        if type(item) is dict:
            if len(item) > 4096 or any(type(k) is not str for k in item):
                _fail("ESTIMATE_INPUT_INVALID")
            stack.extend((k, depth + 1) for k in item)
            stack.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            if len(item) > 4096:
                _fail("ESTIMATE_LIMIT_EXCEEDED")
            stack.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            try:
                size += len(item.encode("utf-8"))
            except UnicodeError:
                _fail("ESTIMATE_INPUT_INVALID")
        elif type(item) is int:
            if item.bit_length() > 64:
                _fail("ESTIMATE_LIMIT_EXCEEDED")
        elif type(item) is float:
            if not math.isfinite(item):
                _fail("ESTIMATE_INPUT_INVALID")
        elif item is not None and type(item) is not bool:
            _fail("ESTIMATE_INPUT_INVALID")
        if size > MAX_BYTES:
            _fail("ESTIMATE_LIMIT_EXCEEDED")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES:
        _fail("ESTIMATE_LIMIT_EXCEEDED")
    return raw


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hex(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _text(value, limit):
    return type(value) is str and bool(value.strip()) and len(value) <= limit


def _decimal(value):
    if (type(value) is not str or len(value) > 40
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None):
        return None
    number = Decimal(value)
    return number if number <= Decimal("1000000000000") else None


def _binding(binding):
    ids = ("organization_id", "project_id", "drawing_id", "input_version_id")
    if type(binding) is not dict or set(binding) != set(ids) | {"intake_sha256", "graph_sha256"}:
        _fail("ESTIMATE_SOURCE_INVALID")
    for key in ids:
        value = binding[key]
        try:
            valid = type(value) is str and str(uuid.UUID(value)) == value
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            _fail("ESTIMATE_SOURCE_INVALID")
    if not all(_hex(binding[key]) for key in ("intake_sha256", "graph_sha256")):
        _fail("ESTIMATE_SOURCE_INVALID")


def price_takeoff(summary, rate_card, *, source_binding):
    """Price only eligible E1 rows; preserve unavailable quantities as unavailable."""
    for value in (summary, rate_card, source_binding):
        _canonical(value)
    _binding(source_binding)
    if (type(summary) is not dict or summary.get("schema") != "leaf.solar-takeoff.v1"
            or type(summary.get("complete")) is not bool
            or set(summary) - {"schema", "maturity", "complete", "head", "source", "settings", "counts", "lines"}
            or type(summary.get("lines")) is not list):
        _fail("ESTIMATE_TAKEOFF_INVALID")
    source = summary.get("source")
    if (type(source) is not dict
            or set(source) != {"drawing_id", "project_id", "graph_sha256", "drawing_version"}
            or type(source["drawing_version"]) is not int or source["drawing_version"] < 1):
        _fail("ESTIMATE_SOURCE_INVALID")
    if any(source[key] != source_binding[key] for key in ("drawing_id", "project_id", "graph_sha256")):
        _fail("ESTIMATE_SOURCE_MISMATCH")
    lines = summary["lines"]
    if len(lines) > 128:
        _fail("ESTIMATE_LIMIT_EXCEEDED")
    inventory = {}
    required = {"line_id", "category", "description", "unit", "status", "reason", "priceable", "source"}
    for line in lines:
        if (type(line) is not dict or not required <= set(line)
                or set(line) - required - {"quantity"}
                or not _text(line["line_id"], 96) or line["line_id"] in inventory
                or not _text(line["unit"], 120)
                or type(line["category"]) is not str or type(line["description"]) is not str
                or line["status"] not in ("computed", "unavailable", "not-computed")
                or type(line["priceable"]) is not bool or type(line["source"]) is not dict
                or (line["reason"] is not None and type(line["reason"]) is not str)):
            _fail("ESTIMATE_TAKEOFF_INVALID")
        inventory[line["line_id"]] = line
    if (type(rate_card) is not dict
            or set(rate_card) != {"schema", "label", "version", "currency", "rates"}
            or rate_card["schema"] != RATE_SCHEMA or rate_card["label"] != "TEST RATES"
            or rate_card["currency"] != "USD" or not _text(rate_card["version"], 120)
            or type(rate_card["rates"]) is not list):
        _fail("ESTIMATE_RATE_CARD_INVALID")
    if len(rate_card["rates"]) > 128:
        _fail("ESTIMATE_LIMIT_EXCEEDED")
    rates = {}
    for rate in rate_card["rates"]:
        if (type(rate) is not dict or set(rate) not in (
                {"line_id", "unit", "rate"},
                {"line_id", "unit", "rate", "minimum_amount", "minimum_semantics"})
                or not _text(rate["line_id"], 96) or rate["line_id"] not in inventory
                or rate["line_id"] in rates or not _text(rate["unit"], 120)
                or _decimal(rate["rate"]) is None):
            _fail("ESTIMATE_RATE_CARD_INVALID")
        if "minimum_amount" in rate:
            if (_decimal(rate["minimum_amount"]) is None
                    or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2}0*)?", rate["minimum_amount"]) is None
                    or rate["minimum_semantics"] != "per_line_floor_after_rounding"):
                _fail("ESTIMATE_RATE_CARD_INVALID")
        rates[rate["line_id"]] = rate
    takeoff = copy.deepcopy(summary)
    takeoff["lines"].sort(key=lambda row: row["line_id"])
    card = copy.deepcopy(rate_card)
    card["rates"].sort(key=lambda row: row["line_id"])
    output = []
    cents = 0
    priced = 0
    # A fresh context also isolates rounding, traps and exponent limits from callers.
    with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
        for line in takeoff["lines"]:
            rate = rates.get(line["line_id"])
            quantity = _decimal(line.get("quantity"))
            if line["status"] != "computed":
                reason = "NONCOMPUTED_LINE"
            elif not line["priceable"]:
                reason = "NONPRICEABLE_LINE"
            elif quantity is None:
                reason = "QUANTITY_MISSING" if line.get("quantity") is None else "QUANTITY_INVALID"
            elif rate is None:
                reason = "RATE_MISSING"
            elif rate["unit"] != line["unit"]:
                reason = "UNIT_MISMATCH"
            else:
                reason = None
            amount = None
            if reason is None:
                amount = (quantity * _decimal(rate["rate"])).quantize(Decimal("0.01"))
                if "minimum_amount" in rate:
                    amount = max(amount, _decimal(rate["minimum_amount"]))
                cents += int(amount * 100)
                priced += 1
            row = copy.deepcopy(line)
            row.update(rate=rate["rate"] if rate else None,
                       amount=format(amount, ".2f") if amount is not None else None,
                       pricing_reason=reason)
            output.append(row)
        subtotal = format(Decimal(cents) / 100, ".2f") if priced else None
    estimate = {"schema": SCHEMA, "label": "TEST RATES", "currency": "USD",
                "rate_version": card["version"], "complete": summary["complete"] and priced == len(lines),
                "priced_subtotal": subtotal, "unpriced_count": len(lines) - priced,
                "source_binding": copy.deepcopy(source_binding), "takeoff": takeoff,
                "rate_card": card, "takeoff_sha256": _hash(takeoff),
                "rate_card_sha256": _hash(card), "lines": output}
    estimate["content_sha256"] = _hash(estimate)
    _canonical(estimate)
    return estimate


def _validate(estimate):
    _canonical(estimate)
    if type(estimate) is not dict or estimate.get("schema") != SCHEMA:
        _fail("ESTIMATE_SCHEMA_INVALID")
    for key in ("content_sha256", "takeoff_sha256", "rate_card_sha256"):
        if not _hex(estimate.get(key)):
            _fail("ESTIMATE_DIGEST_INVALID")
    payload = {key: value for key, value in estimate.items() if key != "content_sha256"}
    if _hash(payload) != estimate["content_sha256"]:
        _fail("ESTIMATE_DIGEST_MISMATCH")
    expected = price_takeoff(estimate.get("takeoff"), estimate.get("rate_card"),
                             source_binding=estimate.get("source_binding"))
    # Canonical bytes distinguish booleans, integers and floats, unlike Python equality.
    if _canonical(estimate) != _canonical(expected):
        _fail("ESTIMATE_MATERIAL_MISMATCH")


def encode_estimate(estimate):
    """Validate material and return its canonical UTF-8 bytes."""
    _validate(estimate)
    return _canonical(estimate)


def decode_estimate(raw):
    """Reject duplicate keys and rederive all pricing before accepting material."""
    if type(raw) not in (bytes, str):
        _fail("ESTIMATE_INPUT_INVALID")
    try:
        if len(raw) > MAX_BYTES:
            _fail("ESTIMATE_LIMIT_EXCEEDED")
        text = raw.decode("utf-8") if type(raw) is bytes else raw
        if len(text.encode("utf-8")) > MAX_BYTES:
            _fail("ESTIMATE_LIMIT_EXCEEDED")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    _fail("ESTIMATE_DUPLICATE_KEY")
                result[key] = value
            return result

        def nonfinite(token):
            _fail("ESTIMATE_INPUT_INVALID")

        estimate = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, GraphValidationError):
            raise
        _fail("ESTIMATE_INPUT_INVALID")
    _validate(estimate)
    return estimate
