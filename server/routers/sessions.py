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
builds ``{confirmation_id, approved, proposal: {tool, params, dwg?, capability}}``
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

APPROVAL GIVE-BACK: the consume above necessarily runs BEFORE the turn's busy
compare-and-swap, because that CAS lives inside ``turn_runner.start_turn``
(``session_store.try_begin_turn``) and start_turn is one call. So a confirm
that races a concurrent turn gets consumed and then answered 409
TURN_IN_PROGRESS — and without a give-back the retry that 409 explicitly
invites could only ever fail ``already_consumed``, destroying the user's one
approval. The ``TurnBusy`` handler therefore calls
``session_store.unconsume_approval``.

The rule is NOT "roll back on TurnBusy". It is: **roll back exactly when the
harness provably never saw the turn, and never otherwise.** Two sites qualify.

``TurnBusy`` means try_begin_turn lost the CAS, which is the first thing
start_turn does after the session guard — no ``turn_started`` event was
appended, no request reached the harness, so nothing anywhere redeemed the
approval and giving it back cannot produce a second redemption.

``TurnRejected`` qualifies ONLY when it carries ``pre_harness`` (turn_runner
sets it where no POST was attempted, or the connection was never established).
Those legs answer ``BROKER_UNREACHABLE``, which ``_RETRYABLE_BY_CODE`` marks
retryable — so skipping the give-back there would invite a retry against an
approval already spent, which is the same defect this note exists to fix.
``pre_harness`` DEFAULTS TO FALSE, so every ambiguous leg still refuses to roll
back: on a read timeout, or a rejection the harness itself returned, the
harness may already be executing the tool call, and un-spending that approval
is precisely the double-execution consume-once exists to prevent.

Single redemption is preserved in both directions: a confirm whose turn really
started still replays into ``already_consumed`` (see
tests/test_sessions_routes.py::test_messages_confirm_busy_gives_the_approval_back).

When a give-back does not succeed the approval stays spent, and the response
says so rather than advertising a retry that could only fail — see
``_busy_response``/``_turn_rejected_response``'s ``approval_recovered``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import deps
import emf_metrics
import entitlements
import instant_execution
import session_policy
import session_store
import turn_runner
from envelopes import (ErrorCode, err_envelope, error_obj, error_response,
                       with_envelope_fields)

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
    <credential> is at least _MIN_CREDENTIAL_LEN characters and entirely
    PRINTABLE ASCII (_CREDENTIAL_CHARS, 0x21-0x7E) — no space, no control
    character, no non-ASCII codepoint. The token VALUE is never logged here
    (nothing in this module prints it)."""
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
    # Per-session approval policy (session_policy.POLICIES). Absent (default)
    # leaves the stored policy untouched — a repeat idempotent POST without the
    # field never resets an earlier choice. confirm_all is the implicit default.
    policy: Optional[str] = None


class MessageRequest(BaseModel):
    text: Optional[str] = None
    confirm: Optional[Dict[str, Any]] = None
    classifier_hint: Optional[Dict[str, Any]] = None
    # Per-turn model override (validated); falls back to the session's stored model.
    model: Optional[str] = None
    # Optional bring-your-own Agent SDK credential for THIS turn only. Ephemeral:
    # validated for shape, forwarded over TLS, never persisted, never logged.
    credential_grant: Optional[Dict[str, Any]] = None
    # OPT-IN busy-turn queue (cap 1): when the session is mid-turn, park this
    # text prompt and start it at the active turn's terminal event instead of
    # answering 409. Opt-in keeps deployed clients byte-identical: absent (the
    # default), a busy session answers exactly the 409 it always has. Text-only:
    # a confirm or a credential_grant with queue=true is a 400, never queued.
    queue: Optional[bool] = False


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


def _give_back_unredeemed_approval(confirmation_id: str, session_id: str,
                                   tenant_id: str) -> bool:
    """Return a consumed-but-never-redeemed approval to the shelf (see the
    module docstring's APPROVAL GIVE-BACK note for when this is legal).

    Returns True IFF the approval is genuinely back on the shelf. A raise and a
    False both mean it is still spent, and the CALLER MUST NOT then tell the
    client to retry — the retry could only fail `already_consumed`. Callers
    thread this through to `_busy_response`/`_turn_rejected_response` so the
    answer stays honest about what the client can actually do next.

    A store failure is not re-raised: the turn genuinely is busy (or rejected),
    and a 500 would replace an accurate status with a misleading one. It is
    reported instead — loudly to stderr for the operator, and truthfully to the
    client via `approval_recovered: false`. The confirmation_id is a
    server-issued opaque id the client already holds — not a secret."""
    try:
        recovered = session_store.unconsume_approval(
            confirmation_id, session_id, tenant_id)
    except Exception as exc:  # noqa: BLE001
        _report_give_back_failure(
            "raised", confirmation_id, session_id,
            detail=f"{type(exc).__name__}: {exc}")
        return False
    if not recovered:
        # No row moved 1 -> 0. We consumed it moments ago, so this means the
        # row is not in the state we left it in — never silently swallow it.
        _report_give_back_failure("not_released", confirmation_id, session_id)
    return recovered


def _report_give_back_failure(reason: str, confirmation_id: str, session_id: str,
                              detail: str = "") -> None:
    """Report a destroyed approval on BOTH channels, and never raise.

    A failed give-back means the user's single approval is gone, with no
    symptom the user or an operator would otherwise see — the highest-priority
    kind of bug to leave silent. So it goes out twice: a human-readable stderr
    line for log reading, and an ApprovalGiveBackFailed EMF metric that
    CloudWatch extracts so it can actually be ALARMED on (a stderr string
    cannot be). Reporting is best-effort — this runs while we are already
    answering an error, and must not replace that answer with a 500."""
    try:
        print(
            f"[leaf-agent] approval give-back FAILED ({reason}) confirmation_id="
            f"{confirmation_id!r} session={session_id!r}"
            f"{' error=' + detail if detail else ''} — the approval is still "
            f"spent and this client must obtain a NEW approval",
            file=sys.stderr, flush=True,
        )
    except Exception:  # noqa: BLE001  # pragma: no cover
        pass
    try:
        emf_metrics.emit_approval_give_back_failed(
            reason, confirmation_id=confirmation_id, session_id=session_id)
    except Exception:  # noqa: BLE001  # pragma: no cover
        pass


def _turn_rejected_response(exc: "turn_runner.TurnRejected",
                            approval_lost: bool = False) -> JSONResponse:
    """TurnRejected -> HTTP response: exc.extra merged TOP-LEVEL (e.g.
    {'grant_required': True}) alongside the §10-valid `error` object.

    `approval_lost` says a confirm's approval was consumed and could NOT be
    given back. It forces `retryable` false and surfaces
    `approval_recovered: false`, because retrying with the same confirmation_id
    can then only fail `already_consumed` — see `_busy_response`."""
    retryable = _RETRYABLE_BY_CODE.get(exc.error_code, False)
    if approval_lost:
        retryable = False
    body = with_envelope_fields({
        **exc.extra,
        **({"approval_recovered": False} if approval_lost else {}),
        "error": error_obj(exc.error_code, exc.message, retryable=retryable),
    })
    return JSONResponse(status_code=exc.status_code, content=body)


def _busy_response(session_id: str, approval_lost: bool = False) -> JSONResponse:
    """The 409 TURN_IN_PROGRESS answer, told honestly.

    Normally this is retryable: the turn is busy, and the confirm's approval
    (if any) went back on the shelf, so the retry can really succeed.

    When `approval_lost` is set the give-back did NOT happen, so the approval
    stays spent — and `turn_in_progress` becomes the WRONG answer, not merely
    an incomplete one. The client classifies that code as `busy`
    (web/src/converse.js classifyAgentError) and tells the user to wait, but
    waiting can never help: the approval is gone, and every retry of this
    confirmation_id can only fail `already_consumed`.

    So the lost case answers `409 BAD_PARAMS`, which that same classifier
    already maps to `approval_stale` -> "That request was already decided — ask
    the assistant to propose it again" (web/src/components/ConversePanel.jsx).
    That is exactly the user's real next step, delivered through an error shape
    the client ALREADY handles — no frontend change, and no new field for
    clients to learn. `approval_recovered: false` rides along for machine
    consumers. (sol-critic round 2, blocker 3.)

    The ordinary busy response is untouched: same code, same `retryable: true`,
    byte-identical body. It routes through ``error_response`` — and the lost
    case through the same ``err_envelope`` builder — because this 409 is a §3
    run envelope carrying `ok`/`tool`/`version`/`result`/`overlay`/`timing_ms`/
    `cost` alongside `error`. Hand-building it from ``with_envelope_fields``
    silently DROPS those seven fields (pinned by
    tests/test_sessions_routes.py::test_messages_busy_response_keeps_the_full_run_envelope)."""
    if not approval_lost:
        return error_response(
            ErrorCode.TURN_IN_PROGRESS,
            f"session {session_id!r} already has an active turn",
            retryable=True,
        )
    body = err_envelope(
        ErrorCode.BAD_PARAMS,
        f"session {session_id!r} already has an active turn, and this "
        f"confirmation could not be returned — request a new approval",
        retryable=False,
    )
    body["approval_recovered"] = False
    return JSONResponse(status_code=409, content=body)


# --------------------------------------------------------------------------- #
# POST /api/sessions
# --------------------------------------------------------------------------- #
@router.post("/api/sessions")
def create_session(req: CreateSessionRequest, tenant=Depends(deps.require_active_tenant)):
    """Idempotent per (tenant, drawing_id) — a repeat POST with the same
    drawing_id returns the SAME session (session_store's UNIQUE constraint +
    INSERT OR IGNORE, not re-derived here). An optional `model` (validated against
    the allowlist) is persisted as the session's per-session model choice."""
    if req.model is not None and not turn_runner.is_allowed_model(req.model):
        return _invalid_model_response(req.model)
    if req.policy is not None and not session_policy.is_valid_policy(req.policy):
        allowed = ", ".join(sorted(session_policy.POLICIES))
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"policy {req.policy!r} is not allowed; choose one of: {allowed}",
            retryable=False, status_code=400,
        )
    sess = session_store.get_or_create_session(str(tenant), req.drawing_id, req.model)
    if req.policy is not None:
        session_policy.set_policy(sess["session_id"], str(tenant), req.policy)
    instant = instant_execution.prepare_session(
        str(tenant), sess["session_id"], req.drawing_id,
    )
    return deps.tenant_echo(
        with_envelope_fields({
            "session_id": sess["session_id"],
            "status": sess["status"],
            "created_at": sess["created_at"],
            "model": sess.get("model"),
            "policy": session_policy.get_policy(sess["session_id"], str(tenant)),
            # Safe readiness only. The executor endpoint and signed lease stay
            # on the authenticated app-to-harness back-edge.
            "instant_ready": bool(instant["ready"]),
            "instant_reason": instant["reason"],
        }),
        tenant,
    )


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/messages
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, req: MessageRequest, request: Request,
                 tenant=Depends(deps.require_active_tenant)):
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

    # 2a. queue eligibility — decided BEFORE the confirm consume below, so an
    # ineligible queue request never burns an approval. Text-only by design:
    # a queued confirm would hold a consumed approval across an unbounded wait
    # (everything the give-back machinery exists to prevent), and a queued
    # credential_grant would persist a value whose whole contract is
    # "never persisted, never logged".
    if req.queue:
        if has_confirm:
            return error_response(
                ErrorCode.BAD_PARAMS, "confirm turns cannot be queued",
                retryable=False, status_code=400,
            )
        if req.credential_grant is not None:
            return error_response(
                ErrorCode.BAD_PARAMS, "credential_grant turns cannot be queued",
                retryable=False, status_code=400,
            )

    # 2b. "Mount your LLM" input hygiene — validated BEFORE the confirm consume,
    # so a bad model or credential never burns an approval. Model:
    # allowlist-checked. Credential: TLS-gated (a BYO token may only ride an
    # encrypted request) and shape-checked; the validated (never the raw) grant
    # is forwarded to the turn.
    #
    # `consume_approval` below still runs BEFORE `start_turn`'s busy CAS — the
    # CAS is the real busy gate and it lives inside start_turn — so a confirm
    # that races a concurrent turn IS consumed and then answered 409. The
    # TurnBusy handler at the bottom of this function gives that consume back
    # (see APPROVAL GIVE-BACK in the module docstring), which is what makes the
    # retryable 409 actually retryable.
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
    # Hoisted so the dispatch step below can give an unredeemed consume back.
    # Every failure between here and that step returns immediately, so by the
    # time `start_turn` is called this is non-None IFF a consume succeeded.
    confirmation_id: Optional[str] = None
    if has_confirm:
        confirmation_id = (req.confirm or {}).get("confirmationId")
        if not confirmation_id:
            return error_response(
                ErrorCode.BAD_PARAMS, "confirm.confirmationId is required",
                retryable=False, status_code=400,
            )
        try:
            approval = session_store.consume_approval(
                confirmation_id, session_id, str(tenant),
                decided_by=getattr(tenant, "subject", None),
            )
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
        proposal = {
            "tool": approval.get("tool"),
            "params": approval.get("params"),
            "capability": approval.get("capability"),
        }
        stored_payload = approval.get("payload")
        stored_dwg = (
            stored_payload.get("dwg")
            if isinstance(stored_payload, dict)
            else None
        )
        if approval.get("tool") and not (
            isinstance(stored_dwg, str) and stored_dwg
        ):
            # Pre-binding approvals cannot be upgraded safely. Falling back to
            # the session drawing would authorize a target the stored row never
            # named. The approval has already been consumed, so the only safe
            # recovery is a fresh proposal with an explicit server-stored dwg.
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"confirmation_id {confirmation_id!r} has no stored drawing; "
                "request a new approval",
                retryable=False,
                status_code=409,
            )
        if isinstance(stored_dwg, str):
            # Server-stored proposal truth only. The confirm request carries no
            # drawing field, so a client cannot retarget an approved action.
            proposal["dwg"] = stored_dwg
        confirm_payload = {
            "confirmation_id": confirmation_id,
            "approved": bool(approval.get("approved")),  # STORED value, never the client's
            "proposal": proposal,
        }

    # 5. dispatch the turn.
    try:
        turn_id = turn_runner.start_turn(
            tenant, session_id,
            text=req.text, confirm=confirm_payload, classifier_hint=req.classifier_hint,
            model=req.model, credential_grant=validated_grant,
        )
    except turn_runner.TurnBusy:
        # OPT-IN queue: a text prompt with queue=true parks (cap 1) instead of
        # bouncing. Step 2a already refused confirm/grant shapes, so this
        # branch never holds an approval (confirmation_id is None on the text
        # path) and never persists a credential.
        if req.queue and has_text:
            try:
                q_status, queued_id = turn_runner.try_enqueue_turn(
                    tenant, session_id, text=req.text,
                    classifier_hint=req.classifier_hint, model=req.model)
            except turn_runner.TurnRejected as exc:
                return _turn_rejected_response(exc)
            if q_status == "queued":
                return JSONResponse(
                    status_code=202,
                    content=deps.tenant_echo(
                        with_envelope_fields(
                            {"status": "queued", "queued_id": queued_id}),
                        tenant,
                    ),
                )
            # q_status == "full": one prompt is already parked — fall through
            # to the byte-identical busy 409 ("a second is refused").
        # The approval (if any) was consumed at step 4 but NOTHING redeemed it:
        # TurnBusy means try_begin_turn lost the CAS, which happens before
        # start_turn appends `turn_started` or calls the harness. Give it back
        # so the retry this 409 explicitly invites can actually succeed.
        approval_lost = False
        if confirmation_id is not None:
            approval_lost = not _give_back_unredeemed_approval(
                confirmation_id, session_id, str(tenant))
        return _busy_response(session_id, approval_lost=approval_lost)
    except turn_runner.TurnRejected as exc:
        # Same rule, second site: give the approval back on every rejection
        # the engine can PROVE happened before the harness saw the turn
        # (exc.pre_harness — e.g. no harness URL configured, or the connection
        # was never established). Those legs report BROKER_UNREACHABLE, which
        # _RETRYABLE_BY_CODE marks retryable, so without this the client is
        # told to retry an approval that has already been spent — the very bug
        # the TurnBusy path fixes. `pre_harness` defaults False, so ambiguous
        # legs (read timeout, an actual harness rejection) still never roll
        # back; see the module docstring's APPROVAL GIVE-BACK note.
        approval_lost = False
        if exc.pre_harness and confirmation_id is not None:
            approval_lost = not _give_back_unredeemed_approval(
                confirmation_id, session_id, str(tenant))
        return _turn_rejected_response(exc, approval_lost=approval_lost)

    return JSONResponse(
        status_code=202,
        content=deps.tenant_echo(
            with_envelope_fields({"turn_id": turn_id, "status": "started"}), tenant
        ),
    )


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/turns/{turn_id}/cancel
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{session_id}/turns/{turn_id}/cancel")
def cancel_turn(session_id: str, turn_id: str, tenant=Depends(deps.require_tenant)):
    """Interrupt the session's active turn (the composer's Esc / Stop).

    Terminal state is `turn_complete{stop_reason:"interrupted"}` — the SAME
    event the client already renders, so no new wire type is introduced and a
    stale client simply sees the turn end. Cancelling a turn that is not the
    session's current active turn is a 409 rather than a silent success: a
    client holding a stale turn_id must not be able to end the turn that
    replaced it.
    """
    # ownership guard first (404-not-403, no existence leak) — mirrors post_message.
    if _require_owned_session(session_id, tenant) is None:
        return _session_not_found(session_id)

    try:
        outcome = turn_runner.request_cancel(str(tenant), session_id, turn_id)
    except turn_runner.TurnRejected as exc:
        return _turn_rejected_response(exc)

    if outcome == "not_active":
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"turn {turn_id!r} is not the active turn for session {session_id!r}",
            retryable=False, status_code=409,
        )

    if outcome == "not_cancellable":
        # A checkpoint restore holds the slot. It is not a turn and releases
        # itself; cancelling it would append a false terminal event and let a
        # real turn start while the restore is still writing the drawing.
        return error_response(
            ErrorCode.TURN_IN_PROGRESS,
            f"a checkpoint restore is in progress on session {session_id!r}; "
            f"it cannot be cancelled and will release shortly",
            retryable=True, status_code=409,
        )

    return JSONResponse(
        status_code=202,
        content=deps.tenant_echo(
            with_envelope_fields({"turn_id": turn_id, "status": "cancelled"}), tenant
        ),
    )


# --------------------------------------------------------------------------- #
# GET /api/sessions/{id}/stream (SSE)
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/stream")
async def stream_session(session_id: str, after_seq: int = 0,
                         tenant=Depends(deps.require_active_tenant)):
    """Event-per-frame SSE (the client's EventSource addEventListener's per
    type — see converse.js openStream): `event: {type}\\ndata: {envelope}\\n\\n`.
    404 guard runs BEFORE the generator is constructed (matches
    routers/jobs.py:120-126's precedent) so an unknown/foreign session never
    starts a stream at all.

    Async generator (mirrors routers/jobs.py's stream_job): a sync generator
    here is consumed via AnyIO's threadpool, pinning one of its ~40 worker
    threads for the stream's whole lifetime — every open or abandoned tab holds
    one for up to STREAM_DEADLINE_S, and enough of them starve every sync
    endpoint in the app. The poll cadence now awaits on the event loop; the
    404 pre-check and each per-tick store read hop to a thread so the loop
    never blocks on the session_store lock. Wire format/timing unchanged."""
    if await asyncio.to_thread(_require_owned_session, session_id, tenant) is None:
        return _session_not_found(session_id)

    async def event_stream():
        cursor = int(after_seq)
        deadline = time.time() + STREAM_DEADLINE_S
        last_activity = time.time()
        while time.time() < deadline:
            events = await asyncio.to_thread(
                session_store.events_after, session_id, cursor, 500
            )
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
            await asyncio.sleep(STREAM_POLL_S)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# GET /api/sessions/{id}/transcript
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/transcript")
def get_transcript(session_id: str, limit: int = TRANSCRIPT_DEFAULT_LIMIT,
                   tenant=Depends(deps.require_active_tenant)):
    """Most-recent-N envelopes, ascending by seq — no after_seq cursor (§2.1.4:
    'most recent N, ascending by seq'). `limit` is clamped to
    [1, TRANSCRIPT_MAX_LIMIT] regardless of what the caller sends."""
    if _require_owned_session(session_id, tenant) is None:
        return _session_not_found(session_id)

    clamped = max(1, min(int(limit), TRANSCRIPT_MAX_LIMIT))
    events = session_store.recent_events(session_id, clamped)
    return with_envelope_fields({"events": events})
