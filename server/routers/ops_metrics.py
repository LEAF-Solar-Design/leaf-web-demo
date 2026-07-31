"""APS observability read-API (leaf-platform half of the APS observability plane).

Operator-only, behind the SAME ``X-Ops-Secret`` gate as ``routers/ops.py`` — it
reuses that module's ``_require_ops`` so the credential semantics can never drift.
It serves what the EMF/CloudWatch plane cannot: the live in-flight job tail and a
billing-grade drill-down over the durable Postgres attribution ledger. The
read-model (``ops_metrics_read``) owns the SQL; this router owns auth + shaping.

Requires the Postgres broker authority (``LEAF_BROKER_STORE=postgres``): the
durable ledger table is the billing-grade source. In legacy JSONL mode the surface
returns a clear enveloped "requires postgres" note (HTTP 503) rather than scraping
an ephemeral, per-task file — legacy fleet trends are the EMF plane's job.

    GET /api/ops/metrics?window=&tenant_id=                    -> fleet/tenant rollup
    GET /api/ops/metrics/inflight?limit=&tenant_id=            -> live in-flight job tail
    GET /api/ops/metrics/runs?limit=&tenant_id=&status=&tool=  -> ledger<->job drill-down
    GET /api/ops/metrics/tools?window=&tenant_id=              -> per-tool aggregates
    GET /api/ops/metrics/tenants?window=&limit=                -> per-tenant aggregates
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header

import ops_metrics_read
from envelopes import ErrorCode, error_response, with_envelope_fields
from routers.ops import _broker_store_mode, _require_ops

router = APIRouter()


def _requires_postgres() -> Optional[Any]:
    """None in Postgres mode; else a 503 envelope. The read-model targets the
    durable ledger table — it deliberately does NOT scrape the legacy JSONL."""
    if _broker_store_mode() != "postgres":
        return error_response(
            ErrorCode.INTERNAL,
            "the APS observability read-API requires the Postgres broker authority "
            "(LEAF_BROKER_STORE=postgres); legacy JSONL trends are served by the "
            "EMF/CloudWatch plane",
            retryable=False, status_code=503)
    return None


@router.get("/api/ops/metrics")
def ops_metrics(window: int = 86_400, tenant_id: Optional[str] = None,
                x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    guard = _requires_postgres()
    if guard is not None:
        return guard
    try:
        data = ops_metrics_read.fleet_metrics(window_seconds=window, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        return error_response(ErrorCode.INTERNAL, f"metrics query failed: {exc}",
                              retryable=True)
    return with_envelope_fields(data)


@router.get("/api/ops/metrics/inflight")
def ops_metrics_inflight(limit: int = 100, tenant_id: Optional[str] = None,
                         x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    guard = _requires_postgres()
    if guard is not None:
        return guard
    try:
        jobs = ops_metrics_read.inflight(limit=limit, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        return error_response(ErrorCode.INTERNAL, f"inflight query failed: {exc}",
                              retryable=True)
    return with_envelope_fields({"jobs": jobs, "count": len(jobs)})


@router.get("/api/ops/metrics/tools")
def ops_metrics_tools(window: int = 86_400, tenant_id: Optional[str] = None,
                      x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    guard = _requires_postgres()
    if guard is not None:
        return guard
    try:
        data = ops_metrics_read.tool_metrics(window_seconds=window, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        return error_response(ErrorCode.INTERNAL, f"tool metrics query failed: {exc}",
                              retryable=True)
    return with_envelope_fields(data)


@router.get("/api/ops/metrics/tenants")
def ops_metrics_tenants(window: int = 86_400, limit: int = 100,
                        x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    guard = _requires_postgres()
    if guard is not None:
        return guard
    try:
        data = ops_metrics_read.tenant_metrics(window_seconds=window, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return error_response(ErrorCode.INTERNAL, f"tenant metrics query failed: {exc}",
                              retryable=True)
    return with_envelope_fields(data)


@router.get("/api/ops/metrics/runs")
def ops_metrics_runs(limit: int = 50, tenant_id: Optional[str] = None,
                     status: Optional[str] = None, tool: Optional[str] = None,
                     x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    guard = _requires_postgres()
    if guard is not None:
        return guard
    try:
        runs = ops_metrics_read.run_drilldown(limit=limit, tenant_id=tenant_id,
                                              status=status, tool=tool)
    except Exception as exc:  # noqa: BLE001
        return error_response(ErrorCode.INTERNAL, f"runs query failed: {exc}",
                              retryable=True)
    return with_envelope_fields({
        "runs": runs,
        "count": len(runs),
        "correlation_note": ("job is joined on the event_key prefix; job is null "
                             "when the run has no owning job (correlation unavailable)"),
    })
