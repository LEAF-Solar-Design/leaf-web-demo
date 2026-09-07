"""Conversation-authenticated finish/status admission for the completion engine.

This is the conversation SEAM only, not another project status store: the model
tools ``finish_project`` and ``project_completion_status`` dispatch here through
the converse spine's app back-edge, and every mutation reuses the campaign
engine's own authoritative implementation verbatim
(``routers.campaigns._finish_campaign``, ``campaign_release_service.snapshot``)
via the exact same error-mapping helper the existing release routes use
(``routers.campaigns._release_call``) -- no state or error taxonomy is
duplicated here.

Project/org authority is derived ONLY from the authenticated app-owned
conversation session, never from a model argument or client-supplied header:
``X-Authority-Session-Id`` / ``X-Authority-Turn-Id`` resolve the live app turn
(``deps.stage_author_identity``), the app-owned session row
(``session_store.get_session``) must carry both ``org_id`` and ``project_id``,
and ``platform_link.require_project_session_access`` rechecks the CURRENT
project role before any mutation. A missing/half-present authority tuple, a
turn the app never authorized, a session with no project link, or a revoked
project role all fail before the campaign engine is ever called.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.concurrency import run_in_threadpool

import deps
import platform_link
import project_repository_source
import session_store
from routers import campaigns

router = APIRouter()

MAX_TITLE = 200
MAX_PROMPT = 32768
# Slack above the bounded prompt for the other finish fields (delivery_profile,
# intended_user, workflow, artifact_refs) and the JSON envelope itself -- a
# hard cap, never an unbounded read (build doctrine: bound every allocation).
MAX_FINISH_BODY_BYTES = MAX_PROMPT + 16384
MAX_STATUS_BODY_BYTES = 4096


class _AuthorityInvalid(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# code -> (http status, wire error_code, message). Every reason the docstring
# names above maps to exactly one entry, so the router's exception handler
# never has to invent a shape.
_AUTHORITY_FAILURES = {
    "invalid_authority_tuple": (
        422, "invalid_authority_tuple",
        "X-Authority-Session-Id and X-Authority-Turn-Id must both be present or both absent"),
    "missing_authority_tuple": (
        422, "missing_authority_tuple",
        "Conversation authority headers are required"),
    "stage_authority_invalid": (
        409, "stage_authority_invalid",
        "Conversation authority is stale or does not match the active app turn"),
    "project_unavailable": (
        404, "project_unavailable", "project is unavailable"),
    "forbidden": (
        403, "forbidden", "project role does not permit access"),
}


def _authority_failure(exc: "_AuthorityInvalid"):
    status, code, message = _AUTHORITY_FAILURES[exc.code]
    return campaigns._failure(status, code, message)


def _resolve_conversation_project(
    tenant: Any,
    authority_session_id: Optional[str],
    authority_turn_id: Optional[str],
):
    """Bind this call to the authenticated conversation's OWN project.

    Returns ``(live_tenant, project_id, authority_session_id)``. Raises
    ``_AuthorityInvalid`` before any mutation on every failure mode named in
    the module docstring.
    """
    if bool(authority_session_id) != bool(authority_turn_id):
        raise _AuthorityInvalid("invalid_authority_tuple")
    if not authority_session_id or not authority_turn_id:
        raise _AuthorityInvalid("missing_authority_tuple")
    live_tenant = deps.stage_author_identity(tenant, authority_session_id, authority_turn_id)
    if live_tenant is None:
        raise _AuthorityInvalid("stage_authority_invalid")
    session = session_store.get_session(authority_session_id)
    if session is None or session.get("org_id") is None or session.get("project_id") is None:
        raise _AuthorityInvalid("project_unavailable")
    try:
        authorized = platform_link.require_project_session_access(session, live_tenant, write=True)
    except platform_link.ProjectSessionForbidden:
        raise _AuthorityInvalid("forbidden")
    if authorized is None or authorized.get("project_id") is None:
        raise _AuthorityInvalid("project_unavailable")
    return live_tenant, str(authorized["project_id"]), authority_session_id


async def _bounded_body(request: Request, limit: int) -> dict:
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > limit):
        raise ValueError("request body is too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise ValueError("request body is too large")
    body = json.loads(bytes(raw), object_pairs_hook=project_repository_source._closed_pairs)
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return value


def _idempotency_key(authority_session_id: str, finish: dict) -> str:
    """Derive request idempotency from the authority session plus the
    canonical, normalized model intent -- stable across a retried turn (the
    model never supplies an idempotency header itself)."""
    canonical = json.dumps(
        {"authority_session_id": authority_session_id, "finish": finish},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.post("/api/campaigns/conversation/finish")
async def conversation_finish(
    request: Request,
    tenant: Any = Depends(deps.require_tenant),
    authority_session_id: Optional[str] = Header(
        default=None, alias="X-Authority-Session-Id"),
    authority_turn_id: Optional[str] = Header(
        default=None, alias="X-Authority-Turn-Id"),
):
    import campaign_release_service as releases

    try:
        body = await _bounded_body(request, MAX_FINISH_BODY_BYTES)
    except (ValueError, UnicodeError):
        return campaigns._failure(400, "invalid_request", "Invalid finish request")
    if set(body) != {
            "title", "prompt", "delivery_profile", "intended_user", "workflow", "artifact_refs"}:
        return campaigns._failure(400, "invalid_request", "Invalid finish request")
    try:
        title = _text(body.get("title"), "title", MAX_TITLE)
        prompt = _text(body.get("prompt"), "prompt", MAX_PROMPT)
        finish = releases.validate_finish({
            key: body[key]
            for key in ("delivery_profile", "intended_user", "workflow", "artifact_refs")
        })
    except ValueError:
        return campaigns._failure(400, "invalid_request", "Invalid finish request")

    try:
        live_tenant, project_id, authority_session_id = _resolve_conversation_project(
            tenant, authority_session_id, authority_turn_id)
    except _AuthorityInvalid as exc:
        return _authority_failure(exc)

    key = _idempotency_key(authority_session_id, {"title": title, "prompt": prompt, **finish})
    return await run_in_threadpool(
        campaigns._release_call, "campaign", campaigns._finish_campaign,
        live_tenant, project_id, title, prompt, finish, key)


@router.post("/api/campaigns/conversation/status")
async def conversation_status(
    request: Request,
    tenant: Any = Depends(deps.require_tenant),
    authority_session_id: Optional[str] = Header(
        default=None, alias="X-Authority-Session-Id"),
    authority_turn_id: Optional[str] = Header(
        default=None, alias="X-Authority-Turn-Id"),
):
    import campaign_release_service as releases

    try:
        body = await _bounded_body(request, MAX_STATUS_BODY_BYTES)
    except (ValueError, UnicodeError):
        return campaigns._failure(400, "invalid_request", "Invalid status request")
    if not set(body) <= {"campaign_id", "release_id"} or "campaign_id" not in body:
        return campaigns._failure(400, "invalid_request", "Invalid status request")
    try:
        campaign_id = campaigns._id(body.get("campaign_id"))
        release_id = (
            campaigns._id(body["release_id"])
            if body.get("release_id") is not None else None)
    except ValueError:
        return campaigns._failure(400, "invalid_request", "Invalid status request")

    try:
        live_tenant, project_id, _ = _resolve_conversation_project(
            tenant, authority_session_id, authority_turn_id)
    except _AuthorityInvalid as exc:
        return _authority_failure(exc)

    return await run_in_threadpool(
        campaigns._release_call, "completion", releases.snapshot,
        live_tenant, project_id, campaign_id, release_id)
