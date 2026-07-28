"""Owner-only authority for Claude grant administration."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import deps
import platform_link
from routers import tenant as tenant_router


class _Store:
    def __init__(self, role: str | None = "owner", binding_tenant: str = "org-canonical"):
        self.role = role
        self.binding_tenant = binding_tenant

    def resolve_active_identity_binding(self, authority: str, subject: str):
        assert authority == "auth0"
        assert subject == "auth0|owner"
        return SimpleNamespace(
            platform_tenant_id=self.binding_tenant,
            binding_id="binding-1",
        )

    def active_identity_role(self, org_id, binding_id):
        assert binding_id == "binding-1"
        return self.role if str(org_id) == self.binding_tenant else None


def _tenant() -> deps.TenantContext:
    return deps.TenantContext(
        "org-canonical",
        org_id="org-canonical",
        tier="enterprise",
        subject="auth0|owner",
    )


def test_auth_off_preserves_legacy_grant_admin(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert tenant_router._require_grant_owner("acme") == "acme"


@pytest.mark.parametrize("role", ["editor", "reviewer", "read_only", None])
def test_live_non_owner_roles_are_denied(monkeypatch, role):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(platform_link, "platform_store", lambda: _Store(role=role))
    with pytest.raises(HTTPException) as exc:
        tenant_router._require_grant_owner(_tenant())
    assert exc.value.status_code == 403


def test_live_owner_is_authorized_against_current_binding(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(platform_link, "platform_store", lambda: _Store(role="owner"))
    assert tenant_router._require_grant_owner(_tenant()) == _tenant()


def test_binding_move_between_resolution_and_admin_check_fails_closed(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(
        platform_link,
        "platform_store",
        lambda: _Store(role="owner", binding_tenant="org-moved"),
    )
    with pytest.raises(HTTPException) as exc:
        tenant_router._require_grant_owner(_tenant())
    assert exc.value.status_code == 403


def test_authority_store_failure_is_retryable_service_failure(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")

    def _broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(platform_link, "platform_store", _broken)
    with pytest.raises(HTTPException) as exc:
        tenant_router._require_grant_owner(_tenant())
    assert exc.value.status_code == 503


def test_every_grant_route_uses_owner_dependency():
    grant_routes = {
        (method, route.path)
        for route in tenant_router.router.routes
        for method in route.methods
        if route.path.startswith("/api/tenant/claude-grant")
    }
    assert grant_routes == {
        ("GET", "/api/tenant/claude-grant/diagnostic"),
        ("POST", "/api/tenant/claude-grant"),
        ("PATCH", "/api/tenant/claude-grant"),
        ("GET", "/api/tenant/claude-grant"),
        ("DELETE", "/api/tenant/claude-grant"),
    }
    for route in tenant_router.router.routes:
        if not route.path.startswith("/api/tenant/claude-grant"):
            continue
        assert any(dep.call is tenant_router._require_grant_owner for dep in route.dependant.dependencies)
