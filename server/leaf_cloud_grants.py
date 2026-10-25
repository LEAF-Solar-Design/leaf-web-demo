"""Broker-only private grants for Leaf Automation cloud adapters.

LEAF_CLOUD_GRANTS_FILE names an operator-owned file outside the repository.
Its mapping keys are public grant references; each value has tenant_id,
audience, expires_at (Unix seconds), and access_token. Provision from staging
Auth0 sign-in. Never accept this mapping from a job or emit it in a receipt.
The m2m-staging source instead mints a process-cached machine token, restricted
to staging and the tenant bindings in LEAF_CLOUD_M2M_GRANTS.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field


class CloudError(ValueError):
    def __init__(self, classification: str, status: int):
        super().__init__(classification)
        self.classification = classification
        self.status = status


@dataclass(frozen=True)
class CloudGrant:
    tenant_id: str
    access_token: str = field(repr=False)


_M2M_AUDIENCE = "https://api.leafdesign.ai"
_m2m_lock = threading.Lock()


@dataclass(frozen=True, repr=False)
class _M2MCache:
    credential_digest: str
    token: str
    expires_at: float
    audience: str = _M2M_AUDIENCE


_m2m_cache: _M2MCache | None = None


def reset_m2m_grant_cache() -> None:
    global _m2m_cache
    with _m2m_lock:
        _m2m_cache = None


def _mint_m2m_token(client_id: str, client_secret: str) -> tuple[str, float]:
    started = time.monotonic()
    deadline = started + 10

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value

    # stdlib DNS resolution (getaddrinfo) cannot be interrupted by a socket timeout.
    connection = http.client.HTTPSConnection("leafautomation.us.auth0.com", timeout=remaining())
    response = None
    try:
        connection.connect()
        transport_socket = connection.sock
        transport_socket.settimeout(remaining())
        connection.request("POST", "/oauth/token", body=json.dumps({
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret, "audience": _M2M_AUDIENCE,
        }).encode("utf-8"), headers={"Content-Type": "application/json"})
        transport_socket.settimeout(remaining())
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError()
        raw = bytearray()
        while True:
            transport_socket.settimeout(remaining())
            chunk = response.read1(65537 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 65536:
                raise ValueError()
        remaining()
        payload = json.loads(raw)
        token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                or any(c.isspace() for c in token)
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or type(expires_in) is not int or not 60 <= expires_in <= 2592000):
            raise ValueError()
        remaining()
        return token, started + expires_in
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def _resolve_m2m_grant(reference: str, tenant_id: str) -> CloudGrant:
    global _m2m_cache
    if os.environ.get("LEAF_RUNTIME_ENV") != "staging":
        raise CloudError("cloud_auth_missing", 401)
    invalid_allowlist = False
    try:
        raw = os.environ.get("LEAF_CLOUD_M2M_GRANTS", "")
        if len(raw.encode("utf-8")) > 4096:
            raise ValueError()
        pairs = raw.split(",")
        if not 1 <= len(pairs) <= 16:
            raise ValueError()
        grants = {}
        for pair in pairs:
            key, value = pair.split("=")
            if (not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                    or key in grants):
                raise ValueError()
            grants[key] = value
        if reference not in grants:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        invalid_allowlist = True
    if invalid_allowlist:
        raise CloudError("cloud_auth_missing", 401)
    if grants[reference] != tenant_id:
        raise CloudError("cloud_tenant_unauthorized", 403)
    client_id = os.environ.get("LEAF_CLOUD_M2M_CLIENT_ID", "")
    client_secret = os.environ.get("LEAF_CLOUD_M2M_CLIENT_SECRET", "")
    if any(not 1 <= len(value) <= 512 or any(c.isspace() for c in value)
           for value in (client_id, client_secret)):
        raise CloudError("cloud_auth_missing", 401)
    credential_digest = hashlib.sha256((client_id + "\0" + client_secret).encode("utf-8")).hexdigest()
    with _m2m_lock:
        if (_m2m_cache is None or _m2m_cache.credential_digest != credential_digest
                or _m2m_cache.audience != _M2M_AUDIENCE
                or time.monotonic() >= _m2m_cache.expires_at - 300):
            mint_failed = False
            try:
                token, expires_at = _mint_m2m_token(client_id, client_secret)
            except Exception:
                # Transport and parser exceptions can contain response bodies or credentials.
                mint_failed = True
            if mint_failed:
                raise CloudError("cloud_auth_missing", 401)
            _m2m_cache = _M2MCache(credential_digest, token, expires_at)
        return CloudGrant(tenant_id=tenant_id, access_token=_m2m_cache.token)


def resolve_grant(reference: str, tenant_id: str) -> CloudGrant:
    if not isinstance(reference, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", reference):
        raise CloudError("cloud_auth_missing", 401)
    source = os.environ.get("LEAF_CLOUD_GRANT_SOURCE", "")
    if source == "m2m-staging":
        return _resolve_m2m_grant(reference, tenant_id)
    if source:
        raise CloudError("cloud_auth_missing", 401)
    path = os.environ.get("LEAF_CLOUD_GRANTS_FILE")
    if not path:
        raise CloudError("cloud_auth_missing", 401)
    try:
        with open(path, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        grants = json.loads(raw)
        grant = grants.get(reference) if isinstance(grants, dict) else None
        if not isinstance(grant, dict):
            raise ValueError()
        token = grant.get("access_token")
        expiry = grant.get("expires_at")
        if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                or any(c.isspace() for c in token)
                or type(expiry) not in (int, float) or not math.isfinite(expiry)
                or expiry <= time.time()
                or grant.get("audience") != "https://api.leafdesign.ai"):
            raise ValueError()
    except (OSError, ValueError, TypeError, OverflowError):
        raise CloudError("cloud_auth_missing", 401) from None
    if grant.get("tenant_id") != tenant_id:
        raise CloudError("cloud_tenant_unauthorized", 403)
    return CloudGrant(tenant_id=tenant_id, access_token=token)
