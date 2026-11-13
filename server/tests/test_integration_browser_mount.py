"""The real application mounts only the jointly authorized dataset bridge."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

import deps
import service_browser_sessions as sessions
from route_flatten import iter_leaf_routes
from test_integration_browser import (BINDING, ELEMENT_PATH, LOCAL, PROJECT, SCOPE,
                                      SCOPE_PATH, SECRET, SUBJECT, TOKEN, context)


@pytest.fixture
def mounted(context, monkeypatch):
    # Import after the fixture installs synthetic session custody and an inert
    # upstream. ASGITransport does not run application lifespan services.
    from app import app

    monkeypatch.setattr(app, "dependency_overrides", {
        **app.dependency_overrides,
        deps.require_tenant: lambda: SimpleNamespace(subject=SUBJECT),
    })

    def send(method="GET", path=SCOPE_PATH, headers=None, content=None):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=LOCAL, trust_env=False) as client:
                return await client.request(method, path,
                    headers=context["headers"]() if headers is None else headers,
                    content=content)
        return asyncio.run(run())

    return app, send


def test_application_has_exact_bridge_method_inventory(mounted):
    app, _ = mounted
    assert {(method, path) for path, route in iter_leaf_routes(app.routes)
            if path.startswith("/api/v1/projects/")
            for method in route.methods} == {
        ("GET", "/api/v1/projects/{project_id}/scope"),
        ("GET", "/api/v1/projects/{project_id}/integrations/{resource:path}"),
        ("POST", "/api/v1/projects/{project_id}/integrations/{resource:path}"),
        ("PUT", "/api/v1/projects/{project_id}/integrations/{resource:path}"),
    }


def test_registered_bridge_uses_application_session_middleware(mounted, context):
    _, send = mounted
    response = send()
    assert response.status_code == 200
    assert response.json() == SCOPE
    [sent] = context["calls"]
    assert sent.headers["authorization"] == "Bearer " + TOKEN
    assert "cookie" not in sent.headers
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("method,path", [
    ("GET", SCOPE_PATH), ("GET", ELEMENT_PATH),
    ("POST", ELEMENT_PATH), ("PUT", ELEMENT_PATH + "/fixture-view"),
])
@pytest.mark.parametrize("proof", ["anonymous", "selector", "cookie", "bearer"])
def test_each_registered_method_refuses_incomplete_browser_proof(mounted, context, method, path, proof):
    _, send = mounted
    headers = [("Origin", LOCAL), ("Content-Type", "application/json")]
    if proof == "selector":
        headers.append(("Authorization", "LeafServiceSession " + BINDING))
    if proof in {"cookie", "bearer"}:
        headers.append(("Cookie", sessions.SESSION_COOKIE + "=" + SECRET))
    if proof == "bearer":
        headers.append(("Authorization", "Bearer " + TOKEN))
    response = send(method, path, headers, b"{}" if method != "GET" else None)
    assert response.status_code in {401, 403}
    assert not context["calls"]
    assert TOKEN not in response.text and SECRET not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_registered_writes_require_same_origin_before_upstream(mounted, context, method):
    _, send = mounted
    path = ELEMENT_PATH if method == "POST" else ELEMENT_PATH + "/fixture-view"
    response = send(method, path, context["headers"]() + [("Content-Type", "application/json")], b"{}")
    assert response.status_code == 403
    assert not context["calls"]


def test_registered_bridge_refuses_revoked_cookie_before_upstream(mounted, context):
    _, send = mounted
    context["revoked"] = True
    response = send()
    assert response.status_code == 401
    assert not context["calls"]


def test_registered_bridge_checks_current_tenant_subject(mounted, context, monkeypatch):
    app, send = mounted
    monkeypatch.setitem(app.dependency_overrides, deps.require_tenant,
                        lambda: SimpleNamespace(subject="another-service@clients"))
    response = send()
    assert response.status_code == 403
    assert not context["calls"]


@pytest.mark.parametrize("resource", ["connections", "actions", "manual-workflow-runs"])
def test_registration_does_not_admit_provider_or_workflow_routes(mounted, context, resource):
    _, send = mounted
    response = send(path=f"/api/v1/projects/{PROJECT}/integrations/{resource}")
    assert response.status_code == 404
    assert not context["calls"]
