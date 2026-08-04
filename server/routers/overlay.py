"""T1 overlay routes: propose a preview, decide it, read the live document.

These three are what make the T1 spine reachable. Before them the whole lane
was unreachable code: a registry with no callers, a decision path with no
proposals, an operator card wired to nothing.

THE SPLIT THAT MATTERS. Proposing is cheap and open; deciding is the gate.

  POST /api/overlay/proposals   the requester's own session opens a preview.
                                No confirmation: it writes one session-scoped
                                row, changes nothing another user sees, and
                                expires by lease.
  POST /api/overlay/decisions   the OPERATOR applies or refuses it. This is the
                                irreversible half, so it carries the CAS
                                witness the card was rendered against and an
                                idempotency key, and it writes an audit row.
  GET  /api/overlay             what this session should be rendering right
                                now: the tenant document plus its own pending
                                preview, already resolved.

WHY THE DECIDE ROUTE TAKES BOTH `approve` AND `document_version`. An operator
tap that omitted the version could land against a tenant state they were never
shown, which is exactly the defect a review found in `deny()`. The route does
not default it: a caller that cannot say which version it saw has no business
deciding.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import deps
import overlay_propose
import overlay_registry
from envelopes import ErrorCode, error_obj

router = APIRouter()


def _fail(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": error_obj(ErrorCode.BAD_PARAMS, message, detail=code),
                 "degraded_mode": False},
    )


def _store():
    """The platform store, imported lazily.

    Lazily because the repo-root `platform/` package shadows the stdlib module
    of the same name, and importing it at module scope drags that ordering
    problem into every consumer of this router.
    """
    from platform import overlay_store  # noqa: PLC0415
    return overlay_store


class ProposeBody(BaseModel):
    tokens: Dict[str, str] = Field(..., description="token id -> requested value")
    request_text: str = ""
    session_id: str


class DecideBody(BaseModel):
    proposal_id: str
    approve: bool
    decision_key: str = Field(..., min_length=8)
    document_version: int


@router.post("/api/overlay/proposals")
def propose_overlay(body: ProposeBody, tenant=Depends(deps.require_tenant)) -> Any:
    """Open a pending preview for one session."""
    store = _store()
    try:
        out = overlay_propose.propose(
            tenant_id=str(tenant),
            session_id=body.session_id,
            requested_tokens=body.tokens,
            request_text=body.request_text,
            store=store,
            current_document=store.document(str(tenant)),
            defaults=overlay_registry.defaults(),
        )
    except overlay_propose.OverlayProposeError as exc:
        return _fail(exc.code, exc.detail or exc.code, exc.status_code)
    except Exception as exc:  # noqa: BLE001 - store raises its own error type
        code = getattr(exc, "code", "overlay_unavailable")
        return _fail(code, str(getattr(exc, "detail", exc))[:200],
                     int(getattr(exc, "status_code", 409)))

    proposal = out["proposal"]
    return {
        "proposal_id": proposal["proposal_id"],
        "tokens": out["tokens"],
        "expires_at": str(proposal.get("lease_expires_at") or ""),
        # The witness the operator card must send back. Handed over here so the
        # card never has to guess which version it is deciding against.
        "document_version": int(store.document(str(tenant)).get("version", 0) or 0),
        "error": None,
        "degraded_mode": False,
    }


@router.post("/api/overlay/decisions")
def decide_overlay(body: DecideBody,
                   x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
                   tenant=Depends(deps.require_tenant)) -> Any:
    """Apply or refuse a pending preview. The operator's single tap."""
    actor = (x_actor or "").strip()
    if not actor:
        # Every runtime mutation must be attributable. The decision path
        # refuses a blank actor too; failing here gives the caller a usable
        # message instead of a 500 from deeper down.
        return _fail("actor_required", "X-Actor is required to decide", 400)

    store = _store()
    try:
        if body.approve:
            proposal, document = store.approve(
                proposal_id=body.proposal_id, actor=actor,
                decision_key=body.decision_key,
                expected_version=body.document_version)
        else:
            proposal = store.deny(
                proposal_id=body.proposal_id, actor=actor,
                decision_key=body.decision_key,
                expected_version=body.document_version)
            document = store.document(str(tenant))
    except Exception as exc:  # noqa: BLE001 - OverlayStoreError carries the code
        code = getattr(exc, "code", "decision_failed")
        return _fail(code, str(getattr(exc, "detail", exc))[:200],
                     int(getattr(exc, "status_code", 409)))

    return {
        "proposal_id": proposal["proposal_id"],
        "state": proposal["state"],
        "document_version": int(document.get("version", 0) or 0),
        "error": None,
        "degraded_mode": False,
    }


@router.get("/api/overlay")
def read_overlay(session_id: Optional[str] = None,
                 tenant=Depends(deps.require_tenant)) -> Any:
    """What this session should render right now.

    Returns the RESOLVED token map (tenant document with the session's own
    pending preview layered on top) rather than the two separately, so a client
    cannot get the precedence wrong. The version is the tenant document's, not
    the preview's: the preview is not a committed state and has no version to
    decide against.
    """
    store = _store()
    document = store.document(str(tenant))
    tokens = store.effective_tokens(str(tenant), session_id)
    pending = (store.pending_for_session(str(tenant), session_id)
               if session_id else None)
    return {
        "tokens": tokens,
        "document_version": int(document.get("version", 0) or 0),
        "pending_proposal_id": (pending or {}).get("proposal_id"),
        "css": overlay_registry.render_css_vars(tokens),
        "error": None,
        "degraded_mode": False,
    }
