"""Ops surface (CONTRACT-ADDENDUM §14) — the broker's metering + kill-switch,
exposed app-side behind an internal role gate.

The broker (server/broker.py) is the attribution + kill-switch chokepoint, but it
has no operator UI. These routes give the ops/QA operator a read of per-tenant
spend/runs joined with kill-switch state, plus a proxied disable/enable — WITHOUT
the tenant-facing UI ever seeing them.

ROLE GATE (like the QA catalog filter, GET /api/capabilities): every route
requires the ``X-Internal-Role: qa`` header (v1 stub until real role identity from
the auth sibling); anything else → 403. This is an internal surface, NOT part of
the tenant surface.

    GET  /api/ops/tenants                  -> {tenants:[{tenant_id, runs, usd_est, disabled}]}
    POST /api/ops/tenants/{tid}/disable     -> proxy broker disable; broker's §-enveloped ack
    POST /api/ops/tenants/{tid}/enable      -> proxy broker enable;  broker's §-enveloped ack

Spend/runs come from the broker attribution ledger (same app-side read as
GET /api/usage — the ledger holds no credential), aggregated per tenant via
``da/usage.py::aggregate_usage``. Kill-switch state comes from the broker
(``GET /broker/health`` authoritative; ``broker_tenants.json`` fallback when the
broker is unreachable). Disable/enable PROXY the broker over ``BROKER_URL`` — only
the broker persists the kill-switch — and return its ack verbatim; a broker that
is unreachable yields a ``BROKER_UNREACHABLE`` envelope at HTTP 502.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Set

import requests
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

import broker_client
import deps
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


# --------------------------------------------------------------------------- #
# role gate (stub — real role identity arrives with the auth sibling)
# --------------------------------------------------------------------------- #
def _require_qa(role: Optional[str]) -> Optional[JSONResponse]:
    """None when the caller holds the internal QA role; else a 403 envelope.

    No frozen ErrorCode names authorization; BAD_PARAMS at HTTP 403 is the honest
    machine-readable choice for a missing/incorrect required internal-role header.
    """
    if (role or "").strip().lower() == "qa":
        return None
    return error_response(ErrorCode.BAD_PARAMS,
                          "X-Internal-Role: qa required for the ops surface",
                          retryable=False, status_code=403)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _usage_mod():
    """da/usage.py under a distinct name (pure module, no credential) — mirrors
    routers/usage.py so it never depends on sys.path order."""
    return deps._load_module_from(deps.DA_DIR / "usage.py", "leaf_usage")


def _ledger_path() -> Path:
    """Same resolution as GET /api/usage: LEAF_USAGE_LEDGER > BROKER_LEDGER >
    default server/broker_ledger.jsonl. Read at call time for subprocess/test env."""
    override = os.environ.get("LEAF_USAGE_LEDGER") or os.environ.get("BROKER_LEDGER")
    return Path(override) if override else (deps.SERVER_DIR / "broker_ledger.jsonl")


def _distinct_tenants(ledger_path: Path) -> Set[str]:
    """Every tenant_id that appears in the attribution ledger (any status)."""
    out: Set[str] = set()
    p = Path(ledger_path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        tid = e.get("tenant_id")
        if tid:
            out.add(str(tid))
    return out


def _disabled_set() -> Set[str]:
    """Kill-switch state, resolved FRESH on every request (read-your-write, Contract
    5b): a tenant disabled via the proxy route is reflected by the very next
    GET /api/ops/tenants — there is NO cached /health. Authoritative source is a
    per-request GET /broker/health `tenants_disabled` (the broker updates its state
    synchronously before acking the disable, so the next health read sees it); a
    Cache-Control: no-cache header defeats any intermediary cache. Fallback (broker
    unreachable): read broker_tenants.json directly, also fresh per call. Never raises."""
    try:
        resp = requests.get(f"{broker_client.broker_url()}/broker/health", timeout=3,
                            headers={"Cache-Control": "no-cache"})
        data = resp.json()
        return {str(t) for t in (data.get("tenants_disabled") or [])}
    except Exception:  # noqa: BLE001
        pass
    try:
        path = Path(os.environ.get("BROKER_TENANTS", str(deps.SERVER_DIR / "broker_tenants.json")))
        if path.exists():
            t = json.loads(path.read_text(encoding="utf-8"))
            return {str(tid) for tid, v in t.items()
                    if isinstance(v, dict) and v.get("disabled")}
    except Exception:  # noqa: BLE001
        pass
    return set()


def _proxy(tid: str, action: str) -> JSONResponse:
    """POST the broker's disable/enable and return its §-enveloped ack verbatim."""
    base = broker_client.broker_url()
    url = f"{base}/broker/tenants/{tid}/{action}"
    try:
        resp = requests.post(url, timeout=10)
    except (requests.ConnectionError, requests.Timeout) as exc:
        return error_response(ErrorCode.BROKER_UNREACHABLE,
                              f"broker at {base} unreachable: {exc}", retryable=True)
    try:
        body = resp.json()
    except ValueError as exc:
        return error_response(ErrorCode.BROKER_UNREACHABLE,
                              f"broker at {base} returned non-JSON: {exc}", retryable=True)
    return JSONResponse(status_code=resp.status_code, content=body)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@router.get("/api/ops/tenants")
def ops_tenants(x_internal_role: Optional[str] = Header(default=None)) -> Any:
    gate = _require_qa(x_internal_role)
    if gate is not None:
        return gate
    ledger = _ledger_path()
    um = _usage_mod()
    disabled = _disabled_set()
    tenant_ids = _distinct_tenants(ledger) | disabled
    rows = []
    for tid in sorted(tenant_ids):
        if um is not None:
            agg = um.aggregate_usage(tid, ledger)
            runs = agg["total"]["runs"]
            usd_est = agg["total"]["usd_est"]
        else:  # da/usage.py somehow unavailable -> honest zeros, never a 500
            runs, usd_est = 0, 0.0
        rows.append({"tenant_id": tid, "runs": runs, "usd_est": usd_est,
                     "disabled": tid in disabled})
    return with_envelope_fields({"tenants": rows})


@router.post("/api/ops/tenants/{tid}/disable")
def ops_disable(tid: str, x_internal_role: Optional[str] = Header(default=None)) -> Any:
    gate = _require_qa(x_internal_role)
    if gate is not None:
        return gate
    return _proxy(tid, "disable")


@router.post("/api/ops/tenants/{tid}/enable")
def ops_enable(tid: str, x_internal_role: Optional[str] = Header(default=None)) -> Any:
    gate = _require_qa(x_internal_role)
    if gate is not None:
        return gate
    return _proxy(tid, "enable")
