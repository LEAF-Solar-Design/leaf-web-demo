"""Read-only monthly statements; no payment or subscription side effects."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext

from .tenant_cost_store import MAX_LIST_LIMIT, list_cost_events

SCHEMA = "leaf.billing-statement.v1"
MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0")
PERCENT_DIVISOR = Decimal("100")
MIN_PRECISION = 64
FIRST_DAY = 1
FIRST_MONTH = 1
LAST_MONTH = 12
EVENT_LIMIT_MESSAGE = "statement window exceeds the event limit"
PERIOD_MESSAGE = "period must be a calendar month in YYYY-MM format"


def month_window(period: str):
    """Validate the period and return its half-open UTC calendar window."""
    if not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise ValueError(PERIOD_MESSAGE)
    year, month = map(int, period.split("-"))
    try:
        start = datetime(year, month, FIRST_DAY, tzinfo=timezone.utc)
        end = datetime(year + FIRST_DAY, FIRST_MONTH, FIRST_DAY, tzinfo=timezone.utc) \
            if month == LAST_MONTH else datetime(year, month + FIRST_MONTH, FIRST_DAY, tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(PERIOD_MESSAGE) from exc
    return start, end


def _money(value):
    return format(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), ".2f")


def compute_statement(events, org_id, period, pricebook) -> dict:
    """Aggregate already-scoped ledger rows, rounding once per line."""
    month_window(period)
    events = list(events)
    pct = Decimal(pricebook["management_fee_pct"])
    numbers = [pct] + [Decimal(str(event[field])) for event in events
                       for field in ("quantity", "cost_usd")]
    # Preserve exact sums even for quantities exceeding the default context.
    precision = max(MIN_PRECISION, sum(len(n.as_tuple().digits) + abs(n.as_tuple().exponent)
                                       for n in numbers))
    with localcontext() as context:
        context.prec = precision
        groups = {}
        for event in events:
            key = (event["addon"], event["mode"])
            group = groups.setdefault(key, {"events": 0, "quantity": {}, "cost": ZERO})
            group["events"] += 1
            unit = event["unit"]
            group["quantity"][unit] = group["quantity"].get(unit, ZERO) + Decimal(str(event["quantity"]))
            group["cost"] += Decimal(str(event["cost_usd"]))
        lines = []
        totals = {field: ZERO for field in ("cost_usd", "fee_usd", "total_usd")}
        for (addon, mode), group in sorted(groups.items()):
            cost = group["cost"] if mode != "byo" else ZERO
            fee = cost * pct / PERCENT_DIVISOR if pricebook["addons"][addon]["billable"] else ZERO
            cost, fee = Decimal(_money(cost)), Decimal(_money(fee))
            line = {"addon": addon, "mode": mode, "events": group["events"],
                    "quantity_by_unit": {unit: format(quantity.normalize(), "f")
                                         for unit, quantity in sorted(group["quantity"].items())},
                    "cost_usd": _money(cost), "fee_usd": _money(fee), "total_usd": _money(cost + fee)}
            lines.append(line)
            for field in totals:
                totals[field] += Decimal(line[field])
        statement = {"schema": SCHEMA, "org_id": str(org_id), "period": period,
                     "pricebook_version": pricebook["version"], "pricebook_digest": pricebook["digest"],
                     "placeholder": pricebook["placeholder"], "lines": lines,
                     "totals": {field: _money(value) for field, value in totals.items()}}
    canonical = json.dumps(statement, sort_keys=True, separators=(",", ":"), allow_nan=False)
    statement["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return statement


def build_statement(conn, org_id, period: str, pricebook: dict) -> dict:
    """Read a bounded month and refuse any potentially truncated result."""
    start, end = month_window(period)
    events = list_cost_events(conn, org_id, start, end, limit=MAX_LIST_LIMIT)
    if len(events) >= MAX_LIST_LIMIT:
        raise ValueError(EVENT_LIMIT_MESSAGE)
    return compute_statement(events, org_id, period, pricebook)
