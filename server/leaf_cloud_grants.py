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
_M2M_CALL_TIMEOUT = 10.0
_m2m_lock = threading.Lock()


@dataclass(frozen=True, repr=False)
class _M2MCache:
    credential_digest: str
    token: str
    expires_at: float
    refresh_at: float
    audience: str = _M2M_AUDIENCE


_m2m_cache: _M2MCache | None = None


@dataclass(repr=False)
class _M2MOperation:
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    result: str | None = None


_m2m_inflight: _M2MOperation | None = None
_m2m_worker: threading.Thread | None = None


def reset_m2m_grant_cache() -> None:
    global _m2m_cache, _m2m_inflight
    with _m2m_lock:
        _m2m_cache = None
        _m2m_inflight = None


def _mint_m2m_token(client_id: str, client_secret: str) -> tuple[str, int]:
    started = time.monotonic()
    deadline = started + 10

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value

    # stdlib DNS resolution (getaddrinfo) cannot be interrupted by a socket timeout.
    connection = http.client.HTTPSConnection("leafautomation.us.auth0.com", timeout=remaining())
    connection.debuglevel = 0
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
        return token, expires_in
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def _m2m_credentials() -> tuple[str, str, str]:
    # Called only on the worker, including on cache hits and at publication.
    client_id = os.environ.get("LEAF_CLOUD_M2M_CLIENT_ID", "")
    client_secret = os.environ.get("LEAF_CLOUD_M2M_CLIENT_SECRET", "")
    if any(not 1 <= len(value) <= 512 or any(not "!" <= c <= "~" for c in value)
           for value in (client_id, client_secret)):
        raise ValueError()
    digest = hashlib.sha256((client_id + "\0" + client_secret).encode("utf-8")).hexdigest()
    return client_id, client_secret, digest


def _m2m_worker_result() -> tuple[bool, str | None, int | None, str]:
    try:
        # Cache validation also runs here: the caller must never read credentials.
        client_id, client_secret, digest = _m2m_credentials()
        with _m2m_lock:
            cached = _m2m_cache
            if (cached is not None and cached.credential_digest == digest
                    and cached.audience == _M2M_AUDIENCE
                    and time.monotonic() < cached.refresh_at):
                return True, cached.token, None, digest
        token, expires_in = _mint_m2m_token(client_id, client_secret)
        return True, token, expires_in, digest
    except BaseException:
        # Parser/transport exceptions and their sensitive frames stay on this thread.
        pass
    return False, None, None, ""


def _run_m2m_worker(operation: _M2MOperation) -> None:
    global _m2m_cache, _m2m_inflight
    token = None
    try:
        mint_time = time.monotonic()
        ok, token, expires_in, digest = _m2m_worker_result()
        with _m2m_lock:
            if _m2m_inflight is operation:
                current_digest = _m2m_credentials()[2] if ok else ""
                if ok and digest == current_digest:
                    if expires_in is not None:
                        _m2m_cache = _M2MCache(
                            digest, token, mint_time + expires_in,
                            mint_time + expires_in - min(300, expires_in // 2))
                    operation.result = token
    except BaseException:
        pass
    finally:
        with _m2m_lock:
            if _m2m_inflight is operation:
                _m2m_inflight = None
            operation.done.set()


def _m2m_token(deadline: float) -> str | None:
    global _m2m_inflight, _m2m_worker
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _m2m_lock.acquire(timeout=remaining):
        return None
    try:
        if time.monotonic() >= deadline:
            return None
        operation = _m2m_inflight
        if operation is not None:
            if time.monotonic() >= operation.deadline:
                return None
            operation.deadline = max(operation.deadline, deadline)
        else:
            # A timed-out worker owns the slot until its thread actually exits.
            if _m2m_worker is not None and _m2m_worker.is_alive():
                return None
            operation = _M2MOperation(deadline)
            _m2m_inflight = operation
            _m2m_worker = threading.Thread(target=_run_m2m_worker, args=(operation,), daemon=True)
            try:
                _m2m_worker.start()
            except Exception:
                _m2m_inflight = None
                return None
    finally:
        _m2m_lock.release()
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not operation.done.wait(remaining):
        return None
    if time.monotonic() >= deadline:
        return None
    return operation.result


def _resolve_m2m_grant(reference: str, tenant_id: str) -> CloudGrant:
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
    deadline = time.monotonic() + _M2M_CALL_TIMEOUT
    token = _m2m_token(deadline)
    if token is None:
        raise CloudError("cloud_auth_missing", 401)
    return CloudGrant(tenant_id=tenant_id, access_token=token)


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
