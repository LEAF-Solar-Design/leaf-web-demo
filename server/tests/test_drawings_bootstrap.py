"""
Binary acceptance for P-183: curated drawing bootstrap.

Only demo, rooftop_demo, and rooftop-demo may bootstrap on first touch.
Unlisted missing IDs stay absent; explicitly ingested custom drawings retain
their real bytes. Aliases remain separate literal store keys.

Covers:
  1. a brand-new curated drawing_id bootstraps on first touch -> GET .../versions
     200, v1 present, filesystem layout tenants/<t>/drawings/<id>/manifest.json
  2. `demo`'s bootstrap is untouched: same golden 2345-polyline cached intake,
     v1/head/latest all 1, "initial ingest" note
  3. all curated aliases (same tenant) ingest the IDENTICAL source bytes
     (sha256-equal v1) -> proves the SAME ingest path, not a parallel one
  4. path-traversal / malformed ids are rejected -- both at the pure-function
     level (write_loop.ensure_demo_drawing directly) and end-to-end over HTTP
     (404 on the GET routes, 400 on undo/redo, matching routers/drawings.py's
     existing (KeyError, ValueError) -> error_response mapping, UNCHANGED)

Hermetic: in-process TestClient wrapping only the drawings router, an isolated
LEAF_STORE_DIR per test (tmp_path), APS_LIVE=0 (FilesystemBackend, no APS/da
credential, no network). No broker, no LLM/Agent-SDK call.

Run:  cd server && python -m pytest tests/test_drawings_bootstrap.py -q
"""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import write_loop  # noqa: E402
from tenant_id_validator import validate_tenant_id  # noqa: E402

CACHED_POLYLINE_COUNT = 2345  # golden count of data/rooftop_demo.intake.json (test_write_loop.py parity)


# --------------------------------------------------------------------------- #
# HTTP harness: minimal FastAPI app wrapping just the drawings router
# --------------------------------------------------------------------------- #
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


def _store_root(tmp_path) -> Path:
    return tmp_path / "drawings"


# --------------------------------------------------------------------------- #
# 1. a fresh, never-seen, valid drawing_id auto-bootstraps
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("did", ["demo", "rooftop_demo", "rooftop-demo"])
def test_fresh_id_bootstraps_versions_200(client, tmp_path, did):
    t = "bootstrap-fresh"

    r = client.get(f"/api/drawings/{did}/versions", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is None
    assert body["head"] == 1 and body["latest"] == 1
    assert len(body["versions"]) == 1
    v1 = body["versions"][0]
    assert v1["v"] == 1 and v1["parent"] is None
    assert v1["note"] == "initial ingest"
    assert body["checkout"] is None

    # per-tenant, per-drawing filesystem layout: tenants/<t>/drawings/<id>/...
    manifest_path = _store_root(tmp_path) / "tenants" / t / "drawings" / did / "manifest.json"
    assert manifest_path.is_file(), manifest_path

    # intake actually carries the cached demo payload
    ir = client.get(f"/api/drawings/{did}/intake", params={"version": "head"}, headers=_h(t))
    assert ir.status_code == 200, ir.text
    intake = ir.json()
    assert intake["version"] == 1 and intake["head"] == 1 and intake["latest"] == 1
    assert len(intake["intake"]["polylines"]) == CACHED_POLYLINE_COUNT


@pytest.mark.parametrize("did", ["demo", "rooftop_demo", "rooftop-demo"])
def test_fresh_id_bootstraps_only_once(client, did):
    """A second touch of the same fresh id must NOT re-ingest (still v1/head/latest=1)."""
    t = "bootstrap-idempotent"

    first = client.get(f"/api/drawings/{did}/versions", headers=_h(t)).json()
    second = client.get(f"/api/drawings/{did}/versions", headers=_h(t)).json()
    assert first == second
    assert second["head"] == 1 and second["latest"] == 1 and len(second["versions"]) == 1


def test_summary_is_bounded_and_omits_raw_geometry(client):
    response = client.get(
        "/api/drawings/rooftop_demo/summary",
        headers=_h("bootstrap-summary"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entity_total"] == CACHED_POLYLINE_COUNT
    assert body["version"] == body["head"] == body["latest"] == 1
    assert isinstance(body["layers"], list)
    assert "intake" not in body
    assert "polylines" not in body
    assert len(response.text) < 10_000


def test_live_demo_summary_uses_cached_intake_without_aps_credentials(
    client, monkeypatch
):
    from routers import drawings as drawings_router

    monkeypatch.setattr(drawings_router.deps, "APS_LIVE", True)

    def fail_if_aps_client_is_loaded():
        raise AssertionError("the app process must not load APS credentials")

    monkeypatch.setattr(
        drawings_router.deps,
        "get_da_client",
        fail_if_aps_client_is_loaded,
    )

    response = client.get(
        "/api/drawings/rooftop_demo/summary",
        headers=_h("bootstrap-live-summary"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entity_total"] == CACHED_POLYLINE_COUNT
    assert body["version"] == body["head"] == body["latest"] == 1


# --------------------------------------------------------------------------- #
# 2 + 3. `demo` stays byte-identical; a fresh id ingests the SAME bytes via the
#         SAME store.ingest_drawing call demo has always taken
# --------------------------------------------------------------------------- #
def test_demo_bootstrap_unchanged(client):
    t = "bootstrap-demo-unchanged"

    r = client.get(f"/api/drawings/{write_loop.DEMO_DRAWING_ID}/versions", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["head"] == 1 and body["latest"] == 1
    assert len(body["versions"]) == 1
    assert body["versions"][0] == {
        "v": 1, "parent": None,
        "created": body["versions"][0]["created"],  # timestamp, not asserted verbatim
        "bytes": body["versions"][0]["bytes"],
        "sha256": body["versions"][0]["sha256"],
        "tool": None, "workitem_id": None, "note": "initial ingest",
        # slice 6a: an ingest has no authored-tool receipt, so no provenance.
        "source_ref": None,
    }

    ir = client.get(f"/api/drawings/{write_loop.DEMO_DRAWING_ID}/intake",
                    params={"version": "head"}, headers=_h(t))
    intake = ir.json()
    assert intake["version"] == 1 and intake["head"] == 1 and intake["latest"] == 1
    assert len(intake["intake"]["polylines"]) == CACHED_POLYLINE_COUNT


def test_allowed_demo_ids_ingest_identical_bytes(client, tmp_path):
    """Curated aliases ingest identical bytes under distinct literal keys."""
    import store

    t = "bootstrap-parity"
    raw = write_loop.CACHED_INTAKE_PATH.read_bytes()
    backend = store.FilesystemBackend(str(_store_root(tmp_path)))
    keys = set()
    for did in ("demo", "rooftop_demo", "rooftop-demo"):
        response = client.get(f"/api/drawings/{did}/versions", headers=_h(t))
        assert response.status_code == 200, response.text
        v1 = response.json()["versions"][0]
        assert v1["sha256"] == hashlib.sha256(raw).hexdigest()
        assert v1["bytes"] == len(raw)
        assert v1["note"] == "initial ingest"
        assert store.load_manifest(backend, t, did)["drawing_id"] == did
        _, key = store.resolve_version(backend, t, did, 1)
        keys.add(key)
        assert backend.get(key) == raw
    assert len(keys) == 3


def _snapshot(tmp_path):
    return {p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("did", ["my-fresh-drawing", "some-other-drawing", "project-drawing"])
def test_unknown_slug_reads_return_404_without_materializing(client, tmp_path, did):
    before = _snapshot(tmp_path)
    for _ in range(2):
        for route in ("versions", "intake", "summary", "dxf"):
            response = client.get(f"/api/drawings/{did}/{route}", headers=_h("missing"))
            assert response.status_code == 404, response.text
            assert response.json()["error"]["error_code"] == "BAD_PARAMS"
            assert _snapshot(tmp_path) == before


def test_existing_unlisted_drawing_is_read_without_bootstrap(client, tmp_path):
    import store

    backend = store.FilesystemBackend(str(_store_root(tmp_path)))
    intake = json.loads(write_loop.CACHED_INTAKE_PATH.read_text(encoding="utf-8"))
    intake["polylines"] = intake["polylines"][:1]
    source = tmp_path / "custom.intake.json"
    source.write_text(json.dumps(intake), encoding="utf-8")
    store.ingest_drawing(backend, "existing", str(source), drawing_id="custom-roof")
    before = _snapshot(tmp_path)
    response = client.get("/api/drawings/custom-roof/intake", headers=_h("existing"))
    assert response.status_code == 200, response.text
    assert response.json()["intake"] == intake
    versions = client.get("/api/drawings/custom-roof/versions", headers=_h("existing"))
    assert versions.json()["head"] == versions.json()["latest"] == 1
    assert _snapshot(tmp_path) == before


def test_live_bootstrap_stores_real_dwg_with_bound_intake(tmp_path, monkeypatch):
    """A fresh live project drawing is a real APS input, not JSON mislabeled
    as HostDwg. Its read surface comes from the source-bound intake cache.
    """
    import store  # noqa: PLC0415

    monkeypatch.setenv("APS_LIVE", "1")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))

    write_loop.ensure_demo_drawing(
        backend, "live-bootstrap", "rooftop-demo")

    version, key = store.resolve_version(
        backend, "live-bootstrap", "rooftop-demo", "head")
    assert version == 1
    assert backend.get(key) == write_loop.DEMO_DWG_PATH.read_bytes()
    read_version, intake = write_loop.read_intake(
        backend, "live-bootstrap", "rooftop-demo", "head")
    assert read_version == 1
    assert len(intake["polylines"]) == CACHED_POLYLINE_COUNT


def test_legacy_live_bootstrap_bridges_exact_tracked_intake_to_dwg(
        tmp_path, monkeypatch):
    """An existing pre-fix v1 remains immutable but executes from its exact,
    repository-bound DWG counterpart.
    """
    import store  # noqa: PLC0415

    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    backend = store.InMemoryBackend()
    source = write_loop.CACHED_INTAKE_PATH.read_bytes()
    legacy_source = tmp_path / "legacy.intake.json"
    legacy_source.write_bytes(source)
    ingested = store.ingest_drawing(
        backend, "legacy-live", str(legacy_source), drawing_id="project-drawing")
    assert ingested == {"drawing_id": "project-drawing", "version": 1}
    version, key = store.resolve_version(
        backend, "legacy-live", "project-drawing", "head")
    assert version == 1
    stored_source = backend.get(key)

    execution_source, bridged = write_loop._live_execution_source_bytes(
        stored_source)

    assert bridged is True
    assert execution_source == write_loop.DEMO_DWG_PATH.read_bytes()
    assert backend.get(key) == source


def test_live_write_rejects_unbound_json_source():
    with pytest.raises(ValueError, match="without a canonical DWG binding"):
        write_loop._live_execution_source_bytes(b'{"not":"the tracked intake"}')


# --------------------------------------------------------------------------- #
# 4. path-traversal / malformed drawing_id is rejected, not auto-bootstrapped
# --------------------------------------------------------------------------- #
BAD_IDS = ["..", ".", "", "foo.bar", "UPPERCASE", "has space", "a" * 64]


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_ensure_demo_drawing_rejects_path_y_ids_directly(bad_id):
    """Pure-function level: no HTTP/URL-normalization ambiguity for ids like '..'."""
    import store  # noqa: PLC0415

    backend = store.InMemoryBackend()
    with pytest.raises(ValueError):
        write_loop.ensure_demo_drawing(backend, "some-tenant", bad_id)
    assert backend.keys() == []  # nothing was written


def test_validate_tenant_id_rejects_the_same_bad_ids_write_loop_uses():
    for bad_id in BAD_IDS:
        with pytest.raises(ValueError):
            validate_tenant_id(bad_id, kind="drawing id")


def test_path_y_id_rejected_over_http_versions_404(client):
    # a single URL path segment containing '.' -- invalid per the slug rule, and
    # (unlike a literal '..' segment) never subject to client-side dot-segment
    # normalization, so this exercises the real routed 404 end to end.
    r = client.get("/api/drawings/foo.bar/versions", headers=_h("bootstrap-badid"))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_path_y_id_rejected_over_http_intake_404(client):
    r = client.get("/api/drawings/foo.bar/intake", headers=_h("bootstrap-badid-2"))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_path_y_id_rejected_over_http_undo_400(client):
    r = client.post("/api/drawings/foo.bar/undo", headers=_h("bootstrap-badid-3"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_path_y_id_never_created_a_manifest_on_disk(client, tmp_path):
    t = "bootstrap-badid-4"
    client.get("/api/drawings/foo.bar/versions", headers=_h(t))
    tenant_dir = _store_root(tmp_path) / "tenants" / t
    assert not tenant_dir.exists()  # rejected before any store write happened


def _corrupt_manifest(client, tmp_path, tenant: str, drawing_id: str) -> None:
    import store

    backend = store.FilesystemBackend(str(_store_root(tmp_path)))
    store.ingest_drawing(backend, tenant, str(write_loop.CACHED_INTAKE_PATH), drawing_id=drawing_id)
    manifest_path = (
        _store_root(tmp_path)
        / "tenants"
        / tenant
        / "drawings"
        / drawing_id
        / "manifest.json"
    )
    manifest_path.write_text("{truncated", encoding="utf-8")


def test_versions_reports_corrupt_manifest_as_server_fault(client, tmp_path):
    tenant, drawing_id = "bootstrap-corrupt-read", "damaged-drawing"
    _corrupt_manifest(client, tmp_path, tenant, drawing_id)

    response = client.get(
        f"/api/drawings/{drawing_id}/versions",
        headers=_h(tenant),
    )

    assert response.status_code == 500, response.text
    assert response.json()["error"]["error_code"] == "INTERNAL"


def test_release_reports_corrupt_manifest_as_server_fault(client, tmp_path):
    tenant, drawing_id = "bootstrap-corrupt-release", "damaged-drawing"
    _corrupt_manifest(client, tmp_path, tenant, drawing_id)

    response = client.delete(
        f"/api/drawings/{drawing_id}/checkout",
        headers=_h(tenant),
    )

    assert response.status_code == 500, response.text
    assert response.json()["error"]["error_code"] == "INTERNAL"
