"""Authorization-negative HTTP coverage for identity and Lab artifact routers."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import lab_artifacts as ledger  # noqa: E402
import lab_run_store as run_store  # noqa: E402
import tenancy  # noqa: E402
from routers import deployment_identity, lab_artifacts, lab_router, lab_runs, lab_widgets  # noqa: E402
from route_flatten import iter_leaf_routes  # noqa: E402
from test_auth import _ENV, bearer, mint  # noqa: E402

MARKER = "LEAF-AUTHZ-TENANT-A-PRIVATE"
DOC = "boundary-doc"
BASE = f"/api/lab/docs/{DOC}"
IDENTITY = {"schema": "leaf.deployment-identity.v1", "status": "unattested",
            "services": {}}
LAB_CASES = [
    pytest.param("POST", "save", {"graph": {
        "schema": "leaf.lab-graph.v1", "blocks": [], "pipes": [], "outputs": {},
    }, "expected_head": 2}, 409, id="save"),
    pytest.param("GET", "head", None, 404, id="head"),
    pytest.param("GET", "revisions", None, 404, id="revisions"),
    pytest.param("GET", "revisions/1", None, 404, id="revision"),
    pytest.param("GET", "diff?before=1&after=2", None, 404, id="diff"),
    pytest.param("POST", "restore", {"revision": 1, "expected_head": 2}, 404,
                 id="restore"),
    pytest.param("POST", "publish", {"expected_head": 2}, 404, id="publish"),
]
RUN_CASES = [
    ("POST", "", {"doc_id": DOC, "graph_revision": 2}),
    ("GET", "/private-run", None),
    ("GET", "/private-run/events", None),
    ("POST", "/private-run/cancel", None),
    ("POST", "/worker/pause", {"reason": "maintenance"}),
    ("POST", "/worker/resume", None),
]


def _h(tenant: str) -> dict:
    # Live auth needs a real bearer as well as the canary's tenant header.
    return {"X-Tenant-Id": tenant,
            "Authorization": bearer(mint(tenant_id=tenant))}


def _graph(marker):
    return {"schema": "leaf.lab-graph.v1", "blocks": [{
        "id": "canary", "registry": {
            "id": "canary", "version": "1.0.0", "code_sha256": "a" * 64,
        }, "settings": {"canary": marker},
    }], "pipes": [], "outputs": {}}


def _owner_snapshot(db):
    with sqlite3.connect(db) as conn:
        return {table: conn.execute(
            f"SELECT * FROM {table} WHERE tenant_id=? ORDER BY rowid",
            ("tenant-a",),
        ).fetchall() for table in (
            "lab_documents", "lab_blobs", "lab_revisions", "lab_publish_receipts",
        )}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_LAB_STORE", "sqlite")
    monkeypatch.setenv("LEAF_LAB_RUN_STORE", "sqlite")
    monkeypatch.setenv("LEAF_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    # The offline mint helper uses one subject. Bind that subject to tenant-b
    # for boundary probes; owner positive controls explicitly bind tenant-a.
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda subject: ("tenant-b", "hosted_pro"))
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset())
    monkeypatch.setattr(tenancy, "get_store", lambda: SimpleNamespace(
        resolve_workspace=lambda tenant_id: None))
    # Identity's live reader can contact ECS. Keep all requests offline.
    monkeypatch.setattr(deployment_identity, "live_deployment_identity",
                        lambda: IDENTITY)
    app = FastAPI()
    app.include_router(deployment_identity.router)
    app.include_router(lab_router.router)
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http


@pytest.fixture()
def owner(client, tmp_path):
    ledger.save("tenant-a", DOC, _graph(MARKER + "-v1"), None, "owner-a")
    ledger.save("tenant-a", DOC, _graph(MARKER), 1, "owner-a")
    ledger.publish("tenant-a", DOC, 2, "owner-a")
    db = tmp_path / "lab.db"
    return db, _owner_snapshot(db)


def test_route_inventory_is_complete():
    # Pin the surface so adding a route requires adding its negative cases.
    assert {(method, path)
            for path, route in iter_leaf_routes(deployment_identity.router.routes)
            for method in route.methods} == {("GET", "/api/deployment-identity")}
    assert {(method, path)
            for path, route in iter_leaf_routes(lab_runs.router.routes)
            for method in route.methods} == {
        ("POST", "/api/lab/runs"),
        ("GET", "/api/lab/runs/{run_id}"),
        ("GET", "/api/lab/runs/{run_id}/events"),
        ("POST", "/api/lab/runs/{run_id}/cancel"),
        ("POST", "/api/lab/runs/worker/pause"),
        ("POST", "/api/lab/runs/worker/resume"),
    }
    assert {(method, path)
            for path, route in iter_leaf_routes(lab_widgets.router.routes)
            for method in route.methods} == {
        ("POST", "/api/lab/widgets/{doc_id}/values"),
    }
    assert {(method, path)
            for path, route in iter_leaf_routes(lab_artifacts.router.routes)
            for method in route.methods} == {
        ("POST", "/api/lab/docs/{doc_id}/save"),
        ("GET", "/api/lab/docs/{doc_id}/head"),
        ("GET", "/api/lab/docs/{doc_id}/revisions"),
        ("GET", "/api/lab/docs/{doc_id}/revisions/{revision}"),
        ("GET", "/api/lab/docs/{doc_id}/diff"),
        ("POST", "/api/lab/docs/{doc_id}/restore"),
        ("POST", "/api/lab/docs/{doc_id}/publish"),
    }


def test_deployment_identity_refuses_unauthenticated(client, monkeypatch):
    def forbidden():
        pytest.fail("unauthenticated deployment identity reached the live reader")

    monkeypatch.setattr(deployment_identity, "live_deployment_identity", forbidden)
    response = client.get("/api/deployment-identity", headers={})
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "missing bearer token (Authorization header)"}


@pytest.mark.parametrize("mode,status", [
    ("anonymous", 401), ("foreign", 404), ("owner", 200),
    ("rail-off", 404), ("auth-off", 503), ("graph-target", 409),
])
def test_widget_values_admission(client, owner, monkeypatch, mode, status):
    db, before = owner
    doc_id = DOC
    if mode in ("owner", "graph-target"):
        monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                            lambda subject: ("tenant-a", "hosted_pro"))
    if mode == "owner":
        doc_id = "boundary-widget"
        ledger.save("tenant-a", doc_id, {
            "schema": "widget-doc.v1", "min_renderer": "1.0.0", "active": "la",
            "formats": {"la": {"h": 160, "radius": 22, "bg": "black",
                               "vibrant": False, "ship": True, "els": []}},
        }, None, "owner-a", kind="widget")
        before = _owner_snapshot(db)
    if mode == "rail-off":
        monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "0")
    if mode == "auth-off":
        monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    if mode in ("anonymous", "rail-off", "auth-off"):
        def forbidden(*args, **kwargs):
            pytest.fail("closed widget admission reached persistence")
        monkeypatch.setattr(ledger, "_run", forbidden)
    response = client.post(f"/api/lab/widgets/{doc_id}/values", json={"bindings": []},
                           headers={} if mode == "anonymous" else
                           _h("tenant-a" if mode in ("owner", "graph-target") else "tenant-b"))
    assert response.status_code == status, response.text
    if mode == "graph-target":
        assert response.json() == {"detail": "lab_document_not_widget"}
    assert MARKER not in response.text
    assert _owner_snapshot(db) == before


@pytest.mark.parametrize("method,suffix,body", RUN_CASES)
@pytest.mark.parametrize("mode,status", [
    ("rail-off", 404), ("auth-off", 503), ("anonymous", 401), ("no-build", 403),
])
def test_runs_admission_precedes_storage(client, monkeypatch, method, suffix, body, mode, status):
    if mode == "rail-off":
        monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "0")
    elif mode == "auth-off":
        monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    elif mode == "no-build":
        monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                            lambda subject: ("tenant-b", "restricted"))
    def forbidden(*args, **kwargs):
        pytest.fail("closed runs admission reached storage")
    monkeypatch.setattr(ledger, "_run", forbidden)
    monkeypatch.setattr(run_store, "_run", forbidden)
    response = client.request(method, "/api/lab/runs" + suffix, json=body,
                              headers={} if mode == "anonymous" else _h("tenant-b"))
    assert response.status_code == status, response.text
    assert MARKER not in response.text


@pytest.mark.parametrize("method,suffix,body", RUN_CASES[:4])
def test_runs_foreign_tenant_is_not_found(client, owner, method, suffix, body):
    row = run_store.create("tenant-a", DOC, "private-run", 2, "a" * 64, "lab-ops.v1",
                           "b" * 64, "lab-worker.v1", "abc1234")
    response = client.request(method, "/api/lab/runs" + suffix, json=body,
                              headers=_h("tenant-b"))
    assert response.status_code == 404, response.text
    assert MARKER not in response.text and "tenant-a" not in response.text
    assert run_store.get("tenant-a", "private-run") == row
    db, before = owner
    assert _owner_snapshot(db) == before


def test_deployment_identity_auth_off_is_public_and_tenant_independent(client, monkeypatch):
    # Deliberately public in legacy auth-off mode. This service-wide identity
    # has no tenant-scoped records, so there is no wrong-tenant resource id.
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    for headers in ({}, {"X-Tenant-Id": "tenant-a"}, {"X-Tenant-Id": "tenant-b"}):
        response = client.get("/api/deployment-identity", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == IDENTITY
        assert MARKER not in response.text
        assert "tenant-a" not in response.text
        assert "tenant-b" not in response.text


@pytest.mark.parametrize("method,suffix,body,wrong_status", LAB_CASES)
def test_lab_route_refuses_unauthenticated(
    client, owner, monkeypatch, method, suffix, body, wrong_status,
):
    def forbidden(*args, **kwargs):
        pytest.fail("unauthenticated Lab request reached persistence")

    monkeypatch.setattr(ledger, "_run", forbidden)
    response = client.request(method, f"{BASE}/{suffix}", json=body, headers={})
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "missing bearer token (Authorization header)"}
    assert MARKER not in response.text
    db, before = owner
    assert _owner_snapshot(db) == before


@pytest.mark.parametrize("method,suffix,body,wrong_status", LAB_CASES)
def test_lab_route_refuses_wrong_tenant(
    client, owner, method, suffix, body, wrong_status,
):
    response = client.request(method, f"{BASE}/{suffix}", json=body,
                              headers=_h("tenant-b"))
    assert response.status_code == wrong_status, response.text
    assert response.json() == {"detail": (
        "stale_expected_head" if suffix == "save" else "lab_document_not_found"
    )}
    assert MARKER not in response.text
    assert "tenant-a" not in response.text
    db, before = owner
    assert _owner_snapshot(db) == before
    with pytest.raises(ledger.NotFound):
        ledger.get("tenant-b", DOC)


def test_lab_first_save_with_same_id_creates_only_callers_data(client, owner):
    graph = _graph("tenant-b-own-data")
    response = client.post(f"{BASE}/save", headers=_h("tenant-b"),
                           json={"graph": graph, "expected_head": None})
    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == "tenant-b"
    assert response.json()["revision"] == 1
    assert response.json()["graph"] == graph
    assert MARKER not in response.text
    assert "tenant-a" not in response.text
    for suffix in ("head", "revisions/1"):
        read = client.get(f"{BASE}/{suffix}", headers=_h("tenant-b"))
        assert read.status_code == 200, read.text
        assert read.json()["graph"] == graph
        assert read.json()["tenant_id"] == "tenant-b"
        assert MARKER not in read.text
    db, before = owner
    assert _owner_snapshot(db) == before


@pytest.mark.parametrize("method,suffix,body,wrong_status", LAB_CASES)
def test_lab_owner_control_reaches_each_route(
    client, owner, monkeypatch, method, suffix, body, wrong_status,
):
    # Ensure negative probes cannot pass merely because build/deploy is denied
    # or the fixture's ledger/route is unavailable for every caller.
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda subject: ("tenant-a", "hosted_pro"))
    response = client.request(method, f"{BASE}/{suffix}", json=body,
                              headers=_h("tenant-a"))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("setting,status,detail", [
    ("LEAF_LAB_SURFACE_ENABLED", 404,
     {"ok": False, "status": "refused", "reason": "lab_surface_disabled"}),
    ("LEAF_AUTH_LIVE", 503,
     {"ok": False, "status": "refused", "reason": "lab_auth_live_required"}),
])
@pytest.mark.parametrize("method,suffix,body,wrong_status", LAB_CASES)
def test_lab_parent_guards_precede_tenant_and_storage(
    client, owner, monkeypatch, method, suffix, body, wrong_status, setting, status, detail,
):
    monkeypatch.setenv(setting, "0")

    def forbidden(*args, **kwargs):
        pytest.fail("closed Lab guard reached tenant authority or persistence")

    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", forbidden)
    monkeypatch.setattr(ledger, "_run", forbidden)
    response = client.request(method, f"{BASE}/{suffix}", json=body,
                              headers=_h("tenant-b"))
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert MARKER not in response.text
    db, before = owner
    assert _owner_snapshot(db) == before
