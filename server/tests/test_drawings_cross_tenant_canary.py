"""HTTP canaries for access to another tenant's drawing."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import write_loop  # noqa: E402
import store  # noqa: E402

MARKER = b"LEAF-CANARY-A-V2-0123456789abcdef"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "legacy")
    monkeypatch.setenv("LEAF_BLOB_STORE", "filesystem")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.delenv("APS_LIVE", raising=False)  # APS_LIVE off -> FilesystemBackend, no da/creds
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)  # legacy X-Tenant-Id header stub

    from routers import drawings as drawings_router  # noqa: PLC0415 (import after env is set)
    from envelopes import install_error_handlers  # noqa: PLC0415

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _owner_hashes(tmp_path):
    root = tmp_path / "drawings" / "tenants" / "tenant-a"
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def _seed(tmp_path):
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    store.ingest_drawing(backend, "tenant-a", str(write_loop.CACHED_INTAKE_PATH), drawing_id="canary-a")
    # _put_bytes_version stages bytes for store.put_drawing, which appends an
    # immutable version and advances head via store.save_manifest.
    version = write_loop._put_bytes_version(
        backend, "tenant-a", "canary-a",
        write_loop.DEMO_DWG_PATH.read_bytes() + b"\n" + MARKER,
        parent_version=1, meta={"tool": "cross-tenant-canary"})
    assert version == 2
    assert store.load_manifest(backend, "tenant-a", "canary-a")["head"] == 2
    hashes = _owner_hashes(tmp_path)
    assert hashes
    return hashes


def test_owner_sees_both_versions(client, tmp_path):
    _seed(tmp_path)
    response = client.get("/api/drawings/canary-a/versions", headers=_h("tenant-a"))
    assert response.status_code == 200
    body = response.json()
    assert [row["v"] for row in body["versions"]] == [1, 2]
    assert body["head"] == 2


def test_other_tenant_read_bootstraps_its_own_copy_known_behaviour(client, tmp_path):
    """P-183: an unlisted ID in another tenant no longer creates a demo copy."""
    _seed(tmp_path)
    before = _store_hashes(tmp_path)
    response = client.get("/api/drawings/canary-a/summary", headers=_h("tenant-b"))
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert MARKER not in response.content and b"tenant-a" not in response.content
    assert not (tmp_path / "drawings" / "tenants" / "tenant-b" / "drawings" / "canary-a").exists()
    assert _store_hashes(tmp_path) == before


def _store_hashes(tmp_path):
    return {p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tmp_path.rglob("*") if p.is_file()}


def test_other_tenant_versions_never_lists_owner_version(client, tmp_path):
    _seed(tmp_path)
    before = _store_hashes(tmp_path)
    response = client.get("/api/drawings/canary-a/versions", headers=_h("tenant-b"))
    assert response.status_code == 404
    assert MARKER not in response.content, response.status_code
    assert b"tenant-a" not in response.content, response.status_code
    assert _store_hashes(tmp_path) == before


def test_other_tenant_summary_graph_mapping_intake_never_carry_marker(client, tmp_path):
    _seed(tmp_path)
    owner = client.get("/api/drawings/canary-a/intake", params={"version": 1},
                       headers=_h("tenant-a"))
    assert owner.status_code == 200
    owner_intake = owner.json()["intake"]
    entity_total = sum(len(owner_intake.get(key) or [])
                       for key in ("polylines", "inserts", "faces3d"))
    assert entity_total > 0
    before = _store_hashes(tmp_path)
    responses = {route: client.get(f"/api/drawings/canary-a/{route}",
                            params={"project_id": "canary-project"}, headers=_h("tenant-b"))
                 for route in ("summary", "graph", "mapping", "intake")}
    for response in responses.values():
        assert response.status_code == 404, response.text
        assert MARKER not in response.content, response.status_code
        assert b"tenant-a" not in response.content, response.status_code
    for route in ("graph", "mapping"):
        assert responses[route].status_code == 404
        assert responses[route].json()["error"]["message"] == f"drawing {route} unavailable"
    assert _store_hashes(tmp_path) == before


def test_other_tenant_dxf_never_carries_marker(client, tmp_path):
    _seed(tmp_path)
    before = _store_hashes(tmp_path)
    response = client.get("/api/drawings/canary-a/dxf", headers=_h("tenant-b"))
    assert response.status_code == 404
    assert MARKER not in response.content, response.status_code
    assert b"tenant-a" not in response.content, response.status_code
    assert _store_hashes(tmp_path) == before


def test_other_tenant_checkout_unknown_id_is_404_and_creates_nothing(client, tmp_path):
    owner_before = _seed(tmp_path)
    before = _store_hashes(tmp_path)
    response = client.post("/api/drawings/canary-a/checkout", headers=_h("tenant-b"))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert not response.json().get("checkout_capability")
    assert not (tmp_path / "drawings" / "tenants" / "tenant-b").exists()
    assert _store_hashes(tmp_path) == before
    assert _owner_hashes(tmp_path) == owner_before
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    assert store.load_manifest(backend, "tenant-a", "canary-a")["head"] == 2


def _other_tenant_writes(client):
    return [client.post(f"/api/drawings/canary-a/{route}", headers=_h("tenant-b"))
            for route in ("undo", "redo", "checkout", "versions/1/restore")]


def test_other_tenant_writes_never_touch_owner_files(client, tmp_path):
    before = _seed(tmp_path)
    responses = _other_tenant_writes(client)
    statuses = [response.status_code for response in responses]
    assert statuses == [400, 400, 404, 400]
    assert _owner_hashes(tmp_path) == before, statuses
    response = client.get("/api/drawings/canary-a/versions", headers=_h("tenant-a"))
    assert response.json()["head"] == 2, statuses


def test_other_tenant_writes_create_files_only_under_own_prefix(client, tmp_path):
    owner_before = _seed(tmp_path)
    root = tmp_path / "drawings"
    before = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    responses = _other_tenant_writes(client)
    after = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    statuses = [response.status_code for response in responses]
    assert _owner_hashes(tmp_path) == owner_before, statuses
    assert after == before, statuses


def test_other_tenant_restore_of_owner_version_is_not_visible_to_owner(client, tmp_path):
    """No HTTP route serves raw head bytes; check via resolve_version and backend.get."""
    before = _seed(tmp_path)
    response = client.post("/api/drawings/canary-a/versions/1/restore", headers=_h("tenant-b"))
    assert _owner_hashes(tmp_path) == before, response.status_code
    owner = client.get("/api/drawings/canary-a/versions", headers=_h("tenant-a"))
    assert owner.json()["head"] == 2, response.status_code
    # get_dxf transforms the head into DXF; drawings.py has no raw-head-byte
    # GET route. Resolve the unchanged head through the store instead.
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    version, key = store.resolve_version(backend, "tenant-a", "canary-a", "head")
    assert version == 2
    assert MARKER in backend.get(key)


def test_traversal_tenant_headers_never_200_and_create_nothing(client, tmp_path):
    _seed(tmp_path)
    for tenant in ("../tenant-a", "tenant-a/", "TENANT-A", "tenant-a%2f"):
        response = client.get("/api/drawings/canary-a/summary", headers=_h(tenant))
        assert response.status_code in {400, 404}
    tenants_root = tmp_path / "drawings" / "tenants"
    assert {path.name for path in tenants_root.iterdir() if path.is_dir()} == {"tenant-a"}


def test_artifact_seeded_for_owner_is_not_readable_by_other_tenant(client, tmp_path):
    import solar_artifacts

    _seed(tmp_path)
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    # Import routes require a Solar graph, absent from the demo bootstrap.
    # ArtifactSink writes the metadata and blob read by drawings.get_artifact
    # through solar_artifacts.read_artifact, under the owner's drawing prefix.
    sink = solar_artifacts.ArtifactSink(
        backend, "tenant-a", "canary-a",
        {"project_id": "canary-project", "resolved_version": 2,
         "graph_sha256": hashlib.sha256(b"canary-graph-binding").hexdigest()},
        "cross-tenant-canary", hashlib.sha256(MARKER).hexdigest(), verify=False)
    prepared = sink.prepare(solar_artifacts.ArtifactOutput(
        {}, "text/csv", "canary.csv", b"marker\n" + MARKER + b"\n"))
    artifact_id = sink.finish(prepared)["artifact_id"]
    owner = client.get(f"/api/drawings/canary-a/artifacts/{artifact_id}", headers=_h("tenant-a"))
    assert owner.status_code == 200
    assert MARKER in owner.content
    response = client.get(f"/api/drawings/canary-a/artifacts/{artifact_id}", headers=_h("tenant-b"))
    assert response.status_code != 200
    assert MARKER not in response.content, response.status_code
