"""Bounded authenticated staging probe for the tenant MCP broker.

This module is a private ECS task command, not an HTTP route. It mints one
deployment-canary attachment with the app task's existing KMS authority and
uses it only long enough to prove the broker's authenticated MCP contract.
"""
from __future__ import annotations

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
class CanaryAttachment:
    bearer: str
    channel_secret: str
    expires_at: int


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
    active_signer = authority_signer or mcp_authority.signer()
    bearer, expires_at = active_signer.issue(
        {
            "sub": _CANARY_ID,
            "tenant_id": _CANARY_ID,
            "subject_id": _CANARY_ID,
            "session_id": session_id,
            "authority_turn_id": turn_id,
            "subscription_mount_id": _CANARY_ID,
            "runner_profile_id": mcp_authority.runner_profile_id(),
            "plan": "starter",
            "allowed_services": ["time"],
            "allowed_effects": ["read"],
            "channel_hash": channel_hash,
            "scope": "tenant:services",
        },
        audience=mcp_authority.attachment_audience(),
        ttl_seconds=CANARY_TTL_SECONDS,
    )
    return CanaryAttachment(
        bearer=bearer,
        channel_secret=channel_secret,
        expires_at=expires_at,
    )


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
        if response.status_code != 200:
            raise ProbeError("the broker returned a non-success status")
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
            headers={str(key).lower(): str(value) for key, value in response.headers.items()},
            body=body,
        )
    finally:
        response.close()


def _validate_initialize(response: JsonResponse) -> None:
    body = response.body
    result = body.get("result")
    if (
        response.status_code != 200
        or body.get("jsonrpc") != "2.0"
        or body.get("id") != 1
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


def _validate_status(response: JsonResponse) -> str:
    body = response.body
    result = body.get("result")
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if (
        response.status_code != 200
        or body.get("jsonrpc") != "2.0"
        or body.get("id") != 2
        or "error" in body
        or not isinstance(result, dict)
        or result.get("isError") is True
        or not isinstance(structured, dict)
        or structured.get("status") != "ready"
        or structured.get("contract_version") != "1"
        or type(structured.get("available_tool_count")) is not int
        or structured["available_tool_count"] < 1
    ):
        raise ProbeError("the broker status contract did not match")
    return structured["contract_version"]


def run_probe(
    *,
    authority_signer: Signer | None = None,
    transport: Transport | None = None,
) -> str:
    """Run the private staging probe and return the proven contract version."""

    url = _staging_mcp_url()
    attachment = create_canary_attachment(authority_signer)
    request = transport or _http_post_json
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {attachment.bearer}",
        "Content-Type": "application/json",
        "x-leaf-gateway-channel": attachment.channel_secret,
    }
    initialized = request(
        url,
        headers,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "leaf-platform-staging-probe",
                    "version": "1",
                },
            },
        },
    )
    _validate_initialize(initialized)

    second_headers = dict(headers)
    session_id = initialized.headers.get("mcp-session-id")
    if session_id:
        second_headers["mcp-session-id"] = session_id
    second_headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    status = request(
        url,
        second_headers,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "services_status", "arguments": {}},
        },
    )
    return _validate_status(status)


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
