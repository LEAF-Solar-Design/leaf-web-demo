"""Staging investor link, session and workspace authority."""
import base64
from collections import OrderedDict
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
import re
import sys
from threading import Lock
from typing import NamedTuple
import uuid


ISSUER_PREFIX = "https://investor-demo.leaf.invalid/"
ISSUER = ISSUER_PREFIX + "staging/"
SUBJECT_PREFIX = "leaf-investor-demo|"
SUBJECT = SUBJECT_PREFIX + "staging"
HOST = "platform-staging.leafdesign.ai"
ORIGIN = "https://" + HOST
TIER = "restricted"
ROLE = "read_only"
ORG_ID, BINDING_ID, PLATFORM_USER_ID = (
    uuid.uuid5(uuid.NAMESPACE_URL, ISSUER + name)
    for name in ("org", "binding", "platform-user")
)
SESSION_TTL_S = 7200
LINK_MAX_LIFETIME_S = 31 * 86400
LINK_MAX_CHARS = 128
ROUTE_ALLOWLIST = frozenset(("GET", path) for path in (
    "/api/session/identity", "/api/session", "/api/tools", "/api/capabilities",
    "/api/entitlements", "/api/orgs/{org_id}", "/api/projects", "/api/jobs",
))
_OTHER_SECRETS = (
    "LEAF_DISPATCH_SECRET", "LEAF_APP_DISPATCH_SECRET", "LEAF_SERVICE_BROWSER_SESSION_KEY",
    "LEAF_GUEST_SECRET", "LEAF_HARNESS_SECRET", "LEAF_BROKER_SECRET",
    "LEAF_OPS_SECRET", "LEAF_DEMO_SESSION_SECRET",
)
_LINK = re.compile(r"v1\.[0-9a-f]{8}\.[1-9][0-9]{9}\.[1-9][0-9]{9}\.[A-Za-z0-9_-]{43}")
_SCOPE = ContextVar("investor_demo_request", default=None)


def _encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text):
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _encode(raw) != text:
        raise ValueError
    return raw


def config() -> bytes:
    """Read the gates and return the decoded secret, or fail without details."""
    secret = os.environ.get("LEAF_INVESTOR_DEMO_SECRET", "")
    if (os.environ.get("LEAF_INVESTOR_DEMO_ENABLED") != "1"
            or os.environ.get("LEAF_RUNTIME_ENV") != "staging"
            or os.environ.get("LEAF_AUTH_LIVE") != "1"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43,86}", secret)
            or any(secret == os.environ.get(name) for name in _OTHER_SECRETS)):
        raise ValueError
    raw = _decode(secret)
    if not 32 <= len(raw) <= 64:
        raise ValueError
    return raw


def enabled() -> bool:
    try:
        config()
        return True
    except Exception:
        return False


def _subkey(secret, info):
    prk = hmac.digest(b"leaf-investor-demo/v1", secret, "sha256")
    return hmac.digest(prk, info + b"\x01", "sha256")


def _key_id(secret):
    return _subkey(secret, b"key-id").hex()[:8]


def key_id():
    return _key_id(config())


class InvalidLink(ValueError):
    """Uniform public link rejection."""


class LinkToken(NamedTuple):
    issued_at: int
    expires_at: int


def _mac(secret, prefix):
    context = (b"leaf-investor-demo-link", ORIGIN.encode(), str(ORG_ID).encode(), ROLE.encode())
    message = prefix.encode("ascii") + b"\0" + b"".join(
        len(field).to_bytes(2, "big") + field for field in context)
    return hmac.digest(_subkey(secret, b"link-mac"), message, "sha256")


def _valid_interval(iat, exp):
    return (type(iat) is int and type(exp) is int and 10**9 <= iat < exp < 10**10
            and exp - iat <= LINK_MAX_LIFETIME_S)


def mint_link_token(*, issued_at: int, expires_at: int) -> str:
    secret = config()
    if not _valid_interval(issued_at, expires_at):
        raise ValueError
    prefix = f"v1.{_key_id(secret)}.{issued_at}.{expires_at}"
    return prefix + "." + _encode(_mac(secret, prefix))


def verify_link_token(token, *, now: int) -> LinkToken:
    if not isinstance(token, str) or len(token) > LINK_MAX_CHARS or not _LINK.fullmatch(token):
        raise InvalidLink
    try:
        secret = config()
        prefix, mac = token.rsplit(".", 1)
        _, kid, issued, expires = prefix.split(".")
        key_matches = hmac.compare_digest(kid, _key_id(secret))
        mac_matches = hmac.compare_digest(_decode(mac), _mac(secret, prefix))
        iat, exp = int(issued), int(expires)
        if (key_matches and mac_matches and _valid_interval(iat, exp)
                and type(now) is int and iat <= now + 60 and now < exp):
            return LinkToken(iat, exp)
    except Exception:
        pass
    raise InvalidLink


def _auth():
    # The platform can load the verifier by file path with server off sys.path.
    if "leaf_server_auth" in sys.modules:
        return sys.modules["leaf_server_auth"]
    import auth
    return auth


def _identity_claims():
    ns = _auth().claim_ns()
    return {"iss": ISSUER, "aud": _auth().audience(), "sub": SUBJECT,
            ns + "tenant_id": str(ORG_ID), ns + "org_id": str(ORG_ID), ns + "tier": TIER}


def mint_session(link: LinkToken, *, now: int) -> tuple[str, int, str]:
    import jwt
    secret = config()
    if (not isinstance(link, LinkToken) or not _valid_interval(*link) or type(now) is not int
            or link.issued_at > now + 60 or now >= link.expires_at):
        raise ValueError
    exp = min(now + SESSION_TTL_S, link.expires_at)
    jti = uuid.uuid4().hex
    claims = dict(_identity_claims(), iat=now, exp=exp, jti=jti)
    return jwt.encode(claims, _subkey(secret, b"session-hs256"), algorithm="HS256"), exp, jti


def peek_unverified(token: str) -> tuple[dict, dict]:
    """Read JWT objects for classification only, independently of the signature."""
    try:
        signing_input, _signature = token.encode("utf-8").rsplit(b".", 1)
        head, body = signing_input.split(b".", 1)
    except (ValueError, UnicodeError):
        return {}, {}

    def part(segment):
        try:
            value = json.loads(base64.urlsafe_b64decode(segment + b"=" * (-len(segment) % 4)))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    return part(head), part(body)


def is_investor_token(header: dict, unverified_claims: dict) -> bool:
    return any(isinstance(unverified_claims.get(name), str) and
               unverified_claims[name].casefold().startswith(prefix)
               for name, prefix in (("iss", ISSUER_PREFIX), ("sub", SUBJECT_PREFIX)))


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        reset = _SCOPE.set(scope)
        try:
            await self.app(scope, receive, send)
        finally:
            _SCOPE.reset(reset)


def verify_session(token: str, header: dict, unverified_claims: dict) -> dict:
    from fastapi import HTTPException
    try:
        secret = config()
    except Exception:
        raise HTTPException(401, "investor demo identity is not permitted in this runtime") from None
    scope = _SCOPE.get()
    if (not scope or scope.get("type") != "http"
            or [v for k, v in scope.get("headers", ()) if k.lower() == b"host"] != [HOST.encode()]
            or (scope.get("method"), getattr(scope.get("route"), "path", None)) not in ROUTE_ALLOWLIST):
        raise HTTPException(403, "investor demo sessions cannot use this route")
    try:
        import jwt
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise ValueError
        expected = _identity_claims()
        payload = jwt.decode(token, _subkey(secret, b"session-hs256"), algorithms=["HS256"],
                             issuer=ISSUER, audience=expected["aud"], leeway=0,
                             options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]})
        if (payload.keys() != expected.keys() | {"exp", "iat", "jti"}
                or any(payload[k] != v for k, v in expected.items())
                or type(payload["exp"]) is not int or type(payload["iat"]) is not int
                or not 0 < payload["exp"] - payload["iat"] <= SESSION_TTL_S
                or not re.fullmatch(r"[0-9a-f]{32}", payload["jti"])):
            raise ValueError
    except Exception:
        raise HTTPException(401, "invalid investor demo session") from None
    if not binding_ok():
        raise HTTPException(401, "invalid investor demo session")
    return payload


def _db():
    platform_link = _auth()._server_sibling("platform_link")
    platform_link._ensure_platform_package()
    return platform_link.platform_db()


def _binding_matches(conn):
    # Limit after selecting by subject: an additional active binding must count
    # even if its org, id or role differs from the expected one.
    rows = conn.execute(
        "SELECT b.binding_id, b.platform_tenant_id, b.role, o.status, o.tier, o.account_class "
        "FROM identity_bindings b LEFT JOIN orgs o ON o.org_id = b.platform_tenant_id "
        "WHERE b.external_authority = 'auth0' AND b.external_subject = %s "
        "AND b.status = 'active' AND b.revoked_at IS NULL LIMIT 2", (SUBJECT,)).fetchall()
    return len(rows) == 1 and rows[0] == {
        "binding_id": BINDING_ID, "platform_tenant_id": ORG_ID, "role": ROLE,
        "status": "active", "tier": TIER, "account_class": "demo"}


def binding_ok() -> bool:
    try:
        return _db().run_transaction(_binding_matches, read_only=True)
    except Exception:
        return False


def provision(conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x4C454146494E5653,))
    conn.execute(
        "INSERT INTO orgs (org_id, name, tier, account_class) "
        "VALUES (%s, 'Leaf Automation investor demo', %s, 'demo') ON CONFLICT DO NOTHING",
        (ORG_ID, TIER))
    conn.execute(
        "INSERT INTO identity_bindings (binding_id, external_authority, external_subject, "
        "platform_tenant_id, platform_user_id, role) VALUES (%s, 'auth0', %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING", (BINDING_ID, SUBJECT, ORG_ID, PLATFORM_USER_ID, ROLE))


def provision_at_startup() -> str:
    status = "disabled"
    if enabled():
        try:
            db = _db()
            db.run_transaction(provision)
            status = "ready" if db.run_transaction(_binding_matches, read_only=True) else "mismatch"
        except Exception:
            status = "unavailable"
    try:
        print(f"[leaf-demo] diagnostic_code=investor_demo_{status}", file=sys.stderr)
    except Exception:
        pass
    return status


class ExchangeGuard:
    """Limit clients before body reads and verified links before database reads.

    The per-client limit is best effort. CF-Connecting-IP is meaningful only
    when the origin is reachable solely through the edge.
    """

    def __init__(self):
        self._lock = Lock()
        self._clients = OrderedDict()
        self._burst_start = None
        self._burst = 0

    def allow_client(self, client_key: str, now: float) -> bool:
        if (not isinstance(client_key, str) or not 0 < len(client_key) <= 255
                or type(now) not in (float, int) or not 0 <= now <= sys.float_info.max):
            return False
        with self._lock:
            start, count = self._clients.get(client_key, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            if count >= 10:
                return False
            self._clients[client_key] = (start, count + 1)
            self._clients.move_to_end(client_key)
            if len(self._clients) > 4096:
                self._clients.popitem(last=False)
            return True

    def allow_verified(self, now: float) -> bool:
        if type(now) not in (float, int) or not 0 <= now <= sys.float_info.max:
            return False
        with self._lock:
            if self._burst_start is None or now - self._burst_start >= 60:
                self._burst_start, self._burst = now, 0
            if self._burst >= 240:
                return False
            self._burst += 1
            return True


def is_investor_context(tenant) -> bool:
    from deps import TenantContext
    return isinstance(tenant, TenantContext) and tenant.subject == SUBJECT
