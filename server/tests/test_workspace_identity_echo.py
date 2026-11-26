"""Display the already resolved workspace without changing tenant authority."""
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import investor_demo
import platform_link
from routers import session, tools


TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
IDENTITY = "/api/session/identity"
CATALOG = "/api/tool-envelopes/tenant"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(
        platform_link, "platform_store",
        lambda: SimpleNamespace(get_org=lambda tenant_id: SimpleNamespace(name="Leaf Automation")),
    )
    app = FastAPI()
    app.include_router(session.router)
    app.include_router(tools.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: deps.TenantContext(
        TENANT, org_id=TENANT, tier="hosted_pro", subject="auth0|owner",
        authority_resolved=True,
    )
    return TestClient(app)


def test_identity_echoes_resolved_workspace(client, monkeypatch):
    looked_up = []

    def get_org(tenant_id):
        looked_up.append(tenant_id)
        return SimpleNamespace(name="Leaf Automation")

    monkeypatch.setattr(platform_link, "platform_store", lambda: SimpleNamespace(get_org=get_org))
    response = client.get(IDENTITY, headers={"X-Tenant-Id": OTHER})
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": TENANT, "org_id": TENANT, "tier": "hosted_pro",
        "workspace": {"id": TENANT, "name": "Leaf Automation"},
    }
    assert looked_up == [UUID(TENANT)]
    assert isinstance(looked_up[0], UUID)


@pytest.mark.parametrize("failure_at", ["store", "get_org"])
def test_identity_workspace_lookup_fails_soft(client, monkeypatch, failure_at):
    def fail(*args):
        raise RuntimeError("platform store unavailable")

    monkeypatch.setattr(
        platform_link, "platform_store",
        fail if failure_at == "store" else lambda: SimpleNamespace(get_org=fail),
    )
    response = client.get(IDENTITY)
    assert response.status_code == 200
    assert response.json()["workspace"] == {"id": TENANT, "name": None}


@pytest.mark.parametrize("org,expected", [
    (None, None),
    (SimpleNamespace(name="   \t"), None),
    (SimpleNamespace(name=None), None),
    (SimpleNamespace(name=42), None),
    (SimpleNamespace(name="x" * 500), "x" * 120),
    (SimpleNamespace(name="  Leaf Automation  "), "Leaf Automation"),
])
def test_identity_workspace_name_is_optional_and_bounded(client, monkeypatch, org, expected):
    monkeypatch.setattr(
        platform_link, "platform_store", lambda: SimpleNamespace(get_org=lambda tenant_id: org),
    )
    response = client.get(IDENTITY)
    assert response.status_code == 200
    assert response.json()["workspace"] == {"id": TENANT, "name": expected}


def test_identity_auth_off_preserves_empty_body(client, monkeypatch):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: "demo-tenant"
    response = client.get(IDENTITY)
    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.parametrize("context", [
    deps.TenantContext(TENANT, authority_resolved=True, verified_demo=True),
    deps.TenantContext(TENANT, authority_resolved=False),
    deps.TenantContext(TENANT, authority_resolved=True, subject=investor_demo.SUBJECT),
    TENANT,
])
def test_identity_omits_workspace_for_ineligible_contexts(client, monkeypatch, context):
    def forbidden():
        pytest.fail("ineligible context must not look up a workspace")

    monkeypatch.setattr(platform_link, "platform_store", forbidden)
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: context
    response = client.get(IDENTITY)
    assert response.status_code == 200
    assert "workspace" not in response.json()


def test_identity_non_uuid_resolved_tenant_has_no_name(client, monkeypatch):
    def forbidden():
        pytest.fail("non-UUID tenant must not reach the store")

    monkeypatch.setattr(platform_link, "platform_store", forbidden)
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: deps.TenantContext(
        "platform-canonical", authority_resolved=True,
    )
    response = client.get(IDENTITY)
    assert response.status_code == 200
    assert response.json()["workspace"] == {"id": "platform-canonical", "name": None}


def test_tenant_list_echoes_workspace_only_when_visible(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER", "1")
    monkeypatch.setenv("LEAF_TENANT_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER_TENANTS", TENANT)
    monkeypatch.delenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", raising=False)
    response = client.get(CATALOG)
    assert response.status_code == 200
    assert response.json() == {"tools": [], "workspace_id": TENANT}

    monkeypatch.setenv("LEAF_PLUGIN_BUILDER", "0")
    hidden = client.get(CATALOG)
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "tool_not_published"}
    assert "workspace_id" not in hidden.json()
