"""Canonical platform tenant authority for mounted conversation sessions."""
from __future__ import annotations

from types import SimpleNamespace

import deps
import session_store
import tenancy
import turn_runner
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import agent, session, sessions


def _route_dependencies(router, paths):
    return {
        (method, route.path): {dependency.call for dependency in route.dependant.dependencies}
        for route in router.routes
        if route.path in paths
        for method in route.methods
    }


def test_intake_and_conversation_routes_use_active_platform_tenant():
    session_paths = {
        "/api/sessions",
        "/api/sessions/{session_id}/messages",
        "/api/sessions/{session_id}/stream",
        "/api/sessions/{session_id}/transcript",
    }
    dependencies = {
        **_route_dependencies(session.router, {"/api/session", "/api/session/identity"}),
        **_route_dependencies(sessions.router, session_paths),
    }

    assert set(dependencies) == {
        ("GET", "/api/session"),
        ("GET", "/api/session/identity"),
        ("GET", "/api/sessions"),
        ("POST", "/api/sessions"),
        ("POST", "/api/sessions/{session_id}/messages"),
        ("GET", "/api/sessions/{session_id}/stream"),
        ("GET", "/api/sessions/{session_id}/transcript"),
    }
    for calls in dependencies.values():
        assert deps.require_active_tenant in calls
        assert deps.require_tenant not in calls


def test_conversation_approval_uses_active_platform_tenant():
    dependencies = _route_dependencies(
        agent.router,
        {"/api/agent/approvals/{confirmation_id}"},
    )

    assert set(dependencies) == {
        ("POST", "/api/agent/approvals/{confirmation_id}"),
    }
    calls = next(iter(dependencies.values()))
    assert deps.require_active_tenant in calls
    assert deps.require_tenant not in calls


def _live_client(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(
        deps,
        "resolve_active_platform_tenant_authority",
        lambda subject: ("platform-canonical", "hosted_pro"),
    )
    monkeypatch.setattr(
        tenancy,
        "get_store",
        lambda: SimpleNamespace(resolve_workspace=lambda tenant_id: None),
    )
    app = FastAPI()
    app.include_router(session.router)
    app.include_router(sessions.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "stale-jwt-claim",
        org_id="stale-jwt-claim",
        tier="hosted_pro",
        subject="auth0|owner",
    )
    return TestClient(app, raise_server_exceptions=False)


def test_stale_jwt_claim_cannot_split_intake_session_and_turn_tenant(monkeypatch):
    client = _live_client(monkeypatch)
    captured = {}

    def create(tenant_id, drawing_id, model=None):
        captured["created_tenant"] = tenant_id
        return {
            "session_id": "canonical-session",
            "tenant_id": tenant_id,
            "drawing_id": drawing_id,
            "status": "idle",
            "created_at": 1,
            "model": model,
        }

    monkeypatch.setattr(session_store, "get_or_create_session", create)
    monkeypatch.setattr(
        session_store,
        "get_session",
        lambda session_id: {
            "session_id": session_id,
            "tenant_id": "platform-canonical",
            "drawing_id": "drawing-1",
            "status": "idle",
        },
    )

    def start(tenant, session_id, **kwargs):
        captured["turn_tenant"] = str(tenant)
        captured["turn_session"] = session_id
        return "turn-1"

    monkeypatch.setattr(turn_runner, "start_turn", start)

    intake = client.get("/api/session?dwg=rooftop_demo")
    assert intake.status_code == 200, intake.text
    assert intake.json()["tenant_id"] == "platform-canonical"

    created = client.post("/api/sessions", json={"drawing_id": "drawing-1"})
    assert created.status_code == 200, created.text
    assert created.json()["tenant_id"] == "platform-canonical"
    assert captured["created_tenant"] == "platform-canonical"

    message = client.post(
        "/api/sessions/canonical-session/messages",
        json={"text": "hello"},
    )
    assert message.status_code == 202, message.text
    assert captured["turn_tenant"] == "platform-canonical"
    assert captured["turn_session"] == "canonical-session"


@pytest.fixture
def identity_client(monkeypatch):
    # Reuse the real offline RS256 verifier fixture, not a require_tenant
    # override: missing, invalid and expired credentials must reach auth.py.
    from test_auth import _ENV

    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        deps, "resolve_active_platform_tenant_authority",
        lambda subject: ("platform-canonical", "hosted_pro"),
    )
    monkeypatch.setattr(
        tenancy, "get_store",
        lambda: SimpleNamespace(resolve_workspace=lambda tenant_id: None),
    )

    def drawing_access(*args, **kwargs):
        pytest.fail("identity proof must not access drawings, APS or the broker")

    for name in ("curated_identity", "store_id", "source_id"):
        monkeypatch.setattr(session.drawing_identity, name, drawing_access)
    monkeypatch.setattr(session, "_stored_drawing_intake", drawing_access)
    monkeypatch.setattr(deps, "load_cached_intake", drawing_access)
    monkeypatch.setattr(session.requests, "post", drawing_access)
    app = FastAPI()
    app.include_router(session.router)
    return TestClient(app)


def test_identity_proves_tenant_without_drawing_access(identity_client, monkeypatch):
    from test_auth import bearer, mint

    # Both APS modes must remain drawing-independent.
    for aps_live in (False, True):
        monkeypatch.setattr(deps, "APS_LIVE", aps_live)
        response = identity_client.get(
            "/api/session/identity",
            headers={"Authorization": bearer(mint(tenant_id="platform-canonical"))},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "tenant_id": "platform-canonical",
            "org_id": "platform-canonical",
            "tier": "hosted_pro",
        }


@pytest.mark.parametrize("credential", ["missing", "invalid", "expired"])
def test_identity_requires_valid_token_like_intake(identity_client, credential):
    from test_auth import bearer, mint

    headers = {} if credential == "missing" else {
        "Authorization": bearer("invalid" if credential == "invalid" else mint(exp_delta=-30)),
    }
    identity = identity_client.get("/api/session/identity", headers=headers)
    intake = identity_client.get("/api/session", headers=headers)
    assert identity.status_code == intake.status_code == 401
    assert identity.json() == intake.json()


def test_identity_preserves_active_authority_refusal(identity_client, monkeypatch):
    from test_auth import bearer, mint

    def refuse(subject):
        raise HTTPException(403, "verified subject has no active platform tenant authority")

    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", refuse)
    headers = {"Authorization": bearer(mint(tenant_id="platform-canonical"))}
    identity = identity_client.get("/api/session/identity", headers=headers)
    intake = identity_client.get("/api/session", headers=headers)
    assert identity.status_code == intake.status_code == 403
    assert identity.json() == intake.json()


def test_identity_preserves_conflicting_tenant_refusal(identity_client):
    from test_auth import bearer, mint

    headers = {"Authorization": bearer(mint(tenant_id="old-tenant"))}
    identity = identity_client.get("/api/session/identity", headers=headers)
    intake = identity_client.get("/api/session", headers=headers)
    assert identity.status_code == intake.status_code == 409
    assert identity.json() == intake.json()


def test_identity_auth_off_has_no_identity_echo_or_drawing_access(identity_client, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    response = identity_client.get("/api/session/identity", headers={"X-Tenant-Id": "demo-tenant"})
    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.parametrize("live_setting", ["1", "true", "yes", "on"])
@pytest.mark.parametrize("tenant_header", [None, "", "demo-tenant", "g4-synthetic-other-tenant"])
def test_identity_ignores_x_tenant_id_under_live_auth(
    identity_client, monkeypatch, live_setting, tenant_header,
):
    from test_auth import bearer, mint

    monkeypatch.setenv("LEAF_AUTH_LIVE", live_setting)
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset())
    subjects = []

    def resolve_authority(subject):
        assert subject == "auth0|tester"
        subjects.append(subject)
        return "platform-canonical", "hosted_pro"

    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", resolve_authority)
    tenant_headers = {} if tenant_header is None else {"X-Tenant-Id": tenant_header}
    response = identity_client.get(
        "/api/session/identity",
        headers={
            **tenant_headers,
            "Authorization": bearer(mint(tenant_id="platform-canonical")),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "tenant_id": "platform-canonical",
        "org_id": "platform-canonical",
        "tier": "hosted_pro",
    }
    assert subjects == ["auth0|tester"]

    unauthenticated = identity_client.get("/api/session/identity", headers=tenant_headers)
    assert unauthenticated.status_code == 401, unauthenticated.text
    assert subjects == ["auth0|tester"]
