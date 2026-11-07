"""
server/test_auth.py â€” automated gate for Concern-1 (Auth0 platform identity).

NO live Auth0: a throwaway RS256 keypair is generated, published as a local
JWKS file, and the verifier (server/auth.py) is pointed at it via
LEAF_AUTH0_JWKS_FILE. Covers:

  UNIT (auth.verify_platform_token / auth.extract_tenant_claims):
    - valid token (+ tenant claim)      -> ACCEPTED, claims extracted
    - tampered signature                -> 401
    - signed by a foreign key           -> 401
    - expired                           -> 401
    - wrong audience                    -> 401
    - wrong issuer                      -> 401
    - missing Authorization / not Bearer-> 401
    - verified but missing tenant claim -> 403 (extract_tenant_claims)

  HTTP matrix (LEAF_AUTH_LIVE=1, TestClient over /api/session):
    - no token                          -> 401
    - valid token + tenant claim        -> 200 + intake + echoed tenant_id/org_id
    - valid token, missing tenant claim -> 403

Run (cwd MUST be server/, to avoid the repo-root platform/ stdlib shadow):
    cd server && python test_auth.py           # -> exit 0
    cd server && python -m pytest test_auth.py -q
    cd server && python -m pytest -q           # whole suite; safe (the live-auth
                                               # env is fixture-scoped to this module)
"""
from __future__ import annotations

# Pin the stdlib `platform` module BEFORE importing deps (which prepends the repo
# root â€” containing a platform/ package â€” to sys.path). Neutralizes the shadow.
import platform  # noqa: F401  (import-order guard, intentional)

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Ensure server/ is importable regardless of invocation cwd.
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

# --------------------------------------------------------------------------- #
# local RS256 keypair + JWKS (no network / no live Auth0)
# --------------------------------------------------------------------------- #
ISS = "https://leaf-test.example/"
AUD = "https://api.leaf-test.example"
NS = "https://leafdesign.ai/"
PLATFORM_TENANT_ID = "bccb0d64-04c9-4108-bcc1-f27b8bb3924d"
KID = "leaf-test-key-1"

_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _priv.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
# a SECOND, unrelated key â€” its tokens must be rejected (not in the JWKS)
_foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_foreign_pem = _foreign.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)

_jwk = json.loads(RSAAlgorithm.to_jwk(_priv.public_key()))
_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
_TMP = Path(tempfile.mkdtemp(prefix="leaf-auth-test-"))
_JWKS_FILE = _TMP / "jwks.json"
_JWKS_FILE.write_text(json.dumps({"keys": [_jwk]}), encoding="utf-8")

# env that points the verifier + tenant store at the local fixtures and turns
# auth ON. Applied ONLY inside the module-scoped fixture below (pytest) or
# _run_all (script mode) â€” NEVER at import time: pytest imports every test
# module during collection, before any test runs, so an import-time
# os.environ.update would leak LEAF_AUTH_LIVE=1 into the whole process and
# 401 the entire tests/ suite in a bare `python -m pytest -q` run.
_ENV = {
    "LEAF_AUTH_LIVE": "1",
    "LEAF_AUTH0_ISSUER": ISS,
    "LEAF_AUTH0_AUDIENCE": AUD,
    "LEAF_TENANT_CLAIM_NS": NS,
    "LEAF_AUTH0_JWKS_FILE": str(_JWKS_FILE),
    "LEAF_TENANTS_FILE": str(SERVER_DIR.parent / "data" / "tenants.sample.json"),
}

import auth  # noqa: E402
import deps  # noqa: E402
import tenancy  # noqa: E402


def _active_test_tenant(subject):
    assert subject == "auth0|tester"
    return PLATFORM_TENANT_ID, "hosted_pro"


@pytest.fixture(autouse=True, scope="module")
def _auth_env():
    """Scope the live-auth env to THIS module (same pattern as tests/
    test_wave5.py's live_auth fixture). Teardown restores the prior env and
    drops the cached tenant store so later-collected modules inherit a clean
    process â€” auth.py/deps.py read the env at call time, so restore is enough."""
    mp = pytest.MonkeyPatch()
    for k, v in _ENV.items():
        mp.setenv(k, v)
    mp.setattr(deps, "resolve_active_platform_tenant_authority", _active_test_tenant)
    tenancy.reset_store()
    yield
    mp.undo()
    tenancy.reset_store()


def mint(*, key=_priv_pem, kid=KID, aud=AUD, iss=ISS, exp_delta=3600,
         include_tenant=True, tenant_id="org_acme_solar",
         org_id=None, extra_claims=None) -> str:
    now = int(time.time())
    payload = {"iss": iss, "aud": aud, "iat": now, "exp": now + exp_delta, "sub": "auth0|tester"}
    if include_tenant:
        payload[NS + "tenant_id"] = tenant_id
        payload[NS + "org_id"] = tenant_id if org_id is None else org_id
        payload[NS + "tier"] = "hosted_pro"
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def bearer(tok: str) -> str:
    return "Bearer " + tok


# Stage A service browser login UNIT evidence only. Scripted PG cursors below
# exercise custody decisions and SQL predicates, not live transaction races.
@pytest.fixture
def service_config(monkeypatch):
    from cryptography.fernet import Fernet
    import service_browser_sessions as service
    monkeypatch.setenv("LEAF_SERVICE_BROWSER_LOGIN_ENABLED", "1")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_SERVICE_BROWSER_SESSION_KEY", Fernet.generate_key().decode())
    return service.config()


def _service_request(path="/api/auth/agent/session", headers=(), method="GET"):
    from starlette.requests import Request
    return Request({"type": "http", "method": method, "scheme": "https",
                    "path": path, "query_string": b"", "server": ("platform-staging.leafdesign.ai", 443),
                    "headers": [(b"host", b"platform-staging.leafdesign.ai"), *headers]})


def test_service_browser_default_disabled_and_invalid_config(monkeypatch):
    import service_browser_sessions as service
    from cryptography.fernet import Fernet
    monkeypatch.delenv("LEAF_SERVICE_BROWSER_LOGIN_ENABLED", raising=False)
    with pytest.raises(HTTPException) as error:
        service.config()
    assert error.value.status_code == 404
    monkeypatch.setenv("LEAF_SERVICE_BROWSER_LOGIN_ENABLED", "1")
    monkeypatch.setenv("LEAF_SERVICE_BROWSER_SESSION_KEY", Fernet.generate_key().decode())
    for environment in ("test", "local", "Production", "staging ", ""):
        monkeypatch.setenv("LEAF_RUNTIME_ENV", environment)
        with pytest.raises(HTTPException) as error:
            service.config()
        assert error.value.status_code == 503
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    for key in ("", "bad-key"):
        monkeypatch.setenv("LEAF_SERVICE_BROWSER_SESSION_KEY", key)
        with pytest.raises(HTTPException):
            service.config()
    shared = Fernet.generate_key().decode()
    monkeypatch.setenv("LEAF_SERVICE_BROWSER_SESSION_KEY", shared)
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", shared)
    with pytest.raises(HTTPException):
        service.config()


def test_service_browser_exact_host_origin(service_config):
    import service_browser_sessions as service
    service.local_request(_service_request(), service_config)
    for headers in (
        [(b"origin", b"https://evil.invalid")],
        [(b"origin", b"https://platform.leafdesign.ai")],
        [(b"origin", b"null")],
    ):
        with pytest.raises(HTTPException):
            service.local_request(_service_request(headers=headers), service_config)
    request = _service_request()
    request.scope["headers"] = [(b"host", b"evil.invalid"),
                                (b"x-forwarded-host", b"platform-staging.leafdesign.ai")]
    with pytest.raises(HTTPException):
        service.local_request(request, service_config)
    with pytest.raises(HTTPException):
        service.local_request(_service_request(), service_config, origin_required=True)
    insecure = _service_request()
    insecure.scope["scheme"] = "http"
    # Uvicorn sees HTTP on the canonical TLS-terminated private hop. External
    # identity still requires the exact configured HTTPS Host/Origin contract.
    service.local_request(insecure, service_config)


def test_service_browser_canonical_machine_admin_authority(service_config, monkeypatch):
    import service_browser_sessions as service
    issuer = "https://leafautomation.us.auth0.com/"
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", issuer)
    monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", SERVICE_SUBJECT)
    tenant = deps.TenantContext("org_acme_solar", tier="admin", subject=SERVICE_SUBJECT,
                                authority_resolved=True)
    token = mint(iss=issuer, extra_claims=_service_admin_claims())
    request = _service_request(headers=[(b"authorization", bearer(token).encode())])
    assert service.authorize(request, tenant)[0] == token
    # The real canonical dependency must resolve a current binding and allowlist.
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda sub: ("org_acme_solar", "hosted_pro"))
    resolved = deps.require_tenant(request)
    assert resolved.subject == SERVICE_SUBJECT and resolved.tier == "admin"
    for claims in ({}, {**_service_admin_claims(), NS + "tier": "hosted_pro"},
                   {**_service_admin_claims(), "azp": "ForeignClient_1234"}):
        request = _service_request(headers=[(b"authorization", bearer(mint(iss=issuer, extra_claims=claims)).encode())])
        with pytest.raises(HTTPException):
            service.authorize(request, tenant)
    request = _service_request(headers=[(b"authorization", bearer(token).encode())])
    monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", "")
    with pytest.raises(HTTPException):
        service.authorize(request, tenant)
    assert deps.require_tenant(request).tier == "hosted_pro"
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda sub: ("foreign-org", "hosted_pro"))
    with pytest.raises(HTTPException):
        deps.require_tenant(request)


class _ServiceCursor:
    def __init__(self, rows):
        self.rows, self.calls = iter(rows), []

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return next(self.rows)


def _service_database(monkeypatch, rows):
    from contextlib import contextmanager
    import service_browser_sessions as service
    cursor = _ServiceCursor(rows)
    @contextmanager
    def database():
        yield cursor
    monkeypatch.setattr(service, "database", database)
    return cursor


def test_service_browser_sql_approval_and_replay(service_config, monkeypatch):
    import service_browser_sessions as service
    challenge = "10000000-0000-4000-8000-000000000001"
    cur = _service_database(monkeypatch, [{"id": challenge}, None])
    service.store.approve(service_config, challenge, "real-token", SERVICE_SUBJECT, time.time() + 100)
    sql, params = cur.calls[0]
    assert "approved_at IS NULL" in sql and "consumed_at IS NULL" in sql
    assert "expires_at>clock_timestamp()" in sql
    assert "real-token" not in params and service.unseal(service_config, params[0]) == "real-token"
    with pytest.raises(HTTPException) as error:
        service.store.approve(service_config, challenge, "real-token", SERVICE_SUBJECT, time.time() + 100)
    assert error.value.status_code == 409


def test_service_browser_sql_consume_nonce_expiry_replay(service_config, monkeypatch):
    import service_browser_sessions as service
    from datetime import datetime, timezone
    challenge = "10000000-0000-4000-8000-000000000001"
    sealed = service_config.cipher.encrypt(b"real-token").decode()
    row = {"sealed_token": sealed, "subject": SERVICE_SUBJECT,
           "token_expires_at": datetime.now(timezone.utc)}
    cur = _service_database(monkeypatch, [None, row, None])
    # Scripted failed nonce predicate, successful transition, consumed replay.
    assert service.store.consume(service_config, challenge, "wrong-nonce") is None
    assert len(cur.calls) == 1 and cur.calls[0][1][1] == service.digest("wrong-nonce")
    binding, secret = service.store.consume(service_config, challenge, "correct-nonce")
    assert binding != secret and "real-token" not in secret
    sql, params = cur.calls[1]
    for predicate in ("nonce_hash=%s", "approved_at IS NOT NULL", "consumed_at IS NULL",
                      "expires_at>clock_timestamp()", "token_expires_at>clock_timestamp()"):
        assert predicate in sql
    insert, values = cur.calls[2]
    assert "INSERT INTO service_browser_sessions" in insert and "LEAST(" in insert
    assert values[1] == service.digest(secret) and secret not in values
    assert service.store.consume(service_config, challenge, "correct-nonce") is None
    assert len(cur.calls) == 4


@pytest.mark.parametrize("row,expected", [
    (None, {"exists": False, "pending": False, "expired": False}),
    ({"expired": False, "consumed_at": None}, {"exists": True, "pending": True, "expired": False}),
    ({"expired": True, "consumed_at": None}, {"exists": True, "pending": False, "expired": True}),
    ({"expired": False, "consumed_at": "used"}, {"exists": True, "pending": False, "expired": False}),
])
def test_service_browser_public_status_sanitized(service_config, monkeypatch, row, expected):
    import service_browser_sessions as service
    if row:
        row = {**row, "environment": "staging", "origin": service_config.origin}
    _service_database(monkeypatch, [row])
    result = service.store.status(service_config, "10000000-0000-4000-8000-000000000001")
    assert result == {**expected, "environment": "staging", "origin": service_config.origin}


def test_service_browser_tamper_and_db_outage_fail_closed(service_config, monkeypatch):
    import service_browser_sessions as service
    import operator_principals
    sealed = service_config.cipher.encrypt(b"real-token").decode()
    for value in ("bad", sealed[:-5] + "xxxxx"):
        with pytest.raises(HTTPException):
            service.unseal(service_config, value)
    def unavailable():
        raise RuntimeError("private driver parameters")
    monkeypatch.setattr(operator_principals, "_db", unavailable)
    with pytest.raises(HTTPException) as error:
        service.store.create(service_config, "nonce")
    assert error.value.status_code == 503
    assert "private" not in error.value.detail


@pytest.mark.parametrize("tamper", [False, True])
def test_service_browser_transaction_unit_rollback(service_config, monkeypatch, tamper):
    """Context-manager wiring only; this fake cannot prove PG lock semantics."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from datetime import datetime, timezone
    import operator_principals
    import service_browser_sessions as service
    events = []
    row = {"sealed_token": "tampered" if tamper else service_config.cipher.encrypt(b"unit-token").decode(),
           "subject": SERVICE_SUBJECT, "token_expires_at": datetime.now(timezone.utc)}
    class Cursor(_ServiceCursor):
        def execute(self, sql, params):
            events.append("insert" if sql.startswith("INSERT") else "consume")
            if sql.startswith("INSERT"):
                raise RuntimeError("DO_NOT_EMIT_DRIVER_PARAMETERS")
            super().execute(sql, params)
    cursor = Cursor([row])
    @contextmanager
    def open_cursor():
        yield cursor
    @contextmanager
    def transaction():
        events.append("begin")
        try:
            yield SimpleNamespace(cursor=open_cursor)
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")
    monkeypatch.setattr(operator_principals, "_db", lambda: SimpleNamespace(transaction=transaction))
    with pytest.raises(HTTPException) as error:
        service.store.consume(service_config, "10000000-0000-4000-8000-000000000001", "unit-nonce")
    assert events == (["begin", "consume", "rollback"] if tamper else
                      ["begin", "consume", "insert", "rollback"])
    assert error.value.status_code == (401 if tamper else 503)
    assert "DO_NOT_EMIT" not in error.value.detail
    if not tamper:
        assert error.value.__suppress_context__


def test_service_browser_middleware_joint_proof_and_preservation(service_config, monkeypatch):
    import asyncio
    import service_browser_sessions as service
    binding = "10000000-0000-4000-8000-000000000002"
    secret = "a" * 43
    resolved, seen = [], []
    def resolve(cfg, selected, supplied):
        resolved.append((selected, supplied))
        assert selected == binding and supplied == secret
        return {"binding_id": binding}, "real.jwt.value"
    monkeypatch.setattr(service.store, "resolve", resolve)
    async def app(scope, receive, send):
        seen.append(scope)
    async def run(path, headers):
        messages = []
        async def send(message):
            messages.append(message)
        await service.ServiceBrowserSessionMiddleware(app)(
            _service_request(path, headers).scope, None, send)
        return messages
    header = (b"authorization", ("LeafServiceSession " + binding).encode())
    cookie = (b"cookie", (service.SESSION_COOKIE + "=" + secret).encode())
    asyncio.run(run("/api/tools", [header, cookie, (b"x-dispatch-secret", b"alternate")]))
    assert dict(seen[-1]["headers"])[b"authorization"] == b"Bearer real.jwt.value"
    assert b"x-dispatch-secret" not in dict(seen[-1]["headers"])
    assert len(resolved) == 1
    for headers in ([header], [(b"authorization", b"LeafServiceSession broken"), cookie],
                    [header, cookie, header]):
        messages = asyncio.run(run("/api/tools", headers))
        assert messages[0]["status"] == 401
    # Cookie alone passes through without injecting any authorization.
    asyncio.run(run("/api/tools", [cookie]))
    assert b"authorization" not in dict(seen[-1]["headers"])
    original = [(b"authorization", b"Bearer human.jwt"), cookie]
    asyncio.run(run("/api/tools", original))
    assert seen[-1]["headers"] == [(b"host", b"platform-staging.leafdesign.ai"), *original]
    asyncio.run(run("/internal/ops", [header, cookie]))
    assert dict(seen[-1]["headers"])[b"authorization"] == header[1]
    assert len(resolved) == 1


def test_service_browser_login_csp_cookie_and_logout_csrf(service_config, monkeypatch):
    from routers import service_browser_auth as routes
    import service_browser_sessions as service
    challenge = "10000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(service.store, "create", lambda cfg, nonce: challenge)
    result = routes.login(_service_request())
    assert challenge.encode() in result.body
    assert "default-src 'none'" in result.headers["content-security-policy"]
    cookie = result.headers["set-cookie"]
    assert "__Host-" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "SameSite=strict" in cookie and "Domain=" not in cookie and "Max-Age=120" in cookie
    assert b"signal: controller.signal" in result.body and b"controller.abort()" in result.body
    # Malformed public lookups are sanitized without consulting custody storage.
    monkeypatch.setattr(service.store, "status", lambda *args: pytest.fail("invalid lookup touched DB"))
    invalid = routes.status("DO_NOT_REFLECT_LOOKUP_INPUT", _service_request())
    assert json.loads(invalid.body) == {"exists": False, "pending": False, "expired": False,
                                      "environment": "staging", "origin": service_config.origin}
    assert b"DO_NOT_REFLECT" not in invalid.body
    called = []
    monkeypatch.setattr(service.store, "revoke", lambda *args: called.append(args))
    with pytest.raises(HTTPException) as error:
        routes.logout(_service_request(headers=[(b"origin", b"https://evil.invalid")]), None)
    assert error.value.status_code == 403 and not called
    with pytest.raises(HTTPException):
        routes.session(_service_request(), None)


def test_service_browser_sql_session_revocation_and_cookie_validation(service_config, monkeypatch):
    import service_browser_sessions as service
    binding = "10000000-0000-4000-8000-000000000002"
    cur = _service_database(monkeypatch, [None, {"binding_id": binding}, None])
    with pytest.raises(HTTPException):
        service.store.resolve(service_config, binding, "secret")
    assert "revoked_at IS NULL" in cur.calls[0][0] and "expires_at>clock_timestamp()" in cur.calls[0][0]
    service.store.revoke(service_config, binding, "secret")
    with pytest.raises(HTTPException):
        service.store.revoke(service_config, binding, "secret")
    for raw in ("", service.SESSION_COOKIE + "=bad",
                (service.SESSION_COOKIE + "=" + "a" * 43 + "; ") * 2):
        with pytest.raises(HTTPException):
            service.cookie_value(_service_request(headers=[(b"cookie", raw.encode())]), service.SESSION_COOKIE)


def test_service_browser_migration_inventory_and_cli_fail_closed(monkeypatch):
    import importlib.util
    root = SERVER_DIR.parent
    inventory = json.loads((root / "platform/authority-inventory.json").read_text())
    assert "0101" in inventory["scope"]["migration_ids"]
    assert "0101_service_browser_sessions.sql" in inventory["scope"]["migration_files"]
    sql = (root / "platform/migrations/0101_service_browser_sessions.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS service_login_challenges" in sql
    assert "CREATE TABLE IF NOT EXISTS service_browser_sessions" in sql
    spec = importlib.util.spec_from_file_location("studio_agent_login_unit", root / "scripts/studio_agent_login.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    with pytest.raises(cli.LoginFailure):
        cli.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")
    package = {"schema": "leaf.studio-agent-admin.credential.v1", "environment": "staging",
               "domain": "leafautomation.us.auth0.com",
               "audience": "https://api.leafdesign.ai", "client_id": SERVICE_CLIENT_ID,
               "client_secret": "fake-unit-secret"}
    assert cli.validate_package(package, "staging") == package
    assert cli.validate_package({**package, "environment": "production"}, "production")["audience"] == "https://api.leafdesign.ai"
    for name, value in (("environment", "production"), ("domain", "evil.invalid"),
                        ("schema", "foreign"), ("client_id", "bad"), ("audience", "foreign")):
        with pytest.raises(cli.LoginFailure):
            cli.validate_package({**package, name: value}, "staging")
    called = []
    monkeypatch.setattr(cli, "credential_package", lambda *args: called.append(args))
    monkeypatch.setattr(cli, "request_json", lambda *args, **kwargs: {"ok": True, "release_source_sha": "b" * 40})
    with pytest.raises(cli.LoginFailure):
        cli.approve("staging", "10000000-0000-4000-8000-000000000001", "a" * 40)
    assert not called


def test_service_browser_http_unit_flow(service_config, monkeypatch):
    """Isolated HTTP + signed JWT evidence with a fake custody store, not PG."""
    from datetime import datetime, timedelta, timezone
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import service_browser_auth as routes
    import service_browser_sessions as service
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", "https://leafautomation.us.auth0.com/")
    monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", SERVICE_SUBJECT)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda sub: ("org_acme_solar", "hosted_pro"))
    challenge = "10000000-0000-4000-8000-000000000001"
    binding = "10000000-0000-4000-8000-000000000002"
    memory = {"approved": False, "consumed": False, "revoked": False}
    token = mint(iss="https://leafautomation.us.auth0.com/", extra_claims=_service_admin_claims())
    class UnitStore:
        def create(self, cfg, nonce):
            memory["nonce"] = nonce
            return challenge

        def approve(self, cfg, selected, supplied_token, subject, exp):
            assert selected == challenge and supplied_token == token and subject == SERVICE_SUBJECT
            if memory["approved"]:
                service.deny(409)
            memory["approved"] = True

        def status(self, cfg, selected):
            return {"exists": True, "pending": not memory["consumed"], "expired": False,
                    "environment": cfg.environment, "origin": cfg.origin}

        def consume(self, cfg, selected, nonce):
            if (selected != challenge or nonce != memory["nonce"]
                    or not memory["approved"] or memory["consumed"]):
                return None
            memory["consumed"] = True
            return binding, "s" * 43

        def resolve(self, cfg, selected, secret):
            if (not memory["consumed"] or memory["revoked"] or selected != binding
                    or secret != "s" * 43):
                service.deny()
            return {"binding_id": binding, "subject": SERVICE_SUBJECT,
                    "expires_at": datetime.now(timezone.utc) + timedelta(seconds=90)}, token

        def revoke(self, cfg, selected, secret):
            assert selected == binding and secret == "s" * 43
            memory["revoked"] = True
    monkeypatch.setattr(service, "store", UnitStore())
    app = FastAPI()
    app.include_router(routes.router)
    app.add_middleware(service.ServiceBrowserSessionMiddleware)
    client = TestClient(app, base_url=service_config.origin)
    assert client.get("/api/auth/agent/login").status_code == 200
    origin = {"Origin": service_config.origin}
    assert client.post("/api/auth/agent/challenge/" + challenge + "/poll", headers=origin).json()["pending"]
    headers = {"Authorization": bearer(token)}
    assert client.post("/api/auth/agent/approve", json={"challenge_id": challenge}, headers=headers).status_code == 200
    assert client.post("/api/auth/agent/approve", json={"challenge_id": challenge}, headers=headers).status_code == 409
    wrong_browser = TestClient(app, base_url=service_config.origin)
    wrong_browser.cookies.set(service.PENDING_COOKIE, "w" * 43)
    wrong_poll = wrong_browser.post("/api/auth/agent/challenge/" + challenge + "/poll", headers=origin)
    assert wrong_poll.status_code == 200 and "redirect" not in wrong_poll.json()
    assert not memory["consumed"] and service.SESSION_COOKIE not in wrong_browser.cookies
    result = client.post("/api/auth/agent/challenge/" + challenge + "/poll", headers=origin)
    assert result.json() == {"redirect": "/?service_session=" + binding}
    assert token not in result.text
    assert client.get("/api/auth/agent/session").status_code == 401  # cookie alone
    assert client.post("/api/auth/agent/logout", headers={"Origin": "https://evil.invalid"}).status_code == 403
    service_headers = {"Authorization": "LeafServiceSession " + binding}
    other_tab = TestClient(app, base_url=service_config.origin)
    assert other_tab.get("/api/auth/agent/session", headers=service_headers).status_code == 401
    identity = client.get("/api/auth/agent/session", headers=service_headers)
    assert identity.status_code == 200 and identity.json()["authenticated"] is True
    assert identity.json()["identity"]["iss"] == "https://leafautomation.us.auth0.com/"
    assert identity.json()["identity"]["sub"] == SERVICE_SUBJECT
    assert identity.json()["identity"]["org_id"] == "org_acme_solar"
    assert token not in identity.text and "sealed_token" not in identity.text
    # Custody must never cache canonical admin/binding authority between calls.
    monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", "")
    assert client.get("/api/auth/agent/session", headers=service_headers).status_code == 403
    monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", SERVICE_SUBJECT)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda sub: ("foreign-org", "hosted_pro"))
    assert client.get("/api/auth/agent/session", headers=service_headers).status_code == 409
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda sub: ("org_acme_solar", "hosted_pro"))
    assert client.post("/api/auth/agent/logout", headers={**service_headers, "Origin": "https://evil.invalid"}).status_code == 403
    assert not memory["revoked"]
    signed_out = client.post("/api/auth/agent/logout", headers={**service_headers, **origin})
    assert signed_out.status_code == 200 and memory["revoked"]
    assert "Max-Age=0" in signed_out.headers["set-cookie"]
    assert client.get("/api/auth/agent/session", headers=service_headers).status_code == 401


def test_service_browser_cli_signed_token_checks_and_sanitized_receipt(monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("studio_agent_login_crypto_unit",
                                                SERVER_DIR.parent / "scripts/studio_agent_login.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    package = {"client_id": SERVICE_CLIENT_ID, "audience": cli.TARGETS["staging"][1]}
    monkeypatch.setattr(cli, "request_json", lambda *args, **kwargs: {"keys": [_jwk]})
    valid = mint(iss=cli.ISSUER, aud=package["audience"], extra_claims=_service_admin_claims())
    assert cli.verify_token(valid, package)["sub"] == SERVICE_SUBJECT
    for bad in (
        mint(iss=cli.ISSUER, aud=package["audience"]),
        mint(iss=cli.ISSUER, aud=package["audience"], extra_claims={**_service_admin_claims(), "azp": "ForeignClient"}),
        mint(iss=cli.ISSUER, aud=package["audience"], exp_delta=-60, extra_claims=_service_admin_claims()),
        mint(iss=cli.ISSUER, aud="https://foreign.invalid", extra_claims=_service_admin_claims()),
        mint(iss=cli.ISSUER, aud=package["audience"], key=_foreign_pem, extra_claims=_service_admin_claims()),
    ):
        with pytest.raises(Exception):
            cli.verify_token(bad, package)
    def secret_error(*args):
        raise RuntimeError("DO_NOT_EMIT_PROVIDER_SECRET")
    monkeypatch.setattr(cli, "approve", secret_error)
    assert cli.main(["approve", "--environment", "staging", "--challenge-id",
                     "10000000-0000-4000-8000-000000000001", "--expect-source", "a" * 40]) == 1
    captured = capsys.readouterr()
    assert "DO_NOT_EMIT" not in captured.out + captured.err
    assert json.loads(captured.out) == {"approved": False, "environment": "staging",
                                        "error": "service login approval failed"}
    assert cli.main(["approve", "--environment", "DO_NOT_ECHO_ARBITRARY_INPUT"]) == 1
    captured = capsys.readouterr()
    assert "DO_NOT_ECHO" not in captured.out + captured.err


@pytest.mark.parametrize("failure", [None, "metadata", "source-changed"])
def test_service_browser_cli_approval_order_unit(monkeypatch, failure):
    """Fake transport/package only: no provider, DPAPI or credential-store I/O."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("studio_agent_login_order_unit",
                                                SERVER_DIR.parent / "scripts/studio_agent_login.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    challenge, source = "10000000-0000-4000-8000-000000000001", "a" * 40
    origin, audience = cli.TARGETS["staging"]
    calls = []
    def package(environment):
        calls.append("credential")
        assert environment == "staging"
        return {"client_id": SERVICE_CLIENT_ID, "client_secret": "unit-secret", "audience": audience}
    def verified(token, package):
        assert token == "unit-token" and package["client_id"] == SERVICE_CLIENT_ID
        calls.append("verify")
    def transport(url, *, body=None, bearer=None):
        if url.endswith("/api/health"):
            assert body is None and bearer is None
            calls.append("health")
            return {"ok": True, "source_identity": "built",
                    "release_source_sha": "b" * 40 if failure == "source-changed" and "verify" in calls else source}
        if url.endswith("/status"):
            assert body is None and bearer is None
            calls.append("metadata")
            return {"exists": True, "pending": True, "expired": False, "origin": origin,
                    "environment": "production" if failure == "metadata" else "staging"}
        if url == cli.ISSUER + "oauth/token":
            calls.append("mint")
            assert bearer is None and body == {"grant_type": "client_credentials",
                    "client_id": SERVICE_CLIENT_ID, "client_secret": "unit-secret", "audience": audience}
            return {"access_token": "unit-token", "token_type": "Bearer"}
        assert url == origin + "/api/auth/agent/approve"
        calls.append("approve")
        assert bearer == "unit-token" and body == {"challenge_id": challenge}
        return {"approved": True, "challenge_id": challenge, "environment": "staging", "origin": origin}
    monkeypatch.setattr(cli, "credential_package", package)
    monkeypatch.setattr(cli, "verify_token", verified)
    monkeypatch.setattr(cli, "request_json", transport)
    if failure:
        with pytest.raises(cli.LoginFailure):
            cli.approve("staging", challenge, source)
    else:
        cli.approve("staging", challenge, source)
    expected = ["health", "metadata"]
    if failure != "metadata":
        expected += ["credential", "mint", "verify", "health"]
    if not failure:
        expected += ["approve"]
    assert calls == expected


# --------------------------------------------------------------------------- #
# UNIT tests
# --------------------------------------------------------------------------- #
def test_valid_token_accepted():
    payload = auth.verify_platform_token(bearer(mint()))
    assert payload[NS + "tenant_id"] == "org_acme_solar"
    claims = auth.extract_tenant_claims(payload)
    # `roles` is RAW here (None when the token predates the roles claim);
    # normalization happens at the deps seam (contract/AUTH.md Â§11.5).
    assert claims == {"tenant_id": "org_acme_solar", "org_id": "org_acme_solar",
                      "tier": "hosted_pro", "roles": None}


def test_tampered_signature_rejected():
    tok = mint()
    head, body, sig = tok.split(".")
    bad = sig[:-2] + ("aa" if sig[-2:] != "aa" else "bb")
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(f"{head}.{body}.{bad}"))
    assert ei.value.status_code == 401


def test_foreign_key_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(key=_foreign_pem)))
    assert ei.value.status_code == 401


def test_expired_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(exp_delta=-30)))
    assert ei.value.status_code == 401


def test_wrong_audience_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(aud="https://api.wrong.example")))
    assert ei.value.status_code == 401


def test_production_audience_rejects_staging_token():
    saved_audience = os.environ.get("LEAF_AUTH0_AUDIENCE")
    try:
        os.environ.pop("LEAF_AUTH0_AUDIENCE", None)
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(bearer(mint(aud="https://api-staging.leafdesign.ai")))
        assert ei.value.status_code == 401
        payload = auth.verify_platform_token(bearer(mint(aud="https://api.leafdesign.ai")))
        assert payload["aud"] == "https://api.leafdesign.ai"
    finally:
        if saved_audience is None:
            os.environ.pop("LEAF_AUTH0_AUDIENCE", None)
        else:
            os.environ["LEAF_AUTH0_AUDIENCE"] = saved_audience


def test_staging_audience_rejects_production_token():
    saved_audience = os.environ.get("LEAF_AUTH0_AUDIENCE")
    try:
        os.environ["LEAF_AUTH0_AUDIENCE"] = "https://api-staging.leafdesign.ai"
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(bearer(mint(aud="https://api.leafdesign.ai")))
        assert ei.value.status_code == 401
        payload = auth.verify_platform_token(bearer(mint(aud="https://api-staging.leafdesign.ai")))
        assert payload["aud"] == "https://api-staging.leafdesign.ai"
    finally:
        if saved_audience is None:
            os.environ.pop("LEAF_AUTH0_AUDIENCE", None)
        else:
            os.environ["LEAF_AUTH0_AUDIENCE"] = saved_audience


def test_accepted_audience_admits_transitional_token():
    saved = {key: os.environ.get(key) for key in
             ("LEAF_AUTH0_AUDIENCE", "LEAF_AUTH0_ACCEPTED_AUDIENCES")}
    try:
        os.environ["LEAF_AUTH0_AUDIENCE"] = "https://api.leafdesign.ai"
        os.environ["LEAF_AUTH0_ACCEPTED_AUDIENCES"] = "https://api-staging.leafdesign.ai"
        for aud in ("https://api-staging.leafdesign.ai", "https://api.leafdesign.ai"):
            payload = auth.verify_platform_token(bearer(mint(aud=aud)))
            assert payload["aud"] == aud
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_accepted_audience_unset_keeps_single_audience():
    saved = {key: os.environ.get(key) for key in
             ("LEAF_AUTH0_AUDIENCE", "LEAF_AUTH0_ACCEPTED_AUDIENCES")}
    try:
        os.environ["LEAF_AUTH0_AUDIENCE"] = "https://api.leafdesign.ai"
        os.environ.pop("LEAF_AUTH0_ACCEPTED_AUDIENCES", None)
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(bearer(mint(aud="https://api-staging.leafdesign.ai")))
        assert ei.value.status_code == 401
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_malformed_accepted_audiences_fail_closed():
    saved = {key: os.environ.get(key) for key in
             ("LEAF_AUTH0_AUDIENCE", "LEAF_AUTH0_ACCEPTED_AUDIENCES")}
    try:
        os.environ["LEAF_AUTH0_AUDIENCE"] = "https://api.leafdesign.ai"
        os.environ["LEAF_AUTH0_ACCEPTED_AUDIENCES"] = (
            "https://api-staging.leafdesign.ai, http://insecure.example")
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(bearer(mint(aud="https://api-staging.leafdesign.ai")))
        assert ei.value.status_code == 401
        payload = auth.verify_platform_token(bearer(mint(aud="https://api.leafdesign.ai")))
        assert payload["aud"] == "https://api.leafdesign.ai"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_too_many_accepted_audiences_fail_closed():
    saved = {key: os.environ.get(key) for key in
             ("LEAF_AUTH0_AUDIENCE", "LEAF_AUTH0_ACCEPTED_AUDIENCES")}
    try:
        os.environ["LEAF_AUTH0_AUDIENCE"] = "https://api.leafdesign.ai"
        os.environ["LEAF_AUTH0_ACCEPTED_AUDIENCES"] = (
            "https://api-staging.leafdesign.ai,https://one.example,"
            "https://two.example,https://three.example")
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(bearer(mint(aud="https://api-staging.leafdesign.ai")))
        assert ei.value.status_code == 401
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_accepted_audiences_order_and_dedup():
    saved = {key: os.environ.get(key) for key in
             ("LEAF_AUTH0_AUDIENCE", "LEAF_AUTH0_ACCEPTED_AUDIENCES")}
    try:
        os.environ["LEAF_AUTH0_AUDIENCE"] = AUD
        transitional = "https://api-staging.leafdesign.ai"
        os.environ["LEAF_AUTH0_ACCEPTED_AUDIENCES"] = (
            f" {transitional} , {AUD}, {transitional} ,")
        assert auth.accepted_audiences() == [AUD, transitional]
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_wrong_issuer_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(iss="https://evil.example/")))
    assert ei.value.status_code == 401


def test_missing_or_bad_authorization_rejected():
    for hdr in (None, "", "Basic abc", "Bearer", "Bearer   "):
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(hdr)
        assert ei.value.status_code == 401, hdr


def test_missing_tenant_claim_rejected_403():
    payload = auth.verify_platform_token(bearer(mint(include_tenant=False)))  # verifies OK
    with pytest.raises(HTTPException) as ei:
        auth.extract_tenant_claims(payload)
    assert ei.value.status_code == 403


SERVICE_CLIENT_ID = "LeafService_1234"
SERVICE_SUBJECT = SERVICE_CLIENT_ID + "@clients"


def _service_admin_claims():
    return {
        "sub": SERVICE_SUBJECT,
        "azp": SERVICE_CLIENT_ID,
        NS + "tier": "admin",
        NS + "roles": ["platform_admin"],
        NS + "principal_type": "service_admin",
    }


def _assert_principal_rejected(claims):
    # Both seams must enforce the check: platform uses the verifier directly,
    # while server callers additionally extract claims from a verified payload.
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(extra_claims=claims)))
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        auth.extract_tenant_claims({NS + "tenant_id": "org_acme_solar", **claims})
    assert ei.value.status_code == 403


def test_elevated_machine_requires_exact_service_admin_marker():
    for tier, roles in [
        ("admin", []), ("hosted_pro", ["platform_admin"]),
        ("restricted", [" Platform_Admin "]),
    ]:
        for marker in [None, "", "human", "Service_Admin", "service_admin "]:
            claims = {
                **_service_admin_claims(), NS + "tier": tier,
                NS + "roles": roles, NS + "principal_type": marker,
            }
            _assert_principal_rejected(claims)
        claims.pop(NS + "principal_type")
        _assert_principal_rejected(claims)


def test_service_admin_requires_exact_azp_even_without_admin_claims():
    for tier, roles in [("admin", ["platform_admin"]), ("restricted", [])]:
        for azp in [None, "", 42, [SERVICE_CLIENT_ID], SERVICE_SUBJECT,
                    SERVICE_CLIENT_ID.lower(), "LeafServ1ce_1234",
                    SERVICE_CLIENT_ID + " "]:
            claims = {**_service_admin_claims(), "azp": azp,
                      NS + "tier": tier, NS + "roles": roles}
            _assert_principal_rejected(claims)
        claims.pop("azp")
        _assert_principal_rejected(claims)


def test_service_admin_marker_requires_a_well_formed_machine_subject():
    for tier in ["admin", "hosted_pro"]:
        for subject in ["auth0|tester", "", "@clients", "short@clients",
                        "bad.client@clients", "nonascii_Ã©@clients",
                        SERVICE_SUBJECT + "@clients", SERVICE_SUBJECT + " ",
                        SERVICE_SUBJECT + "\n", SERVICE_CLIENT_ID + "@CLIENTS",
                        "x" * 129 + "@clients"]:
            # Matching azp alone cannot rescue a malformed service subject.
            azp = subject[:-len("@clients")] if subject.endswith("@clients") else subject
            _assert_principal_rejected({
                **_service_admin_claims(), "sub": subject, "azp": azp,
                NS + "tier": tier,
            })
    with pytest.raises(HTTPException) as ei:
        auth.extract_tenant_claims({
            NS + "tenant_id": "org_acme_solar",
            NS + "principal_type": "service_admin", "azp": SERVICE_CLIENT_ID,
        })
    assert ei.value.status_code == 403


def test_exact_service_admin_shape_is_accepted_without_implying_grants():
    for tier, roles in [("admin", ["platform_admin"]), ("hosted_pro", [])]:
        payload = auth.verify_platform_token(bearer(mint(extra_claims={
            **_service_admin_claims(), NS + "tier": tier, NS + "roles": roles,
        })))
        assert payload["sub"] == SERVICE_SUBJECT
        assert payload["azp"] == SERVICE_CLIENT_ID
        assert auth.extract_tenant_claims(payload) == {
            "tenant_id": "org_acme_solar", "org_id": "org_acme_solar",
            "tier": tier, "roles": roles,
        }
    # The marker confers no second factor. Use the existing server seam, with
    # its allowlist explicitly scoped here, to keep that distinction pinned.
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("LEAF_PLATFORM_ADMIN_SUBJECTS", raising=False)
        assert deps.admin_elevated_tier("admin", SERVICE_SUBJECT, "hosted_pro") == "hosted_pro"
        mp.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", SERVICE_SUBJECT + "x")
        assert deps.admin_elevated_tier("admin", SERVICE_SUBJECT, "hosted_pro") == "hosted_pro"
        mp.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", SERVICE_SUBJECT)
        assert deps.admin_elevated_tier("admin", SERVICE_SUBJECT, "hosted_pro") == "admin"
        assert deps.admin_elevated_tier("hosted_pro", SERVICE_SUBJECT, "hosted_pro") == "hosted_pro"


def test_humans_and_ordinary_machines_keep_existing_claim_behavior():
    for claims in [
        {"sub": "auth0|tester", NS + "tier": "admin", NS + "roles": ["platform_admin"]},
        {"sub": SERVICE_SUBJECT, NS + "roles": ["owner"]},
        {"sub": SERVICE_SUBJECT, "azp": "unrelated", NS + "roles": []},
        {"sub": SERVICE_SUBJECT, NS + "roles": "platform_admin"},
    ]:
        payload = auth.verify_platform_token(bearer(mint(extra_claims=claims)))
        extracted = auth.extract_tenant_claims(payload)
        assert extracted["tier"] == claims.get(NS + "tier", "hosted_pro")
        assert extracted["roles"] == claims[NS + "roles"]


def test_service_marker_does_not_bypass_ordinary_jwt_verification():
    invalid_tokens = [
        mint(extra_claims=_service_admin_claims(), key=_foreign_pem),
        mint(extra_claims=_service_admin_claims(), aud="https://api.wrong.example"),
        mint(extra_claims=_service_admin_claims(), iss="https://evil.example/"),
        mint(extra_claims=_service_admin_claims(), exp_delta=-30),
    ]
    for token in invalid_tokens:
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(bearer(token))
        assert ei.value.status_code == 401


# --------------------------------------------------------------------------- #
# HTTP matrix over /api/session (LEAF_AUTH_LIVE=1)
# --------------------------------------------------------------------------- #
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import session as session_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(session_router.router)
    return TestClient(app, raise_server_exceptions=False)


def test_http_session_no_token_401():
    r = _client().get("/api/session")
    assert r.status_code == 401


def test_http_session_valid_token_200_echoes_tenant():
    r = _client().get(
        "/api/session",
        headers={"Authorization": bearer(mint(
            tenant_id=PLATFORM_TENANT_ID, org_id="website_org_cuid"))},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == PLATFORM_TENANT_ID
    assert body["org_id"] == PLATFORM_TENANT_ID
    assert isinstance(body["intake"], dict) and "polylines" in body["intake"]


def _raw_tenant_client():
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/tenant")
    def tenant(tenant_id=Depends(deps.require_tenant)):
        return deps.tenant_echo({}, tenant_id)

    return TestClient(app, raise_server_exceptions=False)


def test_service_admin_still_requires_current_canonical_tenant_authority():
    def service_binding(subject):
        assert subject == SERVICE_SUBJECT
        return PLATFORM_TENANT_ID, "hosted_pro"

    def no_binding(subject):
        assert subject == SERVICE_SUBJECT
        raise HTTPException(status_code=403, detail="no active platform identity binding")

    token = mint(tenant_id=PLATFORM_TENANT_ID, extra_claims=_service_admin_claims())
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", SERVICE_SUBJECT)
        mp.setattr(deps, "resolve_active_platform_tenant_authority", service_binding)
        client = _raw_tenant_client()
        response = client.get("/tenant", headers={"Authorization": bearer(token)})
        assert response.status_code == 200
        assert response.json()["tenant_id"] == PLATFORM_TENANT_ID
        assert response.json()["tier"] == "admin"
        stale = client.get("/tenant", headers={
            "Authorization": bearer(mint(extra_claims=_service_admin_claims())),
        })
        assert stale.status_code == 409
        mp.setattr(deps, "resolve_active_platform_tenant_authority", no_binding)
        unbound = client.get("/tenant", headers={"Authorization": bearer(token)})
        assert unbound.status_code == 403


def test_live_tenant_namespace_comes_from_the_active_binding():
    r = _raw_tenant_client().get(
        "/tenant",
        headers={"Authorization": bearer(mint(
            tenant_id=PLATFORM_TENANT_ID, org_id="website_org_cuid"))},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == PLATFORM_TENANT_ID
    assert body["org_id"] == PLATFORM_TENANT_ID
    assert body["tier"] == "hosted_pro"


def test_a_stale_tenant_claim_fails_closed_instead_of_rerouting():
    r = _raw_tenant_client().get(
        "/tenant", headers={"Authorization": bearer(mint())})
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "verified tenant claim conflicts with the active platform binding")


def test_post_login_action_prefers_the_root_platform_tenant_id():
    action = (
        Path(__file__).resolve().parent
        / "auth0-actions"
        / "post-login-add-tenant-claim.js"
    )
    script = (
        "const a=require(process.argv[1]);"
        "const c=a.deriveClaims({user:{app_metadata:{"
        "leaf_platform_tenant_id:'" + PLATFORM_TENANT_ID + "',"
        "leaf:{organization_id:'website_org_cuid',plan:'pro'}}}});"
        "process.stdout.write(JSON.stringify(c));"
    )
    result = subprocess.run(
        ["node", "-e", script, str(action)],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "tenant_id": PLATFORM_TENANT_ID,
        "org_id": "website_org_cuid",
        "tier": "hosted_pro",
        "roles": [],
    }


def test_http_session_missing_claim_403():
    r = _client().get("/api/session", headers={"Authorization": bearer(mint(include_tenant=False))})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# script runner (acceptance: `python test_auth.py` exits 0)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    # script mode has no pytest fixtures: apply the env directly (process-local,
    # the interpreter exits right after the run so nothing can leak).
    os.environ.update(_ENV)
    tenancy.reset_store()
    original_resolver = deps.resolve_active_platform_tenant_authority
    deps.resolve_active_platform_tenant_authority = _active_test_tenant
    # Fixture-driven service custody units run under pytest. Keep the legacy
    # no-argument script runner working for its original standalone evidence.
    import inspect
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v) and not inspect.signature(v).parameters]
    failures = 0
    try:
        for t in tests:
            try:
                t()
                print(f"PASS {t.__name__}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    finally:
        deps.resolve_active_platform_tenant_authority = original_resolver
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
