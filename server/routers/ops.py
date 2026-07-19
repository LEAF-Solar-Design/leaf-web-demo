"""Ops surface (CONTRACT-ADDENDUM §14) — the broker's metering + kill-switch,
exposed app-side behind an internal role gate.

The broker (server/broker.py) is the attribution + kill-switch chokepoint, but it
has no operator UI. These routes give the ops/QA operator a read of per-tenant
spend/runs joined with kill-switch state, plus a proxied disable/enable — WITHOUT
the tenant-facing UI ever seeing them.

CREDENTIAL GATE (F7): every route requires a real internal shared secret — the
value of the ``LEAF_OPS_SECRET`` env, presented in the ``X-Ops-Secret`` header and
compared CONSTANT-TIME (``hmac.compare_digest``). The old plain ``X-Internal-Role:
qa`` header let anyone read every tenant's spend and flip kill-switches; it is gone.
Fail-closed: in live-auth mode (``LEAF_AUTH_LIVE=1``) a server with no
``LEAF_OPS_SECRET`` configured refuses the surface entirely (503). A wrong/absent
presented secret is always 403. With auth OFF and no secret configured the surface
stays open (byte-identical to the local single-operator demo, mirroring
``deps.require_tenant``). This is an internal surface, NOT part of the tenant surface.

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

import hmac
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
# credential gate (F7) — real internal shared secret, constant-time compared
# --------------------------------------------------------------------------- #
def _ops_secret() -> Optional[str]:
    """The configured internal ops shared secret, or None when unset/blank.

    Read from the ``LEAF_OPS_SECRET`` env at CALL TIME (so subprocess/test env
    overrides apply). Codex injects the value at deploy; this code only reads it —
    the secret is never hardcoded or logged."""
    val = os.environ.get("LEAF_OPS_SECRET", "").strip()
    return val or None


def _require_ops(presented: Optional[str]) -> Optional[JSONResponse]:
    """None when the caller presents the correct internal ops secret; else an
    error envelope.

    * secret configured → require ``hmac.compare_digest(presented, secret)``
      (constant-time); wrong/absent → 403.
    * secret UNSET + live-auth (``LEAF_AUTH_LIVE=1``) → FAIL-CLOSED 503: the ops
      surface never serves unguarded in live mode.
    * secret UNSET + auth off (local demo) → open passthrough (byte-identical to
      the rest of the auth-off demo; mirrors ``deps.require_tenant``).

    No frozen ErrorCode names authorization; BAD_PARAMS at HTTP 403 stays the
    honest machine-readable choice for a missing/incorrect credential, and INTERNAL
    at HTTP 503 flags the misconfigured (unusable) surface."""
    secret = _ops_secret()
    if secret is None:
        if deps.auth_live():
            return error_response(
                ErrorCode.INTERNAL,
                "ops surface unavailable: LEAF_OPS_SECRET is not configured",
                retryable=False, status_code=503)
        return None  # local demo, unguarded like the rest of the off-auth surface
    if hmac.compare_digest(presented or "", secret):
        return None
    return error_response(ErrorCode.BAD_PARAMS,
                          "valid X-Ops-Secret required for the ops surface",
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
        resp = requests.post(url, headers=broker_client.broker_headers(), timeout=10)
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
def ops_tenants(x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
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
def ops_disable(tid: str, x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    return _proxy(tid, "disable")


@router.post("/api/ops/tenants/{tid}/enable")
def ops_enable(tid: str, x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    return _proxy(tid, "enable")
