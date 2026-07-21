"""
Client-facing session routes (sessions wire spec, gap audit §2.1 /
CONTRACT-ADDENDUM sessions addendum). Matches ``console/converse.js``
byte-for-byte:

    POST /api/sessions                  -> get_or_create_session (idempotent)
    POST /api/sessions/{id}/messages    -> turn dispatch (turn_runner.start_turn)
    GET  /api/sessions/{id}/stream      -> SSE, event-per-frame, after_seq replay
    GET  /api/sessions/{id}/transcript  -> poll fallback, most-recent-N ascending

``POST /api/agent/approvals/{confirmation_id}`` is a SIBLING router
(routers/agent.py, S4 lane) — record-only approval decisions live there, not
here; this file never calls ``session_store.decide_approval``.

Ownership guard posture (matches routers/jobs.py's cross-tenant pattern,
deps.py's require_tenant doc): an unknown session_id and a real-but-foreign-
tenant session_id collapse to the IDENTICAL 404 session_not_found response —
no existence leak across the tenant boundary.

CONFIRM-SHAPE SEAM: the client's ``{confirm: {confirmationId, approved}}`` is
NOT the frozen ``ConverseTurnInput.confirm`` shape the harness expects — this
router is the ONE place that bridges them. It looks up the approval row
(created by turn_runner when the harness emitted confirmation_required) and
builds ``{confirmation_id, approved, proposal: {tool, params, capability}}``
from it before ever calling ``turn_runner.start_turn`` — the params/tool/
capability the harness resumes with come from the DURABLE row, never from the
client (the client only ever sends confirmationId + approved).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import deps
import entitlements
import session_store
import turn_runner
from envelopes import ErrorCode, error_obj, error_response, with_envelope_fields

router = APIRouter()

# SSE polling cadence (§2.1.3): cheap poll of the durable event log, a ": ping"
# comment to keep idle connections alive through proxies, and a bounded
# lifetime after which the client's own reconnect-with-after_seq logic takes
# over. Read as bare module globals INSIDE the generator (not captured as
# default args) so tests can monkeypatch them for a fast round-trip.
STREAM_POLL_S = 0.3
STREAM_PING_S = 15.0
STREAM_DEADLINE_S = 300.0

# GET .../transcript?limit= default + hard cap (session_store.recent_events
# also clamps internally; clamping here too keeps this route's own contract
# self-evident and independent of the store's internal choice).
TRANSCRIPT_DEFAULT_LIMIT = 200
TRANSCRIPT_MAX_LIMIT = 10000

# HTTP status a TurnRejected(status_code, ...) is allowed to carry, mapped to
# a sane `retryable` flag for the §10 error object (TurnRejected itself only
# carries status_code/error_code/message/extra — retryable is a presentation
# concern this router owns, mirroring author.py/tenant.py's own per-code
# retryable choices for the same underlying codes).
_RETRYABLE_BY_CODE = {
    ErrorCode.GRANT_REQUIRED: False,
    ErrorCode.LLM_QUOTA_EXHAUSTED: False,
    ErrorCode.LLM_RATE_LIMITED: True,
    ErrorCode.BROKER_UNREACHABLE: True,
    ErrorCode.SESSION_NOT_FOUND: False,
}


class CreateSessionRequest(BaseModel):
    drawing_id: str


class MessageRequest(BaseModel):
    text: Optional[str] = None
    confirm: Optional[Dict[str, Any]] = None
    classifier_hint: Optional[Dict[str, Any]] = None


def _session_not_found(session_id: str) -> JSONResponse:
    return error_response(
        ErrorCode.SESSION_NOT_FOUND, f"unknown session_id {session_id!r}", retryable=False,
    )


def _require_owned_session(session_id: str, tenant: Any) -> Optional[Dict[str, Any]]:
    """Look up session_id and enforce the tenant guard. Returns the session row
    on success, None on a 404-worthy miss (caller returns _session_not_found)."""
    sess = session_store.get_session(session_id)
    if sess is None or str(sess.get("tenant_id")) != str(tenant):
        return None
    return sess


def _turn_rejected_response(exc: "turn_runner.TurnRejected") -> JSONResponse:
    """TurnRejected -> HTTP response: exc.extra merged TOP-LEVEL (e.g.
    {'grant_required': True}) alongside the §10-valid `error` object."""
    retryable = _RETRYABLE_BY_CODE.get(exc.error_code, False)
    body = with_envelope_fields({
        **exc.extra,
        "error": error_obj(exc.error_code, exc.message, retryable=retryable),
    })
    return JSONResponse(status_code=exc.status_code, content=body)


# --------------------------------------------------------------------------- #
# POST /api/sessions
# --------------------------------------------------------------------------- #
@router.post("/api/sessions")
def create_session(req: CreateSessionRequest, tenant=Depends(deps.require_tenant)):
    """Idempotent per (tenant, drawing_id) — a repeat POST with the same
    drawing_id returns the SAME session (session_store's UNIQUE constraint +
    INSERT OR IGNORE, not re-derived here)."""
    sess = session_store.get_or_create_session(str(tenant), req.drawing_id)
    return deps.tenant_echo(
        with_envelope_fields({
            "session_id": sess["session_id"],
            "status": sess["status"],
            "created_at": sess["created_at"],
        }),
        tenant,
    )


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/messages
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, req: MessageRequest, tenant=Depends(deps.require_tenant)):
    # 1. ownership guard (404-not-403, no existence leak).
    if _require_owned_session(session_id, tenant) is None:
        return _session_not_found(session_id)

    # 2. exactly one of {text}|{confirm}.
    has_text = req.text is not None
    has_confirm = req.confirm is not None
    if has_text == has_confirm:  # neither set, or both set
        return error_response(
            ErrorCode.BAD_PARAMS, "exactly one of `text` or `confirm` is required",
            retryable=False, status_code=409,
        )

    # 3. entitlement gate (§17): mirrors routers/author.py:76-78 — the tenant's
    # tier must grant the `converse` capability. Off-auth/demo grants everything.
    tier = entitlements.resolve_tier(tenant)
    if not entitlements.entitlements_for(tier).get("converse", False):
        return entitlements.entitlement_denied_response("converse", tier)

    # 4. confirm path: resolve the durable approval row and build the frozen
    # ConverseTurnInput.confirm shape ourselves (see module docstring).
    confirm_payload: Optional[Dict[str, Any]] = None
    if has_confirm:
        confirmation_id = (req.confirm or {}).get("confirmationId")
        if not confirmation_id:
            return error_response(
                ErrorCode.BAD_PARAMS, "confirm.confirmationId is required",
                retryable=False, status_code=400,
            )
        approval = session_store.get_approval(confirmation_id)
        if approval is None or str(approval.get("tenant_id")) != str(tenant):
            # unknown / cross-tenant collapse to the SAME response (agent.py's
            # own /approvals precedent) — no existence leak.
            return error_response(
                ErrorCode.BAD_PARAMS, f"unknown confirmation_id {confirmation_id!r}",
                retryable=False, status_code=404,
            )
        if approval.get("expired"):
            return error_response(
                ErrorCode.CONFIRMATION_EXPIRED,
                f"confirmation_id {confirmation_id!r} has expired",
                retryable=False,
            )
        confirm_payload = {
            "confirmation_id": confirmation_id,
            "approved": bool((req.confirm or {}).get("approved")),
            "proposal": {
                "tool": approval.get("tool"),
                "params": approval.get("params"),
                "capability": approval.get("capability"),
            },
        }

    # 5. dispatch the turn.
    try:
        turn_id = turn_runner.start_turn(
            str(tenant), session_id,
            text=req.text, confirm=confirm_payload, classifier_hint=req.classifier_hint,
        )
    except turn_runner.TurnBusy:
        return error_response(
            ErrorCode.TURN_IN_PROGRESS,
            f"session {session_id!r} already has an active turn",
            retryable=True,
        )
    except turn_runner.TurnRejected as exc:
        return _turn_rejected_response(exc)

    return JSONResponse(
        status_code=202,
        content=deps.tenant_echo(
            with_envelope_fields({"turn_id": turn_id, "status": "started"}), tenant
        ),
    )


# --------------------------------------------------------------------------- #
# GET /api/sessions/{id}/stream (SSE)
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/stream")
def stream_session(session_id: str, after_seq: int = 0, tenant=Depends(deps.require_tenant)):
    """Event-per-frame SSE (the client's EventSource addEventListener's per
    type — see converse.js openStream): `event: {type}\\ndata: {envelope}\\n\\n`.
    404 guard runs BEFORE the generator is constructed (matches
    routers/jobs.py:120-126's precedent) so an unknown/foreign session never
    starts a stream at all."""
    if _require_owned_session(session_id, tenant) is None:
        return _session_not_found(session_id)

    def event_stream():
        cursor = int(after_seq)
        deadline = time.time() + STREAM_DEADLINE_S
        last_activity = time.time()
        while time.time() < deadline:
            events = session_store.events_after(session_id, cursor, limit=500)
            if events:
                for ev in events:
                    cursor = ev["seq"]
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                last_activity = time.time()
            else:
                now = time.time()
                if now - last_activity >= STREAM_PING_S:
                    yield ": ping\n\n"
                    last_activity = now
            time.sleep(STREAM_POLL_S)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# GET /api/sessions/{id}/transcript
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/transcript")
def get_transcript(session_id: str, limit: int = TRANSCRIPT_DEFAULT_LIMIT,
                   tenant=Depends(deps.require_tenant)):
    """Most-recent-N envelopes, ascending by seq — no after_seq cursor (§2.1.4:
    'most recent N, ascending by seq'). `limit` is clamped to
    [1, TRANSCRIPT_MAX_LIMIT] regardless of what the caller sends."""
    if _require_owned_session(session_id, tenant) is None:
        return _session_not_found(session_id)

    clamped = max(1, min(int(limit), TRANSCRIPT_MAX_LIMIT))
    events = session_store.recent_events(session_id, clamped)
    return with_envelope_fields({"events": events})
