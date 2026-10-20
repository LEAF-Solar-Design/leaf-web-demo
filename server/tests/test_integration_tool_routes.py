"""Exercise real host authority resolution with in-memory producer doubles."""
import platform as _stdlib_platform
_stdlib_platform.python_implementation()

from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
import integration_tool_service as service
from routers import integration_tools
from leaf_platform.integration_turn_grant_store import Binding, EncryptedGrant

REF = {"id": "sales", "version": 1, "digest": "a" * 64}
BODY = {"dataset_ref": REF, "query": {"fields": ["id"], "limit": 10}}


@pytest.fixture
def host(monkeypatch):
    tenant, project, user, binding_id = (str(uuid4()) for _ in range(4))
    state = SimpleNamespace(tenant=tenant, project=project, subject="auth0|person", turn="turn-1",
                            role=True, write_access=True, write_checks=[], ops=[], gates=[],
                            decision="allow", on_http=lambda: None)
    state.session = {"session_id": "app-session", "tenant_id": tenant,
                     "org_id": tenant, "project_id": project}
    state.binding = SimpleNamespace(platform_tenant_id=UUID(tenant), binding_id=UUID(binding_id),
                                    platform_user_id=UUID(user), status="active",
                                    external_authority="auth0", external_subject=state.subject)
    state.headers = {"X-Tenant-Id": tenant, "X-Dispatch-Secret": "dispatch-secret",
                     "X-Authority-Session-Id": "app-session", "X-Authority-Turn-Id": "turn-1"}
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    monkeypatch.setenv("LEAF_INTEGRATION_TURN_GRANTS_ENABLED", "1")
    monkeypatch.setenv("LEAF_INTEGRATION_SERVICE_ORIGIN", "https://ops.example.com")
    monkeypatch.setattr(service.session_store, "get_session", lambda sid: state.session if sid == "app-session" else None)
    def active(sid, turn, storage_tenant, max_age):
        assert max_age == service.turn_runner.turn_max_s()
        return state.subject if (sid == "app-session" and turn == state.turn
                                 and storage_tenant == state.session["tenant_id"]) else None
    monkeypatch.setattr(service.session_store, "active_turn_subject", active)
    monkeypatch.setattr(service.deps, "resolve_active_platform_tenant_authority", lambda subject: (state.tenant, "hosted_pro"))
    monkeypatch.setattr(service.platform_link, "resolve_caller_binding", lambda ctx: state.binding)
    def access(ctx, pid, *, write, binding=None):
        state.write_checks.append(write)
        if not state.role or (write and not state.write_access):
            raise service.platform_link.ProjectSessionForbidden("removed role")
        if pid != state.project or str(ctx) != state.tenant or binding is not state.binding:
            raise LookupError("foreign project")
        return state.tenant
    monkeypatch.setattr(service.platform_link, "require_project_access", access)
    monkeypatch.setattr(service.entitlements, "entitlements_for", lambda tier: {"converse": state.role})
    authority = service.resolve_current(tenant, "app-session", "turn-1").authority
    binding = Binding(authority, str(uuid4()), 1000, 1120, 1120, 1100)
    state.custody = EncryptedGrant(binding, b"encrypted-only")
    def load(authority, now):
        # A collision in another project cannot select this project record.
        return state.custody if state.custody and state.custody.binding.authority == authority else None
    def decrypt(envelope, binding, callback, keys):
        token = bytearray(b"route-synthetic-bearer")
        try:
            return callback(token)
        finally:
            service.crypto._scrub(token)
    def gate(*args, **kwargs):
        state.gates.append((args, kwargs))
        return {"decision": state.decision, "reason": "fixture", "policy": "auto", "rung": 1,
                **({"confirmation_id": "a" * 16} if state.decision == "awaiting_approval" else {})}
    async def http(request):
        state.ops.append(request)
        state.on_http()
        if "/integrations/elements" in request.url.path:
            return httpx.Response(200, json={"element": json.loads(request.content), "revision": 1})
        return httpx.Response(200, json={
            "schema_version": 1, "dataset_ref": REF,
            "fields": [{"id": "id", "label": "ID", "type": {"kind": "identifier"},
                        "nullable": False, "sensitivity": "internal"}],
            "rows": [{"key": "1", "values": {"id": "one"}}], "next_cursor": None,
            "status": "ready", "freshness": {"source_updated_at": None, "synced_at": None,
                                               "fetched_at": "2026-10-20T12:00:00Z"}})
    original = service.IntegrationToolService
    monkeypatch.setattr(service, "IntegrationToolService", lambda: original(
        store=SimpleNamespace(load_active=load), decrypt=decrypt, gate=gate, clock=lambda: 1001,
        transport=httpx.MockTransport(http)))
    app = FastAPI()
    app.include_router(integration_tools.router)
    state.client = TestClient(app)
    state.post = lambda **kwargs: state.client.post("/internal/integrations/query", headers=state.headers,
                                                   json=kwargs.get("body", BODY))
    return state


def test_authenticated_query(host):
    response = host.post()
    assert response.status_code == 200
    assert response.json()["result"]["dataset_ref"] == REF
    assert len(host.ops) == len(host.gates) == 1
    assert response.headers["cache-control"] == "no-store, private"


@pytest.mark.parametrize("header", ["X-Tenant-Id", "X-Dispatch-Secret", "X-Authority-Session-Id", "X-Authority-Turn-Id"])
def test_missing_authority_header(host, header):
    del host.headers[header]
    response = host.post()
    assert response.status_code in {400, 401}
    assert set(response.json()) == {"error"}
    assert not host.ops and not host.gates


@pytest.mark.parametrize("enabled", ["0", "false", ""])
def test_feature_disabled(host, monkeypatch, enabled):
    monkeypatch.setenv("LEAF_INTEGRATION_TURN_GRANTS_ENABLED", enabled)
    assert host.post().status_code == 503
    assert not host.ops and not host.gates


def test_live_auth_and_dispatch_are_both_required(host, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert host.post().status_code == 401
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    host.headers["X-Dispatch-Secret"] = "wrong"
    assert host.post().status_code == 401
    assert not host.ops and not host.gates


@pytest.mark.parametrize("mutation", ["foreign_header", "storage_marker_header", "foreign_org", "foreign_project",
                                       "superseded_turn", "removed_role", "removed_binding", "tenant_move",
                                       "expired_grant", "missing_grant", "noncanonical_project"])
def test_authority_boundary(host, mutation):
    if mutation == "foreign_header":
        host.headers["X-Tenant-Id"] = str(uuid4())
    elif mutation == "storage_marker_header":
        host.headers["X-Tenant-Id"] = f"project:{host.tenant}:{host.project}"
    elif mutation == "foreign_org":
        host.session["org_id"] = str(uuid4())
    elif mutation == "foreign_project":
        host.session["project_id"] = str(uuid4())
    elif mutation == "superseded_turn":
        host.turn = "turn-2"
    elif mutation == "removed_role":
        host.role = False
    elif mutation == "removed_binding":
        host.binding = None
    elif mutation == "tenant_move":
        host.tenant = str(uuid4())
    elif mutation == "expired_grant":
        host.custody = EncryptedGrant(replace(host.custody.binding, issued_at=800,
            verified_jwt_exp=920, turn_deadline=920, expires_at=900), b"encrypted")
    elif mutation == "missing_grant":
        host.custody = None
    else:
        host.session["project_id"] = "{" + host.project + "}"
    response = host.post()
    assert response.status_code in {400, 403, 409, 503}
    assert not host.ops and not host.gates
    assert set(response.json()["error"]) == {"code", "correlationId"}
    UUID(response.json()["error"]["correlationId"])


def test_colliding_ref_in_different_project_cannot_load_custody(host):
    host.project = str(uuid4())
    host.session["project_id"] = host.project
    assert host.post().status_code == 503
    assert not host.ops and not host.gates


@pytest.mark.parametrize("mutation", ["turn", "binding", "role", "tenant", "origin"])
def test_current_authority_rechecked_after_http(host, monkeypatch, mutation):
    def change():
        if mutation == "turn":
            host.turn = "turn-2"
        elif mutation == "binding":
            host.binding = None
        elif mutation == "role":
            host.role = False
        elif mutation == "tenant":
            host.tenant = str(uuid4())
        else:
            monkeypatch.setenv("LEAF_INTEGRATION_SERVICE_ORIGIN", "https://other.example.com")
    host.on_http = change
    response = host.post()
    assert response.status_code in {403, 409}
    assert "result" not in response.json()
    assert len(host.ops) == len(host.gates) == 1


@pytest.mark.parametrize("origin", ["http://ops.example.com", "https://ops.example.com/", "https://ops.example.com?q=1",
                                     "https://user@ops.example.com", "https://ops.example.com#x", "https://ops.example.com:443"])
def test_unsafe_origin_fails_before_custody_gate_or_http(host, monkeypatch, origin):
    monkeypatch.setenv("LEAF_INTEGRATION_SERVICE_ORIGIN", origin)
    assert host.post().status_code == 503
    assert not host.ops and not host.gates


@pytest.mark.parametrize("decision", ["deny", "awaiting_approval"])
def test_discovery_normalized_gate_nonallow(host, decision):
    host.decision = decision
    response = host.client.post("/internal/integrations/datasets", headers=host.headers,
                                json={"after": "opaque_1", "confirmation_id": "a" * 16})
    assert response.status_code == 200
    assert set(response.json()) == {"gate"}
    assert not host.ops and len(host.gates) == 1
    assert host.gates[0][0][4] == {"operation": "datasets", "limit": 50,
                                    "after": "opaque_1", "confirmation_id": "a" * 16}


def test_discovery_rejects_wrong_producer_shape(host):
    response = host.client.post("/internal/integrations/datasets", headers=host.headers, json={})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_response"
    assert len(host.ops) == 1


@pytest.mark.parametrize("body", [b'{"limit":1,"limit":2}', b'{"limit":NaN}', b'{"limit":1e999}',
                                   b'x' * (service.REQUEST_CAP + 1), b'{"url":"https://foreign"}'],
                         ids=["duplicate", "nonfinite", "overflow", "oversized", "extra"])
def test_strict_stream_body(host, body):
    response = host.client.post("/internal/integrations/datasets", content=body,
                                headers={**host.headers, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert not host.ops and not host.gates


def test_content_type_and_duplicate_authority_headers(host):
    response = host.client.post("/internal/integrations/query", content=json.dumps(BODY),
                                headers={**host.headers, "Content-Type": "text/plain"})
    assert response.status_code == 400
    headers = list(host.headers.items()) + [("X-Tenant-Id", host.tenant)]
    assert host.client.post("/internal/integrations/query", json=BODY, headers=headers).status_code == 400
    assert not host.ops and not host.gates


def test_no_additional_methods_or_routes(host):
    assert host.client.get("/internal/integrations/query", headers=host.headers).status_code == 405
    assert host.client.post("/internal/integrations/save", headers=host.headers, json={}).status_code == 404
    assert not host.ops and not host.gates
