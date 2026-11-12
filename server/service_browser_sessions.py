"""PostgreSQL-only service browser custody. Public selectors are not credentials.

No provider or database work occurs on import. Canonical JWT/tenant gates remain
responsible for authorization after the narrowly scoped ASGI translation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from http.cookies import SimpleCookie
import math
import os
import re
import secrets
import time
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

ORIGINS = {"staging": "https://platform-staging.leafdesign.ai",
           "production": "https://platform.leafdesign.ai"}
PENDING_COOKIE = "__Host-leaf-agent-pending"
SESSION_COOKIE = "__Host-leaf-agent-session"
SCHEME = "LeafServiceSession"
NS = "https://leafdesign.ai/"


def deny(code=401):
    raise HTTPException(code, "service browser login unavailable" if code == 503
                        else "service browser request denied")


@dataclass(frozen=True)
class Config:
    environment: str
    origin: str
    cipher: Fernet


def config():
    if os.environ.get("LEAF_SERVICE_BROWSER_LOGIN_ENABLED") != "1":
        deny(404)
    environment = os.environ.get("LEAF_RUNTIME_ENV")
    if environment not in ORIGINS or os.environ.get("LEAF_AUTH_LIVE") != "1":
        deny(503)
    key = os.environ.get("LEAF_SERVICE_BROWSER_SESSION_KEY", "")
    if not key or key in {os.environ.get("LEAF_DISPATCH_SECRET"),
                          os.environ.get("LEAF_APP_DISPATCH_SECRET")}:
        deny(503)
    try:
        cipher = Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        deny(503)
    return Config(environment, ORIGINS[environment], cipher)


def local_request(request, cfg, *, origin_required=False):
    # Use the actual Host header; never X-Forwarded-Host/Origin.
    for name in (b"host", b"origin"):
        if sum(k.lower() == name for k, _ in request.scope.get("headers", [])) > 1:
            deny(403)
    # The canonical HTTPS listener terminates TLS before Uvicorn. Its private
    # hop is HTTP, so ASGI's scheme does not prove the browser transport. Keep
    # the fixed public Host/Origin checks without trusting forwarded headers.
    if request.headers.get("host") != cfg.origin.removeprefix("https://"):
        deny(403)
    origin = request.headers.get("origin")
    if (origin_required or origin is not None) and origin != cfg.origin:
        deny(403)


def public_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        deny(401)


def digest(value):
    return sha256(value.encode("ascii")).hexdigest()


def cookie_value(request, name):
    # Reject duplicate cookie names, parser ambiguity and non-canonical secrets.
    raw = request.headers.get("cookie", "")
    if sum(k.lower() == b"cookie" for k, _ in request.scope.get("headers", [])) > 1:
        deny()
    if len(raw) > 8192:
        deny()
    parts = [part.strip() for part in raw.split(";")]
    if sum(part.split("=", 1)[0] == name for part in parts) != 1:
        deny()
    jar = SimpleCookie()
    try:
        jar.load(raw)
        value = jar[name].value
    except Exception:
        deny()
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        deny()
    return value


@contextmanager
def database():
    import operator_principals
    try:
        # All multi-statement custody transitions commit or roll back together.
        with operator_principals._db().transaction() as conn:
            with conn.cursor() as cur:
                yield cur
    except HTTPException:
        raise
    except Exception:
        # Never propagate driver messages/parameters or encrypted/plain tokens.
        raise HTTPException(503, "service browser login unavailable") from None


def machine_claims(payload):
    sub = payload.get("sub")
    exp = payload.get("exp")
    if (payload.get(NS + "principal_type") != "service_admin"
            or not isinstance(sub, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{8,128}@clients", sub) is None
            or payload.get("azp") != sub[:-8]
            or payload.get(NS + "tier") != "admin"
            or type(exp) not in (int, float) or not math.isfinite(exp)
            or exp <= time.time()):
        deny(403)
    return sub, exp


def authorize(request, tenant):
    import auth
    import deps
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not deps.auth_live():
        deny(403)
    try:
        payload = auth.verify_platform_token(header)
        # Require the real issuer even if another deployment verifier mode exists.
        if payload.get("iss") != "https://leafautomation.us.auth0.com/":
            deny(403)
        sub, exp = machine_claims(payload)
        if (not isinstance(tenant, deps.TenantContext)
                or tenant.subject != sub or tenant.tier != "admin"
                or sub not in deps.admin_subjects()
                or not getattr(tenant, "authority_resolved", False)):
            deny(403)
        return header[7:], sub, exp
    except HTTPException:
        deny(403)


class Store:
    def create(self, cfg, nonce):
        challenge = str(uuid4())
        with database() as cur:
            cur.execute("INSERT INTO service_login_challenges "
                        "(id, nonce_hash, environment, origin, expires_at) "
                        "VALUES (%s,%s,%s,%s,clock_timestamp()+interval '120 seconds')",
                        (challenge, digest(nonce), cfg.environment, cfg.origin))
        return challenge

    def status(self, cfg, challenge):
        with database() as cur:
            cur.execute("SELECT environment, origin, "
                        "expires_at <= clock_timestamp() AS expired, consumed_at "
                        "FROM service_login_challenges WHERE id=%s AND environment=%s AND origin=%s",
                        (public_id(challenge), cfg.environment, cfg.origin))
            row = cur.fetchone()
        if not row:
            return {"exists": False, "pending": False, "expired": False,
                    "environment": cfg.environment, "origin": cfg.origin}
        return {"exists": True, "pending": not row["expired"] and row["consumed_at"] is None,
                "expired": bool(row["expired"]), "environment": row["environment"],
                "origin": row["origin"]}

    def approve(self, cfg, challenge, token, subject, exp):
        sealed = cfg.cipher.encrypt(token.encode()).decode("ascii")
        with database() as cur:
            cur.execute("UPDATE service_login_challenges SET sealed_token=%s, subject=%s, "
                        "token_expires_at=to_timestamp(%s), approved_at=clock_timestamp() "
                        "WHERE id=%s AND environment=%s AND origin=%s "
                        "AND approved_at IS NULL AND consumed_at IS NULL "
                        "AND expires_at>clock_timestamp() RETURNING id",
                        (sealed, subject, exp, public_id(challenge), cfg.environment, cfg.origin))
            if not cur.fetchone():
                deny(409)

    def consume(self, cfg, challenge, nonce):
        secret, binding = secrets.token_urlsafe(32), str(uuid4())
        with database() as cur:
            cur.execute("UPDATE service_login_challenges SET consumed_at=clock_timestamp() "
                        "WHERE id=%s AND nonce_hash=%s AND environment=%s AND origin=%s "
                        "AND approved_at IS NOT NULL AND consumed_at IS NULL "
                        "AND expires_at>clock_timestamp() AND token_expires_at>clock_timestamp() "
                        "RETURNING sealed_token,subject,token_expires_at",
                        (public_id(challenge), digest(nonce), cfg.environment, cfg.origin))
            row = cur.fetchone()
            if row is None:
                return None
            unseal(cfg, row["sealed_token"])
            cur.execute("INSERT INTO service_browser_sessions "
                        "(binding_id,secret_hash,sealed_token,subject,environment,origin,expires_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,LEAST(clock_timestamp()+interval '900 seconds',%s))",
                        (binding, digest(secret), row["sealed_token"], row["subject"],
                         cfg.environment, cfg.origin, row["token_expires_at"]))
        return binding, secret

    def resolve(self, cfg, binding, secret):
        with database() as cur:
            cur.execute("SELECT binding_id,sealed_token,subject,expires_at "
                        "FROM service_browser_sessions WHERE binding_id=%s AND secret_hash=%s "
                        "AND environment=%s AND origin=%s AND revoked_at IS NULL "
                        "AND expires_at>clock_timestamp()",
                        (public_id(binding), digest(secret), cfg.environment, cfg.origin))
            row = cur.fetchone()
        if row is None:
            deny()
        return row, unseal(cfg, row["sealed_token"])

    def revoke(self, cfg, binding, secret):
        with database() as cur:
            cur.execute("UPDATE service_browser_sessions SET revoked_at=clock_timestamp() "
                        "WHERE binding_id=%s AND secret_hash=%s AND environment=%s AND origin=%s "
                        "AND revoked_at IS NULL RETURNING binding_id",
                        (public_id(binding), digest(secret), cfg.environment, cfg.origin))
            if not cur.fetchone():
                deny()


def unseal(cfg, sealed):
    try:
        token = cfg.cipher.decrypt(sealed.encode("ascii")).decode("ascii")
        if not token or any(c.isspace() for c in token):
            deny()
        return token
    except (InvalidToken, ValueError, UnicodeError, AttributeError, TypeError):
        deny()


store = Store()


class ServiceBrowserSessionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        auths = [v for k, v in headers if k.lower() == b"authorization"]
        # Ordinary Bearer traffic is passed through byte-for-byte, cookie or not.
        if not any(v.lower().startswith(b"leafservicesession") for v in auths):
            return await self.app(scope, receive, send)
        try:
            if len(auths) != 1:
                deny()
            value = auths[0].decode("ascii")
            if not value.startswith(SCHEME + " "):
                deny()
            binding = value[len(SCHEME) + 1:]
            if public_id(binding) != binding:
                deny()
            # Approval must always present the original provider bearer itself.
            if scope["path"] == "/api/auth/agent/approve":
                deny(403)
            cfg = config()
            if sum(k.lower() == b"cookie" for k, _ in headers) > 1:
                deny()
            request = Request(scope)
            local_request(request, cfg)
            secret = cookie_value(request, SESSION_COOKIE)
            # The pool and driver are synchronous. Keep custody I/O off the
            # ASGI event loop, as FastAPI does for the synchronous routes.
            row, token = await run_in_threadpool(store.resolve, cfg, binding, secret)
            scope = dict(scope)
            scope["state"] = dict(scope.get("state", {}))
            scope["state"]["service_browser_session"] = row
            # The canonical dependency must take its JWT leg, never a dispatch,
            # guest or demo leg supplied alongside the joint session proof.
            alternatives = {b"authorization", b"x-dispatch-secret", b"x-guest-session", b"x-demo-session"}
            scope["headers"] = [(k, v) for k, v in headers if k.lower() not in alternatives] + [
                (b"authorization", ("Bearer " + token).encode("ascii"))]
        except (HTTPException, UnicodeError) as exc:
            code = exc.status_code if isinstance(exc, HTTPException) else 401
            return await JSONResponse({"detail": "service browser request denied"},
                                      status_code=code, headers={"Cache-Control": "no-store"})(scope, receive, send)
        return await self.app(scope, receive, send)
