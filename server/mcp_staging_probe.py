"""Bounded authenticated staging probe for the tenant MCP broker.

This module is a private ECS task command, not an HTTP route. It mints one
deployment-canary attachment with the app task's existing KMS authority and
uses it only long enough to prove the broker's authenticated MCP contract.

The command is started only by the protected broker deployment transaction in
``LEAF-Solar-Design/leaf-automation-aws-terraform``. Its canonical ownership
and invocation contract is documented in
``docs/tenant-mcp-broker-staging-rollout.md`` in that repository. The Leaf app
does not expose or independently dispatch this command.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import mcp_authority


ENABLE_ENV = "LEAF_TENANT_MCP_STAGING_PROBE_ENABLED"
BROKER_URL_ENV = "LEAF_TENANT_MCP_BROKER_URL"
# The deployed broker accepts the stable handshake flow at this version. The
# 2026 wire is a different per-request envelope and must not be half-emitted.
MCP_PROTOCOL_VERSION = "2025-11-25"
CANARY_TTL_SECONDS = 60
MAX_RESPONSE_BYTES = 64 * 1024
HTTP_TIMEOUT_SECONDS = (3.05, 10.0)
STAGING_BROKER_HOST = "staging-api.leafdesign.ai"
CANARY_RUNNER_PROFILE_ID = "spine"
CANARY_PLAN = "starter"
CANARY_SCOPE = "tenant:services"
CANARY_ALLOWED_SERVICES = ("time",)
CANARY_ALLOWED_EFFECTS = ("read",)

_CANARY_ID = "leaf-platform-deployment-canary"


class ProbeError(RuntimeError):
    """The staging probe could not prove the authenticated broker contract."""


class Signer(Protocol):
    def issue(
        self,
        claims: Mapping[str, Any],
        *,
        audience: str,
        ttl_seconds: int,
    ) -> tuple[str, int]: ...


@dataclass(frozen=True)
class CanaryAuthority:
    tenant_id: str
    subject_id: str
    session_id: str
    authority_turn_id: str
    subscription_mount_id: str
    runner_profile_id: str
    plan: str
    scope: str
    allowed_services: tuple[str, ...]
    allowed_effects: tuple[str, ...]
    token_jti: str


@dataclass(frozen=True)
class CanaryAttachment:
    bearer: str
    channel_secret: str
    expires_at: int
    authority: CanaryAuthority


@dataclass(frozen=True)
class JsonResponse:
    status_code: int
    headers: Mapping[str, str]
    body: Mapping[str, Any]


Transport = Callable[[str, Mapping[str, str], Mapping[str, Any]], JsonResponse]


def _require_staging_enable() -> None:
    if os.environ.get(ENABLE_ENV, "").strip() != "1":
        raise ProbeError("the staging probe is not explicitly enabled")
    if os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() != "staging":
        raise ProbeError("the staging probe can run only in staging")


def _staging_mcp_url() -> str:
    _require_staging_enable()
    base = os.environ.get(BROKER_URL_ENV, "").strip()
    parsed = urlparse(base)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProbeError("the broker URL must be the staging HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != STAGING_BROKER_HOST
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ProbeError("the broker URL must be an HTTPS origin")
    return base.rstrip("/") + "/mcp"


def create_canary_attachment(
    authority_signer: Signer | None = None,
) -> CanaryAttachment:
    """Mint one least-authority, channel-bound deployment canary attachment."""

    _require_staging_enable()
    channel_secret = secrets.token_urlsafe(48)
    channel_hash = hashlib.sha256(channel_secret.encode("utf-8")).hexdigest()
    session_id = f"{_CANARY_ID}-session-{secrets.token_urlsafe(18)}"
    turn_id = f"{_CANARY_ID}-turn-{secrets.token_urlsafe(18)}"
    authority_claims = dict(
        tenant_id=_CANARY_ID,
        subject_id=_CANARY_ID,
        session_id=session_id,
        authority_turn_id=turn_id,
        subscription_mount_id=_CANARY_ID,
        runner_profile_id=CANARY_RUNNER_PROFILE_ID,
        plan=CANARY_PLAN,
        scope=CANARY_SCOPE,
        allowed_services=CANARY_ALLOWED_SERVICES,
        allowed_effects=CANARY_ALLOWED_EFFECTS,
    )
    active_signer = authority_signer or mcp_authority.signer()
    bearer, expires_at = active_signer.issue(
        {
            "sub": authority_claims["subject_id"],
            "tenant_id": authority_claims["tenant_id"],
            "subject_id": authority_claims["subject_id"],
            "session_id": authority_claims["session_id"],
            "authority_turn_id": authority_claims["authority_turn_id"],
            "subscription_mount_id": authority_claims["subscription_mount_id"],
            "runner_profile_id": authority_claims["runner_profile_id"],
            "plan": authority_claims["plan"],
            "allowed_services": list(authority_claims["allowed_services"]),
            "allowed_effects": list(authority_claims["allowed_effects"]),
            "channel_hash": channel_hash,
            "scope": authority_claims["scope"],
        },
        audience=mcp_authority.attachment_audience(),
        ttl_seconds=CANARY_TTL_SECONDS,
    )
    authority = CanaryAuthority(
        **authority_claims,
        token_jti=_signed_token_jti(bearer),
    )
    return CanaryAttachment(
        bearer=bearer,
        channel_secret=channel_secret,
        expires_at=expires_at,
        authority=authority,
    )


def _signed_token_jti(bearer: str) -> str:
    """Read the signer-issued JTI from the local JWT without exposing it."""

    segments = bearer.split(".")
    if len(segments) != 3 or not segments[1] or len(segments[1]) > 16_384:
        raise ProbeError("the attachment authority returned an invalid bearer")
    padding = "=" * (-len(segments[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode((segments[1] + padding).encode("ascii"))
        payload = json.loads(raw)
    except (binascii.Error, UnicodeEncodeError, ValueError, TypeError) as exc:
        raise ProbeError("the attachment authority returned an invalid bearer") from exc
    jti = payload.get("jti") if isinstance(payload, dict) else None
    if (
        type(jti) is not str
        or not 16 <= len(jti) <= 256
        or jti != jti.strip()
    ):
        raise ProbeError("the attachment authority returned an invalid token ID")
    return jti


def _http_post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> JsonResponse:
    """Post one bounded JSON request without following credential-bearing redirects."""

    try:
        import requests

        response = requests.post(
            url,
            headers=dict(headers),
            json=dict(payload),
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001 - never expose provider details
        raise ProbeError("the broker request failed") from exc
    try:
        normalized_headers = {
            str(key).lower(): str(value) for key, value in response.headers.items()
        }
        if response.status_code != 200:
            return JsonResponse(
                status_code=response.status_code,
                headers=normalized_headers,
                body={},
            )
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if media_type != "application/json":
            raise ProbeError("the broker did not return JSON")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ProbeError("the broker response exceeded the probe limit")
            chunks.append(bytes(chunk))
        try:
            body = json.loads(b"".join(chunks))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ProbeError("the broker returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ProbeError("the broker returned an invalid JSON-RPC envelope")
        return JsonResponse(
            status_code=response.status_code,
            headers=normalized_headers,
            body=body,
        )
    finally:
        response.close()


def _validate_initialize(response: JsonResponse, request_id: int) -> None:
    body = response.body
    result = body.get("result")
    if (
        response.status_code != 200
        or body.get("jsonrpc") != "2.0"
        or body.get("id") != request_id
        or "error" in body
        or not isinstance(result, dict)
        or result.get("protocolVersion") != MCP_PROTOCOL_VERSION
        or not isinstance(result.get("capabilities"), dict)
        or not isinstance(result.get("serverInfo"), dict)
        or not isinstance(result["serverInfo"].get("name"), str)
        or not result["serverInfo"]["name"]
        or not isinstance(result["serverInfo"].get("version"), str)
        or not result["serverInfo"]["version"]
    ):
        raise ProbeError("the MCP initialize contract did not match")


def _validate_rejection(response: JsonResponse, allowed_statuses: frozenset[int]) -> None:
    if response.status_code not in allowed_statuses:
        raise ProbeError("the broker did not reject an invalid credential challenge")


def _validate_wrong_channel_result(
    response: JsonResponse, request_id: int
) -> None:
    body = response.body
    result = body.get("result")
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if (
        response.status_code != 200
        or body.get("jsonrpc") != "2.0"
        or body.get("id") != request_id
        or "error" in body
        or not isinstance(result, dict)
        or result.get("isError") is True
        or not isinstance(structured, dict)
        or structured.get("status") != "error"
        or structured.get("code") not in {"invalid_channel", "token_replay"}
    ):
        raise ProbeError("the broker did not reject the wrong-channel challenge")


def _validate_status(
    response: JsonResponse,
    authority: CanaryAuthority,
    request_id: int,
) -> str:
    body = response.body
    result = body.get("result")
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if (
        response.status_code != 200
        or body.get("jsonrpc") != "2.0"
        or body.get("id") != request_id
        or "error" in body
        or not isinstance(result, dict)
        or result.get("isError") is True
        or not isinstance(structured, dict)
        or structured.get("status") != "ready"
        or structured.get("contract_version") != "1"
        or structured.get("tenant_id") != authority.tenant_id
        or structured.get("subject_id") != authority.subject_id
        or structured.get("session_id") != authority.session_id
        or structured.get("authority_turn_id") != authority.authority_turn_id
        or structured.get("subscription_mount_id")
        != authority.subscription_mount_id
        or structured.get("runner_profile_id") != authority.runner_profile_id
        or structured.get("plan") != authority.plan
        or structured.get("scope") != authority.scope
        or structured.get("allowed_services") != list(authority.allowed_services)
        or structured.get("allowed_effects") != list(authority.allowed_effects)
        or structured.get("token_jti_digest")
        != hashlib.sha256(authority.token_jti.encode("utf-8")).hexdigest()
        or type(structured.get("available_tool_count")) is not int
        or structured["available_tool_count"] != 1
    ):
        raise ProbeError("the broker status contract did not match")
    return structured["contract_version"]


def _initialize_payload(request_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "leaf-platform-staging-probe",
                "version": "1",
            },
        },
    }


def _status_payload(request_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "services_status", "arguments": {}},
    }


def _invalid_signature_bearer(bearer: str) -> str:
    segments = bearer.split(".")
    if len(segments) != 3 or not segments[2]:
        raise ProbeError("the attachment authority returned an invalid bearer")
    first = "A" if segments[2][0] != "A" else "B"
    segments[2] = first + segments[2][1:]
    return ".".join(segments)


def run_probe(
    *,
    authority_signer: Signer | None = None,
    transport: Transport | None = None,
) -> str:
    """Run the private staging probe and return the proven contract version."""

    url = _staging_mcp_url()
    challenge_attachment = create_canary_attachment(authority_signer)
    request = transport or _http_post_json
    base_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    invalid_bearer_headers = {
        **base_headers,
        "Authorization": (
            f"Bearer {_invalid_signature_bearer(challenge_attachment.bearer)}"
        ),
        "x-leaf-gateway-channel": challenge_attachment.channel_secret,
    }
    _validate_rejection(
        request(url, invalid_bearer_headers, _initialize_payload(91)),
        frozenset({401}),
    )

    challenge_headers = {
        **base_headers,
        "Authorization": f"Bearer {challenge_attachment.bearer}",
        "x-leaf-gateway-channel": challenge_attachment.channel_secret,
    }
    challenge_initialized = request(
        url, challenge_headers, _initialize_payload(11)
    )
    _validate_initialize(challenge_initialized, 11)
    challenge_status_headers = dict(challenge_headers)
    challenge_session_id = challenge_initialized.headers.get("mcp-session-id")
    if challenge_session_id:
        challenge_status_headers["mcp-session-id"] = challenge_session_id
    challenge_status_headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    challenge_status = request(
        url, challenge_status_headers, _status_payload(12)
    )
    _validate_status(challenge_status, challenge_attachment.authority, 12)

    wrong_channel = secrets.token_urlsafe(48)
    if secrets.compare_digest(wrong_channel, challenge_attachment.channel_secret):
        raise ProbeError("the wrong-channel challenge collided with the valid channel")
    wrong_channel_headers = {
        **challenge_status_headers,
        "x-leaf-gateway-channel": wrong_channel,
    }
    _validate_wrong_channel_result(
        request(url, wrong_channel_headers, _status_payload(13)),
        13,
    )

    attachment = create_canary_attachment(authority_signer)
    if (
        attachment.bearer == challenge_attachment.bearer
        or secrets.compare_digest(
            attachment.channel_secret, challenge_attachment.channel_secret
        )
        or attachment.authority.session_id
        == challenge_attachment.authority.session_id
        or attachment.authority.authority_turn_id
        == challenge_attachment.authority.authority_turn_id
        or attachment.authority.token_jti
        == challenge_attachment.authority.token_jti
    ):
        raise ProbeError("the final attachment was not fresh")

    headers = {
        **base_headers,
        "Authorization": f"Bearer {attachment.bearer}",
        "x-leaf-gateway-channel": attachment.channel_secret,
    }
    initialized = request(
        url,
        headers,
        _initialize_payload(1),
    )
    _validate_initialize(initialized, 1)

    second_headers = dict(headers)
    session_id = initialized.headers.get("mcp-session-id")
    if session_id:
        second_headers["mcp-session-id"] = session_id
    second_headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    status = request(
        url,
        second_headers,
        _status_payload(2),
    )
    return _validate_status(status, attachment.authority, 2)


def main() -> int:
    try:
        contract_version = run_probe()
    except Exception:  # noqa: BLE001 - command output must never expose credentials
        print("tenant_mcp_staging_probe=FAIL")
        return 1
    print(
        "tenant_mcp_staging_probe=PASS "
        f"contract_version={contract_version}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
