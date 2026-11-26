"""Durable, dark-by-default demo identity with explicit W1 route grants.

Credential precedence (shared by server and platform), first matching row wins:
  auth off + demo                 -> 401 (never the development tenant stub)
  verified dispatch leg + demo   -> 401 (never dispatch authority)
  any Authorization header       -> Bearer only, including empty/bad headers
  guest + demo, no Authorization  -> 400
  demo alone, auth live           -> verify durable demo identity
  otherwise                      -> existing account/guest/development path

Identity resolution is deliberately separate from explicit route admission.
Configuration: LEAF_DEMO_LIVE_ENABLED must equal "1"; session signing uses
LEAF_DEMO_SESSION_SECRET, address HMAC uses LEAF_DEMO_IP_HASH_SECRET, and mint
requires positive LEAF_DEMO_PER_IP_DAILY_CAP and LEAF_DEMO_GLOBAL_DAILY_CAP.
LEAF_DEMO_TRUSTED_PROXIES is an optional comma-separated IP/CIDR set. Only
X-Forwarded-For from an immediate peer in that set is trusted; walk it from
right to left and select the first untrusted hop. Never retain raw addresses.

ASGI proxy rewriting must not erase the socket peer's provenance. Minting
refuses FORWARDED_ALLOW_IPS='*', malformed or universal proxy ranges, and any
forwarder not also named by LEAF_DEMO_TRUSTED_PROXIES (including Uvicorn's
default 127.0.0.1). It also refuses Uvicorn's rewritten client (port zero).
Run with FORWARDED_ALLOW_IPS='' so this module alone walks forwarded headers;
name the actual ingress socket peers in LEAF_DEMO_TRUSTED_PROXIES. Programmatic
servers must likewise disable proxy rewriting rather than override that env.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import ipaddress
import os
from pathlib import Path
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
import uuid

from fastapi import HTTPException
from starlette.routing import compile_path


DEMO_ROUTE_ALLOWLIST = frozenset({
    ("POST", "/api/run"),
    ("GET", "/api/jobs/{job_id}"),
    ("GET", "/api/jobs/{job_id}/stream"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs/{job_id}/close"),
    ("GET", "/api/platform/capabilities"),
    ("GET", "/api/projects/{project_id}/drawing-versions/{input_version_id}/context"),
    ("GET", "/api/projects/{project_id}/drawings/{drawing_id}/checkout"),
    ("POST", "/api/projects/{project_id}/drawings/{drawing_id}/checkout"),
    ("DELETE", "/api/projects/{project_id}/drawings/{drawing_id}/checkout"),
    ("POST", "/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/solar-reads"),
    ("GET", "/api/projects/{project_id}/drawings/{drawing_id}/solar-artifacts/{artifact_id}"),
    ("POST", "/api/projects/{project_id}/conversations"),
    ("GET", "/api/projects/{project_id}/conversations"),
    ("GET", "/api/projects/{project_id}/conversations/{conversation_id}"),
    ("POST", "/api/projects/{project_id}/conversations/recover"),
    ("POST", "/api/projects/{project_id}/conversations/{conversation_id}/messages"),
    ("GET", "/api/projects/{project_id}/conversations/recovery/tail"),
    ("POST", "/api/projects/{project_id}/conversations/recovery/tail"),
    ("GET", "/api/capabilities"),
    ("GET", "/api/receipts"),
    ("POST", "/api/sessions"),
    ("GET", "/api/sessions"),
    ("POST", "/api/sessions/{session_id}/messages"),
    ("POST", "/api/sessions/{session_id}/turns/{turn_id}/cancel"),
    ("GET", "/api/sessions/{session_id}/stream"),
    ("GET", "/api/sessions/{session_id}/transcript"),
})
DEMO_ROUTE_PATTERNS = tuple(
    (method, template, compile_path(template)[0])
    for method, template in sorted(DEMO_ROUTE_ALLOWLIST)
)
_PROVISIONING_LOAD_LOCK = threading.Lock()


class DemoSession(NamedTuple):
    id: uuid.UUID
    subject: str
    tenant_id: str
    org_id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    client_ip_hash: str


class MintedSession(NamedTuple):
    token: str
    expires_at: datetime


class MintCapExceeded(HTTPException):
    def __init__(self):
        super().__init__(429, "demo session mint cap reached")


class UnsafeProxyConfiguration(HTTPException):
    def __init__(self):
        super().__init__(503, "demo session proxy configuration unavailable")


def is_enabled() -> bool:
    return os.environ.get("LEAF_DEMO_LIVE_ENABLED") == "1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret(name: str) -> bytes:
    raw = os.environ.get(name, "")
    if not raw.strip():
        raise HTTPException(503, "demo session configuration unavailable")
    return raw.encode("utf-8")


def _cap(name: str) -> int:
    raw = os.environ.get(name, "")
    if not re.fullmatch(r"[0-9]{1,10}", raw) or not 0 < int(raw) <= 2147483647:
        raise HTTPException(503, "demo session configuration unavailable")
    return int(raw)


def _address(raw: str):
    if not isinstance(raw, str) or not raw.strip() or "%" in raw:
        raise ValueError("invalid address")
    address = ipaddress.ip_address(raw.strip())
    return getattr(address, "ipv4_mapped", None) or address


def client_address(request) -> str:
    try:
        peer = _address(request.client.host if request.client else None)
        networks = [ipaddress.ip_network(part.strip(), strict=False)
                    for part in os.environ.get("LEAF_DEMO_TRUSTED_PROXIES", "").split(",")
                    if part.strip()]
    except ValueError:
        raise HTTPException(400, "demo session client address unavailable") from None

    # request.client may already have been rewritten by the ASGI server. A
    # wildcard (or an undeclared trusted socket peer) makes it caller-controlled
    # before our own right-to-left walk even starts. Refuse before DB access.
    try:
        forwarders = [ipaddress.ip_network(part.strip(), strict=False)
                      for part in os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1").split(",")
                      if part.strip()]
        # Multiple ranges can express trust-all without a literal '*' or /0.
        if any(network.prefixlen == 0
               for version in (4, 6)
               for network in ipaddress.collapse_addresses(
                   item for item in [*forwarders, *networks] if item.version == version)):
            raise ValueError("unbounded proxy trust")
        if any(not any(
                network.version == trusted_network.version and network.subnet_of(trusted_network)
                for trusted_network in networks) for network in forwarders):
            raise ValueError("unbounded or undeclared forwarder")
    except ValueError:
        raise UnsafeProxyConfiguration() from None
    if request.client.port == 0:
        raise UnsafeProxyConfiguration()

    def trusted(address):
        return any(address in network for network in networks)

    if not trusted(peer):
        return str(peer)  # Untrusted peers cannot supply their own forwarding chain.
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        raise HTTPException(400, "demo session client address unavailable")
    try:
        chain = [_address(hop) for hop in forwarded.split(",")]
    except ValueError:
        raise HTTPException(400, "demo session client address unavailable") from None
    for hop in reversed(chain):
        if not trusted(hop):
            return str(hop)
    # No untrusted client was identified. Do not hash an ingress as a client.
    raise HTTPException(400, "demo session client address unavailable")


def address_hash(address: str) -> str:
    try:
        canonical = str(_address(address))
    except ValueError:
        raise HTTPException(400, "demo session client address unavailable") from None
    return hmac.new(_secret("LEAF_DEMO_IP_HASH_SECRET"), canonical.encode(),
                    hashlib.sha256).hexdigest()


def _db():
    """The existing leaf_platform alias and transaction seam; loaded only live."""
    if "leaf_platform" not in sys.modules:
        directory = Path(__file__).resolve().parents[1] / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", directory / "__init__.py",
            submodule_search_locations=[str(directory)])
        if spec is None or spec.loader is None:
            raise RuntimeError("platform package unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    from leaf_platform import db
    return db


def _load_demo_provisioning():
    # Platform loads this resolver by file path, without server on sys.path.
    # Keep the adjacent provisioning module reachable on that same import leg.
    module = sys.modules.get("demo_provisioning")
    if module is None:
        with _PROVISIONING_LOAD_LOCK:
            module = sys.modules.get("demo_provisioning")
            if module is None:
                spec = importlib.util.spec_from_file_location(
                    "demo_provisioning", Path(__file__).with_name("demo_provisioning.py"))
                if spec is None or spec.loader is None:
                    raise RuntimeError("demo provisioning unavailable")
                module = importlib.util.module_from_spec(spec)
                # Publish only a fully initialized module; failed loads can retry.
                spec.loader.exec_module(module)
                sys.modules["demo_provisioning"] = module
    return module


def provision_demo_tenant(conn, session):
    module = _load_demo_provisioning()
    return module.provision_demo_tenant(conn, session)


def _signature(record_id: uuid.UUID, expiry: int, secret: bytes) -> str:
    return hmac.new(secret, f"{record_id}.{expiry}".encode(), hashlib.sha256).hexdigest()


def mint(request) -> MintedSession:
    if not is_enabled():
        raise HTTPException(404, "not found")
    if "authorization" in request.headers:
        raise HTTPException(400, "Authorization is not accepted by the demo issuer")
    secret = _secret("LEAF_DEMO_SESSION_SECRET")
    ip_cap = _cap("LEAF_DEMO_PER_IP_DAILY_CAP")
    global_cap = _cap("LEAF_DEMO_GLOBAL_DAILY_CAP")
    ip_hash = address_hash(client_address(request))
    now = _now().replace(microsecond=0)
    expiry = now + timedelta(minutes=60)
    record_id, org_id = uuid.uuid4(), uuid.uuid4()

    def insert(conn):
        # Fixed lock order across issuers. Conditional upserts serialize even
        # independent connections; a failed second cap or insert rolls back
        # both counters. No process-local locks or read-then-increment race.
        for scope, limit in (("global", global_cap), (f"ip:{ip_hash}", ip_cap)):
            row = conn.execute(
                "INSERT INTO demo_mint_counters AS c (day, scope, count) "
                "VALUES (%s, %s, 1) ON CONFLICT (day, scope) DO UPDATE "
                "SET count = c.count + 1 WHERE c.count < %s RETURNING count",
                (now.date(), scope, limit)).fetchone()
            if row is None:
                raise MintCapExceeded()
        conn.execute(
            "INSERT INTO demo_sessions "
            "(id, subject, tenant_id, org_id, created_at, expires_at, client_ip_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (record_id, f"demo:{record_id}", f"demo-{record_id}", org_id,
             now, expiry, ip_hash))
        return provision_demo_tenant(conn, DemoSession(
            record_id, f"demo:{record_id}", f"demo-{record_id}", org_id,
            now, expiry, None, ip_hash))

    try:
        _db().run_transaction(insert)
    except MintCapExceeded:
        raise
    except Exception:
        raise HTTPException(503, "demo session store unavailable") from None
    timestamp = int(expiry.timestamp())
    return MintedSession(f"{record_id}.{timestamp}.{_signature(record_id, timestamp, secret)}",
                         expiry)


def verify(token: str) -> DemoSession:
    # Must precede config parsing and loading the DB adapter, including on a
    # correctly formed credential. The switch is read on EVERY request.
    if not is_enabled():
        raise HTTPException(401, "demo sessions disabled")
    try:
        record_text, expiry_text, signature = token.split(".")
        record_id = uuid.UUID(record_text)
        if str(record_id) != record_text or not re.fullmatch(r"[0-9]{1,12}", expiry_text):
            raise ValueError()
        expiry = int(expiry_text)
        if not hmac.compare_digest(_signature(record_id, expiry, _secret(
                "LEAF_DEMO_SESSION_SECRET")), signature):
            raise ValueError()
        if _now().timestamp() >= expiry:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise HTTPException(401, "invalid demo session") from None
    try:
        row = _db().run_transaction(lambda conn: conn.execute(
            "SELECT id, subject, tenant_id, org_id, created_at, expires_at, "
            "revoked_at, client_ip_hash FROM demo_sessions WHERE id = %s",
            (record_id,)).fetchone(), read_only=True)
        if row is None:
            raise ValueError()
        record = DemoSession(**row)
        if (record.revoked_at is not None or record.expires_at.timestamp() != expiry
                or _now() >= record.expires_at
                or record.subject != f"demo:{record_id}"
                or record.tenant_id != f"demo-{record_id}"):
            raise ValueError()
        return record
    except Exception:
        raise HTTPException(401, "invalid demo session") from None


def resolve_demo_session(*, auth_live: bool, demo: str | None,
                         authorization: str | None = None, guest: str | None = None,
                         dispatch_leg: bool = False) -> DemoSession | None:
    """One credential decision for both adapters; see the precedence table above."""
    if demo is None:
        return None
    if not auth_live or dispatch_leg:
        raise HTTPException(401, "demo credential not accepted on this auth leg")
    if authorization is not None:
        return None
    if guest is not None:
        raise HTTPException(400, "guest and demo credentials cannot be combined")
    return verify(demo)


def require_demo_route(request) -> None:
    template = request.scope.get("demo_route_template")
    dispatched_template = getattr(request.scope.get("route"), "path", None)
    if (template != dispatched_template or
            (request.method.upper(), template) not in DEMO_ROUTE_ALLOWLIST):
        raise HTTPException(403, "demo sessions have no application routes")
