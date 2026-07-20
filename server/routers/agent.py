"""
Agent spine policy surface (§18 slice owned by this lane):

    POST /internal/agent/gate                       -> run the gate chain (harness back-edge)
    POST /api/agent/approvals/{confirmation_id}     -> tenant grants/denies a pending approval
    GET  /api/agent/audit?limit=                    -> tenant's own audit records (projected)
    GET  /api/agent/killswitch                      -> {active} read-only (NO off-toggle)

BACK-EDGE TRUST (wire contract §0): `/internal/agent/gate` is the endpoint the
harness's canUseTool calls before EVERY conversational tool call. It is gated
by the `X-Dispatch-Secret` header against the `LEAF_APP_DISPATCH_SECRET` env,
compared CONSTANT-TIME (`hmac.compare_digest`) — same trust model as the
broker's X-Broker-Secret. Secret unset in env => the back-edge is disabled =>
401 always (fail closed; there is no unguarded mode for this surface). The
gate body carries the harness-resolved `tenant_id` verbatim.

Tenant-facing routes use the ordinary `require_tenant` dependency; an approval
that exists but belongs to another tenant is a 404, never a 403 — no
cross-tenant existence oracle (same rule as jobs, security-audit F8).
"""
from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import agent_audit
import agent_gate
import deps
import entitlements
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


# --------------------------------------------------------------------------- #
# back-edge gate (X-Dispatch-Secret)
# --------------------------------------------------------------------------- #
def _dispatch_secret() -> Optional[str]:
    """Read at CALL TIME (test/subprocess env overrides apply); never logged."""
    val = os.environ.get("LEAF_APP_DISPATCH_SECRET", "").strip()
    return val or None


def _require_dispatch(presented: Optional[str]) -> Optional[JSONResponse]:
    """None when the harness presents the correct back-edge secret; else 401.
    Unset secret => back-edge DISABLED => 401 (never an open fallback: this
    endpoint hands out allow decisions, not reads)."""
    secret = _dispatch_secret()
    if secret is None or not hmac.compare_digest(presented or "", secret):
        return error_response(ErrorCode.BAD_PARAMS,
                              "valid X-Dispatch-Secret required for /internal/agent/gate",
                              retryable=False, status_code=401)
    return None


class GateRequest(BaseModel):
    tenant_id: str
    session_id: str
    turn_id: str
    action: str
    args: Dict[str, Any] = {}


@router.post("/internal/agent/gate")
def internal_gate(req: GateRequest,
                  x_dispatch_secret: Optional[str] = Header(default=None)) -> Any:
    denied = _require_dispatch(x_dispatch_secret)
    if denied is not None:
        return denied
    # Tier resolution: the back-edge carries a plain tenant_id string, so the
    # tier resolves like any off-auth caller ("demo" / policy default). The
    # broker re-checks entitlement from its OWN trusted tier source on every
    # dispatch (broker.py:588-595), so this gate can narrow but never widen
    # what the execution chain would allow anyway.
    tier = entitlements.resolve_tier(req.tenant_id)
    tier_caps = entitlements.entitlements_for(tier)
    result = agent_gate.gate(req.tenant_id, req.session_id, req.turn_id,
                             req.action, req.args, tier_caps, tier=tier)
    return with_envelope_fields(result)


# --------------------------------------------------------------------------- #
# tenant-facing routes
# --------------------------------------------------------------------------- #
class ApprovalDecision(BaseModel):
    approved: bool


@router.post("/api/agent/approvals/{confirmation_id}")
def resolve_approval(confirmation_id: str, decision: ApprovalDecision,
                     tenant=Depends(deps.require_tenant)) -> Any:
    record = agent_gate.read_pending(confirmation_id)
    # 404 (never 403) both when unknown AND when it belongs to another tenant.
    if record is None or record.get("tenant_id") != str(tenant):
        return error_response(ErrorCode.BAD_PARAMS,
                              f"unknown confirmation_id: {confirmation_id}",
                              retryable=False, status_code=404)
    if decision.approved:
        ok, _, reason = agent_gate.grant_approval(confirmation_id, by=str(tenant))
    else:
        ok, _, reason = agent_gate.deny_approval(confirmation_id, by=str(tenant))
    if not ok:
        # expired / already decided — the chip is stale; the client re-proposes.
        return error_response(ErrorCode.BAD_PARAMS,
                              f"approval not resolvable: {reason}",
                              retryable=False, status_code=409)
    return with_envelope_fields(
        deps.tenant_echo({"resolved": True, "approved": decision.approved}, tenant))


@router.get("/api/agent/audit")
def own_audit(limit: int = 100, tenant=Depends(deps.require_tenant)) -> Any:
    # Records are audit_extra-projected at WRITE time (agent_gate), so this
    # read never has raw args to leak — it only filters by tenant.
    limit = max(1, min(int(limit), 1000))
    records = agent_audit.for_tenant(str(tenant), limit=limit)
    return with_envelope_fields(
        deps.tenant_echo({"records": records, "count": len(records)}, tenant))


@router.get("/api/agent/killswitch")
def killswitch(tenant=Depends(deps.require_tenant)) -> Any:
    # Read-only BY DESIGN: the kill switch is file-presence
    # (LEAF_AGENT_KILL_FILE) and has NO API off-toggle at any privilege level.
    return with_envelope_fields({"active": agent_gate.kill_switch_active()})
