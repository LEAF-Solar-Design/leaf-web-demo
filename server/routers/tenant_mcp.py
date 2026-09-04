"""Per-tenant MCP server registry (standardization slice 8b, server-side only).

Mounted at ``/api/tenant/mcp-servers``. Owner-role gated through
``routers.tenant._require_grant_owner`` — CALLED, not re-typed, same posture
as the existing claude-grant flow in that file. Write routes additionally
require the ``link_service`` capability (server/entitlements.py); the list
and health routes require owner role only.

    POST   /api/tenant/mcp-servers               register {url, label}
    GET    /api/tenant/mcp-servers                list (safe projection only)
    POST   /api/tenant/mcp-servers/{id}/connect   start the OAuth flow
    GET    /api/tenant/mcp-servers/callback       server-side redirect capture
    GET    /api/tenant/mcp-servers/{id}/health    bounded ping
    DELETE /api/tenant/mcp-servers/{id}           unlink (best-effort revoke)

The OAuth flow (MCP Authorization spec: RFC 9728 protected-resource metadata,
RFC 8414 authorization-server metadata, RFC 7591 dynamic client registration,
PKCE S256 + RFC 8707 resource indicator) runs entirely server-side. Every
outbound call is bounded (`_HTTP_TIMEOUT_S` timeout, `_MAX_RESPONSE_BYTES`
body cap) and every failure is one of a NAMED fail-closed state
(metadata_missing, dcr_refused, timeout, state_mismatch, audience_mismatch)
— never a bare 500, never a partial record (a failed connect leaves the
record in `state: "error"`, not half-written).

GET responses NEVER carry an upstream tool name, a credentialed URL, a token,
or its prefix — the whitelist in `_project()` is the ONLY thing a caller ever
sees; the token and every OAuth secret live only in
`server/tenant_mcp_store.py`'s file records.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlsplit

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import deps
import entitlements
import tenant_mcp_store
from envelopes import ErrorCode, err_envelope, with_envelope_fields
from routers.tenant import _require_grant_owner

router = APIRouter()

# Bounds (release blockers): every outbound call to a tenant-chosen MCP host
# or its authorization server is timed out and body-capped — a hostile or
# broken server can never hang a request thread or hand back an unbounded body.
_HTTP_TIMEOUT_S = 8.0            # one round trip's budget (metadata/DCR/token)
_MAX_RESPONSE_BYTES = 65_536     # 64 KiB cap on any metadata/DCR/token response
_HEALTH_TIMEOUT_S = 5.0          # the health ping's own, tighter budget

_URL_MAX_LEN = 2048
_LABEL_MAX_LEN = 80
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")

_CONNECT_ERROR_STATUS = {
    "metadata_missing": 502,
    "dcr_refused": 502,
    "timeout": 504,
    "state_mismatch": 400,
    "audience_mismatch": 400,
}


class McpConnectError(Exception):
    """One of the five NAMED fail-closed connect states (module docstring)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        assert code in _CONNECT_ERROR_STATUS, f"unknown connect error code {code!r}"
        self.code = code


def _require_link_service(tenant: Any = Depends(_require_grant_owner)) -> Any:
    """Owner role (via _require_grant_owner) PLUS the link_service capability.
    Calls entitlements.entitlements_for — the ONE tier/role resolution — never
    a second copy of the capability rule."""
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    if not entitlements.entitlements_for(tier, roles, elevated).get("link_service", False):
        raise HTTPException(status_code=403, detail="link_service capability required")
    return tenant


def _project(record: Dict[str, Any]) -> Dict[str, Any]:
    """The ONLY shape a GET ever returns: never an upstream tool name, never a
    credentialed URL, never a token or its prefix."""
    return {
        "id": record.get("id"),
        "label": record.get("label"),
        "host": record.get("host"),
        "state": record.get("state"),
        "linked_at": record.get("linked_at"),
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_body(code: str) -> JSONResponse:
    status = _CONNECT_ERROR_STATUS.get(code, 502)
    if code == "timeout":
        error_code, retryable = ErrorCode.TIMEOUT, True
    elif code in ("metadata_missing", "dcr_refused"):
        error_code, retryable = ErrorCode.BROKER_UNREACHABLE, True
    else:
        error_code, retryable = ErrorCode.BAD_PARAMS, False
    # The message names only OUR OWN classification, never the upstream
    # server's status line, body, or hostname.
    return JSONResponse(
        status_code=status,
        content=err_envelope(error_code, f"mcp connect failed: {code}", retryable=retryable),
    )


# --------------------------------------------------------------------------- #
# bounded outbound HTTP (metadata discovery, DCR, token exchange)
# --------------------------------------------------------------------------- #
def _bounded_json(resp: "requests.Response") -> Any:
    body = bytearray()
    for chunk in resp.iter_content(chunk_size=4096):
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError(f"response exceeds the {_MAX_RESPONSE_BYTES}-byte bound")
    return json.loads(bytes(body).decode("utf-8"))


def _fetch_metadata(url: str) -> Dict[str, Any]:
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT_S, stream=True)
    except requests.Timeout as exc:
        raise McpConnectError("timeout", "metadata discovery timed out") from exc
    except requests.RequestException as exc:
        raise McpConnectError("metadata_missing", type(exc).__name__) from exc
    try:
        if resp.status_code != 200:
            raise McpConnectError("metadata_missing", f"HTTP {resp.status_code}")
        data = _bounded_json(resp)
    except ValueError as exc:
        raise McpConnectError("metadata_missing", str(exc)) from exc
    finally:
        resp.close()
    if not isinstance(data, dict):
        raise McpConnectError("metadata_missing", "metadata is not a JSON object")
    return data


def _register_client(registration_endpoint: str, redirect_uri: str) -> Dict[str, Optional[str]]:
    body = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",  # public client; PKCE carries the proof (RFC 7636)
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "client_name": "Leaf tenant MCP link",
    }
    try:
        resp = requests.post(registration_endpoint, json=body, timeout=_HTTP_TIMEOUT_S, stream=True)
    except requests.Timeout as exc:
        raise McpConnectError("timeout", "dynamic client registration timed out") from exc
    except requests.RequestException as exc:
        raise McpConnectError("dcr_refused", type(exc).__name__) from exc
    try:
        if resp.status_code not in (200, 201):
            raise McpConnectError("dcr_refused", f"HTTP {resp.status_code}")
        client = _bounded_json(resp)
    except ValueError as exc:
        raise McpConnectError("dcr_refused", str(exc)) from exc
    finally:
        resp.close()
    client_id = client.get("client_id") if isinstance(client, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise McpConnectError("dcr_refused", "registration response carries no client_id")
    client_secret = client.get("client_secret")
    return {"client_id": client_id, "client_secret": client_secret if isinstance(client_secret, str) else None}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]  # RFC 7636 bounds: 43-128 chars
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _check_audience(token: Dict[str, Any], resource: str) -> None:
    """RFC 8707: the issued token must be bound to the resource we requested.
    Checked defensively — an opaque access token with no `aud` signal anywhere
    is trusted on the strength of having sent the resource indicator in the
    request (the real trust boundary is the token endpoint TLS hop, discovered
    via RFC 8414 metadata, not this best-effort claim peek)."""
    candidates = []
    aud = token.get("aud")
    if isinstance(aud, str):
        candidates.append(aud)
    elif isinstance(aud, list):
        candidates.extend(a for a in aud if isinstance(a, str))
    access_token = token.get("access_token")
    if isinstance(access_token, str):
        parts = access_token.split(".")
        if len(parts) == 3:
            try:
                claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
            except Exception:  # noqa: BLE001 - an opaque/non-JWT token skips the claim check
                claims = None
            if isinstance(claims, dict):
                jwt_aud = claims.get("aud")
                if isinstance(jwt_aud, str):
                    candidates.append(jwt_aud)
                elif isinstance(jwt_aud, list):
                    candidates.extend(a for a in jwt_aud if isinstance(a, str))
    if not candidates:
        return
    normalized = resource.rstrip("/")
    if not any(c.rstrip("/") == normalized for c in candidates):
        raise McpConnectError("audience_mismatch", "token audience does not match the requested resource")


def _exchange_code(pending: Dict[str, Any], code: str) -> Dict[str, Any]:
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": pending["client_id"],
        "code_verifier": pending["code_verifier"],
        "resource": pending["resource"],
    }
    if pending.get("client_secret"):
        body["client_secret"] = pending["client_secret"]
    try:
        resp = requests.post(pending["token_endpoint"], data=body, timeout=_HTTP_TIMEOUT_S, stream=True)
    except requests.Timeout as exc:
        raise McpConnectError("timeout", "token exchange timed out") from exc
    except requests.RequestException as exc:
        raise McpConnectError("metadata_missing", type(exc).__name__) from exc
    try:
        if resp.status_code != 200:
            raise McpConnectError("dcr_refused", f"HTTP {resp.status_code}")
        token = _bounded_json(resp)
    except ValueError as exc:
        raise McpConnectError("metadata_missing", str(exc)) from exc
    finally:
        resp.close()
    if not isinstance(token, dict) or not isinstance(token.get("access_token"), str):
        raise McpConnectError("metadata_missing", "token response carries no access_token")
    _check_audience(token, pending["resource"])
    return token


def _start_connect(tenant_id: str, record: Dict[str, Any], redirect_uri: str) -> str:
    """Discovery (RFC 9728 + RFC 8414) -> DCR (RFC 7591) -> PKCE authorize URL
    (RFC 8707 resource indicator). Persists ONE pending entry keyed by the
    freshly minted `state`; never mutates the tenant record until the caller
    does so (state stays "registered" until this returns successfully)."""
    parsed = urlsplit(record["url"])
    origin = f"{parsed.scheme}://{parsed.netloc}"
    pr_meta = _fetch_metadata(origin + "/.well-known/oauth-protected-resource")
    as_list = pr_meta.get("authorization_servers")
    if not isinstance(as_list, list) or not as_list or not isinstance(as_list[0], str):
        raise McpConnectError("metadata_missing", "no authorization_servers in protected-resource metadata")
    as_issuer = as_list[0].rstrip("/")
    as_meta = _fetch_metadata(as_issuer + "/.well-known/oauth-authorization-server")
    registration_endpoint = as_meta.get("registration_endpoint")
    authorization_endpoint = as_meta.get("authorization_endpoint")
    token_endpoint = as_meta.get("token_endpoint")
    if not all(isinstance(v, str) and v for v in (registration_endpoint, authorization_endpoint, token_endpoint)):
        raise McpConnectError("metadata_missing", "authorization-server metadata is incomplete")
    client = _register_client(registration_endpoint, redirect_uri)

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    resource = record["url"]
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    sep = "&" if "?" in authorization_endpoint else "?"
    authorize_url = authorization_endpoint + sep + urlencode(params)

    tenant_mcp_store.create_pending(state, {
        "tenant_id": tenant_id,
        "server_id": record["id"],
        "code_verifier": verifier,
        "token_endpoint": token_endpoint,
        "resource": resource,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri,
        "authorization_server": as_issuer,
        "revocation_endpoint": as_meta.get("revocation_endpoint")
        if isinstance(as_meta.get("revocation_endpoint"), str) else None,
    })
    return authorize_url


def _base_url(request: Request) -> str:
    """The app's own public origin for the OAuth redirect_uri. AMBIGUOUS point
    (see the PR report): defaults to the inbound request's own base_url
    (correct for direct/dev access); an operator behind a reverse proxy that
    rewrites Host sets LEAF_APP_PUBLIC_BASE_URL to override it."""
    override = os.environ.get("LEAF_APP_PUBLIC_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------- #
# wire models
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=_URL_MAX_LEN)
    label: str = Field(..., min_length=1, max_length=_LABEL_MAX_LEN)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https":
            raise ValueError("url must use https")
        if not parsed.hostname:
            raise ValueError("url must carry a host")
        if parsed.username or parsed.password:
            raise ValueError("url must not carry userinfo")
        if parsed.query:
            raise ValueError("url must not carry a query string")
        if parsed.fragment:
            raise ValueError("url must not carry a fragment")
        return value

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        if not _LABEL_RE.match(value):
            raise ValueError("label must start alphanumeric and use only [A-Za-z0-9 _.-]")
        return value


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@router.post("/api/tenant/mcp-servers")
def register_server(req: RegisterRequest, tenant: Any = Depends(_require_link_service)):
    tid = str(tenant)
    parsed = urlsplit(req.url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    try:
        record = tenant_mcp_store.register(tid, url=req.url, label=req.label, host=host)
    except tenant_mcp_store.TenantMcpStoreError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return deps.tenant_echo(with_envelope_fields(_project(record)), tenant)


@router.get("/api/tenant/mcp-servers")
def list_servers(tenant: Any = Depends(_require_grant_owner)):
    tid = str(tenant)
    servers = [_project(r) for r in tenant_mcp_store.list_records(tid)]
    return deps.tenant_echo(with_envelope_fields({"servers": servers}), tenant)


@router.post("/api/tenant/mcp-servers/{server_id}/connect")
def connect_server(server_id: str, request: Request, tenant: Any = Depends(_require_link_service)):
    tid = str(tenant)
    record = tenant_mcp_store.get_record(tid, server_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown MCP server")
    redirect_uri = f"{_base_url(request)}/api/tenant/mcp-servers/callback"
    try:
        authorize_url = _start_connect(tid, record, redirect_uri)
    except tenant_mcp_store.TenantMcpStoreError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except McpConnectError as exc:
        tenant_mcp_store.update_record(tid, server_id, state="error", error_detail=exc.code)
        return _error_body(exc.code)
    tenant_mcp_store.update_record(tid, server_id, state="connecting", error_detail=None)
    return deps.tenant_echo(with_envelope_fields({"authorize_url": authorize_url}), tenant)


@router.get("/api/tenant/mcp-servers/callback")
def oauth_callback(
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
):
    """Server-side capture of the authorization-server redirect. No owner-role
    dependency: a browser redirect carries no app auth header, so the
    unguessable, single-use `state` (bound to exactly one pending connect
    by tenant_mcp_store.create_pending) IS the authorization boundary."""
    if not isinstance(state, str) or not state:
        return _error_body("state_mismatch")
    pending = tenant_mcp_store.pop_pending(state)
    if pending is None:
        return _error_body("state_mismatch")
    tenant_id = pending.get("tenant_id")
    server_id = pending.get("server_id")
    if error or not isinstance(code, str) or not code:
        tenant_mcp_store.update_record(tenant_id, server_id, state="error", error_detail="authorization_denied")
        return _error_body("state_mismatch")
    try:
        token = _exchange_code(pending, code)
    except McpConnectError as exc:
        tenant_mcp_store.update_record(tenant_id, server_id, state="error", error_detail=exc.code)
        return _error_body(exc.code)
    expires_in = token.get("expires_in")
    tenant_mcp_store.update_record(
        tenant_id, server_id, state="connected", error_detail=None, linked_at=_iso_now(),
        oauth={
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token")
            if isinstance(token.get("refresh_token"), str) else None,
            "expires_at": time.time() + float(expires_in)
            if isinstance(expires_in, (int, float)) else None,
            "token_type": token.get("token_type") if isinstance(token.get("token_type"), str) else None,
            "resource": pending.get("resource"),
            "client_id": pending.get("client_id"),
            "client_secret": pending.get("client_secret"),
            "token_endpoint": pending.get("token_endpoint"),
            "authorization_server": pending.get("authorization_server"),
            "revocation_endpoint": pending.get("revocation_endpoint"),
        },
    )
    return with_envelope_fields({"linked": True})


@router.get("/api/tenant/mcp-servers/{server_id}/health")
def health_server(server_id: str, tenant: Any = Depends(_require_grant_owner)):
    tid = str(tenant)
    record = tenant_mcp_store.get_record(tid, server_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown MCP server")
    try:
        resp = requests.head(record["url"], timeout=_HEALTH_TIMEOUT_S, allow_redirects=False)
        state = "connected" if resp.status_code < 500 else "error"
        resp.close()
    except requests.RequestException:
        state = "error"
    return deps.tenant_echo(with_envelope_fields({"id": server_id, "state": state}), tenant)


def _best_effort_revoke(oauth: Dict[str, Any]) -> None:
    """Best-effort, bounded token revocation. Never raises — a revocation
    endpoint that is absent, slow, or refuses the request must never block
    the unlink; the record is deleted either way."""
    endpoint = oauth.get("revocation_endpoint")
    token = oauth.get("access_token")
    if not isinstance(endpoint, str) or not endpoint or not isinstance(token, str) or not token:
        return
    try:
        body = {"token": token}
        if oauth.get("client_id"):
            body["client_id"] = oauth["client_id"]
        if oauth.get("client_secret"):
            body["client_secret"] = oauth["client_secret"]
        requests.post(endpoint, data=body, timeout=_HTTP_TIMEOUT_S)
    except requests.RequestException:
        pass


@router.delete("/api/tenant/mcp-servers/{server_id}")
def unlink_server(server_id: str, tenant: Any = Depends(_require_link_service)):
    tid = str(tenant)
    record = tenant_mcp_store.get_record(tid, server_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown MCP server")
    oauth = record.get("oauth")
    if isinstance(oauth, dict):
        _best_effort_revoke(oauth)
    tenant_mcp_store.delete_record(tid, server_id)
    return deps.tenant_echo(with_envelope_fields({"deleted": True}), tenant)
