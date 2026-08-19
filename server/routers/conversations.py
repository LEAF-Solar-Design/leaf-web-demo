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

import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import conversations
import deps
import platform_link
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

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

    # 4. The one durable write (INSERT .. ON CONFLICT .. RETURNING): a fresh
    # key inserts, a replayed key returns the stored result, neither path
    # leaves a duplicate row.
    message, inserted = conversations.create_message(
        org_id, project_id, conversation_id, actor_binding_id,
        content, metadata, idempotency_key,
    )
    return JSONResponse(
        status_code=201 if inserted else 200,
        content=deps.tenant_echo(with_envelope_fields(message), tenant),
    )
