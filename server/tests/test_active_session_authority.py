"""Canonical platform tenant authority for mounted conversation sessions."""
from __future__ import annotations

from types import SimpleNamespace

import deps
import session_store
import tenancy
import turn_runner
from fastapi import FastAPI
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
        **_route_dependencies(session.router, {"/api/session"}),
        **_route_dependencies(sessions.router, session_paths),
    }

    assert set(dependencies) == {
        ("GET", "/api/session"),
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
