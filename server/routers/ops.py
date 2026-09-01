"""Ops surface (CONTRACT-ADDENDUM §14) — the broker's metering + kill-switch.

The broker (server/broker.py) is the attribution + kill-switch chokepoint, but it
has no operator UI. These routes give the ops/QA operator a read of per-tenant
spend/runs joined with kill-switch state, plus a proxied disable/enable — WITHOUT
the tenant-facing UI ever seeing them.

BROWSER OPERATOR GATE: the drawer uses ``/api/operator/tenants``. Those routes
resolve the bearer subject through the server-owned ``operator_principals`` grant
with ``require_operator``. The browser never receives or sends an ops secret.

SERVICE CREDENTIAL GATE (F7): the older ``/api/ops/*`` integration surface keeps
its internal ``LEAF_OPS_SECRET`` credential for trusted service callers. It is not
the browser authority path. The value is presented in ``X-Ops-Secret`` and compared
constant-time. The old plain ``X-Internal-Role: qa`` header grants nothing.

    GET  /api/ops/tenants                  -> {tenants:[{tenant_id, runs, usd_est,
                                              llm_turns, llm_cost_tokens, llm_usd_est,
                                              disabled}],
                                             platform:{profiles, autocad_backend, llm}}
    POST /api/ops/tenants/{tid}/disable     -> proxy broker disable; broker's §-enveloped ack
    POST /api/ops/tenants/{tid}/enable      -> proxy broker enable;  broker's §-enveloped ack

    GET  /api/operator/tenants              -> same list, server-granted operator only
    POST /api/operator/tenants/{tid}/disable -> same mutation, server-granted operator only
    POST /api/operator/tenants/{tid}/enable  -> same mutation, server-granted operator only

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
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Set
from uuid import uuid4

import requests
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

import agent_audit
import agent_ledger
import agent_policy
import broker_client
import deps
import operator_deps
from operator_deps import OperatorContext
from customization_service import CustomizationService, CustomizationServiceError
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()
operator_router = APIRouter()


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


class UnitEconomicsObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    idempotency_key: str = Field(..., min_length=1, max_length=256)
    period_start: datetime
    period_end: datetime
    kind: Literal["shared_fixed", "usage_variable", "revenue"]
    category: str = Field(..., min_length=1, max_length=100)
    amount_usd: Decimal = Field(..., ge=0)
    quantity: Optional[Decimal] = Field(default=None, ge=0)
    unit: Optional[str] = Field(default=None, min_length=1, max_length=50)
    source: str = Field(..., min_length=1, max_length=100)
    source_ref: Optional[str] = Field(default=None, max_length=500)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("idempotency_key", "category", "source")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


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
    broker_tenants.json. Never crosses authorities.

    The health read is trusted ONLY on a 200 carrying a real `tenants_disabled`
    LIST. A connection error was already handled, but a broker that ANSWERS badly
    was not: FastAPI's own 500 body (`{"detail": ...}`) parses as JSON, so
    `data.get("tenants_disabled") or []` turned a broker fault into the confident
    claim "no tenant is disabled" AND returned before the authoritative store
    fallback below could run. The ops drawer then renders every kill-switched
    tenant as Active during the exact incident an operator opens it for. A
    degraded read must fall through to the authority, never answer 'all clear'."""
    try:
        resp = requests.get(f"{broker_client.broker_url()}/broker/health", timeout=3,
                            headers={"Cache-Control": "no-cache"})
        if resp.status_code == 200:
            data = resp.json()
            listed = data.get("tenants_disabled") if isinstance(data, dict) else None
            # An absent key is NOT an empty kill list — it means this reply does not
            # carry the field, so it cannot settle the question.
            if isinstance(listed, list):
                return {str(t) for t in listed}
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
# --------------------------------------------------------------------------- #
# usage scoreboard join (M6) — LLM usage beside the broker's AutoCAD usage
# --------------------------------------------------------------------------- #
# ABSENT usage and ZERO usage are different facts. A tenant with no turns has
# spent 0; a tenant whose ledger we could not read has spent an UNKNOWN amount.
# The scoreboard renders those differently ("0" vs an em dash), so nothing in
# this layer is allowed to collapse one into the other -- a confident zero over
# a failed read is the one reading that inverts an operator's judgement.
_LLM_ABSENT: Dict[str, Any] = {
    "llm_turns": None, "llm_cost_tokens": None, "llm_usd_est": None}


def _agent_usage_snapshot() -> Optional[Dict[str, Dict[str, Any]]]:
    """Lifetime agent usage for EVERY tenant, read exactly once per listing.

    ``agent_ledger.tenants_seen()`` makes its own full pass over the ledger, so
    calling it inside the row loop would be an N+1 over the whole file. The one
    call stays out here and every row indexes into the result: cost is one pass,
    not one pass per tenant. Returns None (unknown) on any fault, never {}.
    """
    try:
        seen = agent_ledger.tenants_seen(raise_on_read_error=True)
    except Exception:  # ledger unreadable / agent store down -> unknown
        return None
    return seen if isinstance(seen, dict) else None


def _llm_row(snapshot: Optional[Dict[str, Dict[str, Any]]], tid: str) -> Dict[str, Any]:
    """This tenant's lifetime LLM columns, or the absent triple."""
    if snapshot is None:
        return dict(_LLM_ABSENT)
    agg = snapshot.get(tid) or {}
    return {
        "llm_turns": int(agg.get("turns") or 0),
        "llm_cost_tokens": int(agg.get("cost_tokens") or 0),
        "llm_usd_est": round(float(agg.get("usd_est") or 0.0), 6),
    }


def _platform_totals(rows: list, snapshot: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """Platform-scope lifetime totals -- NOT merely the sum of the rows above.

    The AutoCAD total does sum the listing, because the listing IS the broker
    authority's whole tenant set. The LLM total sums the WHOLE agent ledger: a
    tenant can spend LLM turns without ever reaching the broker, so clipping to
    the listed rows would silently under-report the platform. ``profiles`` is
    the union of both authorities and may therefore exceed the row count, which
    is why the drawer labels this scope "platform" and never "these rows".
    """
    cad_runs = sum(int(r.get("runs") or 0) for r in rows)
    cad_usd = round(sum(float(r.get("usd_est") or 0.0) for r in rows), 6)
    if snapshot is None:
        return {
            "profiles": len(rows),
            "autocad_backend": {"runs": cad_runs, "usd_est": cad_usd},
            "llm": {"turns": None, "cost_tokens": None, "usd_est": None},
        }
    aggs = list(snapshot.values())
    return {
        "profiles": len({str(r.get("tenant_id")) for r in rows} | set(snapshot)),
        "autocad_backend": {"runs": cad_runs, "usd_est": cad_usd},
        "llm": {
            "turns": sum(int(a.get("turns") or 0) for a in aggs),
            "cost_tokens": sum(int(a.get("cost_tokens") or 0) for a in aggs),
            "usd_est": round(sum(float(a.get("usd_est") or 0.0) for a in aggs), 6),
        },
    }


def _tenant_listing() -> Any:
    um = _usage_mod()
    disabled = _disabled_set()
    agent_snapshot = _agent_usage_snapshot()
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
                     "disabled": tid in disabled,
                     **_llm_row(agent_snapshot, tid)})
    return with_envelope_fields({
        "tenants": rows,
        "platform": _platform_totals(rows, agent_snapshot),
    })


@router.get("/api/ops/tenants")
def ops_tenants(x_ops_secret: Optional[str] = Header(default=None)) -> Any:
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    return _tenant_listing()


@operator_router.get("/api/operator/tenants")
def operator_tenants(
    _operator: OperatorContext = Depends(operator_deps.require_operator),
) -> Any:
    return _tenant_listing()


@router.get("/api/ops/unit-economics")
def ops_unit_economics(
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
    x_ops_secret: Optional[str] = Header(default=None),
) -> Any:
    """Return one fleet-only report. No tenant or external identifiers leave it."""
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    end = period_end or datetime.now(timezone.utc)
    start = period_start or end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        import platform_link

        report = platform_link.unit_economics_store().fleet_report(start, end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return report


@router.post("/api/ops/unit-economics/observations")
def ops_unit_economics_observation(
    body: UnitEconomicsObservationRequest,
    x_ops_secret: Optional[str] = Header(default=None),
) -> Any:
    """Append an observed fleet cost or revenue line with idempotent replay."""
    gate = _require_ops(x_ops_secret)
    if gate is not None:
        return gate
    import platform_link

    unit_store = platform_link.unit_economics_store()
    try:
        result = unit_store.append_observation(
            idempotency_key=body.idempotency_key,
            period_start=body.period_start,
            period_end=body.period_end,
            kind=body.kind,
            category=body.category,
            amount_usd=body.amount_usd,
            quantity=body.quantity,
            unit=body.unit,
            source=body.source,
            source_ref=body.source_ref,
            metadata=body.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except unit_store.LedgerConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return with_envelope_fields(result)


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


@operator_router.post("/api/operator/tenants/{tid}/disable")
def operator_disable(
    tid: str,
    _operator: OperatorContext = Depends(operator_deps.require_operator),
) -> Any:
    return _proxy(tid, "disable")


@operator_router.post("/api/operator/tenants/{tid}/enable")
def operator_enable(
    tid: str,
    _operator: OperatorContext = Depends(operator_deps.require_operator),
) -> Any:
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


class ToolPublicationPolicyRequest(BaseModel):
    tool_publication_approval_required: StrictBool
    expected_revision: StrictInt = Field(..., ge=0)

    class Config:
        extra = "forbid"


def _require_account_owner(
    tenant=Depends(deps.require_active_tenant),
) -> deps.TenantContext:
    """Require the current account's authoritative owner binding."""
    if (not deps.auth_live() or not isinstance(tenant, deps.TenantContext)
            or not tenant.subject):
        raise HTTPException(
            status_code=403,
            detail="account owner authority required",
        )
    try:
        import platform_link

        store = platform_link.platform_store()
        binding = store.resolve_active_identity_binding("auth0", tenant.subject)
        if binding is None or str(binding.platform_tenant_id) != str(tenant):
            raise HTTPException(
                status_code=403, detail="account owner authority required"
            )
        role = store.active_identity_role(
            binding.platform_tenant_id, binding.binding_id
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - authority outages fail closed
        raise HTTPException(
            status_code=503,
            detail="platform identity binding authority is unavailable",
        ) from exc
    if role != "owner":
        raise HTTPException(
            status_code=403, detail="account owner authority required"
        )
    return tenant


def _tool_publication_policy_body(
    tenant_id: str, state: Dict[str, Any], *, allow_disabled_state: bool = False
) -> Dict[str, Any]:
    overlay = state.get("overlay")
    if not isinstance(overlay, dict):
        raise agent_policy.PolicyError("tenant overlay must be a mapping")
    action = agent_policy.effective_action(
        agent_policy.load_policy(),
        "request_publication",
        tenant_overlay=overlay,
    )
    if action is None:
        raise agent_policy.PolicyError("request_publication policy is unavailable")
    if not action.enabled and not allow_disabled_state:
        raise agent_policy.PolicyError("tool publication is disabled for this account")
    return {
        "tenant_id": tenant_id,
        "tool_publication_approval_required": (
            not action.enabled or action.policy != "auto"
        ),
        "revision": int(state.get("revision", 0)),
    }


@router.get("/api/admin/account-controls")
def get_tool_publication_policy(
    tenant: deps.TenantContext = Depends(_require_account_owner),
) -> Dict[str, Any]:
    try:
        state = agent_policy.load_tenant_state(str(tenant))
        return _tool_publication_policy_body(str(tenant), state)
    except agent_policy.PolicyError as exc:
        if "tool publication is disabled" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(
            status_code=503,
            detail="tool publication policy authority unavailable",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - authority outages fail closed
        raise HTTPException(
            status_code=503,
            detail="tool publication policy authority unavailable",
        ) from exc


@router.put("/api/admin/account-controls")
def put_tool_publication_policy(
    req: ToolPublicationPolicyRequest,
    tenant: deps.TenantContext = Depends(_require_account_owner),
) -> Any:
    tenant_id = str(tenant)
    try:
        current = agent_policy.load_tenant_state(tenant_id)
        overlay = dict(current["overlay"])
        publication = overlay.get("request_publication", {})
        if not isinstance(publication, dict):
            raise agent_policy.PolicyError(
                "request_publication overlay must be a mapping"
            )
        publication = dict(publication)
        if req.tool_publication_approval_required:
            publication["policy"] = "always-confirm"
            overlay["request_publication"] = publication
        else:
            publication.pop("policy", None)
            if publication:
                overlay["request_publication"] = publication
            else:
                overlay.pop("request_publication", None)
        entry = agent_policy.set_tenant_overlay(
            tenant_id,
            overlay,
            expected_revision=req.expected_revision,
            audit_event={
                "kind": "tool_publication_policy",
                "scope": "tenant",
                "tenant_id": tenant_id,
                "tool_publication_approval_required": (
                    req.tool_publication_approval_required
                ),
                "actor_subject": tenant.subject,
                "via": "account_owner",
            },
        )
        return _tool_publication_policy_body(
            tenant_id, entry, allow_disabled_state=True
        )
    except agent_policy.PolicyError as exc:
        if "stale agent tenant state revision" in str(exc):
            return error_response(
                ErrorCode.BAD_PARAMS,
                str(exc),
                retryable=True,
                status_code=409,
            )
        return error_response(
            ErrorCode.INTERNAL,
            "tool publication policy authority unavailable",
            retryable=True,
            status_code=503,
        )
    except Exception:  # noqa: BLE001 - authority outages fail closed
        return error_response(
            ErrorCode.INTERNAL,
            "tool publication policy authority unavailable",
            retryable=True,
            status_code=503,
        )


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
