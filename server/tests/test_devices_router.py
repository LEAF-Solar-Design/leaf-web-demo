"""Device routes with TestClient and the real SQLite queue/credential store."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import device_catalog
import device_store as store
import platform_link
from device_auth import DeviceError
from envelopes import install_error_handlers
from routers import devices
from routers.tenant import _require_grant_owner

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "device-agent"))
from leaf_agent import sign

SEED = b"\x01" * 32
PLATFORM = {"os": "darwin", "os_version": "15", "arch": "arm64"}
PROJECT = str(uuid4())
TENANT = str(uuid4())
_REAL_PROJECT_OWNER = devices._project_owner


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_DEVICE_STORE", "sqlite")
    monkeypatch.setenv("LEAF_DEVICE_DB", str(tmp_path / "devices.db"))
    monkeypatch.delenv("LEAF_DEVICE_BRIDGE_ENABLED", raising=False)
    monkeypatch.delenv("LEAF_DEVICE_PUBLIC_ORIGINS", raising=False)
    state = SimpleNamespace(tenant=deps.TenantContext(TENANT, subject="auth0|owner", tier="pro"),
                            project_owner=True, projects={PROJECT: TENANT})

    def access(tenant, project, **kwargs):
        if state.projects.get(project) != str(tenant):
            raise LookupError("inaccessible project")
        return str(tenant)

    def owner(tenant, project):
        if not state.project_owner:
            raise DeviceError(403, "forbidden")

    monkeypatch.setattr(platform_link, "require_project_access", access)
    monkeypatch.setattr(devices, "_project_owner", owner)
    app = FastAPI()
    app.include_router(devices.router)
    install_error_handlers(app)
    app.dependency_overrides[deps.require_tenant] = lambda: state.tenant
    app.dependency_overrides[deps.require_active_tenant] = lambda: state.tenant
    app.dependency_overrides[devices._require_link_service] = lambda: state.tenant
    state.app = app
    with TestClient(app, base_url="https://platform.leafdesign.ai") as client:
        state.client = client
        yield state


def enroll(api, **overrides):
    return api.client.post("/api/devices/enrollments", json={"project_id": PROJECT, "label": "Mini", **overrides})


def register(api, token, seed=SEED, **overrides):
    return api.client.post("/api/devices/agent/register", json={"token": token, "public_key": sign.public_key(seed),
        "agent_version": "1", "platform": PLATFORM, "hostname": "mini.local", **overrides})


def device(api, seed=SEED):
    response = enroll(api)
    assert response.status_code == 201, response.text
    response = register(api, response.json()["token"], seed)
    assert response.status_code == 201, response.text
    return response.json()["device_id"]


def signed(api, device_id, path, body=None, seed=SEED, method=None):
    method = method or ("GET" if body is None else "POST")
    raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = sign.headers(seed, device_id, method, path, raw)
    headers["content-type"] = "application/json"
    return api.client.request(method, path, content=raw, headers=headers)


def enqueue(api, device_id, **overrides):
    return api.client.post(f"/api/devices/{device_id}/commands", json={"op": "pkg.health",
        "args": {"package": "homeassistant"}, **overrides})


def result(command_id, **overrides):
    return {"command_id": command_id, "status": "ok", "finished_at": store._now().isoformat(), **overrides}


def test_full_lifecycle_and_audit_projections(api):
    enrolled = enroll(api)
    token = enrolled.json()["token"]
    assert enrolled.headers["cache-control"] == "no-store"
    assert enrolled.json()["install_command"] == (
        f"curl -fsSL https://platform.leafdesign.ai/api/devices/agent/install.sh | LEAF_ENROLL_TOKEN={token} sh")
    registration = register(api, token)
    device_id = registration.json()["device_id"]
    empty = signed(api, device_id, "/api/devices/agent/poll?wait=0")
    assert empty.status_code == 204 and empty.content == b""
    command_id = enqueue(api, device_id).json()["command_id"]
    pending = api.client.get(f"/api/devices/{device_id}/commands/{command_id}").json()
    assert pending["result"] is None
    polled = signed(api, device_id, "/api/devices/agent/poll?wait=0")
    assert polled.status_code == 200
    assert polled.json()["commands"][0]["command_id"] == command_id
    assert set(polled.json()["commands"][0]) == {"command_id", "op", "args", "issued_at", "expires_at"}
    completed = result(command_id, data={"healthy": True})
    response = signed(api, device_id, "/api/devices/agent/results", {"results": [completed]})
    assert response.json() == {"accepted": 1}
    assert signed(api, device_id, "/api/devices/agent/results", {"results": [completed]}).json() == {"accepted": 1}
    assert api.client.get(f"/api/devices/{device_id}/commands/{command_id}").json()["result"] == completed
    heartbeat = signed(api, device_id, "/api/devices/agent/heartbeat", {"agent_version": "2", "packages": [],
        "free_mem_mb": 1024, "uptime_s": 10})
    assert heartbeat.status_code == 200 and "server_time" in heartbeat.json()
    views = api.client.get("/api/devices", params={"project_id": PROJECT}).json()["devices"]
    assert len(views) == 1 and views[0]["agent_version"] == "2"
    assert device_catalog.VALIDATORS["DeviceView"].is_valid(views[0])
    audit = api.client.get(f"/api/devices/{device_id}/audit").json()["events"]
    assert any(event["op"] == "pkg.health" and event["actor"] == "user:auth0|owner" for event in audit)
    assert all(device_catalog.VALIDATORS["AuditEvent"].is_valid(event) for event in audit)
    assert token not in json.dumps(views + audit)
    assert sign.public_key(SEED) not in json.dumps(views + audit)


def test_reused_expired_and_unknown_tokens_are_unauthorized(api, monkeypatch):
    token = enroll(api).json()["token"]
    assert register(api, token).status_code == 201
    assert register(api, token).json()["error"]["code"] == "enrollment_invalid"
    token = enroll(api).json()["token"]
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=601))
    response = register(api, token, b"\x02" * 32)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "enrollment_invalid"
    assert register(api, "A" * 43).status_code == 401


def test_duplicate_public_key_does_not_consume_second_token(api):
    device(api)
    token = enroll(api).json()["token"]
    duplicate = register(api, token)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "device_key_in_use"
    assert register(api, token, b"\x02" * 32).status_code == 201


def test_revoke_is_idempotent_audited_and_stops_poll_and_enqueue(api):
    device_id = device(api)
    enqueue(api, device_id)
    first = api.client.post(f"/api/devices/{device_id}/revoke")
    assert first.status_code == 200
    assert api.client.post(f"/api/devices/{device_id}/revoke").json() == first.json()
    polled = signed(api, device_id, "/api/devices/agent/poll?wait=0")
    assert polled.status_code == 401 and polled.json()["error"]["code"] == "device_revoked"
    assert enqueue(api, device_id).status_code == 409
    audit = api.client.get(f"/api/devices/{device_id}/audit").json()["events"]
    assert len([row for row in audit if row["op"] == "device.revoke"]) == 2
    assert api.client.get("/api/devices", params={"project_id": PROJECT}).json()["devices"][0]["status"] == "revoked"


@pytest.mark.parametrize("operation", ["list", "command", "enqueue", "revoke", "audit"])
def test_cross_tenant_access_is_not_found(api, operation):
    device_id = device(api)
    command_id = enqueue(api, device_id).json()["command_id"]
    api.tenant = deps.TenantContext(str(uuid4()), subject="auth0|other", tier="pro")
    routes = {"list": ("GET", f"/api/devices?project_id={PROJECT}"),
              "command": ("GET", f"/api/devices/{device_id}/commands/{command_id}"),
              "enqueue": ("POST", f"/api/devices/{device_id}/commands"),
              "revoke": ("POST", f"/api/devices/{device_id}/revoke"),
              "audit": ("GET", f"/api/devices/{device_id}/audit")}
    method, path = routes[operation]
    response = api.client.request(method, path, json={"op": "agent.pause", "args": {}} if operation == "enqueue" else None)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_same_tenant_inaccessible_project_is_not_found(api):
    device_id = device(api)
    api.projects.clear()
    assert api.client.get(f"/api/devices/{device_id}/audit").status_code == 404
    assert api.client.get("/api/devices", params={"project_id": PROJECT}).status_code == 404
    assert enqueue(api, device_id).status_code == 404
    assert api.client.post(f"/api/devices/{device_id}/revoke").status_code == 404
    assert enroll(api).status_code == 404


def test_project_nonowner_can_read_but_cannot_manage(api):
    device_id = device(api)
    api.project_owner = False
    assert api.client.get("/api/devices", params={"project_id": PROJECT}).status_code == 200
    assert api.client.get(f"/api/devices/{device_id}/audit").status_code == 200
    assert enqueue(api, device_id).status_code == 403
    assert enroll(api).status_code == 403
    assert api.client.post(f"/api/devices/{device_id}/revoke").status_code == 403


def test_real_tenant_owner_and_capability_gate_is_reused(api, monkeypatch):
    api.app.dependency_overrides.pop(devices._require_link_service)
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    binding = SimpleNamespace(platform_tenant_id=TENANT, binding_id=uuid4())
    authority = SimpleNamespace(resolve_active_identity_binding=lambda *args: binding,
                                active_identity_role=lambda *args: "member")
    monkeypatch.setattr(platform_link, "platform_store", lambda: authority)
    assert enroll(api).status_code == 403
    # Keep the actual link_service entitlement resolution, overriding only its owner input.
    api.app.dependency_overrides[_require_grant_owner] = lambda: deps.TenantContext(TENANT, subject="auth0|owner", tier="free")
    assert enroll(api).status_code == 403


def test_real_project_owner_check_rejects_editor(api, monkeypatch):
    platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle
    binding = SimpleNamespace(binding_id=uuid4())
    monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda tenant: binding)
    monkeypatch.setattr(project_lifecycle, "require_project_role", lambda *args, **kwargs: "editor")
    monkeypatch.setattr(devices, "_project_owner", _REAL_PROJECT_OWNER)
    assert enroll(api).status_code == 403


@pytest.mark.parametrize("body", [
    {"op": "shell", "args": {}}, {"op": "agent.pause", "args": {"shell": "secret-value"}},
    {"op": "pkg.health", "args": {}}, {"op": "pkg.uninstall", "args": {"package": "homeassistant", "keep_data": "false"}},
    {"op": "agent.pause", "args": {}, "extra": True}, {"op": "ha.flow.start", "args": {"handler": "evil"}},
])
def test_invalid_command_never_enqueued_and_is_audited(api, body):
    device_id = device(api)
    response = api.client.post(f"/api/devices/{device_id}/commands", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "command_invalid"
    assert store.lease_commands(device_id) == []
    audit = store.list_audit(TENANT, device_id)
    assert audit[0]["status"] == "refused" and len(audit[0]["request_sha256"]) == 64
    assert "secret-value" not in json.dumps(audit)


@pytest.mark.parametrize("op,args,code", [
    ("pkg.health", {"package": "unknown"}, "package_unknown"),
    ("pkg.install", {"package": "homeassistant", "version": "unknown"}, "version_unknown"),
    ("http.proxy", {"package": "homeassistant", "method": "GET", "path": "/api/statesX"}, "path_denied"),
    ("http.proxy", {"package": "homeassistant", "method": "POST", "path": "/api/states"}, "path_denied"),
])
def test_catalog_enforcement(api, op, args, code):
    device_id = device(api)
    response = enqueue(api, device_id, op=op, args=args)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert store.lease_commands(device_id) == []


def test_poll_is_device_bound_and_concurrent_polls_do_not_duplicate(api):
    first = device(api)
    second = device(api, b"\x02" * 32)
    command_id = enqueue(api, first).json()["command_id"]
    assert signed(api, second, "/api/devices/agent/poll?wait=0", seed=b"\x02" * 32).status_code == 204
    barrier = threading.Barrier(2)
    def poll(_):
        barrier.wait(timeout=10)
        return signed(api, first, "/api/devices/agent/poll?wait=0")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(poll, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 204]
    delivered = [row["command_id"] for response in responses if response.status_code == 200
                 for row in response.json()["commands"]]
    assert delivered == [command_id]


def test_poll_holds_asynchronously_until_enqueue(api, monkeypatch):
    device_id = device(api)
    first_lease = threading.Event()
    original = store.lease_commands
    def lease(*args, **kwargs):
        rows = original(*args, **kwargs)
        first_lease.set()
        return rows
    monkeypatch.setattr(store, "lease_commands", lease)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(signed, api, device_id, "/api/devices/agent/poll?wait=2")
        assert first_lease.wait(timeout=5)
        command_id = enqueue(api, device_id).json()["command_id"]
        response = pending.result(timeout=5)
    assert response.status_code == 200
    assert response.json()["commands"][0]["command_id"] == command_id


def test_schema_invalid_result_batch_has_no_partial_completion(api):
    device_id = device(api)
    command_id = enqueue(api, device_id).json()["command_id"]
    signed(api, device_id, "/api/devices/agent/poll?wait=0")
    response = signed(api, device_id, "/api/devices/agent/results", {"results": [result(command_id),
        result(str(uuid4()), finished_at="not-a-time")]})
    assert response.status_code == 400
    assert store.get_command(TENANT, device_id, command_id)["result"] is None


def test_results_count_only_independently_accepted_commands(api):
    first = device(api)
    second = device(api, b"\x02" * 32)
    own = enqueue(api, first).json()["command_id"]
    foreign = enqueue(api, second).json()["command_id"]
    store.lease_commands(second)
    signed(api, first, "/api/devices/agent/poll?wait=0")
    response = signed(api, first, "/api/devices/agent/results", {"results": [result(own), result(foreign), result(str(uuid4()))]})
    assert response.status_code == 200 and response.json() == {"accepted": 1}
    assert store.get_command(TENANT, second, foreign)["result"] is None
    conflict = signed(api, first, "/api/devices/agent/results", {"results": [result(own, data={"different": True})]})
    assert conflict.json() == {"accepted": 0}


def test_production_flag_is_checked_before_any_auth_or_store(api, monkeypatch):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    paths = [("GET", "/api/devices/packages"), ("POST", "/api/devices/enrollments"),
             ("GET", "/api/devices/agent/package.tar.gz"), ("GET", "/api/devices/agent/install.sh"),
             ("GET", "/api/devices/agent/poll"), ("POST", "/api/devices/agent/register"),
             ("POST", "/api/devices/agent/results"), ("POST", "/api/devices/agent/heartbeat"),
             ("GET", "/api/devices"), ("GET", "/api/devices/id/audit"),
             ("POST", "/api/devices/id/commands"), ("GET", "/api/devices/id/commands/id"),
             ("POST", "/api/devices/id/revoke")]
    for method, path in paths:
        assert api.client.request(method, path).status_code == 404
    monkeypatch.setenv("LEAF_DEVICE_BRIDGE_ENABLED", "1")
    assert api.client.get("/api/devices/packages").status_code == 200


def test_package_archive_is_cached_deterministic_and_matches_installer(api, monkeypatch):
    monkeypatch.setattr(devices, "_PACKAGE", None)
    first = api.client.get("/api/devices/agent/package.tar.gz")
    assert first.status_code == 200
    digest = hashlib.sha256(first.content).hexdigest()
    assert digest == first.headers["x-leaf-document-sha256"]
    assert "immutable" in first.headers["cache-control"]
    cached = devices._PACKAGE
    assert api.client.get("/api/devices/agent/package.tar.gz").content == first.content
    assert devices._PACKAGE is cached
    installer = api.client.get("/api/devices/agent/install.sh")
    assert installer.status_code == 200
    assert digest in installer.text
    assert "export LEAF_SERVER_ORIGIN='https://platform.leafdesign.ai'" in installer.text
    assert "@SHA256@" not in installer.text and "@VERSION@" not in installer.text
    monkeypatch.setattr(devices, "_PACKAGE", None)
    assert api.client.get("/api/devices/agent/package.tar.gz").content == first.content


@pytest.mark.parametrize("host,origin", [
    ("platform.leafdesign.ai", "https://platform.leafdesign.ai"),
    ("platform-staging.leafdesign.ai", "https://platform-staging.leafdesign.ai"),
    ("studio.leafautomation.ai", "https://platform.leafdesign.ai"),
])
@pytest.mark.parametrize("header", ["host", "x-forwarded-host"])
def test_public_hosts_use_canonical_platform_origin(api, host, origin, header):
    headers = {header: host}
    response = api.client.post("/api/devices/enrollments", headers=headers,
                               json={"project_id": PROJECT, "label": "Mini"})
    assert response.status_code == 201
    token = response.json()["token"]
    assert response.json()["install_command"] == (
        f"curl -fsSL {origin}/api/devices/agent/install.sh | LEAF_ENROLL_TOKEN={token} sh")
    installer = api.client.get("/api/devices/agent/install.sh", headers=headers)
    assert installer.status_code == 200
    assert f"export LEAF_SERVER_ORIGIN='{origin}'" in installer.text


@pytest.mark.parametrize("host", ["evil.example", "platform.leafdesign.ai;evil",
    "platform.leafdesign.ai,evil.example", "leafautomation.ai", "platform.leafautomation.ai"])
def test_unknown_public_origin_is_rejected(api, host):
    headers = {"x-forwarded-host": host}
    response = api.client.post("/api/devices/enrollments", headers=headers,
                               json={"project_id": PROJECT, "label": "Mini"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    installer = api.client.get("/api/devices/agent/install.sh", headers=headers)
    assert installer.status_code == 400 and installer.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("header,value", [("host", "platform.leafdesign.ai"),
    ("x-forwarded-host", "platform.leafdesign.ai"), ("x-forwarded-proto", "https")])
def test_duplicate_origin_headers_are_rejected(api, header, value):
    headers = [(header, value), (header, value)]
    response = api.client.post("/api/devices/enrollments", headers=headers,
                               json={"project_id": PROJECT, "label": "Mini"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    installer = api.client.get("/api/devices/agent/install.sh", headers=headers)
    assert installer.status_code == 400 and installer.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("proto", ["http", "https,http"])
def test_public_origin_requires_https(api, proto):
    headers = {"x-forwarded-proto": proto}
    response = api.client.post("/api/devices/enrollments", headers=headers,
                               json={"project_id": PROJECT, "label": "Mini"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    assert api.client.get("/api/devices/agent/install.sh", headers=headers).status_code == 400


def test_test_origin_extensions_are_additive(api, monkeypatch):
    monkeypatch.setenv("LEAF_DEVICE_PUBLIC_ORIGINS",
                       "https://localhost:8443, https://device-test.example, https://studio.leafautomation.ai")
    for host, origin in [("localhost:8443", "https://localhost:8443"),
                         ("device-test.example", "https://device-test.example"),
                         ("platform.leafdesign.ai", "https://platform.leafdesign.ai"),
                         ("studio.leafautomation.ai", "https://platform.leafdesign.ai")]:
        headers = {"x-forwarded-host": host}
        response = api.client.post("/api/devices/enrollments", headers=headers,
                                   json={"project_id": PROJECT, "label": "Mini"})
        assert response.status_code == 201
        assert response.json()["install_command"].startswith(f"curl -fsSL {origin}/")
        installer = api.client.get("/api/devices/agent/install.sh", headers=headers)
        assert installer.status_code == 200
        assert f"export LEAF_SERVER_ORIGIN='{origin}'" in installer.text


def test_production_ignores_origin_extensions(api, monkeypatch):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    monkeypatch.setenv("LEAF_DEVICE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("LEAF_DEVICE_PUBLIC_ORIGINS", "https://device-test.example, https://studio.leafautomation.ai")
    headers = {"x-forwarded-host": "device-test.example"}
    assert api.client.get("/api/devices/agent/install.sh", headers=headers).status_code == 400
    installer = api.client.get("/api/devices/agent/install.sh", headers={"x-forwarded-host": "studio.leafautomation.ai"})
    assert installer.status_code == 200
    assert "export LEAF_SERVER_ORIGIN='https://platform.leafdesign.ai'" in installer.text


@pytest.mark.parametrize("origin", ["http://device-test.example", "https://user@device-test.example",
    "https://device-test.example/path", "https://device-test.example/';echo injected"])
def test_origin_extensions_reject_non_https_and_non_origin_values(api, monkeypatch, origin):
    monkeypatch.setenv("LEAF_DEVICE_PUBLIC_ORIGINS", origin)
    response = api.client.get("/api/devices/agent/install.sh", headers={"x-forwarded-host": "device-test.example"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


def test_package_catalog_is_available_to_members_and_validated(api, tmp_path):
    api.project_owner = False
    api.app.dependency_overrides.pop(devices._require_link_service)
    response = api.client.get("/api/devices/packages")
    assert response.status_code == 200
    entry = response.json()["packages"][0]
    assert entry["package"] == "homeassistant" and entry["version"] == "2026.10.4"
    assert entry["port"] == 8123 and entry["health_path"] == "/api/"
    assert device_catalog.VALIDATORS["PackageCatalogEntry"].is_valid(entry)
    invalid = tmp_path / "bad-catalog.json"
    invalid.write_text(json.dumps([{**entry, "port": 0}]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid device catalog"):
        device_catalog.load_catalog(invalid)


def test_poll_refreshes_last_seen_even_when_empty(api, monkeypatch):
    device_id = device(api)
    before = store.get_device(device_id)["last_seen_at"]
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=1))
    assert signed(api, device_id, "/api/devices/agent/poll?wait=0").status_code == 204
    assert store.get_device(device_id)["last_seen_at"] > before


@pytest.mark.parametrize("query", ["wait=-1", "wait=26", "wait=0.1", "wait=0&wait=1", "device_id=foreign"])
def test_invalid_poll_queries_are_safe_errors(api, query):
    response = signed(api, device(api), "/api/devices/agent/poll?" + query)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_duplicate_heartbeat_packages_and_unknown_registration_fields_rejected(api):
    device_id = device(api)
    package = {"package": "homeassistant", "version": "1", "status": "running", "last_health_at": None}
    response = signed(api, device_id, "/api/devices/agent/heartbeat", {"agent_version": "1", "packages": [package, package],
        "free_mem_mb": 0, "uptime_s": 0})
    assert response.status_code == 422
    assert register(api, enroll(api).json()["token"], tenant_id="foreign").status_code == 422


def test_store_unavailability_is_safe_and_does_not_echo_exception(api, monkeypatch):
    def unavailable(*args, **kwargs):
        raise store.StoreUnavailable("fixture-secret-token")
    monkeypatch.setattr(store, "create_enrollment", unavailable)
    response = enroll(api)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "fixture-secret-token" not in response.text
