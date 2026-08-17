"""Changed-surface proofs for the private P8 coordination route adapter."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

PLATFORM_DIR = SERVER_DIR.parent / "platform"
if "leaf_platform" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "leaf_platform", PLATFORM_DIR / "__init__.py",
        submodule_search_locations=[str(PLATFORM_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["leaf_platform"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

from routers import project_repository_edit as route  # noqa: E402

TENANT = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT = "22222222-2222-4222-8222-222222222222"
SECRET = "test-dispatch-secret"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", SECRET)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app, raise_server_exceptions=False)


def _headers(*, tenant: str = TENANT, secret: str = SECRET) -> dict[str, str]:
    return {"X-Tenant-Id": tenant, "X-Dispatch-Secret": secret}


def _body(action: str) -> dict:
    body = {"action": action, "tenant_id": TENANT}
    if action == "record_staged":
        body.pop("tenant_id")
        body["receipt"] = {"tenant_id": TENANT}
    return body


@pytest.mark.parametrize(
    ("attribute", "action", "path"),
    [
        ("handle_record_staged", "record_staged", "record-staged"),
        ("handle_authorize_publish", "authorize_publish", "authorize-publish"),
        ("handle_settle_publish", "settle_publish", "settle-publish"),
        ("handle_recover_publish", "recover_publish", "recover-publish"),
    ],
)
def test_routes_call_only_the_closed_coordination_handler(
    monkeypatch, attribute: str, action: str, path: str,
):
    calls = []

    def handler(state, body):
        calls.append((state, body))
        return {"contract": "leaf.project-repository-edit-coordination.v1", "action": action}

    monkeypatch.setattr(route, attribute, handler)
    response = _client(monkeypatch).post(
        f"/internal/project-repository-edit/{path}", json=_body(action), headers=_headers())
    assert response.status_code == 200
    assert response.json()["action"] == action
    assert calls == [(route._state, _body(action))]


@pytest.mark.parametrize("headers", [{}, _headers(secret="wrong"), {"X-Dispatch-Secret": SECRET}])
def test_dispatch_authority_fails_closed_before_handler(monkeypatch, headers):
    called = False

    def handler(state, body):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(route, "handle_authorize_publish", handler)
    response = _client(monkeypatch).post(
        "/internal/project-repository-edit/authorize-publish",
        json=_body("authorize_publish"), headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "dispatch authority unavailable"}
    assert called is False


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("record-staged", {"action": "record_staged", "receipt": {"tenant_id": OTHER_TENANT}}),
        ("authorize-publish", {"action": "authorize_publish", "tenant_id": OTHER_TENANT}),
        ("settle-publish", {"action": "settle_publish", "tenant_id": OTHER_TENANT}),
        ("recover-publish", {"action": "recover_publish", "tenant_id": OTHER_TENANT}),
        ("record-staged", {"action": "record_staged", "receipt": {}}),
    ],
)
def test_tenant_mismatch_or_missing_body_tenant_fails_before_handler(monkeypatch, path, body):
    called = False

    def handler(state, request):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(route, "handle_record_staged", handler)
    monkeypatch.setattr(route, "handle_authorize_publish", handler)
    monkeypatch.setattr(route, "handle_settle_publish", handler)
    monkeypatch.setattr(route, "handle_recover_publish", handler)
    response = _client(monkeypatch).post(
        f"/internal/project-repository-edit/{path}", json=body, headers=_headers())
    assert response.status_code == 404
    assert response.json() == {"detail": "repository edit unavailable"}
    assert called is False


@pytest.mark.parametrize(
    "code",
    [
        "authorize_publish_request_fields_invalid",
        "actor_authority_unavailable",
        "stale_lease_generation",
        "confirmation_already_consumed",
    ],
)
def test_coordination_failures_are_indistinguishable(monkeypatch, code):
    def handler(state, body):
        raise route.CoordinationError(code)

    monkeypatch.setattr(route, "handle_authorize_publish", handler)
    response = _client(monkeypatch).post(
        "/internal/project-repository-edit/authorize-publish",
        json=_body("authorize_publish"), headers=_headers())
    assert response.status_code == 404
    assert response.json() == {"detail": "repository edit unavailable"}


def test_backend_failure_is_sanitized_and_retryable(monkeypatch):
    def handler(state, body):
        raise RuntimeError("database host and source must not escape")

    monkeypatch.setattr(route, "handle_recover_publish", handler)
    response = _client(monkeypatch).post(
        "/internal/project-repository-edit/recover-publish",
        json=_body("recover_publish"), headers=_headers())
    assert response.status_code == 503
    assert response.json() == {"detail": "repository edit unavailable"}
    assert "database" not in response.text


def test_composition_root_mounts_only_the_private_router():
    app_source = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    router_source = (SERVER_DIR / "routers" / "project_repository_edit.py").read_text(
        encoding="utf-8")
    assert "project_repository_edit as project_repository_edit_router" in app_source
    assert "app.include_router(project_repository_edit_router.router)" in app_source
    assert router_source.count('@router.post("/internal/project-repository-edit/') == 4
    for forbidden in ("SdkRepoEditor", "subprocess", "git update-ref", "/api/"):
        assert forbidden not in router_source
