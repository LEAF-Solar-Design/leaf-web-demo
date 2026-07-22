"""
The fabrication-trap guards (§19): an uploaded/guest drawing id must NEVER
fall through to write_loop.ensure_demo_drawing's cached rooftop_demo
bootstrap. Real extraction or an honest 404 — nothing else. Also regression-
proves the pre-§19 bootstrap behavior for ordinary tenants is untouched.

Run:  cd server && python -m pytest tests/test_guest_fail_closed.py -q
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import guest_uploads
import write_loop

SERVER_DIR = Path(__file__).resolve().parent.parent
ROOFTOP = json.loads((SERVER_DIR.parent / "data" / "rooftop_demo.intake.json")
                     .read_text(encoding="utf-8"))


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    guest_uploads._reset_rate_state()
    return TestClient(app_module.app)


def _in_memory_backend():
    import store
    return store.InMemoryBackend()


def test_ensure_demo_drawing_guest_guard_raises_and_writes_nothing():
    backend = _in_memory_backend()
    with pytest.raises(KeyError):
        write_loop.ensure_demo_drawing(backend, "guest-abc123", "u-deadbeef")
    assert backend.keys() == []  # not one byte was bootstrapped


def test_ensure_demo_drawing_marker_guard_raises():
    backend = _in_memory_backend()
    backend.put(write_loop.upload_marker_key("acme-solar", "u-cafe"),
                b'{"status": "extracting"}')
    with pytest.raises(ValueError) as exc:
        write_loop.ensure_demo_drawing(backend, "acme-solar", "u-cafe")
    assert "upload-status" in str(exc.value)
    # the marker is the ONLY key; no manifest / no demo v1 appeared
    assert all("manifest" not in k and "/v/" not in k for k in backend.keys())


def test_guest_unknown_drawing_404_no_bootstrap(client, tmp_path):
    r = client.get("/api/drawings/some-drawing/intake",
                   headers={"X-Tenant-Id": "guest-abc123"})
    assert r.status_code == 404
    # and the guest store stayed empty on disk
    guest_root = Path(write_loop.guest_store_dir())
    assert not (guest_root / "tenants" / "guest-abc123").exists()


def test_pending_upload_intake_404_with_honest_message(client, monkeypatch):
    # Suppress extraction entirely: the marker stays "extracting".
    monkeypatch.setattr(guest_uploads, "start_extraction_thread",
                        lambda *a, **k: None)
    dxf = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    r = client.post("/api/drawings/upload",
                    files={"file": ("f.dxf", io.BytesIO(dxf))})
    assert r.status_code == 202
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]

    i = client.get(f"/api/drawings/{did}/intake", headers={"X-Tenant-Id": tenant})
    assert i.status_code == 404

    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.status_code == 200
    assert s.json()["status"] == "extracting"


def test_pending_upload_versions_and_checkout_fail_closed(client, monkeypatch):
    monkeypatch.setattr(guest_uploads, "start_extraction_thread",
                        lambda *a, **k: None)
    dxf = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    r = client.post("/api/drawings/upload",
                    files={"file": ("f.dxf", io.BytesIO(dxf))})
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    assert client.get(f"/api/drawings/{did}/versions",
                      headers={"X-Tenant-Id": tenant}).status_code == 404
    assert client.post(f"/api/drawings/{did}/checkout",
                       headers={"X-Tenant-Id": tenant}).status_code == 404


def test_account_demo_bootstrap_regression_byte_identical(client):
    """Ordinary tenants keep the pre-§19 behavior: any first-seen slug-safe
    drawing id bootstraps v1 from the cached intake, byte-identical."""
    r = client.get("/api/drawings/demo/intake",
                   headers={"X-Tenant-Id": "acme-regression"})
    assert r.status_code == 200
    assert r.json()["intake"] == ROOFTOP
    r2 = client.get("/api/drawings/any-fresh-id/intake",
                    headers={"X-Tenant-Id": "acme-regression"})
    assert r2.status_code == 200
    assert r2.json()["intake"] == ROOFTOP


def test_guest_cannot_reach_demo_bootstrap_even_for_wellknown_id(client):
    r = client.get("/api/drawings/demo/intake",
                   headers={"X-Tenant-Id": "guest-abc123"})
    assert r.status_code == 404
