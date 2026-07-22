"""
Broker upload resolver (§19): _resolve_upload_dwg must apply the IDENTICAL
strictness as the library resolver — bare names only, no traversal, no
symlinks, parent must BE the uploads root — and the two namespaces must never
cross-resolve. Plus the /broker/extract {upload: true} endpoint wiring.

Run:  cd server && python -m pytest tests/test_broker_upload_resolver.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import broker


@pytest.fixture()
def uploads(monkeypatch, tmp_path):
    updir = tmp_path / "uploads"
    updir.mkdir()
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(updir))
    return updir


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
