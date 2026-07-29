"""
Agent surface — UNION of two lanes (merge resolution, 2026-07-21):

  §18 spine policy plane (PR #5):
    POST /internal/agent/gate                   -> run the gate chain (harness back-edge)
    GET  /api/agent/audit?limit=                -> tenant's own audit records (projected)
    GET  /api/agent/killswitch                  -> {active} read-only (NO off-toggle)

  §2.1 sessions wire (PR #12):
    POST /api/agent/approvals/{confirmation_id} -> record-only approval decision
                                                   against session_store (S4)

Both lanes shipped an approvals route at the same path. The SESSIONS-WIRE one
wins: the live console (leaf_website) drives turns through the §2.1 turn
engine, whose relayed proposal events create rows in server/session_store.py —
so tenant decisions land in THAT store first. SPINE UNIFICATION (census #12
chip 1): with the §18 engine mounted behind POST /turn, the gate mints every
confirmation_id and holds its own args-bound pending record, so a recorded
tenant decision is now ALSO bridged into the gate store
(agent_gate.grant_approval / deny_approval below) — that record is what the
resume turn's gate consult redeems before dispatching. A confirmation_id with
no gate record (pre-spine turns) bridges as a silent not_found: record-only
behavior for those is unchanged.

BACK-EDGE TRUST (wire contract §0): `/internal/agent/gate` is the endpoint the
harness's canUseTool calls before EVERY conversational tool call. It is gated
by the `X-Dispatch-Secret` header against the `LEAF_APP_DISPATCH_SECRET` env,
compared CONSTANT-TIME (`hmac.compare_digest`) — same trust model as the
broker's X-Broker-Secret. Secret unset in env => the back-edge is disabled =>
401 always (fail closed; there is no unguarded mode for this surface). The
gate body carries the harness-resolved `tenant_id` verbatim.

APPROVALS (record-only, sessions wire spec §2.1.5): the endpoint ONLY records
an approve/reject decision against the durable `approvals` row created by the
turn engine (S3) when it emitted `confirmation_required`. It NEVER starts (or
resumes) a turn itself — the client always follows up with a separate
`POST /sessions/{id}/messages {confirm:{confirmationId, approved}}`.
Tenant + existence guard (no existence leak): unknown confirmation_id OR one
owned by another tenant both collapse to the SAME 404 bad_params response.
The SESSION row's `expired` flag is deliberately not checked here — an
expired-but-undecided sessions-wire approval is still recorded; its 410
confirmation_expired belongs to the SUBSEQUENT /messages{confirm} call. A
GATE-minted record is different: grant on an expired gate record answers 410
here and records NOTHING (the gate auto-denied it; recording a session-side
approve would put the two stores in contradiction).
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
import session_store
import turn_runner
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
    authority_session_id: Optional[str] = None
    authority_turn_id: Optional[str] = None
    action: str
    args: Dict[str, Any] = {}


@router.post("/internal/agent/gate")
def internal_gate(req: GateRequest,
                  x_dispatch_secret: Optional[str] = Header(default=None)) -> Any:
    denied = _require_dispatch(x_dispatch_secret)
    if denied is not None:
        return denied
    # Legacy tier resolution: without an authenticated active-turn snapshot, the tier
    # comes from the broker-TRUSTED source keyed on it (deps.backedge_tenant) —
    # resolving a bare string would default every caller to "demo" (full access)
    # and silently skip tier_overrides that TIGHTEN a rung. This gate is the ONLY
    # tier check for converse/deploy/agent_write_autopilot: the broker's own
    # re-check (broker.py:588-595) covers run_read/run_write/build alone.
    # The client-facing message route binds the verified JWT tier to the exact
    # active turn before the harness can call this gate. This preserves
    # authority across the app-to-harness-to-app hop without trusting model
    # output. Non-session callers retain the broker-trusted fallback.
    authority_session_id = req.authority_session_id or req.session_id
    authority_turn_id = req.authority_turn_id or req.turn_id
    turn_tier = session_store.active_turn_tier(
        authority_session_id, authority_turn_id, req.tenant_id)
    if turn_tier is not None:
        tier = entitlements.resolve_tier(
            deps.TenantContext(req.tenant_id, tier=turn_tier))
    else:
        tier = entitlements.resolve_tier(deps.backedge_tenant(req.tenant_id))
    try:
        tier_caps = entitlements.entitlements_for(tier)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("converse", tier)
    # Bind a confirm-once grant to the person who answered the chip. Same
    # authority tuple the tier lookup above uses, so the gate never trusts a
    # subject asserted over the wire.
    turn_subject = session_store.active_turn_subject(
        authority_session_id, authority_turn_id, req.tenant_id,
        turn_runner.turn_max_s())
    result = agent_gate.gate(req.tenant_id, req.session_id, req.turn_id,
                             req.action, req.args, tier_caps, tier=tier,
                             subject=turn_subject)
    return with_envelope_fields(result)


# --------------------------------------------------------------------------- #
# tenant-facing routes
# --------------------------------------------------------------------------- #
class ApprovalDecisionRequest(BaseModel):
    approved: bool


def _not_found(confirmation_id: str):
    return error_response(
        ErrorCode.BAD_PARAMS,
        f"unknown confirmation_id {confirmation_id!r}",
        retryable=False,
        status_code=404,
    )


@router.post("/api/agent/approvals/{confirmation_id}")
def decide_approval(confirmation_id: str, req: ApprovalDecisionRequest,
                     tenant=Depends(deps.require_active_tenant)):
    """Record-only: approve/reject a pending confirmation. Never starts a turn."""
    approval = session_store.get_approval(confirmation_id)
    if approval is None or approval["tenant_id"] != str(tenant):
        # Unknown and cross-tenant collapse to the IDENTICAL response -- no
        # existence leak to a caller who isn't the owning tenant.
        return _not_found(confirmation_id)

    decision_subject = getattr(tenant, "subject", None)
    decision_actor = (decision_subject
                      if isinstance(decision_subject, str) and decision_subject
                      else str(tenant))

    # Spine unification (census #12 chip 1): land the decision in the §18 gate
    # store FIRST — the resume turn's gate consult redeems THAT args-bound
    # record before any dispatch. Gate-first ordering is load-bearing: the
    # session_store row below can only become decidable (and later consumable)
    # AFTER the gate record is decided, so the stuck divergence "session row
    # consumed / gate record still pending" (whose re-consult would re-propose
    # the SAME already-consumed confirmation_id — an undeliverable chip) is
    # unreachable. Every gate outcome is HANDLED, never ignored (review round 2
    # finding): the two stores must not record opposite decisions.
    gate_rec, gate_status = agent_gate.read_pending_strict(confirmation_id)
    if gate_status == "io_error":
        return error_response(
            ErrorCode.INTERNAL,
            "approval decision not recorded (gate store unreadable) — retry",
            retryable=True, status_code=503,
        )
    if gate_status == "ok":
        try:
            if req.approved:
                ok, rec, reason = agent_gate.grant_approval(
                    confirmation_id, by=decision_actor)
            else:
                ok, rec, reason = agent_gate.deny_approval(
                    confirmation_id, by=decision_actor)
        except Exception as exc:  # noqa: BLE001
            return error_response(
                ErrorCode.INTERNAL,
                f"approval decision not recorded (gate store write failed: "
                f"{type(exc).__name__}) — retry",
                retryable=True, status_code=503,
            )
        if not ok:
            if reason == "expired":
                # grant_approval auto-denied the expired record; recording an
                # approve session-side would create the exact cross-store
                # contradiction gate-first ordering exists to prevent.
                return error_response(
                    ErrorCode.CONFIRMATION_EXPIRED,
                    f"confirmation_id {confirmation_id!r} expired before the decision",
                    retryable=False, status_code=410,
                )
            if reason == "already_decided":
                if bool((rec or {}).get("granted")) != bool(req.approved):
                    # Contradicts the gate's recorded decision (e.g. an opposite
                    # click racing a partial earlier failure): refuse rather
                    # than let the session row diverge from the gate's truth.
                    return error_response(
                        ErrorCode.BAD_PARAMS,
                        f"confirmation_id {confirmation_id!r} already decided",
                        retryable=False, status_code=409,
                    )
                # Same-direction retry completing a partial earlier failure:
                # fall through and land the session half.
            elif reason == "not_found":
                # Existed at the strict read, gone at the write — a raced
                # rewrite/cleanup. Conservative: retryable, nothing recorded.
                return error_response(
                    ErrorCode.INTERNAL,
                    "approval decision not recorded (gate record raced away) — retry",
                    retryable=True, status_code=503,
                )
    # gate_status absent => a pre-spine sessions-wire-only confirmation_id —
    # record-only semantics, unchanged. corrupt => the gate chain will deny
    # that record at resume regardless (malformed reads as absent -> deny),
    # so the session-side record stays fail-safe.

    outcome = session_store.decide_approval(
        confirmation_id, req.approved, by=decision_actor)
    if outcome == "not_found":
        return _not_found(confirmation_id)
    if outcome == "already_decided":
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"confirmation_id {confirmation_id!r} already decided",
            retryable=False,
            status_code=409,
        )

    # outcome == "recorded"
    session_store.append_event(
        approval["session_id"], approval["turn_id"], "confirmation_resolved",
        {"confirmation_id": confirmation_id, "approved": bool(req.approved),
         "by": decision_actor},
    )
    return deps.tenant_echo(
        with_envelope_fields({"resolved": True, "approved": bool(req.approved)}),
        tenant,
    )


@router.get("/api/agent/audit")
def own_audit(limit: int = 100, tenant=Depends(deps.require_active_tenant)) -> Any:
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
