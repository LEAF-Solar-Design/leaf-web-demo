"""P-183: missing unlisted drawings stay absent across bootstrap callers.

Only local temporary stores are used. Authority probes are stubbed, and route
admission uses server-owned TenantContext overrides rather than tier headers.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import write_loop
import store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "legacy")
    monkeypatch.setenv("LEAF_BLOB_STORE", "filesystem")
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(tmp_path / "tenant-tools"))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.setenv("APS_LIVE", "0")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    import deps

    monkeypatch.setattr(deps, "APS_LIVE", False)


def _files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def _objects(backend, root):
    if isinstance(backend, store.InMemoryBackend):
        return {key: backend.get(key) for key in backend.keys()}
    return _files(root)


def _missing(*args, **kwargs):
    raise KeyError("missing")


@pytest.fixture(params=["nowhere", "other-tenant"])
def missing_drawing(request, tmp_path):
    if request.param == "other-tenant":
        backend = store.FilesystemBackend(str(tmp_path / "drawings"))
        store.ingest_drawing(backend, "owner-tenant", str(write_loop.CACHED_INTAKE_PATH),
                             drawing_id="unlisted-roof")
    return "unlisted-roof"


def _client(tenant="scope-tenant"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    from envelopes import install_error_handlers
    from routers import drawings, session, search

    app = FastAPI()
    install_error_handlers(app)
    for router in (drawings.router, session.router, search.router):
        app.include_router(router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("tier", [
    "demo", "restricted", "self_hosted", "hosted_starter", "hosted_pro", "admin", "guest",
])
def test_bootstrap_scope_is_fixed_across_tenant_tiers(tmp_path, tier):
    from deps import TenantContext

    tenant = TenantContext("guest-scope" if tier == "guest" else "scope-tenant",
                           tier=tier, authority_resolved=True)
    with _client(tenant) as client:
        for did in ("demo", "rooftop_demo", "rooftop-demo"):
            before = _files(tmp_path)
            response = client.get(f"/api/drawings/{did}/versions")
            assert response.status_code == (404 if tier == "guest" else 200), response.text
            if tier == "guest":
                assert _files(tmp_path) == before
            else:
                assert response.json()["head"] == 1
        before = _files(tmp_path)
        response = client.get("/api/drawings/unlisted-roof/versions")
        assert response.status_code == 404, response.text
        assert response.json()["error"]["error_code"] == "BAD_PARAMS"
        assert _files(tmp_path) == before


@pytest.mark.parametrize("kind", ["filesystem", "memory"])
@pytest.mark.parametrize("did", ["unlisted-roof", "a", "roof_123"])
def test_missing_unlisted_id_refuses_before_ingest_or_lock(tmp_path, monkeypatch, kind, did):
    backend = (store.InMemoryBackend() if kind == "memory"
               else store.FilesystemBackend(str(tmp_path / "drawings")))
    before = _objects(backend, tmp_path)
    ingest = Mock(side_effect=AssertionError("must not ingest"))
    temporary = Mock(side_effect=AssertionError("must not stage source bytes"))
    source = Mock()
    source.read_bytes.side_effect = AssertionError("must not read source")
    guard = Mock(side_effect=AssertionError("must not enter mutation guard"))
    lock = Mock(side_effect=AssertionError("must not lock"))
    monkeypatch.setattr(store, "ingest_drawing", ingest)
    monkeypatch.setattr(write_loop.tempfile, "mkstemp", temporary)
    monkeypatch.setattr(write_loop, "CACHED_INTAKE_PATH", source)
    monkeypatch.setattr(write_loop, "DEMO_DWG_PATH", source)
    monkeypatch.setattr(write_loop, "drawing_mutation_refusal_guard", guard)
    monkeypatch.setattr(backend, "cross_process_lock", lock)
    with pytest.raises(KeyError):
        write_loop.ensure_demo_drawing(backend, "scope-tenant", did)
    ingest.assert_not_called()
    temporary.assert_not_called()
    source.read_bytes.assert_not_called()
    guard.assert_not_called()
    lock.assert_not_called()
    assert _objects(backend, tmp_path) == before
    assert _files(tmp_path) == {}


def test_postgres_existing_unlisted_manifest_bypasses_allowlist(tmp_path, monkeypatch):
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    lookup = Mock(return_value={"drawing_id": "custom-roof", "head": 3})
    ingest = Mock(side_effect=AssertionError("existing authority must return"))
    monkeypatch.setattr(store, "authority_mode", lambda: "postgres")
    monkeypatch.setattr(store, "load_manifest", lookup)
    monkeypatch.setattr(store, "ingest_drawing", ingest)
    write_loop.ensure_demo_drawing(backend, "scope-tenant", "custom-roof")
    lookup.assert_called_once_with(backend, "scope-tenant", "custom-roof")
    ingest.assert_not_called()
    assert not backend.exists(store.manifest_key("scope-tenant", "custom-roof"))
    assert _files(tmp_path) == {}


def test_postgres_missing_unlisted_manifest_never_ingests(tmp_path, monkeypatch):
    import guest_uploads

    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    lookup = Mock(side_effect=_missing)
    marker = Mock(return_value=None)
    ingest = Mock(side_effect=AssertionError("missing unlisted authority must refuse"))
    monkeypatch.setattr(store, "authority_mode", lambda: "postgres")
    monkeypatch.setattr(store, "load_manifest", lookup)
    monkeypatch.setattr(guest_uploads, "upload_store_mode", lambda: "postgres")
    monkeypatch.setattr(guest_uploads, "read_marker", marker)
    monkeypatch.setattr(store, "ingest_drawing", ingest)
    with pytest.raises(KeyError):
        write_loop.ensure_demo_drawing(backend, "scope-tenant", "custom-roof")
    lookup.assert_called_once_with(backend, "scope-tenant", "custom-roof")
    marker.assert_called_once_with(backend, "scope-tenant", "custom-roof")
    ingest.assert_not_called()
    assert _files(tmp_path) == {}


@pytest.mark.parametrize("authority", ["compatibility", "postgres"])
@pytest.mark.parametrize("did", ["demo", "unlisted-roof"])
def test_pending_upload_guard_precedes_bootstrap_allowlist(tmp_path, monkeypatch, authority, did):
    import guest_uploads

    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    if authority == "postgres":
        monkeypatch.setattr(store, "authority_mode", lambda: "postgres")
        monkeypatch.setattr(store, "load_manifest", _missing)
        monkeypatch.setattr(guest_uploads, "upload_store_mode", lambda: "postgres")
        monkeypatch.setattr(guest_uploads, "read_marker", lambda *a: {"status": "extracting"})
    else:
        backend.put(write_loop.upload_marker_key("scope-tenant", did), b'{"status":"extracting"}')
    before = _files(tmp_path)
    ingest = Mock(side_effect=AssertionError("pending upload must not ingest"))
    monkeypatch.setattr(store, "ingest_drawing", ingest)
    with pytest.raises(ValueError, match="/upload-status"):
        write_loop.ensure_demo_drawing(backend, "scope-tenant", did)
    ingest.assert_not_called()
    assert _files(tmp_path) == before


def test_unknown_session_and_search_do_not_materialize(tmp_path, missing_drawing):
    from routers.search import _version_rows

    before = _files(tmp_path)
    with _client() as client:
        response = client.get("/api/session", params={"dwg": "unlisted-roof"})
        assert response.status_code == 404, response.text
        assert response.json()["error"]["error_code"] == "BAD_PARAMS"
        response = client.get("/api/search", params={"q": "initial ingest", "drawing_id": "unlisted-roof"})
        assert response.status_code == 200, response.text
        assert not any(row["kind"] == "version" for row in response.json()["results"])
    assert _version_rows("scope-tenant", "unlisted-roof", "") == []
    assert _files(tmp_path) == before


def test_unknown_legacy_job_head_does_not_materialize(tmp_path, missing_drawing):
    from routers.jobs import _legacy_drawing_head

    before = _files(tmp_path)
    with pytest.raises(KeyError):
        _legacy_drawing_head("scope-tenant", "unlisted-roof")
    assert _files(tmp_path) == before


def test_unknown_broker_mock_read_does_not_materialize(tmp_path, monkeypatch, missing_drawing):
    import broker

    monkeypatch.setattr(broker, "_demo_denial", lambda *a: None)
    monkeypatch.setattr(broker, "tenant_disabled", lambda *a: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_tenant_tier", lambda *a: "demo")
    monkeypatch.setattr(broker, "_deployed_runtime", lambda: False)
    runner = Mock(side_effect=AssertionError("missing drawing must not run tool"))
    monkeypatch.setattr(broker, "run_tool_dynamic", runner)
    monkeypatch.setattr(broker, "_get_da", Mock(side_effect=AssertionError("no APS")))
    tool = {"name": "count-by-layer", "version": "1.0.0", "capabilities": [], "kind": "script"}
    req = broker.BrokerRunRequest(tenant_id="scope-tenant", tool=tool,
                                  dwg="unlisted-roof", aps_live=False)
    before = _files(tmp_path)
    env, status = broker._execute(req, tool, "count_by_layer", time.perf_counter(), {})
    assert status == 400
    assert env["error"]["error_code"] == "BAD_PARAMS"
    assert env["error"]["reason_code"] == "uploaded_resolution_invalid"
    runner.assert_not_called()
    assert _files(tmp_path) == before


def test_unknown_mock_write_does_not_run_tool_or_materialize(tmp_path, missing_drawing):
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    runner = Mock(side_effect=AssertionError("missing drawing must not run tool"))
    before = _files(tmp_path)
    env, status = write_loop.run_write_mock(
        {"name": "move-panels", "version": "1.0.0"}, {"drawing_id": "unlisted-roof"},
        "scope-tenant", backend=backend, t0=time.perf_counter(), run_tool_dynamic_fn=runner)
    assert status == 400
    assert env["error"]["error_code"] == "BAD_PARAMS"
    runner.assert_not_called()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("route", ["edited", "plan"])
def test_unknown_edit_routes_do_not_materialize(tmp_path, monkeypatch, route, missing_drawing):
    from routers import drawings

    data = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n8\nRoof\n90\n3\n70\n1\n"
            "10\n0\n20\n0\n10\n50\n20\n0\n10\n50\n20\n30\n0\nENDSEC\n0\nEOF\n").encode("ascii")
    # Observe the real parser succeeding, so a payload rejection cannot satisfy
    # this absence test accidentally.
    receive = Mock(wraps=drawings._receive_edited_dxf)
    monkeypatch.setattr(drawings, "_receive_edited_dxf", receive)
    form = {"parent_version": "1", "source_digest": hashlib.sha256(data).hexdigest()}
    if route == "plan":
        form["plan"] = json.dumps({"mutations": {}})
    before = _files(tmp_path)
    with _client() as client:
        response = client.post(f"/api/drawings/unlisted-roof/versions/{route}",
                               files={"file": ("edited.dxf", io.BytesIO(data), "application/dxf")},
                               data=form)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert response.json()["error"]["message"] == "drawing unavailable or malformed request"
    receive.assert_called_once()
    assert _files(tmp_path) == before


def test_unknown_checkout_release_does_not_materialize(tmp_path, missing_drawing):
    before = _files(tmp_path)
    with _client() as client:
        response = client.delete("/api/drawings/unlisted-roof/checkout")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert not response.json().get("checkout")
    assert _files(tmp_path) == before
