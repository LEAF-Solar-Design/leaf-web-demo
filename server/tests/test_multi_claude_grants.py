"""Token-free multi-account grant proxy contract."""

import json
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import deps
import platform_link
from routers import tenant as tenant_routes
from routers.tenant import _status_body


@pytest.fixture
def readiness_client(monkeypatch):
    org = "tenant-a"
    active = deps.TenantContext(org, org_id=org, subject="managed-caller", authority_resolved=True)
    calls = []
    app = FastAPI()
    app.include_router(tenant_routes.router)

    def verified_identity(request: Request):
        if request.headers.get("Authorization") != "LeafServiceSession test-binding":
            raise HTTPException(status_code=401)
        return active

    app.dependency_overrides[deps.require_tenant] = verified_identity
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "https://harness.invalid")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "test-back-edge")
    session = SimpleNamespace(role="owner", tenant=org, echoed=org)
    monkeypatch.setattr(platform_link, "platform_store", lambda: SimpleNamespace(
        resolve_active_identity_binding=lambda *_: SimpleNamespace(
            platform_tenant_id=session.tenant, binding_id="binding"),
        active_identity_role=lambda *_: session.role,
    ))

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {
            "schema": "leaf.project-source-authoring-readiness.v1", "tenant_id": session.echoed,
            "dependency_ready": True, "route_class": "approved_subscription_pool",
            "eligible": True, "reason": "ready", "override_url": "private",
            "accounts": [{"id": "private", "label": "private"}],
        })

    monkeypatch.setattr(requests, "get", fake_get)
    return TestClient(app), session, calls


def test_readiness_uses_active_tenant_and_returns_only_masked_projection(readiness_client):
    client, _, calls = readiness_client
    response = client.get("/api/tenant/project-source-authoring/readiness",
                          headers={"Authorization": "LeafServiceSession test-binding"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schema": "leaf.project-source-authoring-readiness.v1", "tenant_id": "tenant-a",
        "dependency_ready": True, "route_class": "approved_subscription_pool",
        "eligible": True, "reason": "ready",
    }
    assert calls == [("https://harness.invalid/internal/project-repository-source/readiness", {
        "headers": {"X-Harness-Secret": "test-back-edge", "X-Tenant-Id": "tenant-a"},
        "timeout": 30, "allow_redirects": False,
    })]


@pytest.mark.parametrize(("auth", "role", "tenant", "query", "expected"), [
    (None, "owner", "tenant-a", "", 401),
    ("LeafServiceSession test-binding", "viewer", "tenant-a", "", 403),
    ("LeafServiceSession test-binding", "owner", "tenant-b", "", 403),
    ("LeafServiceSession test-binding", "owner", "tenant-a", "?tenant_id=tenant-b", 400),
])
def test_readiness_denies_invalid_caller_or_scope_before_proxy(
        readiness_client, auth, role, tenant, query, expected):
    client, session, calls = readiness_client
    session.role, session.tenant = role, tenant
    response = client.get("/api/tenant/project-source-authoring/readiness" + query,
                          headers={"Authorization": auth} if auth else {})
    assert response.status_code == expected
    assert calls == []


def test_readiness_refuses_foreign_harness_binding(readiness_client):
    client, session, _ = readiness_client
    session.echoed = "tenant-b"
    response = client.get("/api/tenant/project-source-authoring/readiness",
                          headers={"Authorization": "LeafServiceSession test-binding"})
    assert response.status_code == 502
    assert "tenant-b" not in response.text


def test_status_whitelists_multi_account_fields_without_secret_material():
    fake_token = "sk-ant-oat01-FAKE-never-return"
    body = _status_body({
        "linked": True,
        "linked_at": "2026-07-25T07:00:00Z",
        "kind": "oauth",
        "active_account_id": "account-a",
        "accounts": [
            {
                "id": "account-a",
                "label": "first@example.com",
                "kind": "oauth",
                "linked_at": "2026-07-25T07:00:00Z",
                "active": True,
                "plan": "team",
                "eligible": True,
                "usage_tokens": 314,
                "cooldown_until": "2026-07-25T07:15:00Z",
                "token": fake_token,
                "path": "/secret/file",
            },
            {"id": "bad", "label": "bad", "kind": "unknown", "token": fake_token},
        ],
        "token": fake_token,
    })

    assert body["active_account_id"] == "account-a"
    assert body["accounts"] == [{
        "id": "account-a",
        "label": "first@example.com",
        "kind": "oauth",
        "linked_at": "2026-07-25T07:00:00Z",
        "active": True,
        "plan": "team",
        "eligible": True,
        "usage_tokens": 314,
        "cooldown_until": "2026-07-25T07:15:00Z",
    }]
    serialized = json.dumps(body)
    assert fake_token not in serialized
    assert "/secret/file" not in serialized


@pytest.mark.parametrize("code", ["authentication_failed", "oauth_org_not_allowed"])
def test_status_surfaces_only_known_refusal_codes_and_iso_timestamp(code):
    body = _status_body({"accounts": [{
        "id": "a", "label": "My account", "kind": "oauth",
        "auth_refused_code": code, "auth_refused_at": "2026-11-25T12:30:00Z",
    }]})
    assert body["accounts"][0]["auth_refused_code"] == code
    assert body["accounts"][0]["auth_refused_at"] == "2026-11-25T12:30:00Z"


@pytest.mark.parametrize("code", ["unknown", "sk-ant-oat01-FAKE-secret", [], {}])
def test_status_drops_unknown_or_nonstring_refusal_codes(code):
    fake_token = "sk-ant-oat01-FAKE-secret"
    body = _status_body({"accounts": [{
        "id": "a", "label": "My account", "kind": "oauth",
        "auth_refused_code": code, "auth_refused_at": fake_token,
    }]})
    assert "auth_refused_code" not in body["accounts"][0]
    assert "auth_refused_at" not in body["accounts"][0]
    assert fake_token not in json.dumps(body)


@pytest.mark.parametrize("stamp", ["sk-ant-oat01-FAKE-secret", "x" * 41, "2026-11-25", [], {}, None])
def test_status_drops_invalid_refusal_timestamp_without_echoing_secret(stamp):
    body = _status_body({"accounts": [{
        "id": "a", "label": "My account", "kind": "oauth",
        "auth_refused_code": "authentication_failed", "auth_refused_at": stamp,
    }]})
    assert body["accounts"][0]["auth_refused_code"] == "authentication_failed"
    assert "auth_refused_at" not in body["accounts"][0]
    assert "sk-ant-oat01-FAKE-secret" not in json.dumps(body)
