"""Leaf Platform token exchanges for the public tenant MCP broker.

The internal exchange lets the trusted harness attach standard services to one
active model turn. The human exchange is separate and requires the user's
verified platform JWT. Neither route accepts tenant, subject, tier, service,
effect, or runner-profile authority from its request body.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import deps
import mcp_authority
import session_store
import turn_runner


router = APIRouter()

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_TOKEN_BODY_MAX_BYTES = 16 * 1024
_TOKEN_ROUTES = frozenset({
    "/internal/mcp/gateway/attachment",
    "/api/mcp/gateway/approvals/token",
})


async def _plain_error(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class McpAuthorityBodyLimitMiddleware:
    """Authenticate the route shape and cap credential bodies before parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in _TOKEN_ROUTES
        ):
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers", [])

        def one_header(name: bytes) -> bytes | None:
            values = [value for key, value in raw_headers if key.lower() == name]
            return values[0] if len(values) == 1 else None

        def ascii_header(name: bytes) -> str:
            value = one_header(name)
            if value is None:
                return ""
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return ""

        path = scope["path"]
        if path.startswith("/internal/"):
            secret = ascii_header(b"x-dispatch-secret")
            tenant = ascii_header(b"x-tenant-id")
            if not tenant or not deps._dispatch_secret_ok(secret):
                await _plain_error(send, 401, "valid dispatch authority is required")
                return
        else:
            authorization = ascii_header(b"authorization")
            parts = authorization.split(None, 1)
            if (
                len(parts) != 2
                or parts[0].lower() != "bearer"
                or not parts[1].strip()
            ):
                await _plain_error(send, 401, "verified human identity is required")
                return

        content_lengths = [
            value for key, value in raw_headers if key.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await _plain_error(send, 400, "invalid content length")
            return
        content_length = content_lengths[0] if content_lengths else None
        if content_length is not None:
            try:
                parsed_length = int(content_length)
                if parsed_length < 0:
                    raise ValueError
                if parsed_length > _TOKEN_BODY_MAX_BYTES:
                    await _plain_error(send, 413, "request body is too large")
                    return
            except ValueError:
                await _plain_error(send, 400, "invalid content length")
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await _plain_error(send, 400, "request body was interrupted")
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > _TOKEN_BODY_MAX_BYTES:
                await _plain_error(send, 413, "request body is too large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


class AttachmentExchangeRequest(BaseModel):
    """Trusted Mushy context plus the app turn used to resolve its human.

    ``session_id`` and ``authority_turn_id`` become the broker-enforced Mushy
    identity. ``authority_session_id`` is not a broker claim. It only selects
    the app-owned active turn whose subject and tenant are verified before the
    credential is minted.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    authority_session_id: str
    authority_turn_id: str
    subscription_mount_id: str


class HumanApprovalTokenRequest(AttachmentExchangeRequest):
    approval_id: str
    argument_digest: str


def _id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"invalid {name}")
    return value


def _active_authority(
    tenant_id: str,
    authority_session_id: str,
    authority_turn_id: str,
) -> tuple[str, str]:
    subject = session_store.active_turn_subject(
        authority_session_id,
        authority_turn_id,
        tenant_id,
        turn_runner.turn_max_s(),
    )
    if not isinstance(subject, str) or not subject:
        raise HTTPException(
            status_code=409,
            detail="the named tenant session and turn are not active",
        )
    platform_tenant_id, tier = deps.resolve_active_platform_tenant_authority(subject)
    if platform_tenant_id != tenant_id:
        raise HTTPException(
            status_code=409,
            detail="the active turn no longer belongs to the named tenant",
        )
    return subject, tier


def _mint_attachment(
    *,
    tenant_id: str,
    subject_id: str,
    tier: str,
    request: AttachmentExchangeRequest,
) -> dict[str, Any]:
    plan, effects = mcp_authority.tier_authority(tier)
    channel_secret = secrets.token_urlsafe(48)
    channel_hash = hashlib.sha256(channel_secret.encode("utf-8")).hexdigest()
    profile = mcp_authority.runner_profile_id()
    token, expires_at = mcp_authority.signer().issue(
        {
            "sub": subject_id,
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "session_id": request.session_id,
            "authority_turn_id": request.authority_turn_id,
            "subscription_mount_id": request.subscription_mount_id,
            "runner_profile_id": profile,
            "plan": plan,
            "allowed_services": list(mcp_authority.PUBLIC_SERVICES),
            "allowed_effects": list(effects),
            "channel_hash": channel_hash,
        },
        audience=mcp_authority.attachment_audience(),
        ttl_seconds=mcp_authority.ATTACHMENT_TTL_SECONDS,
    )
    return {
        "bearer_token": token,
        "channel_secret": channel_secret,
        "expires_at": mcp_authority.expires_at_iso(expires_at),
        "identity": {
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "session_id": request.session_id,
            "authority_turn_id": request.authority_turn_id,
            "subscription_mount_id": request.subscription_mount_id,
            "runner_profile_id": profile,
        },
    }


@router.get("/api/mcp/gateway/.well-known/jwks.json")
def gateway_jwks() -> dict[str, list[dict[str, str]]]:
    try:
        return {"keys": [mcp_authority.signer().public_jwk()]}
    except mcp_authority.McpAuthorityError as exc:
        raise HTTPException(status_code=503, detail="MCP signing is unavailable") from exc


@router.post("/internal/mcp/gateway/attachment")
def exchange_attachment(
    request: AttachmentExchangeRequest,
    response: Response,
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not deps.auth_live():
        raise HTTPException(status_code=503, detail="live authentication is required")
    if not x_tenant_id or not deps._dispatch_secret_ok(x_dispatch_secret):
        raise HTTPException(status_code=401, detail="valid dispatch authority is required")
    tenant_id = _id("tenant_id", x_tenant_id)
    _id("session_id", request.session_id)
    _id("authority_session_id", request.authority_session_id)
    _id("authority_turn_id", request.authority_turn_id)
    _id("subscription_mount_id", request.subscription_mount_id)
    subject, tier = _active_authority(
        tenant_id, request.authority_session_id, request.authority_turn_id
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        mcp_authority.verify_subscription_mount(
            tenant_id, request.subscription_mount_id
        )
        return _mint_attachment(
            tenant_id=tenant_id,
            subject_id=subject,
            tier=tier,
            request=request,
        )
    except mcp_authority.McpMountDenied as exc:
        raise HTTPException(status_code=403, detail="subscription mount is unavailable") from exc
    except mcp_authority.McpAuthorityError as exc:
        raise HTTPException(
            status_code=503, detail="MCP attachment authority is unavailable"
        ) from exc


@router.post("/api/mcp/gateway/approvals/token")
def exchange_human_approval_token(
    request: HumanApprovalTokenRequest,
    response: Response,
    tenant: Any = Depends(deps.require_tenant),
) -> dict[str, Any]:
    if (
        not deps.auth_live()
        or not isinstance(tenant, deps.TenantContext)
        or getattr(tenant, "backedge", False)
        or not getattr(tenant, "authority_resolved", False)
        or not isinstance(tenant.subject, str)
        or not tenant.subject
    ):
        raise HTTPException(status_code=401, detail="verified human identity is required")
    tenant_id = _id("tenant_id", str(tenant))
    _id("session_id", request.session_id)
    _id("authority_session_id", request.authority_session_id)
    _id("authority_turn_id", request.authority_turn_id)
    _id("subscription_mount_id", request.subscription_mount_id)
    if not _APPROVAL_ID.fullmatch(request.approval_id):
        raise HTTPException(status_code=422, detail="invalid approval_id")
    if not _DIGEST.fullmatch(request.argument_digest):
        raise HTTPException(status_code=422, detail="invalid argument_digest")

    active_subject, active_tier = _active_authority(
        tenant_id, request.authority_session_id, request.authority_turn_id
    )
    if not secrets.compare_digest(active_subject, tenant.subject):
        raise HTTPException(
            status_code=409,
            detail="the active turn belongs to another authenticated subject",
        )
    current_tenant, current_tier = deps.resolve_active_platform_tenant_authority(
        tenant.subject
    )
    if current_tenant != tenant_id or current_tier != active_tier:
        raise HTTPException(status_code=409, detail="platform authority changed")

    try:
        mcp_authority.verify_subscription_mount(
            tenant_id, request.subscription_mount_id
        )
        profile = mcp_authority.runner_profile_id()
        token, expires_at = mcp_authority.signer().issue(
            {
                "sub": tenant.subject,
                "tenant_id": tenant_id,
                "subject_id": tenant.subject,
                "session_id": request.session_id,
                "authority_turn_id": request.authority_turn_id,
                "subscription_mount_id": request.subscription_mount_id,
                "runner_profile_id": profile,
                "approval_id": request.approval_id,
                "argument_digest": request.argument_digest,
            },
            audience=mcp_authority.approval_audience(),
            ttl_seconds=mcp_authority.APPROVAL_TTL_SECONDS,
        )
    except mcp_authority.McpMountDenied as exc:
        raise HTTPException(status_code=403, detail="subscription mount is unavailable") from exc
    except mcp_authority.McpAuthorityError as exc:
        raise HTTPException(
            status_code=503, detail="MCP approval authority is unavailable"
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {
        "bearer_token": token,
        "expires_at": mcp_authority.expires_at_iso(expires_at),
    }
