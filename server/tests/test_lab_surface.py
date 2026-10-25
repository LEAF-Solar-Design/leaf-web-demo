"""Lab admission, tenant boundary, status contract, and mounting checks."""
from __future__ import annotations

from pathlib import Path
import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from route_flatten import iter_leaf_routes

import deps
import entitlements
from routers import lab_router, lab_surface

LAB_PATHS = [
    "/api/lab/status",
    "/api/lab/docs/{doc_id}/save",
    "/api/lab/docs/{doc_id}/head",
    "/api/lab/docs/{doc_id}/revisions",
    "/api/lab/docs/{doc_id}/revisions/{revision}",
    "/api/lab/docs/{doc_id}/diff",
    "/api/lab/docs/{doc_id}/restore",
    "/api/lab/docs/{doc_id}/publish",
    "/api/lab/widgets/{doc_id}/values",
    "/api/lab/runs",
    "/api/lab/runs/{run_id}",
    "/api/lab/runs/{run_id}/events",
    "/api/lab/runs/{run_id}/cancel",
    "/api/lab/runs/worker/pause",
    "/api/lab/runs/worker/resume",
]


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.setattr(lab_surface.lab_run_store, "worker_status", lambda age: [])
    monkeypatch.setattr(lab_surface.lab_run_store, "paused", lambda: False)
    monkeypatch.setattr(lab_surface.lab_run_store, "oldest_queued_age_s", lambda: None)
    monkeypatch.delenv(lab_surface.FLAG_LAB_SURFACE, raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    monkeypatch.setenv("LEAF_DEPLOYMENT_ENVIRONMENT", "")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")


def _client(tenant_override=True):
    app = FastAPI()
    app.include_router(lab_router.router)
    if tenant_override:
        app.dependency_overrides[deps.require_tenant] = lambda: "tenant-a"
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("runtime,deployment,enabled", [
    ("staging", "", True),
    (" STAGING ", "staging", True),
    ("production", "production", False),
    ("local", "", False),
    ("test", "", False),
    (None, "", False),
    ("demo", "", False),
    ("dev", "", False),
])
def test_unset_rail_only_defaults_on_in_staging(monkeypatch, runtime, deployment, enabled):
    if runtime is None:
        monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    else:
        monkeypatch.setenv("LEAF_RUNTIME_ENV", runtime)
    monkeypatch.setenv("LEAF_DEPLOYMENT_ENVIRONMENT", deployment)
    response = _client().get("/api/lab/status")
    assert lab_surface.lab_surface_enabled() is enabled
    assert response.status_code == (200 if enabled else 404)


@pytest.mark.parametrize("value,enabled", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    (" TRUE ", True), (" 1 ", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("", False), ("unknown", False),
])
def test_explicit_flag_vocabulary_and_precedence(monkeypatch, value, enabled):
    monkeypatch.setenv(lab_surface.FLAG_LAB_SURFACE, value)
    # On overrides production; off overrides the staging default.
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production" if enabled else "staging")
    response = _client().get("/api/lab/status")
    assert response.status_code == (200 if enabled else 404)


@pytest.mark.parametrize("deployment", ["production", "local"])
def test_inconsistent_staging_deployment_fails_closed(monkeypatch, deployment):
    monkeypatch.setenv("LEAF_DEPLOYMENT_ENVIRONMENT", deployment)
    assert _client().get("/api/lab/status").status_code == 404


def test_environment_is_read_on_every_call(monkeypatch):
    client = _client()
    assert client.get("/api/lab/status").status_code == 200
    monkeypatch.setenv(lab_surface.FLAG_LAB_SURFACE, "0")
    assert client.get("/api/lab/status").status_code == 404
    monkeypatch.setenv(lab_surface.FLAG_LAB_SURFACE, "1")
    assert client.get("/api/lab/status").status_code == 200
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert client.get("/api/lab/status").status_code == 503
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv(lab_surface.FLAG_LAB_SURFACE)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    assert client.get("/api/lab/status").status_code == 404


@pytest.mark.parametrize("auth_live", ["0", "1"])
def test_rail_off_precedes_auth_and_tenant_work(monkeypatch, auth_live):
    monkeypatch.setenv(lab_surface.FLAG_LAB_SURFACE, "0")
    monkeypatch.setenv("LEAF_AUTH_LIVE", auth_live)
    calls = []
    client = _client()

    def tenant():
        calls.append(True)
        return "tenant-a"

    client.app.dependency_overrides[deps.require_tenant] = tenant
    response = client.get("/api/lab/status")
    assert response.status_code == 404
    assert response.json()["detail"] == {
        "ok": False, "status": "refused", "reason": "lab_surface_disabled",
    }
    assert calls == []


def test_auth_live_off_precedes_tenant_work(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    calls = []
    client = _client()

    def tenant():
        calls.append(True)
        return "tenant-a"

    client.app.dependency_overrides[deps.require_tenant] = tenant
    response = client.get("/api/lab/status", headers={"X-Tenant-Id": "untrusted"})
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "lab_auth_live_required"
    assert calls == []


def test_missing_authorization_uses_real_tenant_boundary():
    response = _client(tenant_override=False).get("/api/lab/status")
    assert response.status_code == 401


def test_malformed_authorization_uses_real_tenant_boundary():
    response = _client(tenant_override=False).get(
        "/api/lab/status", headers={"Authorization": "invalid"})
    assert response.status_code == 401


def test_verified_token_without_tenant_claim_is_forbidden(monkeypatch):
    import auth

    # Only signature verification is replaced; require_tenant and claim
    # extraction remain real, so a verified identity cannot skip tenancy.
    monkeypatch.setattr(auth, "verify_platform_token", lambda _: {"sub": "user-a"})
    response = _client(tenant_override=False).get(
        "/api/lab/status", headers={"Authorization": "Bearer no-tenant-claim"})
    assert response.status_code == 403


@pytest.mark.parametrize("runtime,posture", [
    ("staging", "staging"), ("production", "production"), ("test", "local"),
])
@pytest.mark.parametrize("tier,build", [("hosted_pro", True), ("restricted", False)])
def test_status_contract_contains_only_public_fields(monkeypatch, runtime, posture, tier, build):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", runtime)
    monkeypatch.setenv(lab_surface.FLAG_LAB_SURFACE, "1")
    if not build:
        def forbidden(*args):
            pytest.fail("unentitled status accessed worker state")
        for name in ("worker_status", "paused", "oldest_queued_age_s"):
            monkeypatch.setattr(lab_surface.lab_run_store, name, forbidden)
    client = _client()
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", tier=tier)
    response = client.get("/api/lab/status")
    assert response.status_code == 200
    body = response.json()
    expected = {
        "ok": True, "schema": "leaf.lab-status.v1", "rail": "on",
        "posture": posture, "tenant": "tenant-a",
    }
    if build:
        expected["worker"] = {"paused": False, "workers": [], "queue_age_max_s": None}
    assert body == expected
    secret_key = re.compile(
        r"password|credential|secret|token|jwt|auth|cookie|session|host|^sub$", re.I)
    assert not any(secret_key.search(key) for key in body)
    assert not any(lab_surface_value.startswith("eyJ") for lab_surface_value in body.values()
                   if isinstance(lab_surface_value, str))


@pytest.mark.parametrize("lookup", ["resolve_tier", "resolve_roles", "entitlements_for"])
@pytest.mark.parametrize("error", [entitlements.EntitlementsError, RuntimeError])
def test_status_omits_worker_when_entitlement_lookup_fails(monkeypatch, lookup, error):
    def unavailable(*args):
        raise error("policy lookup unavailable")

    def forbidden(*args):
        pytest.fail("failed entitlement lookup accessed worker state")

    monkeypatch.setattr(entitlements, lookup, unavailable)
    for name in ("worker_status", "paused", "oldest_queued_age_s"):
        monkeypatch.setattr(lab_surface.lab_run_store, name, forbidden)
    client = _client()
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", tier="hosted_pro")
    response = client.get("/api/lab/status")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "schema": "leaf.lab-status.v1", "rail": "on",
        "posture": "staging", "tenant": "tenant-a",
    }


def test_status_serializes_only_tenant_id_from_verified_context():
    client = _client()
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-b", subject="private-subject")
    response = client.get("/api/lab/status")
    assert response.status_code == 200
    assert response.json()["tenant"] == "tenant-b"
    assert "private-subject" not in response.text


def test_pinned_routers_match_the_included_set():
    app = FastAPI()
    app.include_router(lab_router.router)
    routes = [route for path, route in iter_leaf_routes(app.router.routes)
              if isinstance(route, APIRoute) and path.startswith("/api/lab")]
    assert lab_router.PINNED_ROUTERS == ("lab_surface", "lab_artifacts", "lab_widgets", "lab_runs")
    assert {route.endpoint.__module__.rsplit(".", 1)[-1]
            for route in routes} == set(lab_router.PINNED_ROUTERS)
    assert {tag for route in routes for tag in route.tags} == {
        "lab_surface", "lab_artifacts", "lab_widgets", "lab_runs"}
    assert [route.path for route in routes] == LAB_PATHS


def test_every_lab_route_has_the_ordered_admission_dependencies():
    app = FastAPI()
    app.include_router(lab_router.router)

    def admission_dependencies(routes, inherited=()):
        for route in routes:
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                # Lazy includes keep admission dependencies on the context,
                # rather than copying them into the leaf's dependant.
                dependencies = tuple(
                    dependency.dependency
                    for dependency in (route.include_context.dependencies or ()))
                yield from admission_dependencies(
                    original_router.routes, inherited + dependencies)
            elif isinstance(route, APIRoute) and route.path.startswith("/api/lab"):
                yield route.path, list(inherited) + [
                    dependency.call for dependency in route.dependant.dependencies]

    routes = list(admission_dependencies(app.router.routes))
    assert [path for path, _ in routes] == LAB_PATHS
    for _, dependencies in routes:
        assert dependencies[:3] == [
            lab_surface.lab_rail, lab_surface.require_auth_live, deps.require_tenant,
        ]


def test_app_mounts_the_aggregator_exactly_once():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    include = "app.include_router(lab_router.router)"
    assert source.count(include) == 1
    assert source.index(include) > source.index("app.include_router(ios_ship.router)")
    assert "app.include_router(lab_surface.router)" not in source
