"""Pooled AWS lines split by the Environment cost allocation tag (TCM-08).

Transparency of Leaf's real cost only, never billing. The Environment and Project
cost allocation tags were activated 2026-09-28, so Cost Explorer can group a
service's Usage cost by TAG Environment; tagged cost exists only from activation
forward, so earlier months read almost entirely untagged.

For every AWS service line without a direct collector this module returns one
usage observation whose unit is "usd-by-environment":
  staging          -> leaf|development
  production       -> tenants in proportion to caller-supplied activity weights,
                      or leaf|unattributed when there is no activity
  untagged / other -> leaf|unattributed (and coverage partial)
Usages are Decimal strings that sum to total_usage exactly; the rounding
remainder of the proportional split lands on leaf|unattributed. Floats are
refused. It never writes the ledger: a publisher joins usage to cost by
resource_id. Only ce:GetCostAndUsage is called, through an injected client, and
pagination is bounded and fails closed rather than truncating.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import storage
from .aws_import import MAX_PAGES, METRIC, period_time_range, resource_id_for_service
from .direct_usage import _counted_cad_row, _dicts
from .ledger import ESTIMATED, LEAF, PARTICIPANT_RE, PERIOD_RE, RESOURCE_ID_RE, to_decimal

SOURCE = "aws-cost-explorer-environment-tag"
UNIT = "usd-by-environment"
TAG_KEY = "Environment"
_TAG_PREFIX = TAG_KEY + "$"  # Cost Explorer renders a TAG group key as "Environment$<value>"
GROUP_BY = (
    {"Type": "DIMENSION", "Key": "SERVICE"},
    {"Type": "TAG", "Key": TAG_KEY},
)
USAGE_FILTER = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}

STAGING = "staging"
PRODUCTION = "production"
DEVELOPMENT_KEY = f"{LEAF}|development"
UNATTRIBUTED_KEY = f"{LEAF}|unattributed"

# Lines that already have a direct usage collector; the script excludes them by default.
DIRECT_COLLECTOR_RESOURCE_IDS = (storage.RESOURCE_ID,)

# Proportional tenant parts are truncated here; the remainder goes to leaf|unattributed.
SPLIT_QUANTUM = Decimal("0.000000000001")


def _check_period(period: Any) -> str:
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    return period


def _fmt(amount: Decimal) -> str:
    """Plain decimal string: no exponent, no trailing zeros, never negative zero."""
    if amount == 0:
        return "0"
    return format(amount.normalize(), "f")


# --------------------------------------------------------------------------- #
# fetch: one bounded, paginated Cost Explorer query
# --------------------------------------------------------------------------- #
def fetch_environment_split(client: Any, period: str, *, today: Optional[date] = None,
                            max_pages: int = MAX_PAGES) -> List[dict]:
    """Every get_cost_and_usage page for the month (End exclusive): UnblendedCost,
    RECORD_TYPE Usage only, grouped by SERVICE and TAG Environment.

    Follows NextPageToken; raises RuntimeError past max_pages instead of
    returning a silently truncated month.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    start, end = period_time_range(_check_period(period), today)
    responses: List[dict] = []
    token: Optional[str] = None
    for _ in range(max_pages):
        request = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": [METRIC],
            "Filter": {"Dimensions": {"Key": USAGE_FILTER["Dimensions"]["Key"],
                                      "Values": list(USAGE_FILTER["Dimensions"]["Values"])}},
            "GroupBy": [dict(g) for g in GROUP_BY],
        }
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


# --------------------------------------------------------------------------- #
# split: one usage observation per pooled service line
# --------------------------------------------------------------------------- #
def _environment(tag_key: Any) -> str:
    """'Environment$staging' -> 'staging'; 'Environment$' (untagged) -> ''. Fails closed."""
    if not isinstance(tag_key, str) or not tag_key.startswith(_TAG_PREFIX):
        raise ValueError(f"group key is not an {TAG_KEY} tag key: {tag_key!r}")
    return tag_key[len(_TAG_PREFIX):].strip().lower()


def _check_excludes(exclude_resource_ids: Any) -> frozenset:
    if isinstance(exclude_resource_ids, (str, bytes)) or isinstance(exclude_resource_ids, Mapping):
        raise TypeError("exclude_resource_ids must be a collection of resource ids")
    out = set()
    for rid in exclude_resource_ids:
        if not isinstance(rid, str) or not RESOURCE_ID_RE.fullmatch(rid):
            raise ValueError(f"invalid excluded resource id: {rid!r}")
        out.add(rid)
    return frozenset(out)


def _check_activity(tenant_activity: Any) -> Dict[str, Decimal]:
    """tenant_id -> non-negative Decimal weight. Zero weights are dropped; floats refused."""
    if tenant_activity is None:
        return {}
    if not isinstance(tenant_activity, Mapping):
        raise TypeError("tenant_activity must map tenant_id to a Decimal weight")
    out: Dict[str, Decimal] = {}
    for tenant_id, weight in tenant_activity.items():
        if not isinstance(tenant_id, str) or not PARTICIPANT_RE.fullmatch(tenant_id) or tenant_id == LEAF:
            raise ValueError(f"invalid tenant id: {tenant_id!r}")
        w = to_decimal(weight, f"activity for {tenant_id}")
        if w < 0:
            raise ValueError(f"activity for {tenant_id} must be >= 0, got {w}")
        if w > 0:
            out[tenant_id] = w
    return out


def _sum_by_service_and_environment(period: str, responses: Iterable[Any]) -> Dict[str, Dict[str, Decimal]]:
    """resource_id -> environment -> exact summed Usage cost. Fails closed on malformed input."""
    by_line: Dict[str, Dict[str, Decimal]] = {}
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
            groups = result.get("Groups") or []
            if not isinstance(groups, list):
                raise TypeError("Groups must be a list")
            for group in groups:
                if not isinstance(group, Mapping):
                    raise TypeError("each group must be an object")
                keys = group.get("Keys")
                if not isinstance(keys, list) or len(keys) != 2:
                    raise ValueError(f"group keys must be [SERVICE, {TAG_KEY} tag], got {keys!r}")
                service, tag_key = keys
                resource_id = resource_id_for_service(service)
                environment = _environment(tag_key)
                metric = (group.get("Metrics") or {}).get(METRIC)
                if not isinstance(metric, Mapping):
                    raise ValueError(f"{service} {tag_key} has no {METRIC}")
                if metric.get("Unit") != "USD":
                    raise ValueError(f"{service} {tag_key} unit is {metric.get('Unit')!r}, not USD")
                amount = to_decimal(metric.get("Amount"), f"{service} {tag_key} amount")
                if amount < 0:
                    raise ValueError(f"{service} {tag_key} usage cost is negative: {amount}")
                envs = by_line.setdefault(resource_id, {})
                envs[environment] = envs.get(environment, Decimal(0)) + amount
    return by_line


def _split_production(amount: Decimal, activity: Dict[str, Decimal]) -> Dict[str, Decimal]:
    """Tenant parts truncated to SPLIT_QUANTUM, so their sum never exceeds amount."""
    total_weight = sum(activity.values(), Decimal(0))
    parts: Dict[str, Decimal] = {}
    for tenant_id in sorted(activity):
        part = (amount * activity[tenant_id] / total_weight).quantize(SPLIT_QUANTUM, rounding=ROUND_DOWN)
        if part > 0:
            parts[f"{tenant_id}|"] = part
    return parts


def pooled_usage_observations(period: str, responses: Iterable[Any], tenant_activity: Any, *,
                              exclude_resource_ids: Iterable[str] = ()) -> List[dict]:
    """One usd-by-environment usage observation per AWS service line, sorted by
    resource_id, skipping exclude_resource_ids. Status is always ESTIMATED: a tag
    split and an activity proxy are estimates, not metered use. Coverage is
    partial whenever untagged (or unknown-environment) cost exists."""
    period = _check_period(period)
    excluded = _check_excludes(exclude_resource_ids)
    activity = _check_activity(tenant_activity)
    observations: List[dict] = []
    with localcontext() as ctx:
        ctx.prec = 60
        ctx.traps[Inexact] = True  # sums are exact or refused, never silently rounded
        by_line = _sum_by_service_and_environment(period, list(responses))
        for resource_id in sorted(by_line):
            if resource_id in excluded:
                continue
            envs = by_line[resource_id]
            total = sum(envs.values(), Decimal(0))
            staging = envs.get(STAGING, Decimal(0))
            production = envs.get(PRODUCTION, Decimal(0))
            untagged = total - staging - production
            ctx.traps[Inexact] = False  # the proportional division truncates by design
            tenant_parts = _split_production(production, activity) if production > 0 and activity else {}
            ctx.traps[Inexact] = True
            unattributed = untagged + (production - sum(tenant_parts.values(), Decimal(0)))
            usages: Dict[str, Decimal] = dict(tenant_parts)
            if staging > 0:
                usages[DEVELOPMENT_KEY] = staging
            if unattributed > 0:
                usages[UNATTRIBUTED_KEY] = unattributed
            if sum(usages.values(), Decimal(0)) != total:  # fail closed, never publish a leaky split
                raise ValueError(f"{resource_id} usages do not sum to total {total}")
            observations.append({
                "kind": "usage",
                "resource_id": resource_id,
                "period": period,
                "unit": UNIT,
                "total_usage": _fmt(total),
                "usages": {k: _fmt(v) for k, v in sorted(usages.items())},
                "status": ESTIMATED,
                "coverage": "partial" if untagged > 0 else "complete",
                "source": SOURCE,
            })
    return observations


# --------------------------------------------------------------------------- #
# activity: active days per tenant from the agent and broker ledgers
# --------------------------------------------------------------------------- #
def _row_date(ts: Any) -> Optional[date]:
    """The UTC calendar date a row falls on: ISO string (agent ledger), epoch
    seconds (broker ledger) or datetime (naive read as UTC). None when unplaceable."""
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo is None else ts.astimezone(timezone.utc)
        return dt.date()
    if isinstance(ts, str):
        text = ts.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
        return (parsed if parsed.tzinfo is None else parsed.astimezone(timezone.utc)).date()
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def tenant_activity_from_rows(period: str, agent_rows: Optional[Iterable[Any]],
                              broker_rows: Optional[Iterable[Any]]) -> Dict[str, Decimal]:
    """tenant_id -> active days in period: distinct UTC dates with any agent turn
    or any broker run (denials excluded, the same filter the direct-use collector
    uses). One linear pass per source; None or malformed rows contribute nothing."""
    period = _check_period(period)
    days: Dict[str, set] = {}

    def _mark(tenant: Any, ts: Any) -> None:
        if not isinstance(tenant, str) or not PARTICIPANT_RE.fullmatch(tenant) or tenant == LEAF:
            return
        day = _row_date(ts)
        if day is None or day.strftime("%Y-%m") != period:
            return
        days.setdefault(tenant, set()).add(day)

    for row in _dicts(agent_rows or ()):
        if row is not None and row.get("kind") == "turn":
            _mark(row.get("tenant_id"), row.get("ts"))
    for row in _dicts(broker_rows or ()):
        if row is not None and _counted_cad_row(row, period):
            _mark(row.get("tenant_id"), row.get("ts"))
    return {tenant: Decimal(len(d)) for tenant, d in sorted(days.items())}
