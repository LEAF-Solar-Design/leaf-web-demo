"""Cost Explorer import (TCM-06): Leaf's real AWS cost per service, gross and credits apart.

Transparency of Leaf's real cost only, never billing. This module never writes the
ledger: it returns plain JSON-able cost observations that a later publisher joins
to usage by resource_id. Every amount is a Decimal string, never a float; float
input is refused. Only ce:GetCostAndUsage is called, through an injected client
(a boto3 "ce" client in production, a fake in tests). Pagination is bounded and
fails closed rather than truncating.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from typing import Any, Iterable, List, Mapping, Optional, Tuple, Union

from .ledger import PERIOD_RE, RESOURCE_ID_RE, to_decimal

SOURCE = "aws-cost-explorer"
METRIC = "UnblendedCost"
GROUP_BY = (
    {"Type": "DIMENSION", "Key": "SERVICE"},
    {"Type": "DIMENSION", "Key": "RECORD_TYPE"},
)
# Credit and Refund are reported apart as credits_usd; every other record type
# (Usage, Tax, Fee, RIFee, Support, savings-plan lines...) is part of gross.
CREDIT_RECORD_TYPES = frozenset({"Credit", "Refund"})
MAX_PAGES = 100  # bounds a looping NextPageToken; exceeding it raises, never truncates

# Physical usage (TCM-21): one quantity per resource, in one unit. Only usage types
# whose name carries the marker are summed, so unlike units are never added.
QUANTITY_SOURCE = "aws-usage-quantities"
QUANTITY_METRIC = "UsageQuantity"
QUANTITY_GROUP_BY = (
    {"Type": "DIMENSION", "Key": "SERVICE"},
    {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
)
# SERVICE -> (usage type marker, unit)
PHYSICAL_USAGE_RULES = {
    "AWS CodeBuild": ("Build-Min", "build-minutes"),
    "Amazon Elastic Compute Cloud - Compute": ("BoxUsage", "instance-hours"),
}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def resource_id_for_service(service: Any) -> str:
    """aws: plus the SERVICE name lowercased, non-alphanumeric runs to '-', trimmed. Fails closed."""
    if not isinstance(service, str):
        raise TypeError("service name must be a string")
    slug = _NON_ALNUM.sub("-", service.lower()).strip("-")
    resource_id = f"aws:{slug}"
    if not slug or not RESOURCE_ID_RE.fullmatch(resource_id):
        raise ValueError(f"service name does not map to a resource id: {service[:80]!r}")
    return resource_id


def _check_period(period: Any) -> Tuple[int, int]:
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    return int(period[:4]), int(period[5:])


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _next_month_start(year: int, month: int) -> date:
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def period_time_range(period: str, today: date) -> Tuple[str, str]:
    """Cost Explorer TimePeriod for a calendar month, End exclusive.

    A closed month runs to the first of the next month; the current month runs
    to tomorrow (month to date, today included); a future month is refused.
    """
    year, month = _check_period(period)
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")
    start = _month_start(year, month)
    end = _next_month_start(year, month)
    if start > today:
        raise ValueError(f"period {period} is in the future")
    end = min(end, today + timedelta(days=1))
    return start.isoformat(), end.isoformat()


def fetch(client: Any, period: str, *, today: Optional[date] = None,
          max_pages: int = MAX_PAGES) -> List[dict]:
    """Every get_cost_and_usage page for the period, grouped by SERVICE and RECORD_TYPE.

    Follows NextPageToken; raises RuntimeError past max_pages instead of
    returning a silently truncated month.
    """
    return _fetch_pages(client, period, today, max_pages, METRIC, GROUP_BY, None)


def fetch_quantities(client: Any, period: str, *, today: Optional[date] = None,
                     max_pages: int = MAX_PAGES) -> List[dict]:
    """Every UsageQuantity page for the period, grouped by SERVICE and USAGE_TYPE.

    Filtered to the services in PHYSICAL_USAGE_RULES; paged and bounded like fetch().
    """
    service_filter = {"Dimensions": {"Key": "SERVICE", "Values": sorted(PHYSICAL_USAGE_RULES)}}
    return _fetch_pages(client, period, today, max_pages, QUANTITY_METRIC, QUANTITY_GROUP_BY,
                        service_filter)


def _fetch_pages(client: Any, period: str, today: Optional[date], max_pages: int, metric: str,
                 group_by: Tuple[dict, ...], service_filter: Optional[dict]) -> List[dict]:
    if today is None:
        today = datetime.now(timezone.utc).date()
    start, end = period_time_range(period, today)
    responses: List[dict] = []
    token: Optional[str] = None
    for _ in range(max_pages):
        request = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": [metric],
            "GroupBy": [dict(g) for g in group_by],
        }
        if service_filter is not None:
            request["Filter"] = json.loads(json.dumps(service_filter))
        if token:
            request["NextPageToken"] = token
        response = client.get_cost_and_usage(**request)
        if not isinstance(response, Mapping):
            raise TypeError("get_cost_and_usage returned a non-object response")
        responses.append(dict(response))
        token = response.get("NextPageToken")
        if not token:
            return responses
        if not isinstance(token, str):
            raise TypeError("NextPageToken must be a string")
    raise RuntimeError(f"Cost Explorer pagination exceeded {max_pages} pages for {period}")


def _canonical_payload(responses: List[Any]) -> List[Any]:
    # ResponseMetadata carries a per-call RequestId: it is transport, not cost data.
    return [
        {k: v for k, v in r.items() if k != "ResponseMetadata"} if isinstance(r, Mapping) else r
        for r in responses
    ]


def _refuse_non_json(value: Any) -> Any:
    raise TypeError(f"response carries a non-JSON value: {type(value).__name__}")


def source_batch_id(responses: Iterable[Any]) -> str:
    """sha256 hex of the canonical JSON (sorted keys, no whitespace) of the raw responses."""
    payload = _canonical_payload(list(responses))
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False, default=_refuse_non_json)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fetched_month(fetched_at: Union[datetime, str]) -> Tuple[int, int]:
    if isinstance(fetched_at, str):
        try:
            fetched_at = datetime.fromisoformat(fetched_at.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"fetched_at is not an ISO timestamp: {fetched_at[:40]!r}") from None
    if not isinstance(fetched_at, datetime):
        raise TypeError("fetched_at must be a datetime or ISO string")
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    utc = fetched_at.astimezone(timezone.utc)
    return utc.year, utc.month


def _fmt(amount: Decimal) -> str:
    """Plain decimal string, never scientific notation and never negative zero."""
    if amount == 0:
        amount = abs(amount)
    return format(amount, "f")


def to_cost_observations(period: str, responses: Iterable[Any],
                         fetched_at: Union[datetime, str]) -> List[dict]:
    """One cost observation per SERVICE, sorted by resource_id. Fails closed on malformed input.

    gross_cost_usd sums every non-credit record type; credits_usd is the absolute
    value of the Credit and Refund record types. Every service is kept, however
    small. coverage is partial for the month of fetched_at, complete for a
    closed month unless Cost Explorer marks it Estimated; a period after
    fetched_at is refused.
    """
    year, month = _check_period(period)
    responses = list(responses)
    fetched = _fetched_month(fetched_at)
    if (year, month) > fetched:
        raise ValueError(f"period {period} is after fetched_at")
    coverage = "partial" if (year, month) == fetched else "complete"
    batch = source_batch_id(responses)

    gross: dict = {}
    credits: dict = {}
    with localcontext() as ctx:
        ctx.prec = 60
        ctx.traps[Inexact] = True  # sums are exact or refused, never silently rounded
        for response in responses:
            if not isinstance(response, Mapping):
                raise TypeError("each response must be an object")
            results = response.get("ResultsByTime") or []
            if not isinstance(results, list):
                raise TypeError("ResultsByTime must be a list")
            for result in results:
                if not isinstance(result, Mapping):
                    raise TypeError("each ResultsByTime entry must be an object")
                start = (result.get("TimePeriod") or {}).get("Start")
                if not isinstance(start, str) or start[:7] != period:
                    raise ValueError(f"result period {start!r} is outside {period}")
                if result.get("Estimated", False):
                    coverage = "partial"
                groups = result.get("Groups") or []
                if not isinstance(groups, list):
                    raise TypeError("Groups must be a list")
                for group in groups:
                    if not isinstance(group, Mapping):
                        raise TypeError("each group must be an object")
                    keys = group.get("Keys")
                    if not isinstance(keys, list) or len(keys) != 2:
                        raise ValueError(f"group keys must be [SERVICE, RECORD_TYPE], got {keys!r}")
                    service, record_type = keys
                    if not isinstance(record_type, str) or not record_type:
                        raise ValueError(f"invalid record type: {record_type!r}")
                    resource_id = resource_id_for_service(service)
                    metric = (group.get("Metrics") or {}).get(METRIC)
                    if not isinstance(metric, Mapping):
                        raise ValueError(f"{service} {record_type} has no {METRIC}")
                    if metric.get("Unit") != "USD":
                        raise ValueError(f"{service} {record_type} unit is {metric.get('Unit')!r}, not USD")
                    amount = to_decimal(metric.get("Amount"), f"{service} {record_type} amount")
                    gross.setdefault(resource_id, Decimal(0))
                    credits.setdefault(resource_id, Decimal(0))
                    if record_type in CREDIT_RECORD_TYPES:
                        credits[resource_id] += amount
                    else:
                        gross[resource_id] += amount

    observations = []
    for resource_id in sorted(gross):
        if gross[resource_id] < 0:
            raise ValueError(f"{resource_id} gross cost is negative: {gross[resource_id]}")
        observations.append({
            "kind": "cost",
            "resource_id": resource_id,
            "period": period,
            "gross_cost_usd": _fmt(gross[resource_id]),
            "credits_usd": _fmt(abs(credits[resource_id])),
            "coverage": coverage,
            "source_batch_id": batch,
            "source": SOURCE,
        })
    return observations


def to_physical_usage(responses: Iterable[Any], *, period: Optional[str] = None,
                      fetched_at: Union[datetime, str, None] = None) -> dict:
    """{resource_id: {quantity, unit, coverage}} from fetch_quantities pages. Fails closed.

    CodeBuild sums the usage types containing "Build-Min" (build-minutes), EC2 compute
    sums those containing "BoxUsage" (instance-hours); every other usage type is ignored,
    so unlike units are never added. A resource appears only when a matching usage type
    was seen. coverage is partial when Cost Explorer marks a result Estimated, or when
    period is the month of fetched_at; otherwise complete.
    """
    partial = False
    if period is not None:
        year, month = _check_period(period)
        if fetched_at is not None:
            fetched = _fetched_month(fetched_at)
            if (year, month) > fetched:
                raise ValueError(f"period {period} is after fetched_at")
            partial = (year, month) == fetched
    totals: dict = {}
    ce_units: dict = {}
    with localcontext() as ctx:
        ctx.prec = 60
        ctx.traps[Inexact] = True  # sums are exact or refused, never silently rounded
        for response in responses:
            if not isinstance(response, Mapping):
                raise TypeError("each response must be an object")
            results = response.get("ResultsByTime") or []
            if not isinstance(results, list):
                raise TypeError("ResultsByTime must be a list")
            for result in results:
                if not isinstance(result, Mapping):
                    raise TypeError("each ResultsByTime entry must be an object")
                if period is not None:
                    start = (result.get("TimePeriod") or {}).get("Start")
                    if not isinstance(start, str) or start[:7] != period:
                        raise ValueError(f"result period {start!r} is outside {period}")
                if result.get("Estimated", False):
                    partial = True
                groups = result.get("Groups") or []
                if not isinstance(groups, list):
                    raise TypeError("Groups must be a list")
                for group in groups:
                    if not isinstance(group, Mapping):
                        raise TypeError("each group must be an object")
                    keys = group.get("Keys")
                    if not isinstance(keys, list) or len(keys) != 2:
                        raise ValueError(f"group keys must be [SERVICE, USAGE_TYPE], got {keys!r}")
                    service, usage_type = keys
                    if not isinstance(usage_type, str):
                        raise ValueError(f"invalid usage type: {usage_type!r}")
                    rule = PHYSICAL_USAGE_RULES.get(service) if isinstance(service, str) else None
                    if rule is None or rule[0] not in usage_type:
                        continue
                    resource_id = resource_id_for_service(service)
                    metric = (group.get("Metrics") or {}).get(QUANTITY_METRIC)
                    if not isinstance(metric, Mapping):
                        raise ValueError(f"{service} {usage_type} has no {QUANTITY_METRIC}")
                    amount = to_decimal(metric.get("Amount"), f"{service} {usage_type} quantity")
                    if amount < 0:
                        raise ValueError(f"{service} {usage_type} quantity is negative: {amount}")
                    ce_unit = metric.get("Unit")
                    seen = ce_units.setdefault(resource_id, ce_unit)
                    if seen != ce_unit:
                        raise ValueError(f"{service} usage units differ: {seen!r} and {ce_unit!r}")
                    totals[resource_id] = totals.get(resource_id, Decimal(0)) + amount
    units = {resource_id_for_service(service): unit for service, (_, unit) in PHYSICAL_USAGE_RULES.items()}
    coverage = "partial" if partial else "complete"
    return {
        resource_id: {"quantity": _fmt(totals[resource_id]), "unit": units[resource_id],
                      "coverage": coverage}
        for resource_id in sorted(totals)
    }
