"""Relay boundary checks using the real credential store and command queue."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import device_store as store
import platform_link
from device_auth import DeviceError
from routers import device_relay as relay, devices

ORIGIN = "https://platform-staging.leafdesign.ai"
PREFIX = "/api/device-relay/homeassistant"
PACKAGE = {"package": "homeassistant", "version": "2026.10.4", "status": "running", "last_health_at": None}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_DEVICE_STORE", "sqlite")
    monkeypatch.setenv("LEAF_DEVICE_DB", str(tmp_path / "devices.db"))
    monkeypatch.delenv("LEAF_DEVICE_BRIDGE_ENABLED", raising=False)
    state = SimpleNamespace(tenant=deps.TenantContext(str(uuid4()), subject="auth0|owner", tier="pro"),
                            project=str(uuid4()), owner=True, capability=True)
    state.projects = {state.project: str(state.tenant)}

    def access(tenant, project, **kwargs):
        if state.projects.get(project) != str(tenant):
            raise LookupError("inaccessible project")
        return str(tenant)

    def owner(tenant, project):
        if not state.owner:
            raise DeviceError(403, "forbidden")

    def link_service():
        if not state.capability:
            raise DeviceError(403, "forbidden")
        return state.tenant

    monkeypatch.setattr(platform_link, "require_project_access", access)
    monkeypatch.setattr(devices, "_project_owner", owner)
    relay._BUCKETS.clear()
    app = FastAPI()
    app.include_router(devices.router)
    app.include_router(relay.router)
    state.app = app
    app.dependency_overrides[deps.require_tenant] = lambda: state.tenant
    app.dependency_overrides[devices._require_link_service] = link_service
    with TestClient(app, base_url=ORIGIN) as client:
        state.client = client
        state.device = add_device(state)
        yield state


def add_device(api, seed=1):
    _, secret = store.create_enrollment(str(api.tenant), api.project, "Mini", "user:test")
    key = base64.urlsafe_b64encode(bytes([seed]) * 32).rstrip(b"=").decode()
    row = store.consume_enrollment(secret, key, "1", {"os": "darwin", "os_version": "15", "arch": "arm64"}, "mini")
    store.touch_device(row["device_id"], "1", [PACKAGE], 1024)
    return row["device_id"]


def mint(api, device_id=None, **overrides):
    return api.client.post(f"/api/devices/{device_id or api.device}/relay-tokens",
                           json={"package": "homeassistant", "label": "Ops", **overrides})


def credential(api, device_id=None):
    response = mint(api, device_id)
    assert response.status_code == 201, response.text
    return response.json()


def call(api, token, path="/api/states", method="GET", **kwargs):
    headers = {"authorization": "Bearer " + token, **kwargs.pop("headers", {})}
    return api.client.request(method, PREFIX + path, headers=headers, **kwargs)


def command_rows(api, device_id=None):
    return store._run("id:" + api.device, lambda sql: [store._row(row) for row in sql.execute(
        "SELECT * FROM device_commands WHERE tenant_id=? AND device_id=? ORDER BY issued_at,command_id",
        (str(api.tenant), device_id or api.device)).fetchall()])


def proxy(api, token, data=None, error=None, device_id=None, **kwargs):
    device_id = device_id or api.device

    def agent():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = store.lease_commands(device_id)
            if rows:
                row = rows[0]
                result = {"command_id": row["command_id"], "status": "error" if error else "ok",
                          "finished_at": store._now().isoformat()}
                result.update({"error": {"code": error, "message": "safe error"}} if error else {"data": data})
                assert store.complete_command(device_id, row["command_id"], result)
                return row
            time.sleep(0.01)
        raise AssertionError("relay did not enqueue work")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(agent)
        response = call(api, token, **kwargs)
        row = future.result(timeout=4)
    return response, row


def code(response, status, error):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == error


def test_mint_list_revoke_secret_only_once_and_audit(api):
    minted = credential(api)
    assert set(minted) == {"token_id", "relay_token", "relay_origin", "path_prefix"}
    assert minted["relay_origin"] == ORIGIN and minted["path_prefix"] == PREFIX
    assert minted["relay_token"].startswith(minted["token_id"] + ".")
    assert store.resolve_relay_token(minted["relay_token"])["device_id"] == api.device
    path = f"/api/devices/{api.device}/relay-tokens"
    listed = api.client.get(path)
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    row = listed.json()["tokens"][0]
    assert set(row) == set(relay._TOKEN_FIELDS)
    assert row["token_id"] == minted["token_id"] and row["last_used_at"] is not None
    assert "secret" not in listed.text and "sha256" not in listed.text
    for _ in range(2):
        revoked = api.client.delete(path + "/" + minted["token_id"])
        assert revoked.status_code == 204 and revoked.content == b""
    code(call(api, minted["relay_token"]), 401, "relay_token_invalid")
    assert api.client.get(path).json()["tokens"][0]["revoked_at"] is not None
    events = store.list_audit(str(api.tenant), api.device)
    assert {"relay.create", "relay.list", "relay.revoke"} <= {row["op"] for row in events}
    assert minted["relay_token"] not in json.dumps(events)


@pytest.mark.parametrize("body,status,error", [
    ({"package": "missing", "label": "Ops"}, 422, "package_unknown"),
    ({"package": "homeassistant", "label": ""}, 422, "invalid_payload"),
    ({"package": "homeassistant", "label": "x" * 129}, 422, "invalid_payload"),
    ({"package": 5, "label": "Ops"}, 422, "invalid_payload"),
    ({"package": "homeassistant", "label": "Ops", "device_id": "other"}, 422, "invalid_payload"),
])
def test_invalid_mint_does_not_issue_credentials(api, body, status, error):
    response = api.client.post(f"/api/devices/{api.device}/relay-tokens", json=body)
    code(response, status, error)
    assert relay._tokens(str(api.tenant), api.device) == []


def test_mint_requires_package_on_device_and_rejects_revoked_device(api):
    store.touch_device(api.device, "1", [], 1024)
    code(mint(api), 422, "package_not_installed")
    store.revoke_device(str(api.tenant), api.device, "user:test")
    code(mint(api), 409, "device_revoked")


@pytest.mark.parametrize("gate", ["owner", "capability"])
def test_mutations_require_owner_and_link_service(api, gate):
    minted = credential(api)
    setattr(api, gate, False)
    code(mint(api), 403, "forbidden")
    code(api.client.delete(f"/api/devices/{api.device}/relay-tokens/{minted['token_id']}"), 403, "forbidden")
    assert store.resolve_relay_token(minted["relay_token"])


def test_token_cap_returns_429(api):
    for _ in range(store.MAX_RELAY_TOKENS):
        credential(api)
    code(mint(api), 429, "rate_limited")


def test_cross_tenant_and_project_isolation(api):
    minted = credential(api)
    original = api.tenant
    api.tenant = deps.TenantContext(str(uuid4()), subject="auth0|foreign", tier="pro")
    path = f"/api/devices/{api.device}/relay-tokens"
    for response in (mint(api), api.client.get(path), api.client.delete(path + "/" + minted["token_id"])):
        code(response, 404, "not_found")
    api.tenant = original
    api.projects.clear()
    code(api.client.get(path), 404, "not_found")
    assert store.resolve_relay_token(minted["relay_token"])


def test_token_cannot_be_revoked_through_another_device(api):
    minted = credential(api)
    other = add_device(api, seed=2)
    code(api.client.delete(f"/api/devices/{other}/relay-tokens/{minted['token_id']}"), 404, "not_found")
    assert store.resolve_relay_token(minted["relay_token"])


def test_relay_happy_path_headers_and_exact_body_hash(api):
    minted = credential(api)
    raw = b'{"entity_id":"light.office","brightness_pct":45}'
    response, row = proxy(api, minted["relay_token"], data={"status": 200, "body": [{"entity_id": "light.office"}],
        "headers": {"set-cookie": "secret", "x-upstream": "secret"}}, method="POST",
        path="/api/services/light/turn_on?room=office", content=raw, headers={"Content-Type": "application/json",
        "Idempotency-Key": "do-not-forward", "cookie": "do-not-forward", "x-caller": "do-not-forward"})
    assert response.status_code == 200 and response.json() == [{"entity_id": "light.office"}]
    assert response.headers["content-type"] == "application/json"
    assert "set-cookie" not in response.headers and "x-upstream" not in response.headers
    assert row["op"] == "http.proxy" and row["issued_by"] == "relay:" + minted["token_id"]
    assert row["args"] == {"package": "homeassistant", "method": "POST", "path": "/api/services/light/turn_on",
                           "query": {"room": "office"}, "body_json": json.loads(raw)}
    assert (store._date(row["expires_at"]) - store._date(row["issued_at"])).total_seconds() == 15
    events = store.list_audit(str(api.tenant), api.device)
    event = next(item for item in events if item["op"] == "http.proxy")
    assert event["actor"] == "relay:" + minted["token_id"] and event["status"] == "ok"
    assert event["target"] == "POST /api/services/light/turn_on"
    assert event["request_sha256"] == hashlib.sha256(raw).hexdigest()
    assert all("body" not in key for item in events for key in item)
    assert "light.office" not in json.dumps(events) and "do-not-forward" not in json.dumps(events)


def test_agent_body_json_compatibility_and_upstream_http_error(api):
    response, _ = proxy(api, credential(api)["relay_token"], data={"status": 422, "body_json": {"detail": "invalid"},
                                                                "content_type": "application/json"})
    assert response.status_code == 422 and response.json() == {"detail": "invalid"}


@pytest.mark.parametrize("status", [204, 304])
def test_bodyless_upstream_status(api, status):
    response, _ = proxy(api, credential(api)["relay_token"], data={"status": status})
    assert response.status_code == status and response.content == b""


def test_device_error_is_a_safe_502(api):
    response, _ = proxy(api, credential(api)["relay_token"], error="paused")
    assert response.status_code == 502 and response.json() == {"error": "paused"}


@pytest.mark.parametrize("data", [{}, {"status": True, "body": []}, {"status": 600, "body": []},
                                  {"status": 200}, {"status": 200, "body": "x" * 1_048_576}])
def test_malformed_or_oversize_proxy_result_is_502(api, data):
    response, _ = proxy(api, credential(api)["relay_token"], data=data)
    code(response, 502, "upstream_error")


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/config"), ("POST", "/api/states"), ("HEAD", "/api/states"),
    ("GET", "/api/statesX"), ("GET", "/api/states//x"),
    ("GET", "/api/states/%2e%2e/config"), ("GET", "/api/states/%2Fconfig"),
    ("GET", "/api/states/%252fconfig"), ("GET", "/api/states/%5cconfig"),
    ("GET", "/api/states/..%2fconfig"), ("GET", "/api/states?" + "k" * 129 + "=x"),
])
def test_denied_paths_never_enqueue(api, method, path):
    minted = credential(api)
    response = call(api, minted["relay_token"], method=method, path=path)
    if method == "HEAD":
        assert response.status_code == 403
    else:
        code(response, 403, "relay_path_denied")
    assert command_rows(api) == []
    event = store.list_audit(str(api.tenant), api.device)[0]
    assert event["op"] == "http.proxy" and event["status"] == "refused"


def test_literal_traversal_rejected_before_client_normalization(api):
    # HTTP clients remove literal dot segments. Preserve the original ASGI
    # target to exercise the relay handler rather than client normalization.
    token = credential(api)["relay_token"]

    async def unnormalized(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, path=PREFIX + "/api/states/../config",
                         raw_path=(PREFIX + "/api/states/../config").encode())
        await api.app(scope, receive, send)

    with TestClient(unnormalized, base_url=ORIGIN) as client:
        response = client.get(PREFIX + "/api/states", headers={"authorization": "Bearer " + token})
    code(response, 403, "relay_path_denied")
    assert command_rows(api) == []


def test_duplicate_query_keys_and_empty_keys_are_invalid(api):
    token = credential(api)["relay_token"]
    for path in ("/api/states?a=1&a=2", "/api/states?=value"):
        code(call(api, token, path=path), 400, "invalid_request")
    assert command_rows(api) == []


def test_package_mismatch_is_forbidden(api):
    token = credential(api)["relay_token"]
    code(api.client.get("/api/device-relay/another/api/states", headers={"authorization": "Bearer " + token}),
         403, "relay_token_mismatch")
    assert command_rows(api) == []


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic invalid", "Bearer", "Bearer a b"])
def test_missing_or_invalid_relay_credentials(api, authorization):
    headers = {} if authorization is None else {"authorization": authorization}
    code(api.client.get(PREFIX + "/api/states", headers=headers), 401, "relay_token_invalid")
    assert command_rows(api) == []


def test_device_revocation_kills_relay_token(api):
    token = credential(api)["relay_token"]
    store.revoke_device(str(api.tenant), api.device, "user:test")
    code(call(api, token), 401, "relay_token_invalid")
    assert command_rows(api) == []


def test_token_alone_selects_its_device(api):
    other = add_device(api, seed=2)
    token = credential(api, other)["relay_token"]
    response, row = proxy(api, token, data={"status": 200, "body": []}, device_id=other)
    assert response.status_code == 200 and row["device_id"] == other
    assert store.lease_commands(api.device) == []
    # A device id in the target path grants no cross-device routing ability.
    code(call(api, token, path="/" + api.device + "/api/states"), 403, "relay_path_denied")


def test_offline_device_does_not_enqueue(api, monkeypatch):
    token = credential(api)["relay_token"]
    original = store.get_device

    def offline(device_id):
        row = original(device_id)
        row["last_seen_at"] = (store._now() - timedelta(seconds=90)).isoformat()
        return row

    monkeypatch.setattr(store, "get_device", offline)
    code(call(api, token), 503, "device_offline")
    assert command_rows(api) == []


def test_timeout_command_later_expires(api, monkeypatch):
    token = credential(api)["relay_token"]
    monkeypatch.setattr(relay, "WAIT_SECONDS", 0.02)
    code(call(api, token), 504, "device_timeout")
    rows = command_rows(api)
    assert len(rows) == 1 and rows[0]["result"] is None
    expired_at = store._date(rows[0]["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(store, "_now", lambda: expired_at)
    assert store.lease_commands(api.device) == []
    assert command_rows(api)[0]["state"] == "expired"


@pytest.mark.parametrize("content,content_type,status,error", [
    (b'"' + b"x" * (256 * 1024) + b'"', "application/json", 413, "payload_too_large"),
    (b"not-json", "text/plain", 415, "json_required"),
    (b"{", "application/json", 400, "invalid_request"),
    (b'{"a":1,"a":2}', "application/json", 400, "invalid_request"),
    (b"NaN", "application/json", 400, "invalid_request"),
    (b"1e999", "application/json", 400, "invalid_request"),
], ids=["oversize", "non-json", "malformed-json", "duplicate-key", "nan", "overflow"])
def test_invalid_body_never_enqueues(api, content, content_type, status, error):
    code(call(api, credential(api)["relay_token"], method="POST", path="/api/services/light/turn_on",
              content=content, headers={"content-type": content_type}), status, error)
    assert command_rows(api) == []


def test_rate_limit_and_refill_are_per_token(api, monkeypatch):
    first, second = credential(api)["relay_token"], credential(api)["relay_token"]
    tick = [100.0]
    monkeypatch.setattr(relay, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    # Denied paths also consume tokens, without waiting on an absent agent.
    for _ in range(20):
        code(call(api, first, path="/api/config"), 403, "relay_path_denied")
    code(call(api, first, path="/api/config"), 429, "rate_limited")
    code(call(api, second, path="/api/config"), 403, "relay_path_denied")
    tick[0] += 0.11
    code(call(api, first, path="/api/config"), 403, "relay_path_denied")
    code(call(api, first, path="/api/config"), 429, "rate_limited")


def test_rate_bucket_lru_is_bounded():
    relay._BUCKETS.clear()
    assert relay._take_token("oldest")
    for index in range(9_999):
        assert relay._take_token(str(index))
    assert relay._take_token("oldest")  # refresh this entry's recency
    assert relay._take_token("newest")
    assert len(relay._BUCKETS) == 10_000 and "oldest" in relay._BUCKETS and "0" not in relay._BUCKETS


def test_openapi_is_public_deterministic_and_digest_pinned(api):
    path = "/api/devices/relay-openapi/homeassistant.json"
    response = api.client.get(path)
    assert response.status_code == 200 and response.headers["content-type"] == "application/json"
    document = response.json()
    assert response.content == json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    assert api.client.get(path).content == response.content
    assert document["servers"] == [{"url": ORIGIN}]
    assert document["components"]["securitySchemes"]["bearer"] == {"type": "http", "scheme": "bearer"}
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["X-Leaf-Document-Sha256"] == digest
    assert digest == "c981e01d65ac85c01378bb04c0ee0d45cf3e1a0ae320afa765013854d10b67d1"
    operations = [operation for methods in document["paths"].values() for operation in methods.values()]
    assert {operation["operationId"] for operation in operations} == {
        "listStates", "lightTurnOn", "lightTurnOff", "sceneTurnOn"}
    assert all(operation["security"] == [{"bearer": []}] for operation in operations)
    assert all(set(operation["responses"]["200"]["content"]) == {"application/json"} for operation in operations)
    on = document["paths"][PREFIX + "/api/services/light/turn_on"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert on["required"] == ["entity_id"] and not on["additionalProperties"]
    properties = on["properties"]
    assert properties["rgb_color"]["minItems"] == properties["rgb_color"]["maxItems"] == 3
    for name, minimum, maximum in (("brightness_pct", 0, 100), ("color_temp_kelvin", 2000, 6500), ("transition", 0, 60)):
        assert properties[name]["minimum"] == minimum and properties[name]["maximum"] == maximum


def test_openapi_uses_validated_public_origin(api):
    path = "/api/devices/relay-openapi/homeassistant.json"
    code(api.client.get(path, headers={"host": "attacker.invalid"}), 400, "invalid_request")
    forwarded = api.client.get(path, headers={"host": "internal", "x-forwarded-host": "platform-staging.leafdesign.ai",
                                             "x-forwarded-proto": "https"})
    assert forwarded.status_code == 200 and forwarded.json()["servers"] == [{"url": ORIGIN}]


def test_production_flag_covers_token_relay_and_public_document(api, monkeypatch):
    token = credential(api)["relay_token"]
    monkeypatch.setattr(devices, "_production_runtime", lambda: True)
    for response in (mint(api), call(api, token), api.client.get("/api/devices/relay-openapi/homeassistant.json")):
        code(response, 404, "not_found")
    monkeypatch.setenv("LEAF_DEVICE_BRIDGE_ENABLED", "1")
    assert mint(api).status_code == 201
    assert api.client.get("/api/devices/relay-openapi/homeassistant.json").status_code == 200
