"""GET /api/cost: the caller's view of Leaf's monthly resource share ledger (TCM-09a).

Transparency of Leaf's real cost, never billing: nothing here feeds Stripe,
quotas or caps. It reads the newest publication of the month from the share
ledger (LEAF_COST_LEDGER_DIR, read by CostLedgerStore) and answers:

    { period, publication_id, published_at, coverage_summary, missing_sources,
      own_use,                                   # tenant_direct_use, the caller only
      totals: { gross_cost_usd, credits_usd },
      resources: [ { resource_id, display_name, unit, total_usage, gross_cost_usd,
                     credits_usd, status, coverage, your_share, your_implied_cost_usd,
                     leaf_share: { development, ci, fleet, unattributed },
                     other_customers_share } ],
      error, degraded_mode }

TENANT BOUNDARY. The tenant is resolved only by deps.require_tenant, exactly as
GET /api/usage does; no query parameter can name one. Every other tenant's
share is folded into other_customers_share, a single sum: another tenant's id,
usage or individual share is never serialized. Every amount is a decimal
string, never a JSON number. No publication for the month is 200 with
publication_id null and resources [].
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import deps
from cost_meter import publisher
from cost_meter.direct_usage import load_agent_rows, load_broker_rows, tenant_direct_use
from cost_meter.ledger import (
    COVERAGES, ESTIMATED, LEAF, LEAF_DIMENSIONS, MEASURED, PERIOD_RE, SHARE_QUANTUM, implied_cost,
)
from cost_meter.store import CostLedgerStore, LedgerCorrupt
from envelopes import with_envelope_fields

router = APIRouter()

_ZERO_USD = "0.00"


def _dec(value: Decimal) -> str:
    """Plain decimal string: str() would print a quantized zero as 0E-12."""
    return format(value, "f")


def _share(value: Decimal) -> str:
    return _dec(value.quantize(SHARE_QUANTUM))


_ZERO_SHARE = _share(Decimal(0))
_COVERAGE_ORDER = ("unknown", "partial", "complete")
_KNOWN_NAMES = {"aps:engine": "APS engine"}


@lru_cache(maxsize=1)
def _vendor_names() -> Dict[str, str]:
    """display_name per vendor or subscription resource from the declared schedule.
    Read once per process; an unreadable schedule means ids are shown instead."""
    try:
        from cost_meter.vendors import load_vendor_config  # noqa: PLC0415

        return {e.resource_id: e.display_name for e in load_vendor_config().entries}
    except Exception:  # noqa: BLE001 - a display nicety must never fail the read
        return {}


def display_name(resource_id: str) -> str:
    if resource_id in _KNOWN_NAMES:
        return _KNOWN_NAMES[resource_id]
    named = _vendor_names().get(resource_id)
    if named:
        return named
    if resource_id.startswith("aws:"):
        return " ".join(w.capitalize() for w in resource_id[4:].split("-") if w) or resource_id
    return resource_id


def _period_or_400(period: Optional[str]) -> str:
    if period is None:
        return datetime.now(timezone.utc).strftime("%Y-%m")
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")
    return period


def _own_use(period: str, tenant_id: str) -> tuple[Optional[Dict[str, Any]], bool]:
    try:
        return tenant_direct_use(period, tenant_id, agent_rows=load_agent_rows(),
                                 broker_rows=load_broker_rows()), False
    except Exception:  # noqa: BLE001 - own use is additive; its failure degrades, never 500s
        return None, True


def _resource_row(revision: Any, tenant_id: str) -> Dict[str, Any]:
    """One resource as the caller may see it. Another tenant appears only inside
    other_customers_share, as part of a sum."""
    rp = revision.resource_period
    your = None
    leaf = {dim: Decimal(0) for dim in LEAF_DIMENSIONS}
    others = Decimal(0)
    status = MEASURED
    for share in revision.shares:
        if share.status != MEASURED:
            status = ESTIMATED
        if share.participant_id == LEAF:
            leaf[share.dimension] += share.usage_share
        elif share.participant_id == tenant_id and share.dimension == "":
            your = share
        else:
            others += share.usage_share
    return {
        "resource_id": rp.resource_id,
        "display_name": display_name(rp.resource_id),
        "unit": rp.unit,
        "total_usage": None if rp.total_usage is None else _dec(rp.total_usage),
        "gross_cost_usd": _dec(rp.gross_cost_usd),
        "credits_usd": _dec(rp.credits_usd),
        "status": status,
        "coverage": rp.coverage,
        "your_share": _share(your.usage_share) if your is not None else _ZERO_SHARE,
        "your_implied_cost_usd": _dec(implied_cost(your, rp).gross_usd) if your is not None else _ZERO_USD,
        "leaf_share": {dim: _share(leaf[dim]) for dim in sorted(LEAF_DIMENSIONS)},
        "other_customers_share": _share(others),
    }


def _coverage_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {c: 0 for c in sorted(COVERAGES)}
    for row in rows:
        counts[row["coverage"]] += 1
    overall = None
    for coverage in _COVERAGE_ORDER:  # weakest present wins
        if counts[coverage]:
            overall = coverage
            break
    return {**counts, "resources": len(rows), "overall": overall}


@router.get("/api/cost")
def cost(period: Optional[str] = Query(default=None),
         tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    tenant_id = str(tenant)
    period = _period_or_400(period)
    own_use, degraded = _own_use(period, tenant_id)

    store = CostLedgerStore()
    publication_id: Optional[str] = None
    info: Dict[str, Any] = {"published_at": None, "missing_sources": []}
    rows: List[Dict[str, Any]] = []
    gross = Decimal(0)
    credits = Decimal(0)
    try:
        publication_id = publisher.latest_publication_id(store, period)
        if publication_id is not None:
            revisions = store.read_publication(publication_id)
            info = publisher.publication_info(store, publication_id)
            for revision in sorted(revisions, key=lambda r: r.resource_id):
                rows.append(_resource_row(revision, tenant_id))
                gross += revision.resource_period.gross_cost_usd
                credits += revision.resource_period.credits_usd
    except (LedgerCorrupt, KeyError, ValueError, OSError):
        # An unreadable publication is shown as none, flagged degraded, never guessed at.
        publication_id, rows, gross, credits = None, [], Decimal(0), Decimal(0)
        info = {"published_at": None, "missing_sources": []}
        degraded = True

    body = {
        "period": period,
        "publication_id": publication_id,
        "published_at": info.get("published_at"),
        "coverage_summary": _coverage_summary(rows),
        "missing_sources": list(info.get("missing_sources") or []),
        "own_use": own_use,
        "totals": {"gross_cost_usd": _dec(gross), "credits_usd": _dec(credits)},
        "resources": rows,
    }
    return with_envelope_fields(deps.tenant_echo(body, tenant), degraded_mode=degraded)
