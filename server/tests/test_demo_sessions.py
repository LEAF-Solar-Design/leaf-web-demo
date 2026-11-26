"""Hermetic credential, precedence, address and dark-boundary contracts."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace
import uuid

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

import demo_sessions as demo
import deps
import entitlements


def request(headers=None, peer="198.51.100.7", path="/api/site/demo-session", method="POST"):
    return Request({"type": "http", "method": method, "scheme": "http",
                    "path": path, "query_string": b"", "server": ("test", 80),
                    "client": (peer, 1234) if peer is not None else None,
                    "headers": [(key.lower().encode(), value.encode())
                                for key, value in (headers or {}).items()]})


class Registry:
    """In-memory DB seam for token tests; SQL atomicity is proved on PostgreSQL."""
    def __init__(self):
        self.rows = {}
        self.counts = {}

    def run_transaction(self, operation, **kwargs):
        before = deepcopy((self.rows, self.counts))
        try:
            return operation(self)
        except Exception:
            self.rows, self.counts = before
            raise

    def execute(self, sql, params):
        if sql.startswith("SELECT"):
            result = self.rows.get(params[0])
        elif "INSERT INTO demo_mint_counters" in sql:
            day, scope, limit = params
            count = self.counts.get((day, scope), 0)
            result = None if count >= limit else {"count": count + 1}
            if result:
                self.counts[day, scope] = count + 1
        else:
            assert sql.startswith("INSERT INTO demo_sessions")
            keys = ("id", "subject", "tenant_id", "org_id", "created_at", "expires_at", "client_ip_hash")
            row = dict(zip(keys, params), revoked_at=None)
            self.rows[row["id"]] = row
            result = None
        return SimpleNamespace(fetchone=lambda: result)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_DEMO_SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.setenv("LEAF_DEMO_IP_HASH_SECRET", "unit-test-address-secret")
    monkeypatch.setenv("LEAF_DEMO_PER_IP_DAILY_CAP", "2")
    monkeypatch.setenv("LEAF_DEMO_GLOBAL_DAILY_CAP", "4")
    monkeypatch.delenv("LEAF_DEMO_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "")
    monkeypatch.setattr(demo, "_now", lambda: datetime(2031, 2, 3, 12, tzinfo=timezone.utc))
    store = Registry()
    monkeypatch.setattr(demo, "_db", lambda: store)
    monkeypatch.setattr(demo, "provision_demo_tenant", lambda conn, session: None)
    return store


def bomb(*args, **kwargs):
    pytest.fail("forbidden dependency was reached")


def test_provisioning_loader_initializes_once_under_concurrency(monkeypatch):
    monkeypatch.setitem(sys.modules, "demo_provisioning", None)
    monkeypatch.delitem(sys.modules, "demo_provisioning", raising=False)
    original_spec = demo.importlib.util.spec_from_file_location
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(21)
    executions = []

    def spec_spy(*args, **kwargs):
        spec = original_spec(*args, **kwargs)
        original_exec = spec.loader.exec_module

        def exec_spy(module):
            executions.append(module)
            entered.set()
            assert release.wait(timeout=10)
            original_exec(module)

        monkeypatch.setattr(spec.loader, "exec_module", exec_spy)
        return spec

    monkeypatch.setattr(demo.importlib.util, "spec_from_file_location", spec_spy)

    def load():
        barrier.wait(timeout=10)
        module = demo._load_demo_provisioning()
        assert callable(module.provision_demo_tenant)
        return module

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(load) for _ in range(20)]
        try:
            barrier.wait(timeout=10)
            assert entered.wait(timeout=10)
            assert "demo_provisioning" not in sys.modules
        finally:
            release.set()
        modules = [future.result(timeout=10) for future in futures]

    assert len(executions) == 1
    assert all(module is modules[0] for module in modules)
    assert sys.modules["demo_provisioning"] is modules[0] is executions[0]


def test_provisioning_loader_does_not_publish_failed_load_and_retries(monkeypatch):
    monkeypatch.setitem(sys.modules, "demo_provisioning", None)
    monkeypatch.delitem(sys.modules, "demo_provisioning", raising=False)
    original_spec = demo.importlib.util.spec_from_file_location
    executions = []

    def spec_spy(*args, **kwargs):
        spec = original_spec(*args, **kwargs)
        original_exec = spec.loader.exec_module

        def exec_spy(module):
            executions.append(module)
            original_exec(module)
            if len(executions) == 1:
                raise RuntimeError("injected provisioning load failure")

        monkeypatch.setattr(spec.loader, "exec_module", exec_spy)
        return spec

    monkeypatch.setattr(demo.importlib.util, "spec_from_file_location", spec_spy)
    with pytest.raises(RuntimeError, match="injected provisioning load failure"):
        demo._load_demo_provisioning()
    assert "demo_provisioning" not in sys.modules

    module = demo._load_demo_provisioning()
    assert callable(module.provision_demo_tenant)
    assert len(executions) == 2
    assert module is executions[1] and module is not executions[0]
    assert sys.modules["demo_provisioning"] is module
    assert demo._load_demo_provisioning() is module
    assert len(executions) == 2


def site_client():
    from routers.site import router
    app = FastAPI()
    @app.middleware("http")
    async def numeric_test_peer(request, call_next):
        request.scope["client"] = ("198.51.100.7", 1234)
        return await call_next(request)
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("switch", [None, "0", "true", " 1", "1 "])
def test_disabled_never_loads_postgres(monkeypatch, switch):
    if switch is None:
        monkeypatch.delenv("LEAF_DEMO_LIVE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", switch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(demo, "_db", bomb)
    token = f"{uuid.uuid4()}.2000000000." + "0" * 64
    response = site_client().post("/api/site/demo-session")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException) as error:
        deps.resolve_demo_tenant(token)
    assert error.value.status_code == 401


def test_mint_identity_is_server_owned_and_active_context_preserves_it(registry, monkeypatch):
    minted = demo.mint(request({"x-tenant-id": "attacker", "x-org-id": str(uuid.uuid4())}))
    tenant = deps.resolve_demo_tenant(minted.token)
    record = next(iter(registry.rows.values()))
    assert tenant.subject == record["subject"] == f"demo:{record['id']}"
    assert tenant == f"demo-{record['id']}"
    assert tenant.org_id == str(record["org_id"])
    assert tenant.tier == "demo_live"
    assert tenant.roles == () and tenant.elevated is False and tenant.verified_demo is True
    assert minted.expires_at - record["created_at"] == timedelta(minutes=60)
    assert "198.51.100.7" not in json.dumps(record, default=str)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", bomb)
    assert deps.require_active_tenant(tenant) is tenant


def test_expiry_boundary_revocation_and_switch_are_read_each_request(registry, monkeypatch):
    minted = demo.mint(request())
    monkeypatch.setattr(demo, "_now", lambda: minted.expires_at - timedelta(seconds=1))
    assert demo.verify(minted.token).tenant_id.startswith("demo-")
    row = next(iter(registry.rows.values()))
    row["revoked_at"] = demo._now()
    with pytest.raises(HTTPException) as error:
        demo.verify(minted.token)
    assert error.value.status_code == 401
    row["revoked_at"] = None
    assert demo.verify(minted.token)
    for offset in (0, 1):
        monkeypatch.setattr(demo, "_now", lambda: minted.expires_at + timedelta(seconds=offset))
        with pytest.raises(HTTPException) as error:
            demo.verify(minted.token)
        assert error.value.status_code == 401
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(HTTPException) as error:
        demo.verify(minted.token)
    assert error.value.status_code == 401


def test_previously_minted_token_and_issuer_are_dark_without_database(registry, monkeypatch):
    token = demo.mint(request()).token
    monkeypatch.delenv("LEAF_DEMO_LIVE_ENABLED")
    monkeypatch.setattr(demo, "_db", bomb)
    response = site_client().post("/api/site/demo-session", headers={"x-demo-session": token})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException) as error:
        deps.require_tenant(request({"x-demo-session": token}), x_tenant_id=None,
                            authorization=None, x_dispatch_secret=None, x_guest_session=None)
    assert error.value.status_code == 401


@pytest.mark.parametrize("defect", ["tampered", "secret", "unknown", "expiry_mismatch", "database"])
def test_token_defects_refuse(registry, monkeypatch, defect):
    minted = demo.mint(request())
    token = minted.token
    if defect == "tampered":
        token = token[:-1] + ("1" if token[-1] != "1" else "2")
    elif defect == "secret":
        monkeypatch.setenv("LEAF_DEMO_SESSION_SECRET", "different-secret")
    elif defect == "unknown":
        registry.rows.clear()
    elif defect == "expiry_mismatch":
        record_id, expiry, _ = token.split(".")
        expiry = int(expiry) + 1
        signature = demo._signature(uuid.UUID(record_id), expiry,
                                    demo._secret("LEAF_DEMO_SESSION_SECRET"))
        token = f"{record_id}.{expiry}.{signature}"
    else:
        monkeypatch.setattr(demo, "_db", lambda: (_ for _ in ()).throw(OSError("database down")))
    with pytest.raises(HTTPException) as error:
        demo.verify(token)
    assert error.value.status_code == 401


@pytest.mark.parametrize("name", ["LEAF_DEMO_SESSION_SECRET", "LEAF_DEMO_IP_HASH_SECRET",
                                  "LEAF_DEMO_PER_IP_DAILY_CAP", "LEAF_DEMO_GLOBAL_DAILY_CAP"])
@pytest.mark.parametrize("value", [None, "", " "])
def test_missing_configuration_refuses_before_database(registry, monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(HTTPException) as error:
        demo.mint(request())
    assert error.value.status_code == 503


@pytest.mark.parametrize("value", ["zero", "-1", "0", "1.5", "2147483648"])
def test_invalid_caps_refuse(registry, monkeypatch, value):
    monkeypatch.setenv("LEAF_DEMO_PER_IP_DAILY_CAP", value)
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(HTTPException) as error:
        demo.mint(request())
    assert error.value.status_code == 503


def test_address_chain_and_canonicalization(registry, monkeypatch):
    forged = request({"x-forwarded-for": "bad address, 192.0.2.1"})
    assert demo.client_address(forged) == "198.51.100.7"
    monkeypatch.setenv("LEAF_DEMO_TRUSTED_PROXIES", "10.0.0.0/8, 2001:db8:ffff::/48")
    chain = request({"x-forwarded-for": "192.0.2.99, 2001:db8::1, 10.0.0.2"}, peer="10.0.0.1")
    assert demo.client_address(chain) == "2001:db8::1"
    assert demo.address_hash("2001:0db8:0000::0001") == demo.address_hash("2001:db8::1")
    assert demo.address_hash("::ffff:192.0.2.1") == demo.address_hash("192.0.2.1")


@pytest.mark.parametrize("peer,forwarded", [(None, None), ("bad", None),
    ("10.0.0.1", None), ("10.0.0.1", "192.0.2.1, garbage"),
    ("10.0.0.1", "10.0.0.2"), ("fe80::1%eth0", None)])
def test_missing_malformed_or_unresolved_address_refuses(registry, monkeypatch, peer, forwarded):
    monkeypatch.setenv("LEAF_DEMO_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setattr(demo, "_db", bomb)
    headers = {} if forwarded is None else {"x-forwarded-for": forwarded}
    with pytest.raises(HTTPException) as error:
        demo.mint(request(headers, peer=peer))
    assert error.value.status_code == 400


@pytest.mark.parametrize("forwarders", ["*", " *,127.0.0.1", "0.0.0.0/0", "::/0",
                                        "10.0.0.1", "not-an-address", None])
def test_mint_refuses_unsafe_asgi_forwarders_before_database(registry, monkeypatch, forwarders):
    if forwarders is None:
        monkeypatch.delenv("FORWARDED_ALLOW_IPS")
    else:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", forwarders)
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(demo.UnsafeProxyConfiguration) as error:
        demo.mint(request({"x-forwarded-for": "192.0.2.99"}))
    assert error.value.status_code == 503
    assert not registry.rows and not registry.counts


def test_wildcard_forwarding_refuses_even_with_declared_proxy(registry, monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("LEAF_DEMO_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(demo.UnsafeProxyConfiguration):
        demo.mint(request({"x-forwarded-for": "192.0.2.99"}, peer="10.0.0.1"))
    assert not registry.rows and not registry.counts


@pytest.mark.parametrize("ranges", ["0.0.0.0/1,128.0.0.0/1", "::/1,8000::/1"])
def test_equivalent_trust_all_proxy_ranges_refuse(registry, monkeypatch, ranges):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", ranges)
    monkeypatch.setenv("LEAF_DEMO_TRUSTED_PROXIES", ranges)
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(demo.UnsafeProxyConfiguration):
        demo.mint(request())
    assert not registry.rows and not registry.counts


def test_rewritten_asgi_peer_refuses_even_with_declared_proxy(registry, monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    monkeypatch.setenv("LEAF_DEMO_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setattr(demo, "_db", bomb)
    rewritten = request({"x-forwarded-for": "192.0.2.99"})
    rewritten.scope["client"] = ("192.0.2.99", 0)
    with pytest.raises(demo.UnsafeProxyConfiguration):
        demo.mint(rewritten)
    assert not registry.rows and not registry.counts


def test_declared_finite_asgi_forwarders_keep_socket_peer_address_walk(registry, monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1,2001:db8:ffff::1")
    monkeypatch.setenv("LEAF_DEMO_TRUSTED_PROXIES", "10.0.0.0/8,2001:db8:ffff::/48")
    assert demo.client_address(request(
        {"x-forwarded-for": "192.0.2.99, 198.51.100.7, 10.0.0.2"},
        peer="10.0.0.1")) == "198.51.100.7"


@pytest.mark.parametrize("authorization", ["", "Basic malformed", "Bearer invalid"])
def test_issuer_refuses_any_authorization_and_never_caches(registry, monkeypatch, authorization):
    monkeypatch.setattr(demo, "_db", bomb)
    response = site_client().post("/api/site/demo-session", headers={"authorization": authorization})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_issuer_authorization_plus_demo_refuses_before_identity_resolution(registry, monkeypatch):
    import auth
    token = demo.mint(request()).token
    monkeypatch.setattr(demo, "_db", bomb)
    monkeypatch.setattr(auth, "verify_platform_token", bomb)
    response = site_client().post("/api/site/demo-session", headers={
        "authorization": "Bearer any", "x-demo-session": token})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_issuer_success_and_cap_are_no_store(registry):
    client = site_client()
    for status in (200, 200, 429):
        response = client.post("/api/site/demo-session")
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        if status == 200:
            assert set(response.json()) == {"token", "expires_at"}
    assert len(registry.rows) == 2
    assert set(registry.counts.values()) == {2}


def test_no_demo_capability_or_role_overlay(registry, monkeypatch):
    import roles
    monkeypatch.setattr(roles, "apply_role_grants", bomb)
    monkeypatch.setattr(entitlements, "load_policy", bomb)
    assert entitlements.entitlements_for("demo_live", ("owner",), True) == {
        cap: False for cap in entitlements.CAPABILITIES}
    policy = json.loads((Path(__file__).resolve().parents[1] / "entitlements.json").read_text())
    assert policy["demo_live"] == entitlements._HARDCODED_DEFAULTS["demo_live"]


def test_representative_gated_ungated_and_public_routes_deny_before_side_effects(registry, monkeypatch):
    from routers import jobs, session, site
    from app import DemoSessionBoundaryMiddleware
    token = demo.mint(request()).token
    app = FastAPI()
    app.include_router(session.router)
    app.include_router(jobs.router)
    app.include_router(site.router)
    app.add_middleware(DemoSessionBoundaryMiddleware, router=app.router)
    monkeypatch.setattr(deps, "load_cached_intake", bomb)
    monkeypatch.setattr(site, "_builtin_tools", bomb)
    monkeypatch.setattr(jobs.jobs, "submit_job", bomb)
    client = TestClient(app)
    for method, path in (("GET", "/api/session"), ("PUT", "/api/jobs/00000000-0000-0000-0000-000000000001"),
                         ("GET", "/api/site/capabilities")):
        response = client.request(method, path, headers={"x-demo-session": token},
                                  json={"tool": "drawing.inspect", "params": {}} if method == "POST" else None)
        assert response.status_code == 403
    assert ("POST", "/api/run") in demo.DEMO_ROUTE_ALLOWLIST


@pytest.fixture
def globally_guarded_app(monkeypatch):
    # No lifespan is entered: these requests must stop before app work starts.
    monkeypatch.setenv("LEAF_GUEST_PURGE_DISABLED", "1")
    import app as composition
    from routers import operator_secrets, tenant_mcp
    from route_flatten import iter_leaf_routes
    from starlette.routing import Mount

    assert composition.app.user_middleware[0].cls is composition.DemoSessionBoundaryMiddleware
    assert ("POST", "/api/campaigns") not in demo.DEMO_ROUTE_ALLOWLIST
    monkeypatch.setattr(tenant_mcp.tenant_mcp_store, "pop_pending", bomb)
    monkeypatch.setattr(tenant_mcp.tenant_mcp_store, "update_record", bomb)
    monkeypatch.setattr(tenant_mcp, "_exchange_code", bomb)
    child = FastAPI()
    child.include_router(operator_secrets.router)
    routes = [*composition.app.router.routes, Mount("/demo-boundary-mounted", app=child)]
    monkeypatch.setattr(composition.app.router, "routes", routes)

    async def forbidden_handler(scope, receive, send):
        pytest.fail("demo credential reached a route handler or mounted application")

    # Trap handler entry itself, including dependency execution, rather than
    # relying on an unrelated auth failure to protect a side-effect mock.
    for _, route in iter_leaf_routes(routes):
        monkeypatch.setattr(route, "app", forbidden_handler)
    # Also forbid entering the mounted app's own middleware, not just leaves.
    monkeypatch.setattr(routes[-1], "app", forbidden_handler)
    return composition.app


@pytest.fixture
def globally_guarded_callback_app(monkeypatch):
    import app as composition
    from routers import ios_ship_provider
    from route_flatten import iter_leaf_routes

    # Match start_ios_ship_callback_tls without starting the TLS listener.
    callback_app = FastAPI(title="Leaf iOS ship private callbacks")
    callback_app.include_router(ios_ship_provider.router)
    callback_app.add_middleware(composition.DemoSessionBoundaryMiddleware, router=callback_app.router)

    async def forbidden_handler(scope, receive, send):
        pytest.fail("demo credential reached a callback handler or its dependencies")

    for _, route in iter_leaf_routes(callback_app.routes):
        monkeypatch.setattr(route, "app", forbidden_handler)
    return callback_app


def boundary_messages(application, scope):
    messages = []

    async def receive():
        pytest.fail("demo credential caused request-body processing")

    async def send(message):
        messages.append(message)

    asyncio.run(application(scope, receive, send))
    return messages


@pytest.mark.parametrize("live", ["0", "1"])
@pytest.mark.parametrize("enabled", ["0", "1"])
@pytest.mark.parametrize("credential", ["empty", "malformed", "valid"])
@pytest.mark.parametrize("method,path", [
    ("POST", "/api/drawings/upload"),
    ("GET", "/api/tenant/mcp-servers/callback"),
    # Five additional routers without require_tenant, including a sub-app.
    ("POST", "/api/demand"),
    ("GET", "/api/ops/metrics"),
    ("POST", "/internal/v1/ios-ship/executions/00000000-0000-0000-0000-000000000001/progress"),
    ("POST", "/internal/project-repository-edit/record-staged"),
    ("GET", "/demo-boundary-mounted/api/operator/secrets"),
    ("POST", "/api/site/demo-session"),
    ("GET", "/api/health"),
    ("OPTIONS", "/api/drawings/upload"),
    ("GET", "/unknown-demo-route"),
])
def test_global_demo_boundary_precedes_bodies_handlers_and_all_auth_legs(
        registry, monkeypatch, globally_guarded_app, globally_guarded_callback_app,
        live, enabled, credential, method, path):
    import auth
    from route_flatten import iter_leaf_routes
    application = globally_guarded_app
    if path.startswith("/internal/v1/ios-ship/"):
        from starlette.routing import Match

        application = globally_guarded_callback_app
        callback_scope = request(path=path, method=method).scope
        assert any(route.matches(callback_scope)[0] == Match.FULL
                   for _, route in iter_leaf_routes(application.routes))
    elif path not in {"/unknown-demo-route", "/demo-boundary-mounted/api/operator/secrets"}:
        assert path in {full_path for full_path, _ in iter_leaf_routes(globally_guarded_app.routes)}
    token = demo.mint(request()).token if credential == "valid" else {
        "empty": "", "malformed": "not-a-demo-token"}[credential]
    monkeypatch.setenv("LEAF_AUTH_LIVE", live)
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", enabled)
    monkeypatch.setattr(demo, "_db", bomb)
    monkeypatch.setattr(auth, "verify_platform_token", bomb)
    monkeypatch.setattr(demo, "verify", bomb)
    scope = request({
        "x-demo-session": token, "x-tenant-id": "demo",
        "authorization": "Bearer account", "x-guest-session": "guest",
        "x-dispatch-secret": "dispatch", "content-length": "999999999",
        "origin": "https://example.test", "access-control-request-method": "POST",
    }, path=path, method=method).scope
    scope["query_string"] = b"state=demo-boundary-state&code=demo-code"
    messages = boundary_messages(application, scope)
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 403
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    assert json.loads(messages[1]["body"])["error"]["error_code"] == "FORBIDDEN"


@pytest.mark.parametrize("live", ["0", "1"])
def test_global_boundary_rejects_duplicate_mixed_case_headers(monkeypatch, globally_guarded_app, live):
    monkeypatch.setenv("LEAF_AUTH_LIVE", live)
    scope = request(path="/api/drawings/upload").scope
    scope["headers"] = [(b"X-Demo-Session", b""), (b"x-demo-session", b"invalid")]
    assert boundary_messages(globally_guarded_app, scope)[0]["status"] == 403


def test_global_boundary_preserves_requests_without_demo_headers():
    from app import DemoSessionBoundaryMiddleware
    seen = []

    async def downstream(scope, receive, send):
        seen.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = request({"authorization": "Bearer account"}).scope
    middleware = DemoSessionBoundaryMiddleware(downstream, router=FastAPI().router)
    assert boundary_messages(middleware, scope)[0]["status"] == 204
    assert seen == [scope]


def test_demo_route_allowlist_is_exact():
    expected = {
        "/api/run": ("POST",),
        "/api/jobs/{job_id}": ("GET",),
        "/api/jobs/{job_id}/stream": ("GET",),
        "/api/jobs": ("GET",),
        "/api/jobs/{job_id}/close": ("POST",),
        "/api/platform/capabilities": ("GET",),
        "/api/projects/{project_id}/drawing-versions/{input_version_id}/context": ("GET",),
        "/api/projects/{project_id}/drawings/{drawing_id}/checkout": ("GET", "POST", "DELETE"),
        "/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/solar-reads": ("POST",),
        "/api/projects/{project_id}/drawings/{drawing_id}/solar-artifacts/{artifact_id}": ("GET",),
        "/api/projects/{project_id}/conversations": ("POST", "GET"),
        "/api/projects/{project_id}/conversations/{conversation_id}": ("GET",),
        "/api/projects/{project_id}/conversations/recover": ("POST",),
        "/api/projects/{project_id}/conversations/{conversation_id}/messages": ("POST",),
        "/api/projects/{project_id}/conversations/recovery/tail": ("GET", "POST"),
        "/api/capabilities": ("GET",),
        "/api/receipts": ("GET",),
        "/api/sessions": ("POST", "GET"),
        "/api/sessions/{session_id}/messages": ("POST",),
        "/api/sessions/{session_id}/turns/{turn_id}/cancel": ("POST",),
        "/api/sessions/{session_id}/stream": ("GET",),
        "/api/sessions/{session_id}/transcript": ("GET",),
    }
    assert demo.DEMO_ROUTE_ALLOWLIST == frozenset(
        (method, path) for path, methods in expected.items() for method in methods)


def test_parameterized_grant_uses_same_template_at_both_boundaries(registry):
    from fastapi import Depends
    from app import DemoSessionBoundaryMiddleware

    application = FastAPI()

    @application.get("/api/jobs/{job_id}")
    def get_job(job_id: uuid.UUID, tenant=Depends(deps.require_tenant)):
        return {"job_id": str(job_id), "verified_demo": tenant.verified_demo}

    application.add_middleware(DemoSessionBoundaryMiddleware, router=application.router)
    token = demo.mint(request()).token
    job_id = str(uuid.uuid4())
    client = TestClient(application)
    response = client.get(f"/api/jobs/{job_id}", headers={"X-Demo-Session": token})
    assert response.status_code == 200, response.text
    assert response.json() == {"job_id": job_id, "verified_demo": True}
    for method, path in (("PUT", f"/api/jobs/{job_id}"), ("POST", "/api/campaigns")):
        assert client.request(method, path, headers={"X-Demo-Session": token}).status_code == 403
    scope = request({"x-demo-session": token}, path=f"/api/jobs/{job_id}", method="GET").scope
    scope["type"] = "websocket"
    assert boundary_messages(application, scope) == [{"type": "websocket.close", "code": 1008}]


def test_dependency_requires_boundary_and_dispatched_templates_to_agree():
    req = request(path="/api/jobs/00000000-0000-0000-0000-000000000001", method="GET")
    with pytest.raises(HTTPException) as caught:
        demo.require_demo_route(req)
    assert caught.value.status_code == 403
    req.scope["route"] = SimpleNamespace(path="/api/jobs/{job_id}")
    with pytest.raises(HTTPException) as caught:
        demo.require_demo_route(req)
    assert caught.value.status_code == 403
    req.scope["demo_route_template"] = "/api/jobs/{job_id}"
    demo.require_demo_route(req)
    req.scope["route"] = SimpleNamespace(path="/api/jobs/{shadow_id}")
    with pytest.raises(HTTPException) as caught:
        demo.require_demo_route(req)
    assert caught.value.status_code == 403


@pytest.mark.parametrize("method,template", sorted(demo.DEMO_ROUTE_ALLOWLIST))
def test_production_app_dispatch_confirms_each_demo_grant(monkeypatch, method, template):
    import app as composition
    from fastapi.responses import Response
    from route_flatten import iter_leaf_routes
    from starlette.routing import request_response

    seen = []

    async def guarded_handler(req):
        # Keep production routing and middleware, replacing only handler work
        # so this contract needs neither a database nor request-body fixtures.
        demo.require_demo_route(req)
        scope = req.scope
        seen.append((scope["demo_route_template"], scope["route"].path))
        return Response(status_code=204)

    routes = list(iter_leaf_routes(composition.app.routes))
    assert any(path == template and method in getattr(route, "methods", ())
               for path, route in routes)
    for _, route in routes:
        if hasattr(route, "get_route_handler"):
            monkeypatch.setattr(route, "get_route_handler", lambda: guarded_handler)
            # Older FastAPI versions cache this handler at construction time.
            monkeypatch.setattr(route, "app", request_response(guarded_handler))
    path = re.sub(r"\{[^}]+\}", lambda _: str(uuid.uuid4()), template)
    response = TestClient(composition.app).request(
        method, path, headers={"X-Demo-Session": "routing-contract"})
    assert response.status_code == 204, response.text
    assert seen == [(template, template)]


def test_shadowing_route_cannot_borrow_demo_grant(registry):
    from fastapi import Depends
    from app import DemoSessionBoundaryMiddleware

    application = FastAPI()

    @application.get("/api/jobs/{shadow_id}")
    def shadow(shadow_id: str, tenant=Depends(deps.require_tenant)):
        pytest.fail("ungranted shadow handler reached")

    @application.get("/api/jobs/{job_id}")
    def granted(job_id: str, tenant=Depends(deps.require_tenant)):
        pytest.fail("shadowed grant handler reached")

    application.add_middleware(DemoSessionBoundaryMiddleware, router=application.router)
    token = demo.mint(request()).token
    response = TestClient(application).get(
        f"/api/jobs/{uuid.uuid4()}", headers={"X-Demo-Session": token})
    assert response.status_code == 403, response.text


def test_boundary_refuses_ambiguous_or_multisegment_grants(monkeypatch):
    from app import DemoSessionBoundaryMiddleware

    scope = request(path=f"/api/jobs/{uuid.uuid4()}", method="GET").scope
    assert DemoSessionBoundaryMiddleware._route_template(scope) == "/api/jobs/{job_id}"
    monkeypatch.setattr(demo, "DEMO_ROUTE_PATTERNS", demo.DEMO_ROUTE_PATTERNS * 2)
    assert DemoSessionBoundaryMiddleware._route_template(scope) is None
    scope["path"] += "/extra"
    assert DemoSessionBoundaryMiddleware._route_template(scope) is None


@pytest.mark.parametrize("authorization", ["", "Basic malformed", "Bearer"])
@pytest.mark.parametrize("guest", [False, True])
def test_upload_present_invalid_authorization_is_never_a_guest(registry, monkeypatch, authorization, guest):
    from routers import uploads
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    monkeypatch.setattr(uploads.write_loop, "upload_mutation_refusal_guard", lambda: nullcontext(None))
    monkeypatch.setattr(uploads.guest_uploads, "enabled", bomb)
    monkeypatch.setattr(uploads.guest_uploads, "guest_secret", bomb)
    monkeypatch.setattr(uploads.guest_uploads, "verify_guest_session", bomb)
    monkeypatch.setattr(uploads.guest_uploads, "mint_guest_tenant_id", bomb)
    monkeypatch.setattr(uploads, "_emit_upload_event", bomb)
    app = FastAPI()
    app.include_router(uploads.router)
    headers = {"authorization": authorization}
    if guest:
        headers["x-guest-session"] = "guest-token"
    response = TestClient(app).post("/api/drawings/upload", headers=headers,
                                    files={"file": ("drawing.dxf", b"invalid drawing")})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/demand", "/api/telemetry", "/api/telemetry/plugin"])
@pytest.mark.parametrize("state", ["enabled", "disabled", "auth_off"])
@pytest.mark.parametrize("body_kind", ["malformed", "valid", "oversized"])
def test_public_write_boundaries_deny_demo_before_body_or_side_effects(
        registry, monkeypatch, path, state, body_kind):
    from routers import demand, telemetry
    # Keep payloads out of pytest IDs and Windows' PYTEST_CURRENT_TEST env var.
    body = {
        "malformed": b"{",
        "valid": b'{"events":[{"name":"site.demo_viewed"}]}',
        "oversized": b"x" * 40000,
    }[body_kind]
    token = demo.mint(request()).token
    if state == "disabled":
        monkeypatch.delenv("LEAF_DEMO_LIVE_ENABLED")
    elif state == "auth_off":
        monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    if state != "enabled":
        monkeypatch.setattr(demo, "_db", bomb)
    monkeypatch.setattr(demand, "_read_bounded_body", bomb)
    monkeypatch.setattr(demand, "_store_capture", bomb)
    monkeypatch.setattr(demand, "_db", bomb)
    monkeypatch.setattr(telemetry, "_bounded_body", bomb)
    monkeypatch.setattr(telemetry, "_bucket_allows", bomb)
    monkeypatch.setattr(telemetry.telemetry_sink, "emit", bomb)
    app = FastAPI()
    app.include_router(demand.router)
    app.include_router(telemetry.router)
    response = TestClient(app).post(path, content=body, headers={"x-demo-session": token})
    assert response.status_code == (403 if state == "enabled" else 401)


@pytest.mark.parametrize("path", ["/api/demand", "/api/telemetry", "/api/telemetry/plugin"])
@pytest.mark.parametrize("authorization", ["", "Basic malformed", "Bearer invalid"])
@pytest.mark.parametrize("guest", [False, True])
def test_public_write_boundaries_keep_bearer_precedence(
        registry, monkeypatch, path, authorization, guest):
    import auth
    from routers import demand, telemetry
    token = demo.mint(request()).token
    monkeypatch.setattr(demo, "_db", bomb)
    seen = []

    def invalid_bearer(value):
        seen.append(value)
        raise HTTPException(401, "invalid bearer")

    monkeypatch.setattr(auth, "verify_platform_token", invalid_bearer)
    monkeypatch.setattr(demand, "_read_bounded_body", bomb)
    monkeypatch.setattr(telemetry, "_bounded_body", bomb)
    app = FastAPI()
    app.include_router(demand.router)
    app.include_router(telemetry.router)
    headers = {"x-demo-session": token, "authorization": authorization}
    if guest:
        headers["x-guest-session"] = "guest-token"
    response = TestClient(app).post(path, content=b"{", headers=headers)
    assert response.status_code == 401
    assert seen == [authorization]


@pytest.mark.parametrize("path", ["/api/demand", "/api/telemetry", "/api/telemetry/plugin"])
def test_public_write_boundaries_reject_guest_plus_demo(registry, monkeypatch, path):
    from routers import demand, telemetry
    token = demo.mint(request()).token
    monkeypatch.setattr(demo, "_db", bomb)
    monkeypatch.setattr(demand, "_store_capture", bomb)
    monkeypatch.setattr(telemetry.telemetry_sink, "emit", bomb)
    app = FastAPI()
    app.include_router(demand.router)
    app.include_router(telemetry.router)
    response = TestClient(app).post(path, headers={
        "x-demo-session": token, "x-guest-session": "guest-token"})
    assert response.status_code == 400


@pytest.mark.parametrize("live", ["0", "1"])
@pytest.mark.parametrize("authorization", [None, "", "Basic malformed", "Bearer invalid", "Bearer valid"])
@pytest.mark.parametrize("guest", [None, "guest-token"])
def test_server_precedence(registry, monkeypatch, live, authorization, guest):
    import auth
    import tenancy
    token = demo.mint(request()).token
    monkeypatch.setenv("LEAF_AUTH_LIVE", live)
    if live != "1" or authorization is not None or guest is not None:
        monkeypatch.setattr(demo, "verify", bomb)
    seen = []

    def verify(value):
        seen.append(value)
        if value != "Bearer valid":
            raise HTTPException(401, "invalid bearer")
        return {"sub": "account"}

    monkeypatch.setattr(auth, "verify_platform_token", verify)
    monkeypatch.setattr(auth, "extract_tenant_claims", lambda payload: {})
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda subject: ("account-tenant", "hosted_pro"))
    monkeypatch.setattr(deps, "require_matching_platform_tenant_claim", lambda *args: None)
    monkeypatch.setattr(tenancy, "get_store", lambda: SimpleNamespace(resolve_workspace=lambda tenant: None))
    headers = {"x-demo-session": token}
    if authorization is not None:
        headers["authorization"] = authorization
    if guest is not None:
        headers["x-guest-session"] = guest
    if live == "1" and authorization == "Bearer valid":
        tenant = deps.require_tenant(request(headers), authorization=authorization, x_guest_session=guest,
                                     x_dispatch_secret=None, x_tenant_id=None)
        assert tenant == "account-tenant" and not tenant.verified_demo
    else:
        expected = (400 if guest else 403) if live == "1" and authorization is None else 401
        with pytest.raises(HTTPException) as error:
            deps.require_tenant(request(headers), authorization=authorization, x_guest_session=guest,
                                x_dispatch_secret=None, x_tenant_id=None)
        assert error.value.status_code == expected
    assert seen == ([authorization] if live == "1" and authorization is not None else [])


@pytest.mark.parametrize("live", ["0", "1"])
def test_dispatch_demo_cannot_borrow_backedge_or_stub(registry, monkeypatch, live):
    token = demo.mint(request()).token
    monkeypatch.setenv("LEAF_AUTH_LIVE", live)
    monkeypatch.setattr(deps, "_dispatch_backedge_route", lambda *args: True)
    monkeypatch.setattr(deps, "_dispatch_secret_ok", lambda secret: True)
    monkeypatch.setattr(deps, "backedge_tenant", bomb)
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(HTTPException) as error:
        deps.require_tenant(request({"x-demo-session": token}), x_tenant_id="victim",
                            authorization=None, x_dispatch_secret="dispatch", x_guest_session=None)
    assert error.value.status_code == 401


def test_migration_and_authority_inventory_agree():
    root = Path(__file__).resolve().parents[2]
    inventory = json.loads((root / "platform/authority-inventory.json").read_text())
    entry = next(item for item in inventory["authorities"] if item["id"] == "demo_sessions")
    assert set(entry["postgres_tables"]) == {
        "demo_sessions", "demo_mint_counters", "identity_bindings", "orgs",
        "projects", "project_member_bindings",
    }
    assert "0076" in inventory["scope"]["migration_ids"]
    sql = (root / "platform/migrations/0076_demo_sessions.sql").read_text()
    for table in ("demo_sessions", "demo_mint_counters"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    migrations = list((root / "platform/migrations").glob("*.sql"))
    for table in ("identity_bindings", "orgs", "projects", "project_member_bindings"):
        pattern = rf"(?i:CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?){re.escape(table)}\s*\("
        assert any(re.search(pattern, migration.read_text()) for migration in migrations)
