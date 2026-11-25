"""Exercise the investor identity through the real application and both auth import paths."""
import asyncio
import base64
from datetime import datetime
import hmac
import json
import re
import secrets
import sys
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException
from fastapi.testclient import TestClient
import jwt
import pytest

import auth
import deps
import investor_demo as demo
from route_flatten import iter_leaf_routes
from routers import investor_demo as exchange


IDENTITY = "/api/session/identity"
TEST_ID = "00000000-0000-4000-8000-000000000001"
HOST = "platform-staging.leafdesign.ai"


def forbidden(*args, **kwargs):
    pytest.fail("unexpected authority, storage or network call")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("LEAF_INVESTOR_DEMO_ENABLED", "1")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_INVESTOR_DEMO_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setattr(demo, "_db", forbidden)
    monkeypatch.setattr(exchange, "_guard", demo.ExchangeGuard())


@pytest.fixture
def client(configured, monkeypatch):
    from app import app
    monkeypatch.setattr(demo, "binding_ok", lambda: True)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda subject: (str(demo.ORG_ID), demo.TIER))
    with_auth = TestClient(app, headers={"host": HOST})
    yield with_auth
    with_auth.close()


def link(now, lifetime=900):
    return demo.mint_link_token(issued_at=now, expires_at=now + lifetime)


def session_token():
    now = int(time.time())
    return demo.mint_session(demo.verify_link_token(link(now), now=now), now=now)[0]


def bearer(token):
    return {"authorization": "Bearer " + token}


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def derived(label):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"leaf-investor-demo/v1",
                info=label).derive(demo.config())


def signed_link(iat, exp):
    prefix = f"v1.{derived(b'key-id').hex()[:8]}.{iat}.{exp}"
    context = (b"leaf-investor-demo-link", demo.ORIGIN.encode(), str(demo.ORG_ID).encode(),
               b"read_only")
    message = prefix.encode() + b"\0" + b"".join(len(x).to_bytes(2, "big") + x for x in context)
    return prefix + "." + b64(hmac.digest(derived(b"link-mac"), message, "sha256"))


def test_configuration_gates(configured, monkeypatch):
    assert demo.enabled()
    settings = {
        "LEAF_INVESTOR_DEMO_ENABLED": (None, "", "true"),
        "LEAF_RUNTIME_ENV": (None, "production", "local", "test", "Staging", " staging"),
        "LEAF_AUTH_LIVE": (None, "0", "true"),
        "LEAF_INVESTOR_DEMO_SECRET": (None, "short", "A" * 42 + "B", "A" * 87,
                                      "A" * 43 + "=", "\N{SNOWMAN}" * 43),
    }
    for name, values in settings.items():
        for value in values:
            with monkeypatch.context() as patch:
                if value is None:
                    patch.delenv(name)
                else:
                    patch.setenv(name, value)
                assert not demo.enabled(), name
    for name in ("LEAF_DISPATCH_SECRET", "LEAF_APP_DISPATCH_SECRET", "LEAF_SERVICE_BROWSER_SESSION_KEY",
                 "LEAF_GUEST_SECRET", "LEAF_HARNESS_SECRET", "LEAF_BROKER_SECRET",
                 "LEAF_OPS_SECRET", "LEAF_DEMO_SESSION_SECRET"):
        with monkeypatch.context() as patch:
            patch.setenv(name, b64(demo.config()))
            assert not demo.enabled(), name


def test_link_format_and_mac(configured, monkeypatch):
    now = int(time.time())
    token = link(now)
    assert hmac.compare_digest(token, signed_link(now, now + 900))
    assert demo.verify_link_token(token, now=now) == (now, now + 900)
    for lifetime in (0, -1, demo.LINK_MAX_LIFETIME_S + 1):
        with pytest.raises(ValueError):
            link(now, lifetime)
    for iat in (True, float(now), "123", 1):
        with pytest.raises(ValueError):
            demo.mint_link_token(issued_at=iat, expires_at=now)
    fields = token.split(".")
    bad = [None, token.encode(), "x" * 129, token + "\N{SNOWMAN}", token.replace("v1.", "v2."),
           signed_link(now, now + demo.LINK_MAX_LIFETIME_S + 1),
           signed_link(now + 30, now + 10), link(now + 61), link(now - 900)]
    for index in range(1, 5):
        parts = fields.copy()
        parts[index] = ("2" if parts[index][0] == "1" else "1") + parts[index][1:]
        bad.append(".".join(parts))
    for start in ("+", "0"):
        parts = fields.copy()
        parts[2] = start + parts[2][1:]
        bad.append(".".join(parts))
    alphabet = "".join(map(chr, [*range(65, 91), *range(97, 123), *range(48, 58)])) + "-_"
    bad.append(token[:-1] + alphabet[alphabet.index(token[-1]) + 1])
    for value in bad:
        with pytest.raises(demo.InvalidLink) as failure:
            demo.verify_link_token(value, now=now)
        assert not failure.value.args
    monkeypatch.setenv("LEAF_INVESTOR_DEMO_SECRET", secrets.token_urlsafe(64))
    with pytest.raises(demo.InvalidLink):
        demo.verify_link_token(token, now=now)


def test_session_shape_and_bad_claims(client):
    token = session_token()
    key = derived(b"session-hs256")
    payload = jwt.decode(token, key, algorithms=["HS256"], audience=auth.audience(), issuer=demo.ISSUER)
    ns = auth.claim_ns()
    assert set(payload) == {"iss", "sub", "aud", "exp", "iat", "jti",
                            ns + "org_id", ns + "tenant_id", ns + "tier"}
    assert jwt.get_unverified_header(token) == {"alg": "HS256", "typ": "JWT"}
    assert re.fullmatch("[0-9a-f]{32}", payload["jti"])
    ok = client.get(IDENTITY, headers=bearer(token))
    assert ok.status_code == 200
    assert ok.json()["org_id"] == str(demo.ORG_ID)
    mutations = [
        {"iss": auth.issuer()}, {"sub": "auth0|other"}, {"aud": "wrong"},
        {"aud": [auth.audience()]}, {ns + "tenant_id": TEST_ID}, {ns + "org_id": TEST_ID},
        {ns + "tier": "admin"}, {ns + "roles": ["platform_admin"]},
        {ns + "principal_type": "service_admin"}, {"extra": 1}, {"jti": "wrong"},
        {"exp": int(time.time()) - 1, "iat": int(time.time()) - 10},
        {"exp": payload["iat"] + demo.SESSION_TTL_S + 1}, {"exp": payload["iat"]},
        {"iat": True}, {"iat": float(payload["iat"])},
    ]
    bad = [jwt.encode({**payload, **changes}, key, algorithm="HS256") for changes in mutations]
    bad.append(jwt.encode({k: v for k, v in payload.items() if k != "jti"}, key, algorithm="HS256"))
    for extra_header in ({"kid": "other"}, {"crit": []}, {"jku": "other"}, {"typ": "other"}):
        bad.append(jwt.encode(payload, key, algorithm="HS256", headers=extra_header))
    bad.append(jwt.encode(payload, derived(b"link-mac"), algorithm="HS256"))
    bad.append(jwt.encode(payload, None, algorithm="none"))
    for value in bad:
        response = client.get(IDENTITY, headers=bearer(value))
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "invalid investor demo session"


def test_classification_stops_before_jwks(client, monkeypatch):
    import test_auth as rig
    monkeypatch.setattr(auth, "_signing_key", forbidden)
    for changes in ({"sub": demo.SUBJECT}, {"iss": demo.ISSUER}, {"sub": demo.SUBJECT.upper()},
                    {"iss": demo.ISSUER.upper()}, {"iss": demo.ISSUER, "sub": auth.SUBJECT_PREFIX + "test"},
                    {"iss": auth.ISSUER_PREFIX + "test", "sub": demo.SUBJECT}):
        response = client.get(IDENTITY, headers=bearer(rig.mint(extra_claims=changes)))
        assert response.status_code == 401


def test_ordinary_auth_with_investor_disabled(configured, monkeypatch):
    import test_auth as rig
    for name, value in rig._ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LEAF_INVESTOR_DEMO_ENABLED")
    token = rig.mint()
    payload = auth.verify_platform_token("Bearer " + token)
    assert payload == jwt.decode(token, rig._priv.public_key(), algorithms=["RS256"],
                                 audience=rig.AUD, issuer=rig.ISS)


def test_ordinary_auth_with_investor_enabled(client, monkeypatch):
    import test_auth as rig
    for name, value in rig._ENV.items():
        monkeypatch.setenv(name, value)
    assert demo.enabled()
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", rig._active_test_tenant)
    monkeypatch.setattr(demo, "binding_ok", forbidden)
    token = rig.mint(tenant_id=rig.PLATFORM_TENANT_ID)
    response = client.get(IDENTITY, headers=bearer(token))
    assert response.status_code == 200
    assert response.json()["org_id"] == rig.PLATFORM_TENANT_ID
    assert response.json()["tier"] == "hosted_pro"


def test_runtime_host_and_binding_refusals(client, monkeypatch):
    headers = bearer(session_token())
    mixed = {**headers, "x-dispatch-secret": "test-dispatch", "x-tenant-id": TEST_ID}
    monkeypatch.setenv("LEAF_DISPATCH_SECRET", "test-dispatch")
    assert client.get("/api/tools", headers=mixed).status_code == 403
    for name, value in (("LEAF_INVESTOR_DEMO_ENABLED", "0"), ("LEAF_RUNTIME_ENV", "production"),
                        ("LEAF_RUNTIME_ENV", "local"), ("LEAF_RUNTIME_ENV", "test")):
        with monkeypatch.context() as patch:
            patch.setenv(name, value)
            assert client.get(IDENTITY, headers=headers).status_code == 401
    assert client.get(IDENTITY, headers={**headers, "host": HOST + ":443"}).status_code == 403
    assert client.get(IDENTITY, headers=[*headers.items(), ("host", HOST),
                                         ("host", HOST)]).status_code == 403
    with pytest.raises(HTTPException) as error:
        auth.verify_platform_token(headers["authorization"])
    assert error.value.status_code == 403
    monkeypatch.setattr(demo, "binding_ok", lambda: False)
    assert client.get(IDENTITY, headers=headers).status_code == 401


def test_websocket_refused_and_context_reset(configured):
    authorization = "Bearer " + session_token()
    async def handler(scope, receive, send):
        assert demo._SCOPE.get() is scope
        auth.verify_platform_token(authorization)
    scope = {"type": "websocket", "headers": [(b"host", HOST.encode())],
             "method": "GET", "route": SimpleNamespace(path=IDENTITY)}
    async def call():
        with pytest.raises(HTTPException) as error:
            await demo.RequestContextMiddleware(handler)(scope, None, None)
        assert error.value.status_code == 403
        assert demo._SCOPE.get() is None
    asyncio.run(call())


def test_concurrent_requests_keep_their_scope_in_sync_dependencies(configured, monkeypatch):
    from starlette.concurrency import run_in_threadpool
    monkeypatch.setattr(demo, "binding_ok", lambda: True)
    authorization = "Bearer " + session_token()

    async def overlap():
        arrived = {path: asyncio.Event() for path in (IDENTITY, "/docs")}

        async def handler(scope, receive, send):
            path = scope["route"].path
            arrived[path].set()
            await asyncio.gather(*(event.wait() for event in arrived.values()))
            if path == IDENTITY:
                payload = await run_in_threadpool(auth.verify_platform_token, authorization)
                assert payload["sub"] == demo.SUBJECT
            else:
                with pytest.raises(HTTPException) as error:
                    await run_in_threadpool(auth.verify_platform_token, authorization)
                assert error.value.status_code == 403

        async def request(path):
            scope = {"type": "http", "method": "GET", "route": SimpleNamespace(path=path),
                     "headers": [(b"host", HOST.encode())]}
            await demo.RequestContextMiddleware(handler)(scope, None, None)
            assert demo._SCOPE.get() is None

        await asyncio.gather(*(request(path) for path in arrived))

    asyncio.run(overlap())


def test_investor_never_elevated(client, monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", demo.SUBJECT)
    original_echo = deps.tenant_echo
    def check(body, tenant):
        assert tenant.elevated is False and tenant.roles == () and tenant.tier == "restricted"
        return original_echo(body, tenant)
    monkeypatch.setattr(deps, "tenant_echo", check)
    headers = bearer(session_token())
    assert client.get(IDENTITY, headers=headers).status_code == 200
    for authority in ((TEST_ID, demo.TIER), (str(demo.ORG_ID), "hosted_free")):
        monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda subject: authority)
        assert client.get(IDENTITY, headers=headers).status_code == 403


def test_bundled_drawing_only(client, monkeypatch, tmp_path):
    import requests
    import tenancy
    import write_loop
    from routers import session
    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(session, "_stored_drawing_intake", forbidden)
    monkeypatch.setattr(tenancy, "get_store", forbidden)
    monkeypatch.setattr(write_loop, "ensure_demo_drawing", forbidden)
    headers = bearer(session_token())
    for live in (True, False):
        monkeypatch.setattr(deps, "APS_LIVE", live)
        response = client.get("/api/session", params={"dwg": "rooftop_demo"}, headers=headers)
        assert response.status_code == 200 and response.json()["intake"] == deps.load_cached_intake()
        response = client.get("/api/session", params={"dwg": TEST_ID}, headers=headers)
        assert response.status_code == 404 and response.json()["error"]["message"] == "drawing unavailable"
    monkeypatch.setattr(deps, "DATA_FILE", tmp_path / "missing")
    assert client.get("/api/session", headers=headers).status_code == 404


@pytest.mark.parametrize("path,field,field_type", [
    ("/api/tools", "tools", list),
    ("/api/capabilities", "families", list),
    ("/api/entitlements", "entitlements", dict),
    (IDENTITY, "org_id", str),
])
def test_reads_do_not_initialize_or_materialize(client, monkeypatch, tmp_path, path, field, field_type):
    import customization_service
    import requests

    # An existing catalog database can select first-open schema initialization
    # even when this org has never had a tenant directory.
    catalog_db = tmp_path / "catalog.sqlite"
    catalog_db.touch()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(catalog_db))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(tmp_path / "tenants"))
    monkeypatch.setattr(customization_service.SQLiteCustomizationStore, "initialize", forbidden)
    monkeypatch.setattr(customization_service, "_materialize_worktree", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    response = client.get(path, headers=bearer(session_token()))
    assert response.status_code == 200
    assert isinstance(response.json()[field], field_type)
    if path in (IDENTITY, "/api/entitlements"):
        assert response.json()["org_id"] == str(demo.ORG_ID)
        assert response.json()["tier"] == demo.TIER


def test_jobs_list_does_not_open_writable_legacy_store(client, monkeypatch):
    import jobs

    monkeypatch.setenv("LEAF_JOBS_STORE", "legacy")
    monkeypatch.setattr(jobs.platform_link, "resolve_caller_binding",
                        lambda tenant: SimpleNamespace(platform_tenant_id=demo.ORG_ID))
    monkeypatch.setattr(jobs.platform_link, "list_canonical_jobs", lambda *args, **kwargs: [])
    monkeypatch.setattr(jobs, "_db", forbidden)
    response = client.get("/api/jobs", headers=bearer(session_token()))
    assert response.status_code == 200 and response.json()["jobs"] == []


def test_platform_verifier_uses_same_context_and_tenant(client, monkeypatch):
    from leaf_platform import deps as platform_deps, store
    assert platform_deps._server_auth()._server_sibling("investor_demo") is demo
    def binding(authority, subject):
        assert (authority, subject) == ("auth0", demo.SUBJECT)
        return SimpleNamespace(platform_tenant_id=demo.ORG_ID)
    monkeypatch.setattr(store, "resolve_active_identity_binding", binding)
    queried = []
    def projects(org_id):
        queried.append(org_id)
        return []
    monkeypatch.setattr(store, "list_projects", projects)
    headers = {**bearer(session_token()), "x-org-id": TEST_ID}
    response = client.get("/api/projects", headers=headers)
    assert response.status_code == 200 and response.json()["projects"] == []
    assert queried == [demo.ORG_ID]
    def get_org(org_id):
        assert org_id == demo.ORG_ID
        return SimpleNamespace(to_dict=lambda: {"org_id": str(org_id)})
    monkeypatch.setattr(store, "get_org", get_org)
    response = client.get("/api/orgs/" + str(demo.ORG_ID), headers=headers)
    assert response.status_code == 200 and response.json()["org"]["org_id"] == str(demo.ORG_ID)
    assert client.get("/api/orgs/" + TEST_ID, headers=headers).status_code == 404
    monkeypatch.setattr(store, "hydrate_project", forbidden)
    assert client.get("/api/projects/" + TEST_ID, headers=headers).status_code == 403


def test_shared_module_when_platform_imports_first(monkeypatch):
    from pathlib import Path
    import platform_link
    platform_link._ensure_platform_package()
    from leaf_platform import deps as platform_deps
    server = Path(auth.__file__).resolve().parent
    monkeypatch.delitem(sys.modules, "investor_demo")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p).resolve() != server])
    first = platform_deps._server_auth()._server_sibling("investor_demo")
    monkeypatch.syspath_prepend(str(server))
    import investor_demo
    assert first is investor_demo and first is auth._server_sibling("investor_demo")


WRITES = (
    ("POST", "/api/run"), ("POST", "/api/orgs"), ("POST", "/api/sessions"),
    ("POST", "/api/telemetry"), ("POST", "/api/drawings/upload"),
    ("POST", "/api/drawings/demo/undo"), ("POST", "/api/drawings/demo/checkout"),
    ("POST", f"/api/jobs/{TEST_ID}/close"), ("POST", "/api/tenant/claude-grant"),
    ("POST", "/api/author"), ("POST", "/api/nl-prompt"), ("DELETE", "/api/tenant/claude-grant"),
    ("PATCH", "/api/tenant/claude-grant"), ("PUT", f"/api/orgs/{TEST_ID}/addons/x"),
)


def test_mutations_and_streams_are_forbidden(client):
    headers = bearer(session_token())
    for method, path in WRITES:
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code == 403, (method, path, response.status_code)
    assert client.get(f"/api/jobs/{TEST_ID}/stream", headers=headers).status_code == 403


def test_entire_dispatched_route_table(client):
    from app import app
    headers = bearer(session_token())
    seen = set()
    grants = set()
    for template, route in iter_leaf_routes(app.routes):
        for method in getattr(route, "methods", ()):
            pair = (method, template)
            if pair in demo.ROUTE_ALLOWLIST:
                assert route.path == template
                grants.add(pair)
                continue
            path = re.sub(r"\{[^}]+\}", TEST_ID, template)
            response = client.request(method, path, headers=headers, follow_redirects=False)
            assert response.status_code == 403, (pair, response.status_code)
            seen.add(pair)
    assert len(seen) > 100
    assert {("GET", "/docs"), ("GET", "/openapi.json"), ("POST", exchange.PATH)} <= seen
    assert ("POST", "/api/run") in seen and ("GET", "/api/projects/{project_id}/scope") in seen
    assert grants == demo.ROUTE_ALLOWLIST
    assert grants == {("GET", path) for path in (
        IDENTITY, "/api/session", "/api/tools", "/api/capabilities", "/api/entitlements",
        "/api/orgs/{org_id}", "/api/projects", "/api/jobs",
    )}


def test_boundary_precedes_preflight_and_credential_translation(client, monkeypatch):
    token = session_token()
    headers = {**bearer(token), "origin": demo.ORIGIN, "access-control-request-method": "POST"}
    assert client.options("/api/telemetry", headers=headers).status_code == 403
    mixed = [("authorization", "LeafServiceSession invalid"), *bearer(token).items()]
    assert client.get("/api/health", headers=mixed).status_code == 403
    duplicate = [*bearer(token).items(), ("authorization", "Bearer other")]
    response = client.get(IDENTITY, headers=duplicate)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "ambiguous investor demo credentials"
    monkeypatch.delenv("LEAF_INVESTOR_DEMO_SECRET")
    assert client.get("/api/health", headers=bearer(token)).status_code == 403
    for other in ("Bearer malformed", "Bearer e30.W10.", "Basic irrelevant"):
        assert client.get("/docs", headers={"authorization": other}).status_code == 200


def test_exchange_rejections_and_success(client, monkeypatch, capsys, caplog):
    def post(expected, **kwargs):
        response = client.post(exchange.PATH, **kwargs)
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        return response.json()
    with monkeypatch.context() as patch:
        patch.delenv("LEAF_INVESTOR_DEMO_ENABLED")
        assert post(404, headers={"x-demo-session": ""}) == {"detail": "not found"}
        assert post(404) == {"detail": "not found"}
    for header in ("authorization", "x-demo-session", "x-guest-session", "x-dispatch-secret"):
        post(400, headers={header: ""})
    post(400, headers={"authorization": "LeafServiceSession invalid"})
    post(403, headers={"host": "localhost"})
    post(403, headers={"origin": demo.ORIGIN + "/"})
    post(403, headers=[("host", HOST), ("host", HOST)])
    post(403, headers=[("origin", demo.ORIGIN), ("origin", demo.ORIGIN)])
    post(413, content=b" " * 1025)
    post(413, content=iter([b" " * 800, b" " * 225]))
    post(400, content=b'{"token":"x","token":"y"}')
    for obj in ([], {}, {"token": "x", "extra": 0}, {"token": 1}, {"token": {}}):
        post(400, json=obj)
    # Exercise the link checks with a fresh client bucket after the body errors.
    client.headers["cf-connecting-ip"] = "192.0.2.1"
    now = int(time.time())
    with monkeypatch.context() as patch:
        patch.setattr(demo, "binding_ok", forbidden)
        for value in ("x", link(now - 1000), link(now + 120)):
            assert post(401, json={"token": value}) == {"detail": "invalid or expired link"}
    value = link(now, 600)
    with monkeypatch.context() as patch:
        patch.setattr(demo, "binding_ok", lambda: False)
        assert post(503, json={"token": value}) == {"detail": "demo workspace unavailable"}
    refused = capsys.readouterr()
    event = "event=investor_demo_session_issued"
    if event in refused.err or event in refused.out or event in caplog.text:
        pytest.fail("refused exchange emitted an issuance event")
    with monkeypatch.context() as patch:
        from unittest.mock import Mock
        check_binding = Mock(return_value=True)
        patch.setattr(demo, "binding_ok", check_binding)
        result = post(200, json={"token": value}, headers={"origin": demo.ORIGIN})
        check_binding.assert_called_once_with()
    assert set(result) == {"session", "expires_at", "org_id", "drawing"}
    assert result["org_id"] == str(demo.ORG_ID)
    assert result["drawing"] == "rooftop_demo"
    expires = datetime.fromisoformat(result["expires_at"])
    assert expires.utcoffset().total_seconds() == 0 and expires.timestamp() == now + 600
    payload = jwt.decode(result["session"], derived(b"session-hs256"), algorithms=["HS256"],
                         audience=auth.audience())
    assert payload["exp"] == now + 600
    assert client.get(IDENTITY, headers=bearer(result["session"])).status_code == 200
    long_link = demo.verify_link_token(link(now, 3 * demo.SESSION_TTL_S), now=now)
    assert demo.mint_session(long_link, now=now)[1] == now + demo.SESSION_TTL_S
    captured = capsys.readouterr()
    prefix = payload["jti"][:8]
    assert re.fullmatch(r"[0-9a-f]{8}", prefix)
    expected = f"[leaf-demo] {event} session={prefix} expires={result['expires_at']}"
    logs = caplog.text + refused.out + refused.err + captured.out + captured.err
    if logs.count(event) != 1 or expected not in captured.err.splitlines():
        pytest.fail("expected exactly one issuance line on stderr matching the issued session")
    if any(material in logs for material in (value, result["session"], b64(demo.config()))):
        pytest.fail("investor credential appeared in exchange output")


def test_closed_audit_stream_preserves_success(client, monkeypatch):
    from io import StringIO
    stream = StringIO()
    stream.close()
    monkeypatch.setattr(exchange, "sys", SimpleNamespace(stderr=stream))
    response = client.post(exchange.PATH, json={"token": link(int(time.time()))})
    assert response.status_code == 200
    assert client.get(IDENTITY, headers=bearer(response.json()["session"])).status_code == 200


def test_exchange_client_address_rate_limit(client):
    for index in range(11):
        # Equivalent IPv6 spellings share one bucket.
        address = "::1" if index % 2 else "0:0:0:0:0:0:0:1"
        response = client.post(exchange.PATH, headers={"cf-connecting-ip": address}, json={"token": "x"})
        assert response.status_code == (401 if index < 10 else 429)
        assert response.headers["cache-control"] == "no-store"
    for index in range(11):
        response = client.post(exchange.PATH, headers={"cf-connecting-ip": f"bad-{index}"}, json={"token": "x"})
        assert response.status_code == (401 if index < 10 else 429)


async def raw_exchange(receive):
    from app import app
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "POST", "scheme": "https",
             "path": exchange.PATH, "raw_path": exchange.PATH.encode(), "query_string": b"",
             "client": ("192.0.2.1", 1234), "server": (HOST, 443),
             "headers": [(b"host", HOST.encode())]}
    messages = []

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    start, *chunks = messages
    assert start["type"] == "http.response.start"
    assert dict(start["headers"])[b"cache-control"] == b"no-store"
    assert chunks and all(chunk["type"] == "http.response.body" for chunk in chunks)
    assert not chunks[-1].get("more_body", False)
    body = b"".join(chunk.get("body", b"") for chunk in chunks)
    return start["status"], json.loads(body)


def test_exchange_body_deadline(configured, monkeypatch):
    monkeypatch.setattr(exchange, "_BODY_TIMEOUT_S", 0.01)

    async def stalled():
        await asyncio.Event().wait()

    assert asyncio.run(raw_exchange(stalled)) == (408, {"detail": "request body timed out"})


def test_exchange_limits_client_before_receiving_body(configured):
    async def bad_link():
        return {"type": "http.request", "body": b'{"token":"invalid"}'}

    async def unexpected_read():
        pytest.fail("rate-limited request read the body")

    async def requests():
        for _ in range(10):
            assert (await raw_exchange(bad_link))[0] == 401
        assert await raw_exchange(unexpected_read) == (429, {"detail": "too many requests"})

    asyncio.run(requests())


def test_exchange_only_charges_shared_ceiling_for_verified_links(client, monkeypatch):
    # Leave one verified exchange in the process budget.
    for _ in range(239):
        assert exchange._guard.allow_verified(time.monotonic())
    assert client.post(exchange.PATH, json={"token": "invalid"}).status_code == 401
    body = {"token": link(int(time.time()))}
    assert client.post(exchange.PATH, json=body).status_code == 200
    monkeypatch.setattr(demo, "binding_ok", forbidden)
    assert client.post(exchange.PATH, json=body).status_code == 429


def test_guard_concurrent_caps_and_lru():
    from concurrent.futures import ThreadPoolExecutor
    guard = demo.ExchangeGuard()
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(lambda _: guard.allow_client("peer", 0), range(30))) == 10
        assert sum(pool.map(lambda _: guard.allow_verified(0), range(260))) == 240
    assert not guard.allow_client("peer", 59)
    assert guard.allow_client("peer", 60)
    assert not guard.allow_verified(59)
    assert guard.allow_verified(60)
    guard = demo.ExchangeGuard()
    for i in range(4096):
        assert guard.allow_client(str(i), i)
    assert guard.allow_client("0", 4096)
    assert guard.allow_client("4096", 4097)
    assert len(guard._clients) == 4096 and "0" in guard._clients and "1" not in guard._clients


def test_unavailable_database_has_only_a_status_diagnostic(configured, monkeypatch, capsys):
    secret = b64(demo.config())
    def unavailable():
        raise RuntimeError(secret)
    monkeypatch.setattr(demo, "_db", unavailable)
    assert demo.binding_ok() is False
    assert demo.provision_at_startup() == "unavailable"
    output = capsys.readouterr()
    if output.out or output.err != "[leaf-demo] diagnostic_code=investor_demo_unavailable\n":
        pytest.fail("unexpected investor startup diagnostic")
