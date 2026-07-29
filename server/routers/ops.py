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

Spend/runs come from the selected broker authority, matching GET /api/usage.
PostgreSQL mode never falls back to JSONL. Kill-switch state comes from the
broker health endpoint, with the selected authority as its fallback.
Disable/enable PROXY the broker over ``BROKER_URL`` — only
the broker persists the kill-switch — and return its ack verbatim; a broker that
is unreachable yields a ``BROKER_UNREACHABLE`` envelope at HTTP 502.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Set
from uuid import uuid4

import requests
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import agent_audit
import agent_ledger
import agent_policy
import broker_client
import deps
from customization_service import CustomizationService, CustomizationServiceError
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


class DeploymentVerifyRequest(BaseModel):
    snapshot_id: str = Field(..., min_length=1, max_length=256)
    expected_effective_catalog_release: str = Field(..., min_length=1, max_length=256)
    expected_platform_release: str = Field(..., min_length=1, max_length=256)

    class Config:
        extra = "forbid"


class DeploymentSnapshotRequest(BaseModel):
    snapshot_id: str = Field(..., min_length=1, max_length=256)

    class Config:
        extra = "forbid"


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
    # BYTES, not str: compare_digest raises TypeError on non-ASCII str, and an
    # ASGI header legally carries latin-1 — a stray accent was an
    # unauthenticated 500 (same defect fixed in routers/skills.py, PR #302).
    try:
        provided = (presented or "").encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates from a hostile raw header
        provided = b""
    if hmac.compare_digest(provided, secret.encode("utf-8")):
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


def _postgres_store():
    from broker_pg_store import get_store

    return get_store()


def _broker_store_mode() -> str:
    mode = os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_BROKER_STORE must be 'legacy' or 'postgres'")
    return mode


def _agent_store_mode() -> str:
    mode = os.environ.get("LEAF_AGENT_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_AGENT_STORE must be 'legacy' or 'postgres'")
    return mode


def _agent_pg_store():
    import agent_pg_store
    return agent_pg_store


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
    Cache-Control: no-cache header defeats any intermediary cache. If the broker
    is unreachable, PostgreSQL mode reads the shared store and legacy mode reads
    broker_tenants.json. Never crosses authorities."""
    try:
        resp = requests.get(f"{broker_client.broker_url()}/broker/health", timeout=3,
                            headers={"Cache-Control": "no-cache"})
        data = resp.json()
        return {str(t) for t in (data.get("tenants_disabled") or [])}
    except Exception:  # noqa: BLE001
        pass
    if _broker_store_mode() == "postgres":
        # Never consult the stale legacy tenant file after authority flips.
        return set(_postgres_store().disabled_tenant_ids())
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
    um = _usage_mod()
    disabled = _disabled_set()
    postgres_mode = _broker_store_mode() == "postgres"
    if postgres_mode:
        store = _postgres_store()
        tenant_ids = set(store.usage_tenant_ids()) | disabled
    else:
        ledger = _ledger_path()
        tenant_ids = _distinct_tenants(ledger) | disabled
    rows = []
    for tid in sorted(tenant_ids):
        if postgres_mode:
            agg = store.aggregate_usage(tid)
            runs = agg["total"]["runs"]
            usd_est = agg["total"]["usd_est"]
        elif um is not None:
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


# --------------------------------------------------------------------------- #
# agent spine ops surface (§18) — same LEAF_OPS_SECRET gate. Reads come from
# the agent's OWN ledger/audit files (no credential in either); the per-tenant
# agent kill flag is APP-SIDE state (agent_tenants.json beside the policy
# file, via agent_policy) — independent of the broker run kill-switch above.
# --------------------------------------------------------------------------- #
@router.get("/api/ops/agent/tenants")
def ops_agent_tenants(x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    usage_by_tenant = agent_ledger.tenants_seen()
    states = (
        _agent_pg_store().tenant_states()
        if _agent_store_mode() == "postgres" else {}
    )
    rows = []
    for tid in sorted(set(usage_by_tenant) | set(states)):
        agg = usage_by_tenant.get(
            tid, {"turns": 0, "cost_tokens": 0, "usd_est": 0.0})
        state = states.get(tid) or agent_policy.load_tenant_state(tid)
        rows.append({"tenant_id": tid, "turns": agg["turns"],
                     "cost_tokens": agg["cost_tokens"], "usd_est": agg["usd_est"],
                     "agent_disabled": bool(state.get("agent_disabled")),
                     **({"revision": int(state["revision"])}
                        if "revision" in state else {})})
    return with_envelope_fields({"tenants": rows})


@router.get("/api/ops/agent/sessions/{session_id}")
def ops_agent_session(session_id: str, limit: int = 100,
                      x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    limit = max(1, min(int(limit), 1000))
    records = agent_audit.for_session(session_id, limit=limit)
    return with_envelope_fields({"session_id": session_id, "records": records,
                                 "count": len(records)})


def _ops_agent_flag(
    tid: str, disabled: bool, expected_revision: Optional[int] = None,
) -> Any:
    audit_event = {
        "kind": "kill_switch",
        "scope": "tenant",
        "tenant_id": tid,
        "agent_disabled": bool(disabled),
        "via": "ops",
    }
    try:
        entry = agent_policy.set_tenant_agent_disabled(
            tid, disabled, expected_revision=expected_revision,
            audit_event=(audit_event if _agent_store_mode() == "postgres" else None))
    except agent_policy.PolicyError as exc:
        conflict = (
            "stale agent tenant state revision",
            "enabling an agent tenant requires its current revision",
            "replacing an agent tenant overlay requires its current revision",
        )
        if not any(message in str(exc) for message in conflict):
            raise
        return error_response(
            ErrorCode.BAD_PARAMS, str(exc), retryable=True, status_code=409)
    if _agent_store_mode() != "postgres":
        agent_audit.append(audit_event)
    body = {"tenant_id": tid,
            "agent_disabled": bool(entry.get("agent_disabled"))}
    if "revision" in entry:
        body["revision"] = int(entry["revision"])
    return with_envelope_fields(body)


@router.post("/api/ops/agent/tenants/{tid}/disable")
def ops_agent_disable(
    tid: str, x_ops_secret: Optional[str] = Header(default=None),
    x_agent_state_revision: Optional[int] = Header(default=None),
) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    return _ops_agent_flag(tid, True, x_agent_state_revision)


@router.post("/api/ops/agent/tenants/{tid}/enable")
def ops_agent_enable(
    tid: str, x_ops_secret: Optional[str] = Header(default=None),
    x_agent_state_revision: Optional[int] = Header(default=None),
) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    return _ops_agent_flag(tid, False, x_agent_state_revision)


class AgentOverlayRequest(BaseModel):
    overlay: Dict[str, Any]


@router.put("/api/ops/agent/tenants/{tid}/overlay")
def ops_agent_overlay(
    tid: str, req: AgentOverlayRequest,
    x_ops_secret: Optional[str] = Header(default=None),
    x_agent_state_revision: Optional[int] = Header(default=None),
) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    audit_event = {
        "kind": "policy_overlay",
        "scope": "tenant",
        "tenant_id": tid,
        "via": "ops",
    }
    try:
        entry = agent_policy.set_tenant_overlay(
            tid, req.overlay,
            expected_revision=x_agent_state_revision,
            audit_event=audit_event,
        )
    except agent_policy.PolicyError as exc:
        conflict = (
            "stale agent tenant state revision",
            "replacing an agent tenant overlay requires its current revision",
        )
        if any(message in str(exc) for message in conflict):
            return error_response(
                ErrorCode.BAD_PARAMS, str(exc), retryable=True, status_code=409)
        return error_response(
            ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields({
        "tenant_id": tid,
        "agent_disabled": bool(entry["agent_disabled"]),
        "overlay": entry["overlay"],
        "revision": int(entry["revision"]),
    })


def _customization_ops_error(exc: Exception) -> JSONResponse:
    reason = exc.code if isinstance(exc, CustomizationServiceError) else "customization_ops_failed"
    status_code = exc.status_code if isinstance(exc, CustomizationServiceError) else 503
    return JSONResponse(
        status_code=status_code,
        content=with_envelope_fields({
            "error": {
                "error_code": "INTERNAL",
                "message": "customization deployment operation failed",
                "retryable": status_code >= 500,
            },
            "reason_code": reason,
        }),
    )


@router.post("/internal/ops/customization/deployment-snapshot")
def customization_deployment_snapshot(
    x_ops_secret: Optional[str] = Header(default=None),
) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    try:
        return CustomizationService.configured().capture_deployment_snapshot(
            idempotency_key=f"deploy-snapshot:{uuid4()}"
        )
    except Exception as exc:  # noqa: BLE001
        return _customization_ops_error(exc)


@router.post("/internal/ops/customization/deployment-verify")
def customization_deployment_verify(
    req: DeploymentVerifyRequest,
    x_ops_secret: Optional[str] = Header(default=None),
) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    try:
        return CustomizationService.configured().verify_deployment(
            snapshot_id=req.snapshot_id,
            expected_effective_catalog_release=req.expected_effective_catalog_release,
            expected_platform_release=req.expected_platform_release,
            idempotency_key=f"deploy-verify:{req.snapshot_id}:{req.expected_platform_release}",
        )
    except Exception as exc:  # noqa: BLE001
        return _customization_ops_error(exc)


@router.post("/internal/ops/customization/deployment-rollback")
def customization_deployment_rollback(
    req: DeploymentSnapshotRequest,
    x_ops_secret: Optional[str] = Header(default=None),
) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    try:
        return CustomizationService.configured().restore_deployment_snapshot(
            snapshot_id=req.snapshot_id,
            idempotency_key=f"deploy-restore:{req.snapshot_id}",
        )
    except Exception as exc:  # noqa: BLE001
        return _customization_ops_error(exc)


@router.post("/internal/ops/customization/deployment-rollback-verify")
def customization_deployment_rollback_verify(
    req: DeploymentSnapshotRequest,
    x_ops_secret: Optional[str] = Header(default=None),
) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    try:
        return CustomizationService.configured().verify_restored_deployment(
            snapshot_id=req.snapshot_id,
            idempotency_key=f"deploy-restore-verify:{req.snapshot_id}",
        )
    except Exception as exc:  # noqa: BLE001
        return _customization_ops_error(exc)
