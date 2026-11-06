"""Assistant device tools against the devices-router harness and real queue."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import deps
import device_store as store
import device_tool_service as service
import platform_link
from routers import device_tools, devices
from test_devices_router import api, device, PROJECT, TENANT, _REAL_PROJECT_OWNER

HEADERS = {"x-tenant-id": TENANT, "x-dispatch-secret": "dispatch-fixture",
           "x-authority-session-id": "session", "x-authority-turn-id": "turn",
           "content-type": "application/json"}


@pytest.fixture
def tools(api, monkeypatch):
    api.app.include_router(device_tools.router)
    api.tenant = deps.TenantContext(TENANT, subject="auth0|owner", tier="hosted_pro")
    api.session = {"session_id": "session", "org_id": TENANT, "tenant_id": TENANT, "project_id": PROJECT}
    api.subject = "auth0|owner"
    api.binding = SimpleNamespace(status="active", external_authority="auth0", external_subject=api.subject,
                                  platform_tenant_id=TENANT)
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(deps, "_dispatch_secret_ok", lambda value: value == "dispatch-fixture")
    monkeypatch.setattr(service.session_store, "get_session", lambda sid: api.session if sid == "session" else None)
    monkeypatch.setattr(service.session_store, "active_turn_subject", lambda *args: api.subject)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda subject: (TENANT, api.tenant.tier))
    monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda context: api.binding)
    monkeypatch.setattr(platform_link, "require_project_session_access", lambda *args, **kwargs: True)
    return api


def call(tools, operation, body, headers=None):
    return tools.client.post("/internal/integrations/devices/" + operation, json=body,
                             headers=HEADERS if headers is None else headers)


@pytest.mark.parametrize("operation,extra,op,args", [
    ("install_package", {"package": "homeassistant"}, "pkg.install", {"package": "homeassistant", "version": "2026.10.4"}),
    ("ha_step", {"op": "ha.onboard", "args": {}}, "ha.onboard", {}),
    ("ha_step", {"op": "ha.flow.start", "args": {"handler": "hue"}}, "ha.flow.start", {"handler": "hue"}),
    ("ha_step", {"op": "ha.flow.step", "args": {"flow_id": "flow", "data": {}}}, "ha.flow.step", {"flow_id": "flow", "data": {}}),
    ("ha_step", {"op": "ha.flow.list", "args": {}}, "ha.flow.list", {}),
    ("light", {"action": "turn_on", "entity_id": "light.desk", "color": "#1234ab", "brightness_pct": 75},
     "http.proxy", {"package": "homeassistant", "method": "POST", "path": "/api/services/light/turn_on",
                    "body_json": {"entity_id": "light.desk", "rgb_color": [18, 52, 171], "brightness_pct": 75}}),
    ("light", {"action": "turn_off", "entity_id": "group.room"}, "http.proxy",
     {"package": "homeassistant", "method": "POST", "path": "/api/services/light/turn_off", "body_json": {"entity_id": "group.room"}}),
    ("light_states", {}, "http.proxy", {"package": "homeassistant", "method": "GET", "path": "/api/states"}),
])
def test_enqueue_operations(tools, operation, extra, op, args):
    device_id = device(tools)
    response = call(tools, operation, {"device_id": device_id, **extra})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store, private"
    row = store.get_command(TENANT, device_id, response.json()["result"]["command_id"])
    assert row["op"] == op and row["args"] == args
    if operation in service.WRITES:
        assert any(r["actor"] == "user:auth0|owner#assistant" and r["op"] == op
                   for r in store.list_audit(TENANT, device_id))


def test_list_is_project_scoped_and_reduced(tools):
    device_id = device(tools)
    tools.project_owner = False
    response = call(tools, "list", {})
    assert response.status_code == 200
    result = response.json()["result"]
    row = result["devices"][0]
    assert row["device_id"] == device_id and row["online"] is True
    assert set(row) == {"device_id", "label", "online", "last_seen_at", "packages"}
    assert set(result["catalog"][0]) == {"package", "display_name", "version"}


@pytest.mark.parametrize("name", list(HEADERS)[:4])
@pytest.mark.parametrize("duplicate", [False, True])
def test_authority_headers_are_exactly_once(tools, name, duplicate):
    headers = [(k, v) for k, v in HEADERS.items() if k != name]
    if duplicate:
        headers.extend([(name, HEADERS[name]), (name, HEADERS[name])])
    response = call(tools, "list", {}, headers)
    assert response.status_code in {400, 401}
    assert response.json()["error"]["code"] in {"invalid_input", "unauthenticated"}


def test_bad_dispatch_secret(tools):
    response = call(tools, "list", {}, {**HEADERS, "x-dispatch-secret": "bad-secret"})
    assert response.status_code == 401 and response.json()["ok"] is False
    assert "bad-secret" not in response.text


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '[]', '{"x":NaN}', '{"x":1e999}', '{', '{"x":"' + 'x' * 16384 + '"}'])
def test_strict_body_and_cap(tools, raw):
    response = tools.client.post("/internal/integrations/devices/list", content=raw, headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("operation,extra", [
    ("install_package", {"package": "homeassistant"}),
    ("ha_step", {"op": "ha.onboard", "args": {}}),
    ("light", {"action": "turn_on", "entity_id": "light.desk"}),
])
def test_editor_cannot_write(tools, operation, extra):
    device_id = device(tools)
    tools.project_owner = False
    response = call(tools, operation, {"device_id": device_id, **extra})
    assert response.status_code == 403
    assert store.lease_commands(device_id) == []
    assert call(tools, "list", {}).status_code == 200


@pytest.mark.parametrize("tier,allowlist,allowed", [
    ("admin", "auth0|owner", True), ("admin", "", False),
    ("hosted_pro", "auth0|owner", False), ("admin", "auth0|someone-else", False),
])
def test_real_admin_pass_requires_both_factors(tools, monkeypatch, tier, allowlist, allowed):
    device_id = device(tools)
    platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle
    tools.tenant = deps.TenantContext(TENANT, subject=tools.subject, tier=tier)
    tools.binding.binding_id = uuid4()
    monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", allowlist)
    monkeypatch.setattr(project_lifecycle, "require_project_role", lambda *args, **kwargs: "editor")
    monkeypatch.setattr(devices, "_project_owner", _REAL_PROJECT_OWNER)
    response = call(tools, "install_package", {"device_id": device_id, "package": "homeassistant"})
    assert response.status_code == (200 if allowed else 403)
    assert bool(store.lease_commands(device_id)) is allowed


def test_other_tenant_device_is_hidden(tools, monkeypatch):
    device_id = device(tools)
    row = store.get_device(device_id)
    row["tenant_id"] = str(uuid4())
    monkeypatch.setattr(store, "get_device", lambda *args: row)
    response = call(tools, "light_states", {"device_id": device_id})
    assert response.status_code == 404
    assert store.lease_commands(device_id) == []


def test_same_tenant_other_project_device_is_hidden(tools):
    device_id = device(tools)
    tools.session = {**tools.session, "project_id": str(uuid4())}
    tools.projects[tools.session["project_id"]] = TENANT
    response = call(tools, "light_states", {"device_id": device_id})
    assert response.status_code == 404
    assert store.lease_commands(device_id) == []
    assert call(tools, "list", {}).json()["result"]["devices"] == []


@pytest.mark.parametrize("extra", [
    {"entity_id": "switch.desk"}, {"entity_id": "light.a/../b"}, {"entity_id": "light."},
    {"color": "red"}, {"color": "#12345g"}, {"brightness_pct": 101},
    {"brightness_pct": -1}, {"brightness_pct": True}, {"action": "toggle"},
])
def test_light_validation(tools, extra):
    device_id = device(tools)
    response = call(tools, "light", {"device_id": device_id, "action": "turn_on", "entity_id": "light.desk", **extra})
    assert response.status_code == 400
    assert store.lease_commands(device_id) == []


def test_ha_operation_is_closed(tools):
    device_id = device(tools)
    response = call(tools, "ha_step", {"device_id": device_id, "op": "agent.update", "args": {}})
    assert response.status_code == 400
    assert store.lease_commands(device_id) == []


def test_light_states_summary_is_filtered_and_bounded(tools):
    device_id = device(tools)
    command_id = call(tools, "light_states", {"device_id": device_id}).json()["result"]["command_id"]
    store.lease_commands(device_id)
    secret = ".".join(("eyJ" + "a" * 16, "b" * 22, "signature"))
    rows = [{"entity_id": "sensor.secret", "state": secret}]
    rows += [{"entity_id": "light.desk", "state": "on", "attributes": {
        "friendly_name": secret, "rgb_color": [1, 2, 3], "brightness": 255, "access_token": secret}}] * 105
    store.complete_command(device_id, command_id, {"command_id": command_id, "status": "ok",
        "finished_at": store._now().isoformat(), "data": {"body_json": rows, "headers": {"authorization": secret}}})
    response = call(tools, "command_status", {"device_id": device_id, "command_id": command_id})
    assert secret not in response.text and "authorization" not in response.text
    result = response.json()["result"]
    assert result["state"] == "done" and result["ok"] is True
    assert len(result["summary"]["lights"]) == 100
    assert result["summary"]["lights"][0] == {"entity_id": "light.desk", "state": "on", "name": "light.desk",
                                             "rgb": [1, 2, 3], "brightness_pct": 100}


def test_other_summary_has_only_safe_fields(tools):
    device_id = device(tools)
    command_id = call(tools, "ha_step", {"device_id": device_id, "op": "ha.onboard", "args": {}}).json()["result"]["command_id"]
    store.lease_commands(device_id)
    secret = "a" * 43
    store.complete_command(device_id, command_id, {"command_id": command_id, "status": "ok", "finished_at": store._now().isoformat(),
        "data": {"flow_id": "flow", "step_id": "step", "type": "form", "title": secret,
                 "errors": {"base": "cannot_connect", "access_token": secret}, "body_json": {"token": secret}}})
    response = call(tools, "command_status", {"device_id": device_id, "command_id": command_id})
    assert secret not in response.text
    assert response.json()["result"]["summary"] == {"status": "ok", "flow_id": "flow", "step_id": "step", "type": "form",
                                                     "errors": {"base": "cannot_connect"}}


@pytest.mark.parametrize("unsafe", [
    ".".join(("eyJ" + "a" * 4, "b" * 5, "c" * 6)),
    "Bearer " + "fixture",
    "aB0_" * 8,
    "a1" * 16,
])
def test_token_shapes_are_filtered_from_every_summary_field(unsafe):
    command = {"op": "http.proxy", "args": {"method": "GET", "path": "/api/states"}}
    result = {"data": {"body_json": [{"entity_id": "light.desk", "state": unsafe,
                                     "attributes": {"friendly_name": unsafe}}]}}
    assert service._summary(command, result) == {
        "lights": [{"entity_id": "light.desk", "name": "light.desk"}]}
    # Entity identifiers and error-map keys are also untrusted summary strings.
    result["data"]["body_json"][0]["entity_id"] = "light." + "a" * 32
    assert service._summary(command, result) == {"lights": []}
    result = {"data": {key: unsafe for key in ("status", "flow_id", "step_id", "type", "title")},
              "error": {"code": unsafe}}
    result["data"]["errors"] = {unsafe: "cannot_connect", "base": unsafe}
    assert service._summary({"op": "ha.onboard"}, result) == {"errors": {}}


@pytest.mark.parametrize("fault,code", [("session", "denied"), ("turn", "conflict"), ("binding", "denied"), ("tenant", "denied")])
def test_authority_fails_closed(tools, monkeypatch, fault, code):
    if fault == "session":
        tools.session = None
    elif fault == "turn":
        tools.subject = None
    elif fault == "binding":
        tools.binding.status = "revoked"
    else:
        monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda subject: (str(uuid4()), "pro"))
    response = call(tools, "list", {})
    assert response.json()["error"]["code"] == code


def test_link_service_entitlement_is_required(tools, monkeypatch):
    device_id = device(tools)
    monkeypatch.setattr(service.entitlements, "entitlements_for", lambda *args: {"converse": True, "link_service": False})
    assert call(tools, "install_package", {"device_id": device_id, "package": "homeassistant"}).status_code == 403
    assert call(tools, "list", {}).status_code == 200
    assert store.lease_commands(device_id) == []


def test_unexpected_exception_is_opaque(tools, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("private-secret-value")
    monkeypatch.setattr(store, "list_devices", broken)
    response = call(tools, "list", {})
    assert response.status_code == 503
    assert response.json() == service.failure("unavailable")


def test_command_status_queued_expired_and_missing(tools, monkeypatch):
    device_id = device(tools)
    command_id = call(tools, "light_states", {"device_id": device_id}).json()["result"]["command_id"]
    body = {"device_id": device_id, "command_id": command_id}
    assert call(tools, "command_status", body).json()["result"] == {"state": "queued"}
    row = store.get_command(TENANT, device_id, command_id)
    row["expires_at"] = (store._now() - timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(store, "get_command", lambda *args: row)
    assert call(tools, "command_status", body).json()["result"] == {"state": "expired"}
    monkeypatch.setattr(store, "get_command", lambda *args: None)
    assert call(tools, "command_status", body).status_code == 404
