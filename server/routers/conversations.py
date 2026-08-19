"""Conversation message write path (B-C2): bounded, validated, fails closed.

Kept in ``server/routers/`` -- the repo's usual home for route handlers --
separate from ``server/conversations.py``, which owns the storage layer and
B-C1's conversation CRUD routes. This module adds exactly one route: posting
a message into an existing, verified-in-scope conversation.

Ordering is load-bearing, not stylistic:
  1. ``conv_durable`` flag (before any body access -- a Pydantic model
     parameter would let FastAPI's automatic body validation run before this
     function body executes at all, so the body is read and parsed by hand
     here instead, and the flag is checked before either happens).
  2. Body shape / size / cap / allowlist validation (before any DB call).
  3. Project authority (the actor's own verified membership, never a header
     or body field) and conversation-scope verification (same org AND
     project, not just "exists somewhere") -- a cross-tenant or
     cross-project conversation id reads exactly like a missing one.
  4. The one durable write.

See ``conversations.py``'s "KNOWN GAP" note: the durable INSERT this route
issues targets ``conversation_messages``, a table outside this card's file
budget (migration: false; files_expected names no migration). Every test
that reaches step 4 is Postgres-gated and skips without ``DATABASE_URL``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

import conversations
import deps
import platform_link
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

# The route's own time budget: bigger than any single statement timeout below
# it (conversations.MESSAGE_STATEMENT_TIMEOUT_MS covers the INSERT, and
# get_conversation carries the same bound) because the route makes THREE
# sequential DB round trips -- the project-authority read
# (platform_link.require_project_access), the identity-binding lookup
# (platform_store.resolve_active_identity_binding), and get_conversation --
# neither of which lives in this card's file budget and so cannot carry its
# own SET LOCAL statement_timeout. asyncio.to_thread + wait_for is this
# route's own bound on that whole chain: it cannot cancel a stuck thread (Python
# threads are not preemptible), but it guarantees THIS request answers a typed
# 504 instead of hanging past its budget, the same shape C2-4's
# lock_timeout_seconds gives templates.py's write route.
ROUTE_TIME_BUDGET_SECONDS = 5.0

# A little slop above the content cap for the JSON envelope (quotes, the
# "content"/"metadata" keys, a handful of short allowlisted metadata values)
# -- NOT a second content-length rail. The real cap is enforced on the
# decoded ``content`` field's own UTF-8 byte length in
# conversations.validate_message_content; this only bounds how much of a
# clearly-oversized body the route parses before rejecting it.
MAX_REQUEST_BODY_BYTES = (
    conversations.MAX_MESSAGE_CONTENT_BYTES
    + len(conversations.ALLOWED_MESSAGE_METADATA_KEYS)
    * conversations.MAX_MESSAGE_METADATA_VALUE_BYTES
    + 4096
)


def _malformed(message: str) -> JSONResponse:
    return error_response(ErrorCode.BAD_PARAMS, message, retryable=False, status_code=400)


def _oversized(message: str) -> JSONResponse:
    return error_response(ErrorCode.BAD_PARAMS, message, retryable=False, status_code=413)


def _rejected(exc: "conversations.MessageRejected") -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=exc.status_code)


def _write_timeout(message: str) -> JSONResponse:
    return error_response(ErrorCode.TIMEOUT, message, retryable=True, status_code=504)


def _resolve_and_write(
    tenant: Any, project_id: str, conversation_id: str,
    content: str, metadata: Dict[str, Any], idempotency_key: str,
) -> JSONResponse:
    """Steps 3-4 (every DB-touching step), run off the event loop in a worker
    thread so ``ROUTE_TIME_BUDGET_SECONDS`` can bound them from the caller
    side even though the callees have no statement timeout of their own.
    """
    # 3. Project authority (never body/header-derived org or project) plus
    # conversation-scope verification -- same org AND project, not merely
    # "exists somewhere" -- so a cross-tenant or cross-project conversation
    # id is indistinguishable from an unknown one (same status, same body).
    try:
        org_id = conversations._require_project(tenant, project_id, write=True)
        if org_id is None:
            return conversations._not_found()
        actor_binding_id = conversations._actor_binding_id(tenant)
    except platform_link.ProjectSessionForbidden:
        return conversations._forbidden()

    target = conversations.get_conversation(org_id, project_id, conversation_id)
    if target is None:
        return conversations._not_found()

    # 4. The one durable write (INSERT .. ON CONFLICT DO NOTHING, replaying
    # the stored row on a hit): a fresh key inserts (201), a replayed key
    # returns the stored result (200), neither path leaves a duplicate row.
    # The write path (B-C4's create_message) also runs the post-commit poison
    # check; a quarantine receipt is a storage-side effect, never surfaced on
    # this response.
    result = conversations.create_message(
        org_id, project_id, conversation_id, actor_binding_id,
        idempotency_key=idempotency_key, content=content, metadata=metadata,
    )
    return JSONResponse(
        status_code=200 if result["replayed"] else 201,
        content=deps.tenant_echo(with_envelope_fields(result["message"]), tenant),
    )


@router.post("/api/projects/{project_id}/conversations/{conversation_id}/messages")
async def api_post_message(
    project_id: str, conversation_id: str, request: Request,
    tenant=Depends(deps.require_active_tenant),
):
    # 1. Flag fence FIRST -- before any body read, so a flag-off request with
    # a malformed body still answers the fence's 404/disabled shape, matching
    # the conv_durable envelope's negative control exactly (never a typed-403
    # "disabled" and never a validation error leaking that the route exists).
    if not conversations.conv_durable_enabled():
        return conversations._flag_off()

    # 2. Body shape, size, caps, allowlist -- all before any DB call, so
    # every rejection here is provably a zero-write path.
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        return _oversized(
            f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _malformed("request body must be valid UTF-8 JSON")
    if type(payload) is not dict:
        return _malformed("request body must be a JSON object")

    idempotency_key = request.headers.get("Idempotency-Key")
    try:
        idempotency_key = conversations.validate_idempotency_key(idempotency_key)
        content = conversations.validate_message_content(payload.get("content"))
        metadata = conversations.validate_message_metadata(payload.get("metadata"))
    except conversations.MessageRejected as exc:
        return _rejected(exc)

    # 3-4. Authority read, conversation-scope check, and the one durable write
    # -- every remaining DB touch -- run in a worker thread bounded by
    # ROUTE_TIME_BUDGET_SECONDS, so a stuck authority read or a stuck insert
    # answers this request a typed 504 instead of hanging it.
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _resolve_and_write, tenant, project_id, conversation_id,
                content, metadata, idempotency_key,
            ),
            timeout=ROUTE_TIME_BUDGET_SECONDS,
        )
    except asyncio.TimeoutError:
        return _write_timeout(
            f"conversation message write did not complete within "
            f"{ROUTE_TIME_BUDGET_SECONDS}s")


# --- recovery-on-reconnect (card B-C3) -------------------------------------- #
#
# GET /api/projects/{project_id}/conversations/recovery/tail returns a
# bounded, cursor-paginated, poison-fenced page of the project's most recent
# conversation's message tail, so a reconnecting client can walk forward page
# by page and reconstruct EXACTLY the state a never-disconnected client would
# have (quarantined rows surface as gap entries, never as items, matching
# recover_conversation_messages' whole-tail semantics).
#
# Route path note (trap: router-order shadowing): this path has more segments
# after /conversations than conversations.py's GET /conversations/{id} and
# POST /conversations/recover, so no path-template regex here can ever
# capture, or be captured by, either of those routes.
#
# Registered for both GET and POST: GET is the real recovery read; POST
# answers the identical flag-off 404 (and, flag-on, the identical read)
# rather than a bare framework 405, so the platform's flag-off envelope
# negative control never trips on verb mismatch.

RECOVERY_PATH = "/api/projects/{project_id}/conversations/recovery/tail"


def _invalid_cursor():
    return error_response(
        ErrorCode.BAD_PARAMS, "recovery cursor is malformed", retryable=False,
        status_code=400,
    )


@router.api_route(RECOVERY_PATH, methods=["GET", "POST"])
def api_recover_conversation_tail(
    project_id: str,
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(
        default=conversations.RECOVERY_DEFAULT_LIMIT,
        ge=1, le=conversations.RECOVERY_MAX_LIMIT,
    ),
    tenant=Depends(deps.require_active_tenant),
):
    if not conversations.conv_durable_enabled():
        return conversations._flag_off()
    try:
        org_id = conversations._require_project(tenant, project_id, write=False)
    except platform_link.ProjectSessionForbidden:
        return conversations._forbidden()
    if org_id is None:
        return conversations._not_found()
    try:
        page = conversations.recover_conversation_tail(
            org_id, project_id, cursor=cursor, limit=limit)
    except conversations.InvalidRecoveryCursor:
        return _invalid_cursor()
    return deps.tenant_echo(with_envelope_fields(page), tenant)
