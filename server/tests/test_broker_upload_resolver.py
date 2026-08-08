"""
Broker upload resolver (§19): _resolve_upload_dwg must apply the IDENTICAL
strictness as the library resolver — bare names only, no traversal, no
symlinks, parent must BE the uploads root — and the two namespaces must never
cross-resolve. Plus the /broker/extract {upload: true} endpoint wiring.

Run:  cd server && python -m pytest tests/test_broker_upload_resolver.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import broker
import guest_uploads
import store
import write_loop


@pytest.fixture()
def uploads(monkeypatch, tmp_path):
    updir = tmp_path / "uploads"
    updir.mkdir()
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(updir))
    return updir


def _live_upload_backend(
        tmp_path, tenant="tenant-owner", drawing="u-owned", *, marker=True):
    backend = store.InMemoryBackend()
    source = b"AC1032" + b"\x00" * 64
    local = tmp_path / "source.dwg"
    local.write_bytes(source)
    store.ingest_drawing(backend, tenant, str(local), drawing_id=drawing)
    intake_ref = write_loop.publish_intake_cache(
        backend, tenant, drawing, 1, source, {"polylines": []})
    if marker:
        upload_marker = guest_uploads.new_marker(
            filename="source.dwg",
            data=source,
            tenant_kind="account",
            source_ext=".dwg",
        )
        upload_marker.update(
            status="ready",
            extracted_version=1,
            intake_ref=intake_ref,
            intake_sha256=hashlib.sha256(backend.get(intake_ref)).hexdigest(),
        )
        backend.put(
            write_loop.upload_marker_key(tenant, drawing),
            json.dumps(upload_marker).encode(),
        )
    return backend, source


def _live_request(tenant="tenant-owner", drawing="u-owned", version=1):
    return broker.BrokerRunRequest(
        tenant_id=tenant,
        tool={"name": "count-by-layer"},
        params={},
        dwg=drawing,
        dwg_version=version,
        aps_live=True,
    )


def test_live_versioned_read_materializes_tenant_owned_dwg(tmp_path, monkeypatch):
    backend, source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)

    resolved, temporary = broker._resolve_live_read_dwg(_live_request())
    try:
        assert temporary is True
        assert resolved.suffix == ".dwg"
        assert resolved.read_bytes() == source
    finally:
        resolved.unlink(missing_ok=True)


def test_live_versioned_read_is_tenant_bound(tmp_path, monkeypatch):
    backend, _source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)

    with pytest.raises(ValueError, match="no readable upload marker"):
        broker._resolve_live_read_dwg(_live_request(tenant="tenant-intruder"))


def test_live_versioned_read_never_falls_back_to_curated_name(tmp_path, monkeypatch):
    backend = store.InMemoryBackend()
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)

    with pytest.raises(ValueError, match="no readable upload marker"):
        broker._resolve_live_read_dwg(
            _live_request(drawing="rooftop_demo", version=1))


@pytest.mark.parametrize("marker_bytes", [None, b"{not-json", b"[]"])
def test_live_versioned_read_requires_readable_upload_marker(
        tmp_path, monkeypatch, marker_bytes):
    backend, _source = _live_upload_backend(tmp_path, marker=False)
    if marker_bytes is not None:
        backend.put(
            write_loop.upload_marker_key("tenant-owner", "u-owned"), marker_bytes)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)

    with pytest.raises(ValueError, match="no readable upload marker"):
        broker._resolve_live_read_dwg(_live_request())


def test_live_versioned_read_requires_ready_dwg_upload(tmp_path, monkeypatch):
    backend, _source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    backend.put(
        write_loop.upload_marker_key("tenant-owner", "u-owned"),
        json.dumps({"status": "extracting", "source_ext": ".dwg"}).encode(),
    )

    with pytest.raises(ValueError, match="not ready"):
        broker._resolve_live_read_dwg(_live_request())


def test_live_versioned_read_rejects_non_dwg_upload(tmp_path, monkeypatch):
    backend, _source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    backend.put(
        write_loop.upload_marker_key("tenant-owner", "u-owned"),
        json.dumps({"status": "ready", "source_ext": ".dxf"}).encode(),
    )

    with pytest.raises(ValueError, match="need a DWG source"):
        broker._resolve_live_read_dwg(_live_request())


def test_live_run_submits_exact_versioned_bytes_and_cleans_temp(
        tmp_path, monkeypatch):
    backend, source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)

    class FakeDA:
        @staticmethod
        def run_tool(*_args, **_kwargs):
            raise AssertionError("broker wrapper must own the live call")

    submitted = {}

    def fake_run_live(_da, local, _tool, _params, *, on_submitted=None):
        path = Path(local)
        submitted["path"] = path
        submitted["bytes"] = path.read_bytes()
        return {"ok": True, "result": {}, "overlay": None}

    monkeypatch.setattr(broker, "_get_da", lambda: FakeDA())
    monkeypatch.setattr(broker, "_live_script_is_nonempty", lambda *_args: True)
    monkeypatch.setattr(broker, "_run_live_tool", fake_run_live)

    response = broker.broker_run(_live_request())

    assert response.status_code == 200, response.body
    assert submitted["bytes"] == source
    assert not submitted["path"].exists()


def test_live_run_maps_unreadable_source_proof_to_retryable_503(
        tmp_path, monkeypatch):
    backend, _source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    monkeypatch.setattr(
        write_loop,
        "read_intake",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            write_loop.ProofStateUnreadable("proof store unavailable")),
    )
    monkeypatch.setattr(
        broker,
        "_get_da",
        lambda: pytest.fail("proof failure must happen before APS access"),
    )

    response = broker.broker_run(_live_request())
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is True


def test_live_run_cleans_temp_when_da_loading_fails(tmp_path, monkeypatch):
    backend, _source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    real_resolver = broker._resolve_live_read_dwg
    materialized = {}

    def tracking_resolver(req):
        path, temporary = real_resolver(req)
        materialized["path"] = path
        return path, temporary

    monkeypatch.setattr(broker, "_resolve_live_read_dwg", tracking_resolver)
    monkeypatch.setattr(
        broker, "_get_da", lambda: (_ for _ in ()).throw(RuntimeError("DA load")))

    response = broker.broker_run(_live_request())

    assert response.status_code == 500
    assert not materialized["path"].exists()


def test_live_resolver_cleans_partial_temp_when_write_fails(
        tmp_path, monkeypatch):
    backend, _source = _live_upload_backend(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    created = {}
    real_mkstemp = broker.tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created["path"] = Path(name)
        return fd, name

    class BrokenWriter:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            return self

        def write(self, _data):
            raise OSError("disk full")

        def __exit__(self, *_args):
            os.close(self.fd)

    monkeypatch.setattr(broker.tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(
        broker.os, "fdopen", lambda fd, _mode: BrokenWriter(fd))

    with pytest.raises(OSError, match="disk full"):
        broker._resolve_live_read_dwg(_live_request())
    assert not created["path"].exists()


def test_happy_resolve_dwg(uploads):
    (uploads / "t1--u-abc123.dwg").write_bytes(b"AC1032data")
    p = broker._resolve_upload_dwg("u-abc123", "t1")
    assert p.name == "t1--u-abc123.dwg"


def test_happy_resolve_dxf(uploads):
    (uploads / "t1--u-abc123.dxf").write_bytes(b"0\nSECTION\n")
    p = broker._resolve_upload_dwg("u-abc123", "t1")
    assert p.name == "t1--u-abc123.dxf"


def test_dwg_preferred_over_dxf(uploads):
    (uploads / "t1--u-both.dwg").write_bytes(b"AC1032")
    (uploads / "t1--u-both.dxf").write_bytes(b"0\nSECTION\n")
    assert broker._resolve_upload_dwg("u-both", "t1").suffix == ".dwg"


def test_tenant_binding_blocks_cross_tenant_reads(uploads):
    """Knowing another tenant's drawing id resolves NOTHING under a
    different tenant_id — the staged name binds both (round 1, MAJOR)."""
    (uploads / "t1--u-abc123.dwg").write_bytes(b"AC1032data")
    with pytest.raises(ValueError, match="unknown uploaded drawing"):
        broker._resolve_upload_dwg("u-abc123", "t2")
    with pytest.raises(ValueError):
        broker._resolve_upload_dwg("t1--u-abc123", "t1")  # composed name is not bare


@pytest.mark.parametrize("bad", [
    "../rooftop_demo", "..\\rooftop_demo", "a/b", "a\\b", ".hidden",
    "UPPER", "sp ace", "", "u-abc123.dwg",  # suffix in the NAME is path-y
])
def test_malformed_names_rejected(uploads, bad):
    with pytest.raises(ValueError):
        broker._resolve_upload_dwg(bad, "t1")
    with pytest.raises(ValueError):
        broker._resolve_upload_dwg("u-abc123", bad)  # tenant part equally strict


def test_unknown_name_rejected(uploads):
    with pytest.raises(ValueError, match="unknown uploaded drawing"):
        broker._resolve_upload_dwg("u-missing", "t1")


def test_missing_uploads_dir_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "never-created"))
    with pytest.raises(ValueError, match="unknown uploaded drawing"):
        broker._resolve_upload_dwg("u-abc123", "t1")


def test_symlink_rejected(uploads, tmp_path):
    target = tmp_path / "outside.dwg"
    target.write_bytes(b"AC1032outside")
    link = uploads / "t1--u-linked.dwg"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    with pytest.raises(ValueError, match="symlink"):
        broker._resolve_upload_dwg("u-linked", "t1")


def test_library_and_upload_namespaces_never_cross(uploads):
    """A curated library drawing must NOT resolve via the upload path, and an
    uploaded drawing must NOT resolve via the library path."""
    # rooftop_demo lives in data/, not data/uploads/ -> upload resolver refuses
    with pytest.raises(ValueError):
        broker._resolve_upload_dwg("rooftop_demo", "t1")
    # an uploads file is invisible to the library resolver
    (uploads / "t1--u-mine.dwg").write_bytes(b"AC1032")
    with pytest.raises(ValueError):
        broker._resolve_live_dwg("u-mine")
    with pytest.raises(ValueError):
        broker._resolve_live_dwg("t1--u-mine")


def test_extract_endpoint_upload_flag_bad_name_400(uploads):
    client = TestClient(broker.app)
    r = client.post("/broker/extract",
                    json={"tenant_id": "t1", "dwg": "../etc", "upload": True})
    assert r.status_code == 400
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_extract_endpoint_upload_flag_reaches_da_stage(uploads, monkeypatch):
    """With a real staged file the upload branch must pass RESOLUTION and
    reach the DA stage. _get_da is stubbed to None — on THIS repo's dev hosts
    da/client.py can resolve REAL APS credentials, and an unstubbed run
    submits a real, paid WorkItem from a unit test (observed live before this
    stub existed). APS_UNAVAILABLE proves we got past resolution to the DA
    gate without leaving the process."""
    (uploads / "t1--u-abc123.dwg").write_bytes(b"AC1032data")
    monkeypatch.setattr(broker, "_get_da", lambda: None)
    client = TestClient(broker.app)
    r = client.post("/broker/extract",
                    json={"tenant_id": "t1", "dwg": "u-abc123", "upload": True})
    assert r.status_code == 502
    assert r.json()["error"]["error_code"] == "APS_UNAVAILABLE"


def test_extract_endpoint_default_is_library_path(uploads, monkeypatch):
    """upload defaults False: the library contract is byte-identical (an
    uploads-only name is unknown there). _get_da stubbed for the same
    no-live-calls reason as above (belt and suspenders — resolution fails
    before the DA stage here anyway)."""
    (uploads / "t1--u-abc123.dwg").write_bytes(b"AC1032data")
    monkeypatch.setattr(broker, "_get_da", lambda: None)
    client = TestClient(broker.app)
    r = client.post("/broker/extract", json={"tenant_id": "t1", "dwg": "u-abc123"})
    assert r.status_code == 400
    assert "unknown drawing" in r.json()["error"]["message"]


def test_offline_run_with_unextracted_upload_id_fails_closed(uploads, monkeypatch, tmp_path):
    """The round-1 BLOCKER's run half: an offline /broker/run against an
    UPLOADED-but-unextracted drawing (upload marker present, no manifest)
    must NEVER execute on the cached demo geometry — the marker guard raises
    -> honest BAD_PARAMS. (A markerless unknown id under an account tenant
    still auto-provisions per the platform's documented pre-§19 rule; the
    marker is exactly what distinguishes an upload.)"""
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(broker, "_get_da", lambda: None)
    import write_loop
    backend = write_loop.backend_for_tenant("t1", aps_live=False)
    backend.put(write_loop.upload_marker_key("t1", "u-pending"),
                b'{"status": "extracting"}')
    client = TestClient(broker.app)
    r = client.post("/broker/run", json={
        "tenant_id": "t1", "tool": {"name": "count-by-layer"},
        "params": {}, "dwg": "u-pending", "aps_live": False})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert "upload-status" in body["error"]["message"]


def test_offline_run_guest_tenant_never_bootstraps(uploads, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setattr(broker, "_get_da", lambda: None)
    client = TestClient(broker.app)
    r = client.post("/broker/run", json={
        "tenant_id": "guest-abc123", "tool": {"name": "count-by-layer"},
        "params": {}, "dwg": "u-nothere", "aps_live": False})
    assert r.status_code == 400
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
