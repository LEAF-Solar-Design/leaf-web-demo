"""Account-owner control for the tool-publication approval policy."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import agent_policy
import deps
import platform_link
from routers import ops as ops_router


class _BindingStore:
    def __init__(self, tenant_id="tenant-a", role="owner", *, missing=False,
                 unavailable=False):
        self.tenant_id = tenant_id
        self.role = role
        self.missing = missing
        self.unavailable = unavailable

    def resolve_active_identity_binding(self, provider, subject):
        assert provider == "auth0"
        assert subject
        if self.unavailable:
            raise RuntimeError("binding authority unavailable")
        if self.missing:
            return None
        return SimpleNamespace(
            platform_tenant_id=self.tenant_id,
            binding_id="binding-1",
        )

    def active_identity_role(self, tenant_id, binding_id):
        assert str(tenant_id) == self.tenant_id
        assert binding_id == "binding-1"
        return self.role


def _client(monkeypatch, tenant, *, authority=None):
    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    if authority is None:
        authority = _BindingStore(str(tenant))
    monkeypatch.setattr(platform_link, "platform_store", lambda: authority)
    return TestClient(app)


@pytest.mark.parametrize(
    "tenant,authority,status_code",
    [
        (
            deps.TenantContext("tenant-a", tier="hosted_pro", subject="auth0|owner"),
            _BindingStore(role="editor"),
            403,
        ),
        (
            deps.TenantContext("tenant-a", tier="hosted_pro", subject="auth0|owner"),
            _BindingStore(tenant_id="tenant-b"),
            403,
        ),
        (
            deps.TenantContext("tenant-a", tier="hosted_pro", subject="auth0|owner"),
            _BindingStore(missing=True),
            403,
        ),
        (deps.TenantContext("tenant-a", tier="hosted_pro"), _BindingStore(), 403),
        ("tenant-a", _BindingStore(), 403),
        (
            deps.TenantContext("tenant-a", tier="hosted_pro", subject="auth0|owner"),
            _BindingStore(unavailable=True),
            503,
        ),
    ],
)
def test_account_control_denies_non_owner_or_unavailable_authority(
    monkeypatch, tenant, authority, status_code
):
    calls = []
    monkeypatch.setattr(
        agent_policy, "set_tenant_overlay", lambda *a, **k: calls.append((a, k))
    )
    client = _client(monkeypatch, tenant, authority=authority)

    get_response = client.get("/api/admin/account-controls")
    assert get_response.status_code == status_code
    assert client.put("/api/admin/account-controls", json={
        "tool_publication_approval_required": False,
        "expected_revision": 0,
    }).status_code == status_code
    if status_code == 403:
        assert "account owner authority" in get_response.text
    else:
        assert "binding authority" in get_response.text
    assert calls == []


def test_account_control_get_defaults_off_without_secrets(monkeypatch):
    tenant = deps.TenantContext(
        "tenant-a", tier="hosted_pro", subject="auth0|owner", authority_resolved=True
    )
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset())
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: {
        "agent_disabled": False,
        "overlay": {"run_read_tool": {"policy": "always-confirm"}},
        "revision": 7,
    })
    response = _client(monkeypatch, tenant).get("/api/admin/account-controls")

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "tenant-a",
        "tool_publication_approval_required": False,
        "revision": 7,
    }
    assert "secret" not in response.text.lower()


def test_disabled_publication_is_a_calm_fail_closed_account_state(monkeypatch):
    tenant = deps.TenantContext(
        "tenant-a", tier="admin", subject="auth0|admin", authority_resolved=True
    )
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset({"auth0|admin"}))
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: {
        "agent_disabled": False,
        "overlay": {"request_publication": {"enabled": False}},
        "revision": 8,
    })
    writes = []

    def save(_tid, overlay, **kwargs):
        writes.append((overlay, kwargs))
        return {"agent_disabled": False, "overlay": overlay, "revision": 9}

    monkeypatch.setattr(agent_policy, "set_tenant_overlay", save)
    client = _client(monkeypatch, tenant)

    get_response = client.get("/api/admin/account-controls")
    put_response = client.put("/api/admin/account-controls", json={
        "tool_publication_approval_required": False,
        "expected_revision": 8,
    })

    assert get_response.status_code == 409
    assert get_response.json()["detail"] == "tool publication is disabled for this account"
    assert put_response.status_code == 200
    assert put_response.json()["tool_publication_approval_required"] is True
    assert len(writes) == 1


def test_account_control_put_preserves_overlay_and_audits_admin(monkeypatch):
    tenant = deps.TenantContext(
        "tenant-a", tier="admin", subject="auth0|admin", authority_resolved=True
    )
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset({"auth0|admin"}))
    stored = {
        "agent_disabled": False,
        "overlay": {"run_read_tool": {"policy": "always-confirm"}},
        "revision": 4,
    }
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: stored)
    calls = []

    def save(tid, overlay, **kwargs):
        calls.append((tid, overlay, kwargs))
        return {"agent_disabled": False, "overlay": overlay, "revision": 5}

    monkeypatch.setattr(agent_policy, "set_tenant_overlay", save)
    client = _client(monkeypatch, tenant)
    response = client.put("/api/admin/account-controls", json={
        "tool_publication_approval_required": True,
        "expected_revision": 4,
    })

    assert response.status_code == 200
    assert calls[0][0] == "tenant-a"
    assert calls[0][1] == {
        "run_read_tool": {"policy": "always-confirm"},
        "request_publication": {"policy": "always-confirm"},
    }
    assert calls[0][2]["expected_revision"] == 4
    audit = calls[0][2]["audit_event"]
    assert audit["actor_subject"] == "auth0|admin"
    assert audit["via"] == "account_owner"
    assert response.json()["tool_publication_approval_required"] is True


def test_account_control_put_off_removes_only_publication_policy(monkeypatch):
    tenant = deps.TenantContext(
        "tenant-a", tier="admin", subject="auth0|admin", authority_resolved=True
    )
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset({"auth0|admin"}))
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: {
        "agent_disabled": False,
        "overlay": {
            "request_publication": {
                "policy": "always-confirm",
                "enabled": False,
            },
            "run_read_tool": {"policy": "always-confirm"},
        },
        "revision": 9,
    })
    captured = {}

    def save(_tid, overlay, **_kwargs):
        captured.update(overlay)
        return {"agent_disabled": False, "overlay": overlay, "revision": 10}

    monkeypatch.setattr(agent_policy, "set_tenant_overlay", save)
    response = _client(monkeypatch, tenant).put(
        "/api/admin/account-controls",
        json={
            "tool_publication_approval_required": False,
            "expected_revision": 9,
        },
    )

    assert response.status_code == 200
    assert captured == {
        "request_publication": {"enabled": False},
        "run_read_tool": {"policy": "always-confirm"},
    }
    assert response.json()["tool_publication_approval_required"] is True


def test_account_control_put_on_preserves_publication_fields(monkeypatch):
    tenant = deps.TenantContext(
        "tenant-a", tier="admin", subject="auth0|admin", authority_resolved=True
    )
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset({"auth0|admin"}))
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: {
        "agent_disabled": False,
        "overlay": {"request_publication": {"enabled": False}},
        "revision": 2,
    })
    captured = {}

    def save(_tid, overlay, **_kwargs):
        captured.update(overlay)
        return {"agent_disabled": False, "overlay": overlay, "revision": 3}

    monkeypatch.setattr(agent_policy, "set_tenant_overlay", save)
    response = _client(monkeypatch, tenant).put(
        "/api/admin/account-controls",
        json={
            "tool_publication_approval_required": True,
            "expected_revision": 2,
        },
    )

    assert response.status_code == 200
    assert captured["request_publication"] == {
        "enabled": False,
        "policy": "always-confirm",
    }


def test_account_control_cas_conflict_and_strict_body(monkeypatch):
    tenant = deps.TenantContext(
        "tenant-a", tier="admin", subject="auth0|admin", authority_resolved=True
    )
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset({"auth0|admin"}))
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: {
        "agent_disabled": False, "overlay": {}, "revision": 3,
    })

    def conflict(*_args, **_kwargs):
        raise agent_policy.PolicyError("stale agent tenant state revision")

    monkeypatch.setattr(agent_policy, "set_tenant_overlay", conflict)
    client = _client(monkeypatch, tenant)
    response = client.put("/api/admin/account-controls", json={
        "tool_publication_approval_required": True,
        "expected_revision": 2,
    })
    assert response.status_code == 409
    assert client.put("/api/admin/account-controls", json={
        "tool_publication_approval_required": "false",
        "expected_revision": 3,
    }).status_code == 422
    assert client.put("/api/admin/account-controls", json={
        "tenant_id": "tenant-b",
        "tool_publication_approval_required": False,
        "expected_revision": 3,
    }).status_code == 422


def test_publication_overlay_validates_through_policy_store(monkeypatch):
    calls = []

    class Store:
        @staticmethod
        def set_tenant_state(tenant_id, **kwargs):
            calls.append((tenant_id, kwargs))
            return {
                "agent_disabled": False,
                "overlay": kwargs["overlay"],
                "revision": kwargs["expected_revision"] + 1,
            }

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setattr(agent_policy, "_pg_store", lambda: Store)
    result = agent_policy.set_tenant_overlay(
        "tenant-a",
        {"request_publication": {"policy": "always-confirm"}},
        expected_revision=0,
        audit_event={"actor_subject": "auth0|admin"},
    )
    assert result["revision"] == 1
    assert calls[0][1]["overlay"]["request_publication"]["policy"] == "always-confirm"
