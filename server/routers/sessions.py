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

APPROVAL CONSUME (merge-gate finding #1): the client's ``confirm.approved``
boolean is NEVER trusted on its own — this path calls
``session_store.consume_approval(confirmation_id, session_id, tenant_id)``,
ONE atomic locked check-then-set that verifies the row belongs to THIS
session+tenant, has actually been decided (via the separate
``POST /api/agent/approvals/{confirmation_id}`` call, routers/agent.py), is
not expired, and has not already been consumed by a prior confirm — then
returns the DURABLY STORED ``approved`` value, which is what gets wired into
the ``ConverseTurnInput.confirm`` sent to the harness. A client that skips
the approvals call, replays a confirm, reuses another session's
confirmation_id, or sends ``approved: true`` against a row that was actually
decided ``approved: false`` all fail closed. Wire-compat: console/converse.js
(leaf_website/console/converse.js)'s ``approve()`` ALWAYS POSTs
``/api/agent/approvals/{confirmationId}`` (which durably records the decision)
BEFORE its caller sends the confirm message via ``postMessage({confirm})`` —
see that file's ``approve()``/``postMessage()`` comments — so requiring
``decided`` here is compatible with every real client.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import deps
import entitlements
import session_store
import turn_runner
from envelopes import ErrorCode, error_obj, error_response, with_envelope_fields

router = APIRouter()


# --------------------------------------------------------------------------- #
# "Mount your LLM" input validation (model allowlist + BYO credential hygiene).
# --------------------------------------------------------------------------- #
def _invalid_model_response(model: Any) -> JSONResponse:
    allowed = ", ".join(sorted(turn_runner.ALLOWED_MODELS))
    return error_response(
        ErrorCode.BAD_PARAMS,
        f"model {model!r} is not allowed; choose one of: {allowed}",
        retryable=False, status_code=400,
    )


# A credential must actually LOOK like one. Accepting any non-empty string made
# downstream redaction unwinnable: the harness scrubs the grant out of the
# transcript by literal match, so a credential of "error", "a" or '"' would
# either corrupt ordinary text and protocol values or have to be left in place
# (sol-critic PR #117 round 4, blockers 2 and 3). Real Agent SDK credentials are
# long, opaque, and whitespace-free — `sk-ant-...` keys and OAuth tokens run to
# ~100 chars — so this floor rejects nothing genuine while removing every
# pathological value at the boundary, where it is cheap and unambiguous.
_MIN_CREDENTIAL_LEN = 24

# PRINTABLE ASCII, no space. Deliberately NOT "not str.isspace()": Python's
# isspace() and JavaScript's \s disagree (U+FEFF is whitespace to \s but not to
# isspace()), so such a credential was ACCEPTED here yet treated as unredactable
# by harness/src/redact.ts — accepted but unstrippable is the worst of both.
# Keep this rule identical to that file's PRINTABLE_ASCII.
# (sol-critic PR #123 rounds 6-8.)
_CREDENTIAL_CHARS = frozenset(chr(c) for c in range(0x21, 0x7F))


def _valid_credential_value(tok: Any) -> bool:
    return (
        isinstance(tok, str)
        and len(tok) >= _MIN_CREDENTIAL_LEN
        and all(ch in _CREDENTIAL_CHARS for ch in tok)
    )


def _validate_credential_grant(raw: Any) -> Optional[Dict[str, Any]]:
    """Return the normalized BYO Agent SDK credential grant, or None if the shape
    is invalid. Accepts EXACTLY {kind:'api_key', api_key:<credential>} or
    {kind:'oauth', oauth_token:<credential>}; extra keys are dropped. A
    <credential> is a whitespace-free string of at least _MIN_CREDENTIAL_LEN
    characters (see above). The token VALUE is never logged here (nothing in this
    module prints it)."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "api_key":
        tok = raw.get("api_key")
        if _valid_credential_value(tok):
            return {"kind": "api_key", "api_key": tok}
    elif kind == "oauth":
        tok = raw.get("oauth_token")
        if _valid_credential_value(tok):
            return {"kind": "oauth", "oauth_token": tok}
    return None


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _credential_insecure_transport(request: Request) -> bool:
    """True when a BYO credential must be REJECTED because the request did not
    arrive over TLS.

    X-Forwarded-Proto is CALLER-CONTROLLED unless a trusted proxy actually sets
    it. docker-compose publishes this app straight to :8130 with nothing in
    front, so honoring that header unconditionally would let anyone bypass the
    gate by adding one line to a plaintext request (sol-critic review of PR #117,
    blocker 1). The header is therefore consulted ONLY when the deployment
    asserts it sits behind a proxy that overwrites it, via
    LEAF_TRUST_FORWARDED_PROTO=1. Unset (the default, including docker-compose)
    means the real transport decides and the header is ignored.

    LEAF_ALLOW_INSECURE_CREDENTIAL=1 remains the explicit dev/test opt-out.
    Both flags fail CLOSED: absent or unparsable -> enforce TLS on the real
    connection."""
    if _truthy_env("LEAF_ALLOW_INSECURE_CREDENTIAL"):
        return False
    scheme = request.url.scheme.lower()
    if _truthy_env("LEAF_TRUST_FORWARDED_PROTO"):
        forwarded = request.headers.get("x-forwarded-proto", "")
        if forwarded:
            scheme = forwarded.split(",")[0].strip().lower()
    return scheme != "https"

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
    # Per-session "mount your LLM" model choice (persisted). Validated against the
    # allowlist; overrides the runner env default for this session's turns.
    model: Optional[str] = None


class MessageRequest(BaseModel):
    text: Optional[str] = None
    confirm: Optional[Dict[str, Any]] = None
    classifier_hint: Optional[Dict[str, Any]] = None
    # Per-turn model override (validated); falls back to the session's stored model.
    model: Optional[str] = None
    # Optional bring-your-own Agent SDK credential for THIS turn only. Ephemeral:
    # validated for shape, forwarded over TLS, never persisted, never logged.
    credential_grant: Optional[Dict[str, Any]] = None


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
    INSERT OR IGNORE, not re-derived here). An optional `model` (validated against
    the allowlist) is persisted as the session's per-session model choice."""
    if req.model is not None and not turn_runner.is_allowed_model(req.model):
        return _invalid_model_response(req.model)
    sess = session_store.get_or_create_session(str(tenant), req.drawing_id, req.model)
    return deps.tenant_echo(
        with_envelope_fields({
            "session_id": sess["session_id"],
            "status": sess["status"],
            "created_at": sess["created_at"],
            "model": sess.get("model"),
        }),
        tenant,
    )


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/messages
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, req: MessageRequest, request: Request,
                 tenant=Depends(deps.require_tenant)):
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

    # 2b. "Mount your LLM" input hygiene — validated BEFORE the confirm consume,
    # so a bad model or credential never burns an approval. Model:
    # allowlist-checked. Credential: TLS-gated (a BYO token may only ride an
    # encrypted request) and shape-checked; the validated (never the raw) grant
    # is forwarded to the turn.
    #
    # This orders THESE inputs ahead of the consume. It is not a general
    # no-burn guarantee: `consume_approval` below still runs before
    # `start_turn`'s busy CAS, so a confirm that races a concurrent turn is
    # consumed and then answered 409, and the retry fails as already-consumed.
    # That window predates this feature (e064086) and is tracked separately.
    if req.model is not None and not turn_runner.is_allowed_model(req.model):
        return _invalid_model_response(req.model)
    validated_grant: Optional[Dict[str, Any]] = None
    if req.credential_grant is not None:
        if _credential_insecure_transport(request):
            return error_response(
                ErrorCode.BAD_PARAMS,
                "credential_grant requires a TLS (https) request",
                retryable=False, status_code=400,
            )
        validated_grant = _validate_credential_grant(req.credential_grant)
        if validated_grant is None:
            return error_response(
                ErrorCode.BAD_PARAMS,
                "credential_grant must be {kind:'api_key',api_key} or "
                "{kind:'oauth',oauth_token}",
                retryable=False, status_code=400,
            )

    # 3. entitlement gate (§17): mirrors routers/author.py:76-78 — the tenant's
    # tier must grant the `converse` capability. Off-auth/demo grants everything.
    tier = entitlements.resolve_tier(tenant)
    try:
        allowed = entitlements.entitlements_for(tier).get("converse", False)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("converse", tier)
    if not allowed:
        return entitlements.entitlement_denied_response("converse", tier)

    # 4. confirm path: atomically verify-and-consume the durable approval row
    # (merge-gate finding #1 — see module docstring's APPROVAL CONSUME note)
    # and build the frozen ConverseTurnInput.confirm shape ourselves, using
    # the STORED approved value — never the client's req.confirm.approved,
    # which is read here only to validate the confirm shape, not trusted.
    confirm_payload: Optional[Dict[str, Any]] = None
    if has_confirm:
        confirmation_id = (req.confirm or {}).get("confirmationId")
        if not confirmation_id:
            return error_response(
                ErrorCode.BAD_PARAMS, "confirm.confirmationId is required",
                retryable=False, status_code=400,
            )
        try:
            approval = session_store.consume_approval(confirmation_id, session_id, str(tenant))
        except session_store.ApprovalConsumeError as exc:
            if exc.reason == "not_found":
                # unknown / cross-session / cross-tenant collapse to the SAME
                # response (agent.py's own /approvals precedent) — no
                # existence leak.
                return error_response(
                    ErrorCode.BAD_PARAMS, f"unknown confirmation_id {confirmation_id!r}",
                    retryable=False, status_code=404,
                )
            if exc.reason == "undecided":
                # client is trying to confirm without ever having called
                # POST /api/agent/approvals first — same (409, BAD_PARAMS)
                # shape converse.js's classifyAgentError() already reads as
                # 'approval_stale' for the already-decided case.
                return error_response(
                    ErrorCode.BAD_PARAMS,
                    f"confirmation_id {confirmation_id!r} has not been decided",
                    retryable=False, status_code=409,
                )
            if exc.reason == "already_consumed":
                return error_response(
                    ErrorCode.BAD_PARAMS,
                    f"confirmation_id {confirmation_id!r} was already consumed",
                    retryable=False, status_code=409,
                )
            # exc.reason == "expired"
            return error_response(
                ErrorCode.CONFIRMATION_EXPIRED,
                f"confirmation_id {confirmation_id!r} has expired",
                retryable=False,
            )
        confirm_payload = {
            "confirmation_id": confirmation_id,
            "approved": bool(approval.get("approved")),  # STORED value, never the client's
            "proposal": {
                "tool": approval.get("tool"),
                "params": approval.get("params"),
                "capability": approval.get("capability"),
            },
        }

    # 5. dispatch the turn.
    try:
        turn_id = turn_runner.start_turn(
            tenant, session_id,
            text=req.text, confirm=confirm_payload, classifier_hint=req.classifier_hint,
            model=req.model, credential_grant=validated_grant,
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
