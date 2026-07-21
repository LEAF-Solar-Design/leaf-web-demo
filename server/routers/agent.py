"""
POST /api/agent/approvals/{confirmation_id} -- record-only approval decisions
(sessions wire spec, gap audit §2.1.5 / CONTRACT-ADDENDUM sessions addendum).

This endpoint ONLY records an approve/reject decision against the durable
`approvals` row created by the turn engine (S3) when it emitted
`confirmation_required`. It NEVER starts (or resumes) a turn itself -- the
client always follows up with a separate `POST /sessions/{id}/messages
{confirm:{confirmationId, approved}}` to actually resume the paused turn
(spec §2.1.5: "This call only records the decision -- must NOT itself start
the resume turn").

Tenant + existence guard (no existence leak): an unknown confirmation_id OR
one that belongs to a different tenant than the caller both collapse to the
SAME 404 bad_params response -- a cross-tenant caller can't distinguish
"doesn't exist" from "exists but isn't yours".

Outcomes (session_store.decide_approval):
  'recorded'         -> 200 {resolved: true, approved} + append_event(
                        confirmation_resolved) into the approval's OWN
                        session log (confirmation_resolved is cross-turn --
                        reconciled client-side by confirmation_id regardless
                        of which turn originally raised it, per spec §2.1.3).
  'already_decided'  -> 409 bad_params (client maps this to 'approval_stale',
                        swallows it, and still proceeds to POST the confirm
                        message since the decision is safely already there).
  'not_found'        -> 404 bad_params (only reachable via a race between our
                        own existence check and decide_approval -- the row
                        vanished in between; identical shape to the upfront
                        not-found case, so no separate error surface).

Deliberately does NOT check the approval's `expired` flag here -- an
expired-but-undecided approval is still recorded. The 410
confirmation_expired belongs to the SUBSEQUENT /messages{confirm} call, per
§2.1.5, never to this one.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import deps
import session_store
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


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
                     tenant=Depends(deps.require_tenant)):
    """Record-only: approve/reject a pending confirmation. Never starts a turn."""
    approval = session_store.get_approval(confirmation_id)
    if approval is None or approval["tenant_id"] != str(tenant):
        # Unknown and cross-tenant collapse to the IDENTICAL response -- no
        # existence leak to a caller who isn't the owning tenant.
        return _not_found(confirmation_id)

    outcome = session_store.decide_approval(confirmation_id, req.approved, by=str(tenant))
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
        {"confirmation_id": confirmation_id, "approved": bool(req.approved), "by": str(tenant)},
    )
    return deps.tenant_echo(
        with_envelope_fields({"resolved": True, "approved": bool(req.approved)}),
        tenant,
    )
