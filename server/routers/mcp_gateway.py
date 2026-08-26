"""Leaf Platform token exchanges for the public tenant MCP broker.

The internal exchange lets the trusted harness attach standard services to one
active model turn. The human exchange is separate and requires the user's
verified platform JWT. Neither route accepts tenant, subject, tier, service,
effect, or runner-profile authority from its request body.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import deps
import mcp_authority
import platform_link
import session_store
import standard_service_contract
import turn_runner


router = APIRouter()


# --- Observability (TEL-3): every HTTP authority denial, credential mint, and
#     human-approved tool execution in this router funnels through ONE of the
#     three choke points below (_deny / _emit_attachment_exchanged /
#     _emit_tool_invoked) before it returns to the caller. All three are
#     best-effort (mirrors telemetry_sink.emit's own never-raise contract): a
#     broken sink can never mask, delay, or soften the actual HTTP response.
#     The EMF plane (Leaf/Platform/APS) tracks reliability only, never
#     per-tool identity, so per-tool usage is answerable ONLY from these
#     events. --------------------------------------------------------------
def _ledger_tool(value: Any) -> str | None:
    """Normalize `tool` exactly the way broker.py's ledger choke point does
    (`_conform_ledger_entry`, server/broker.py:332-333): a string or None,
    never any other JSON shape a harness response could carry."""
    return value if isinstance(value, str) else None


def _emit_authority_denied(reason: str) -> None:
    """Best-effort mcp.authority_denied {reason}. No tenant/session identity
    is stamped: a denial can fire before either is verified (an
    unauthenticated caller has neither), so every row uses the same
    process-level identity operator_egress_guard's security events use."""
    try:
        import telemetry_sink

        telemetry_sink.emit(
            "mcp.authority_denied", tenant_id="system", tenant_kind="system",
            session_id="server", labels={"reason": reason},
        )
    except Exception:  # noqa: BLE001 - telemetry must never mask a denial
        pass


def _deny(status_code: int, detail: str, reason: str) -> HTTPException:
    """Single choke point for every authority-denying HTTPException this
    router raises: emit mcp.authority_denied BEFORE returning the exception
    for the caller to raise (mirrors operator_egress_guard._deny), so a
    telemetry fault can never suppress or delay the deny itself."""
    _emit_authority_denied(reason)
    return HTTPException(status_code=status_code, detail=detail)


def _emit_attachment_exchanged(direction: str, tenant_id: str, session_id: str) -> None:
    """Best-effort mcp.attachment_exchanged {direction, mime_class}, fired
    once per minted credential bundle from the single _mint_attachment choke
    point. `direction` is one of the two exchanges this module's own
    docstring names: "internal" (the trusted-harness route) or "human" (the
    verified-human approval route). Every bundle minted here is the JSON
    credential envelope (bearer_token + channel_secret) -- its real wire
    Content-Type -- so mime_class is "json" today; the label exists so a
    future non-JSON channel lands in its own class instead of a silent
    mislabel."""
    try:
        import telemetry_sink

        telemetry_sink.emit(
            "mcp.attachment_exchanged",
            tenant_id=tenant_id,
            tenant_kind="guest" if tenant_id.startswith("guest-") else "account",
            session_id=session_id,
            labels={"direction": direction, "mime_class": "json"},
        )
    except Exception:  # noqa: BLE001 - telemetry must never mask the mint
        pass


def _emit_tool_invoked(
    tool: str | None, outcome: str, duration_ms: int, approval_state: str,
    tenant_id: str, session_id: str,
) -> None:
    """Best-effort mcp.tool_invoked {tool, outcome, duration_ms,
    approval_state}, fired exactly once per /approvals/execute request from
    the single choke point in execute_human_approval -- the one place a
    human-approved tool call actually runs. `tool` is null -> absent (see
    _ledger_tool)."""
    try:
        import telemetry_sink

        labels: dict[str, Any] = {
            "outcome": outcome, "duration_ms": duration_ms,
            "approval_state": approval_state,
        }
        if tool is not None:
            labels["tool"] = tool
        telemetry_sink.emit(
            "mcp.tool_invoked",
            tenant_id=tenant_id,
            tenant_kind="guest" if tenant_id.startswith("guest-") else "account",
            session_id=session_id,
            labels=labels,
        )
    except Exception:  # noqa: BLE001 - telemetry must never mask the result
        pass


def _require_project_execution(
    session_id: str, tenant_id: str, subject: str, tier: str,
) -> None:
    tenant = deps.TenantContext(
        tenant_id, org_id=tenant_id, tier=tier, subject=subject,
        authority_resolved=True,
    )
    try:
        session = platform_link.require_project_session_access(
            session_store.get_session(session_id), tenant, write=True,
        )
    except platform_link.ProjectSessionForbidden as exc:
        raise _deny(
            403, "current project role does not permit execution",
            "execution_forbidden",
        ) from exc
    if session is None:
        raise _deny(409, "MCP session is unavailable", "session_unavailable")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_TOKEN_BODY_MAX_BYTES = 16 * 1024
_TOKEN_ROUTES = frozenset({
    "/internal/mcp/gateway/attachment",
    "/api/mcp/gateway/approvals/token",
    "/api/mcp/gateway/approvals/execute",
})
_RECEIPT_DEDUPE_EVENT_WINDOW = 256


def _append_completed_receipt_once(
    session_id: str,
    turn_id: str,
    receipt_id: str,
    artifact_ids: list[str] | None,
) -> None:
    for event in session_store.recent_events(
        session_id, _RECEIPT_DEDUPE_EVENT_WINDOW
    ):
        if (
            event.get("type") == "standard_service_approval_receipt"
            and isinstance(event.get("data"), dict)
            and event["data"].get("receipt_id") == receipt_id
        ):
            return
    session_store.append_event(
        session_id,
        turn_id,
        "standard_service_approval_receipt",
        {
            "status": "completed",
            "receipt_id": receipt_id,
            **({"artifact_ids": artifact_ids} if artifact_ids else {}),
        },
    )


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
    runner_profile_id: str


class HumanApprovalTokenRequest(AttachmentExchangeRequest):
    approval_id: str
    argument_digest: str


class HumanApprovalExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    argument_digest: str


def _id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"invalid {name}")
    return value


def _runner_profile(value: str) -> str:
    _id("runner_profile_id", value)
    try:
        return mcp_authority.runner_profile_id(value)
    except mcp_authority.McpAuthorityError as exc:
        raise HTTPException(status_code=422, detail="invalid runner_profile_id") from exc


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
        raise _deny(
            409, "the named tenant session and turn are not active",
            "turn_inactive",
        )
    platform_tenant_id, tier = deps.resolve_active_platform_tenant_authority(subject)
    if platform_tenant_id != tenant_id:
        raise _deny(
            409, "the active turn no longer belongs to the named tenant",
            "tenant_mismatch",
        )
    return subject, tier


def _mint_attachment(
    *,
    tenant_id: str,
    subject_id: str,
    tier: str,
    request: AttachmentExchangeRequest,
    direction: str,
) -> dict[str, Any]:
    subject_id = mcp_authority.harness_safe_subject(subject_id)
    plan, effects = mcp_authority.tier_authority(tier)
    channel_secret = secrets.token_urlsafe(48)
    channel_hash = hashlib.sha256(channel_secret.encode("utf-8")).hexdigest()
    profile = _runner_profile(request.runner_profile_id)
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
            "scope": "tenant:services",
        },
        audience=mcp_authority.attachment_audience(),
        ttl_seconds=mcp_authority.ATTACHMENT_TTL_SECONDS,
    )
    _emit_attachment_exchanged(direction, tenant_id, request.session_id)
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


def _harness_approval_call(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").strip().rstrip("/")
    secret = os.environ.get("LEAF_APP_DISPATCH_SECRET", "").strip()
    parsed = urlparse(base)
    if (
        not base
        or not secret
        or path not in {"review", "execute"}
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise mcp_authority.McpAuthorityError("MCP approval host is unavailable")
    try:
        import requests

        response = requests.post(
            f"{base}/internal/standard-services/approvals/{path}",
            headers={"X-Dispatch-Secret": secret, "Content-Type": "application/json"},
            json=payload,
            timeout=5,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise mcp_authority.McpAuthorityError("MCP approval host is unavailable") from exc
    try:
        if response.status_code != 200:
            raise mcp_authority.McpAuthorityError("MCP approval is unavailable")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            declared_length = int(declared, 10)
            if declared_length < 0 or declared_length > 64 * 1024:
                raise mcp_authority.McpAuthorityError(
                    "MCP approval host returned invalid data"
                )
        raw = getattr(response, "raw", None)
        if raw is None or not callable(getattr(raw, "read", None)):
            raise mcp_authority.McpAuthorityError(
                "MCP approval host returned invalid data"
            )
        chunks: list[bytes] = []
        received = 0
        while received <= 64 * 1024:
            remaining = 64 * 1024 + 1 - received
            chunk = raw.read(min(8192, remaining), decode_content=True)
            if not chunk:
                break
            if not isinstance(chunk, bytes) or len(chunk) > remaining:
                raise mcp_authority.McpAuthorityError(
                    "MCP approval host returned invalid data"
                )
            received += len(chunk)
            if received > 64 * 1024:
                raise mcp_authority.McpAuthorityError("MCP approval host returned invalid data")
            chunks.append(chunk)
        def reject_constant(value: str) -> None:
            raise ValueError(f"invalid JSON constant: {value}")

        def reject_duplicate_keys(
            pairs: list[tuple[str, Any]],
        ) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        value = json.loads(
            b"".join(chunks).decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except mcp_authority.McpAuthorityError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport details are private
        raise mcp_authority.McpAuthorityError(
            "MCP approval host returned invalid data"
        ) from exc
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - close details are private
            pass


def _mint_human_approval_token(
    *, tenant_id: str, subject_id: str, identity: dict[str, str],
    approval_id: str, argument_digest: str,
) -> tuple[str, int]:
    subject_id = mcp_authority.harness_safe_subject(subject_id)
    return mcp_authority.signer().issue(
        {
            "sub": subject_id,
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            **identity,
            "approval_id": approval_id,
            "argument_digest": argument_digest,
        },
        audience=mcp_authority.approval_audience(),
        ttl_seconds=mcp_authority.APPROVAL_TTL_SECONDS,
    )


@router.get("/api/mcp/gateway/.well-known/jwks.json")
def gateway_jwks() -> dict[str, list[dict[str, str]]]:
    try:
        return {"keys": [mcp_authority.signer().public_jwk()]}
    except mcp_authority.McpAuthorityError as exc:
        raise _deny(503, "MCP signing is unavailable", "signing_unavailable") from exc


@router.post("/internal/mcp/gateway/attachment")
def exchange_attachment(
    request: AttachmentExchangeRequest,
    response: Response,
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not deps.auth_live():
        raise _deny(503, "live authentication is required", "auth_not_live")
    if not x_tenant_id or not deps._dispatch_secret_ok(x_dispatch_secret):
        raise _deny(401, "valid dispatch authority is required", "unauthenticated")
    tenant_id = _id("tenant_id", x_tenant_id)
    _id("session_id", request.session_id)
    _id("authority_session_id", request.authority_session_id)
    _id("authority_turn_id", request.authority_turn_id)
    _id("subscription_mount_id", request.subscription_mount_id)
    _runner_profile(request.runner_profile_id)
    subject, tier = _active_authority(
        tenant_id, request.authority_session_id, request.authority_turn_id
    )
    _require_project_execution(request.authority_session_id, tenant_id, subject, tier)
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
            direction="internal",
        )
    except mcp_authority.McpMountDenied as exc:
        raise _deny(403, "subscription mount is unavailable", "mount_denied") from exc
    except mcp_authority.McpAuthorityError as exc:
        raise _deny(
            503, "MCP attachment authority is unavailable", "attachment_unavailable",
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
        raise _deny(401, "verified human identity is required", "unauthenticated")
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
        raise _deny(
            409, "the active turn belongs to another authenticated subject",
            "subject_mismatch",
        )
    current_tenant, current_tier = deps.resolve_active_platform_tenant_authority(
        tenant.subject
    )
    if current_tenant != tenant_id or current_tier != active_tier:
        raise _deny(409, "platform authority changed", "authority_changed")
    _require_project_execution(
        request.authority_session_id, tenant_id, tenant.subject, current_tier,
    )

    try:
        mcp_authority.verify_subscription_mount(
            tenant_id, request.subscription_mount_id
        )
        profile = _runner_profile(request.runner_profile_id)
        token, expires_at = _mint_human_approval_token(
            tenant_id=tenant_id,
            subject_id=tenant.subject,
            identity={
                "session_id": request.session_id,
                "authority_turn_id": request.authority_turn_id,
                "subscription_mount_id": request.subscription_mount_id,
                "runner_profile_id": profile,
            },
            approval_id=request.approval_id,
            argument_digest=request.argument_digest,
        )
    except mcp_authority.McpMountDenied as exc:
        raise _deny(403, "subscription mount is unavailable", "mount_denied") from exc
    except mcp_authority.McpAuthorityError as exc:
        raise _deny(
            503, "MCP approval authority is unavailable", "approval_unavailable",
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {
        "bearer_token": token,
        "expires_at": mcp_authority.expires_at_iso(expires_at),
    }


@router.post("/api/mcp/gateway/approvals/execute")
def execute_human_approval(
    request: HumanApprovalExecuteRequest,
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
        raise _deny(401, "verified human identity is required", "unauthenticated")
    tenant_id = _id("tenant_id", str(tenant))
    if not _APPROVAL_ID.fullmatch(request.approval_id):
        raise HTTPException(status_code=422, detail="invalid approval_id")
    if not _DIGEST.fullmatch(request.argument_digest):
        raise HTTPException(status_code=422, detail="invalid argument_digest")
    current_tenant, current_tier = deps.resolve_active_platform_tenant_authority(
        tenant.subject
    )
    if current_tenant != tenant_id:
        raise _deny(409, "platform authority changed", "authority_changed")

    # mcp.tool_invoked (TEL-3) fires exactly once below, from the `finally`,
    # timing the whole review+mint+execute round trip -- THE one place a
    # human-approved tool call actually runs. Nothing before this point
    # identifies a tool, so the 401/422/409 guards above are
    # mcp.authority_denied's job, not this event's.
    start = time.monotonic()
    invocation: dict[str, Any] = {
        "tool": None, "approval_state": "error", "outcome": "error",
        "session_id": "unknown",
    }
    try:
        try:
            reviewed = _harness_approval_call("review", {
                "approval_id": request.approval_id,
                "argument_digest": request.argument_digest,
                "tenant_id": tenant_id,
                "subject_id": mcp_authority.harness_safe_subject(tenant.subject),
            })
            invocation["tool"] = _ledger_tool(reviewed.get("tool"))
            identity_value = reviewed.get("identity")
            approval_status = reviewed.get("status")
            if approval_status not in {"pending", "approved", "completed", "uncertain"} or not isinstance(identity_value, dict):
                raise mcp_authority.McpAuthorityError("MCP approval is unavailable")
            invocation["approval_state"] = approval_status
            identity = {
                "session_id": _id("session_id", identity_value.get("session_id")),
                "authority_turn_id": _id(
                    "authority_turn_id", identity_value.get("authority_turn_id")
                ),
                "subscription_mount_id": _id(
                    "subscription_mount_id", identity_value.get("subscription_mount_id")
                ),
                "runner_profile_id": _runner_profile(
                    identity_value.get("runner_profile_id")
                ),
            }
            invocation["session_id"] = identity["session_id"]
            if (
                identity_value.get("tenant_id") != tenant_id
                or identity_value.get("subject_id")
                != mcp_authority.harness_safe_subject(tenant.subject)
            ):
                raise mcp_authority.McpAuthorityError("MCP approval is unavailable")
            session = session_store.get_session(identity["session_id"])
            if not session or session.get("tenant_id") != tenant_id:
                raise mcp_authority.McpAuthorityError(
                    "MCP approval receipt session is unavailable"
                )
            _require_project_execution(
                identity["session_id"], tenant_id, tenant.subject, current_tier,
            )
            if approval_status in {"pending", "approved"}:
                mcp_authority.verify_subscription_mount(
                    tenant_id, identity["subscription_mount_id"]
                )
                human_token, _human_expires = _mint_human_approval_token(
                    tenant_id=tenant_id,
                    subject_id=tenant.subject,
                    identity=identity,
                    approval_id=request.approval_id,
                    argument_digest=request.argument_digest,
                )
                attachment_request = AttachmentExchangeRequest(
                    session_id=identity["session_id"],
                    authority_session_id=identity["session_id"],
                    authority_turn_id=identity["authority_turn_id"],
                    subscription_mount_id=identity["subscription_mount_id"],
                    runner_profile_id=identity["runner_profile_id"],
                )
                attachment = _mint_attachment(
                    tenant_id=tenant_id,
                    subject_id=tenant.subject,
                    tier=current_tier,
                    request=attachment_request,
                    direction="human",
                )
                receipt = _harness_approval_call("execute", {
                    "approval_id": request.approval_id,
                    "argument_digest": request.argument_digest,
                    "identity": attachment["identity"],
                    "human_bearer": human_token,
                    "attachment": {
                        "bearer_token": attachment["bearer_token"],
                        "channel_secret": attachment["channel_secret"],
                        "expires_at": attachment["expires_at"],
                    },
                })
                approval_status = receipt.get("status")
                invocation["tool"] = _ledger_tool(receipt.get("tool")) or invocation["tool"]
                invocation["approval_state"] = (
                    approval_status if isinstance(approval_status, str)
                    else invocation["approval_state"]
                )
            else:
                receipt = reviewed

            if approval_status == "uncertain":
                session_store.append_event(
                    identity["session_id"],
                    identity["authority_turn_id"],
                    "standard_service_approval_status",
                    {
                        "status": "uncertain",
                        "approval_id": request.approval_id,
                    },
                )
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                invocation["outcome"] = "uncertain"
                return {"status": "uncertain"}

            receipt_id = receipt.get("receipt_id")
            artifact_ids = receipt.get("artifact_ids")
            if (
                receipt.get("status") != "completed"
                or not isinstance(receipt_id, str)
                or not _DIGEST.fullmatch(receipt_id)
                or (
                    artifact_ids is not None
                    and (
                        not standard_service_contract.valid_artifact_ids(
                            artifact_ids
                        )
                    )
                )
            ):
                raise mcp_authority.McpAuthorityError(
                    "MCP approval host returned invalid data"
                )
            _append_completed_receipt_once(
                identity["session_id"],
                identity["authority_turn_id"],
                receipt_id,
                artifact_ids,
            )
            invocation["outcome"] = "completed"
        except mcp_authority.McpMountDenied as exc:
            invocation["outcome"] = "denied"
            raise _deny(403, "subscription mount is unavailable", "mount_denied") from exc
        except (mcp_authority.McpAuthorityError, HTTPException) as exc:
            if isinstance(exc, HTTPException):
                raise
            invocation["outcome"] = "error"
            raise _deny(
                409, "MCP approval is unavailable", "approval_unavailable",
            ) from exc
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {
            "status": "completed",
            "receipt_id": receipt_id,
            **({"artifact_ids": artifact_ids} if artifact_ids else {}),
        }
    finally:
        _emit_tool_invoked(
            invocation["tool"], invocation["outcome"],
            int((time.monotonic() - start) * 1000), invocation["approval_state"],
            tenant_id, invocation["session_id"],
        )
