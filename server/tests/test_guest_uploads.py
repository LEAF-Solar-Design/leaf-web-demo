"""
Guest/account drawing uploads (§19): endpoint validation, guest minting,
rate caps, marker lifecycle, honest status, policy endpoint — all in-process
(TestClient), APS_LIVE=0, isolated stores per test.

THE test that matters most here is
test_upload_dxf_happy_path_serves_their_geometry: an uploaded drawing must
render THE USER'S coordinates and provably NOT the cached rooftop_demo intake
(the fabrication trap this whole lane exists to close).

Run:  cd server && python -m pytest tests/test_guest_uploads.py -q
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

# A distinctive DXF nothing in the repo shares coordinates with. Rooftop demo
# coordinates live around 14000-15500; these are nowhere near.
DXF_BYTES = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n5\nABCD\n8\nPanels\n70\n1\n"
    "10\n111.25\n20\n222.5\n10\n333.75\n20\n444.0\n10\n555.5\n20\n666.25\n"
    "0\nENDSEC\n0\nEOF\n"
).encode("utf-8")
DISTINCTIVE_COORD = 111.25
ROOFTOP_COORD = 14323.816  # first polyline x of data/rooftop_demo.intake.json


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_GUEST_RETENTION_HOURS", raising=False)
    guest_uploads._reset_rate_state()
    # Deterministic tests: run extraction inline instead of a thread.
    monkeypatch.setattr(
        guest_uploads, "start_extraction_thread",
        lambda tenant_id, drawing_id, ext: guest_uploads.run_extraction(
            tenant_id, drawing_id, ext))
    return TestClient(app_module.app)


def _upload(client, data=DXF_BYTES, name="mine.dxf", headers=None):
    return client.post("/api/drawings/upload",
                       files={"file": (name, io.BytesIO(data))},
                       headers=headers or {})


def test_upload_dxf_happy_path_serves_their_geometry(client):
    r = _upload(client)
    assert r.status_code == 202
    body = r.json()
    assert body["error"] is None
    assert body["tenant_kind"] == "guest"
    assert body["tenant_id"].startswith("guest-")
    assert body["drawing_id"].startswith("u-")
    assert body["retention_expires_at"]  # stamped for guests
    assert body["status"] == "extracting"

    tenant, did = body["tenant_id"], body["drawing_id"]
    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.status_code == 200
    assert s.json()["status"] == "ready"

    i = client.get(f"/api/drawings/{did}/intake", headers={"X-Tenant-Id": tenant})
    assert i.status_code == 200
    intake = i.json()["intake"]
    coords = [c for p in intake["polylines"] for pt in p["pts"] for c in pt]
    assert DISTINCTIVE_COORD in coords, "their geometry must be served"
    assert ROOFTOP_COORD not in coords, "the cached demo intake must NEVER leak in"
    assert intake["layers"] == ["Panels"]
    assert intake["polylines"][0]["handle"] == "ABCD"
    assert intake["polylines"][0]["closed"] is True


def test_upload_account_tenant_no_retention(client):
    r = _upload(client, headers={"X-Tenant-Id": "acme-solar"})
    assert r.status_code == 202
    body = r.json()
    assert body["tenant_kind"] == "account"
    assert body["tenant_id"] == "acme-solar"
    assert body["retention_expires_at"] is None
    assert body["guest_session"] is None


def test_upload_oversize_413(client, monkeypatch):
    monkeypatch.setenv("LEAF_UPLOAD_MAX_BYTES", "64")
    r = _upload(client, data=b"0\nSECTION\n" + b"x" * 100 + b"\nENTITIES\n")
    assert r.status_code == 413
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_upload_bad_extension_400(client):
    r = _upload(client, name="mine.pdf")
    assert r.status_code == 400
    assert "dwg" in r.json()["error"]["message"].lower()


def test_upload_empty_400(client):
    r = _upload(client, data=b"")
    assert r.status_code == 400


def test_upload_fake_dwg_magic_400(client):
    r = _upload(client, data=b"this is not a dwg at all", name="mine.dwg")
    assert r.status_code == 400
    assert "AC1" in r.json()["error"]["message"]


def test_guest_rate_limit_per_ip(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "2")
    assert _upload(client).status_code == 202
    assert _upload(client).status_code == 202
    r = _upload(client)
    assert r.status_code == 429
    assert r.json()["error"]["error_code"] == "quota_exceeded"
    # accounts are NOT rate-capped by the guest limiter
    assert _upload(client, headers={"X-Tenant-Id": "acme-solar"}).status_code == 202


def test_guest_rate_limit_global(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_DAY", "1")
    assert _upload(client).status_code == 202
    r = _upload(client)
    assert r.status_code == 429


def test_upload_status_unknown_404(client):
    r = client.get("/api/drawings/u-doesnotexist/upload-status",
                   headers={"X-Tenant-Id": "guest-nobody"})
    assert r.status_code == 404


def test_upload_status_timeout_becomes_failed(client, monkeypatch):
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    marker = guest_uploads.read_marker(backend, tenant, did)
    marker["status"] = "extracting"
    marker["uploaded_at"] = "2020-01-01T00:00:00+00:00"
    guest_uploads.write_marker(backend, tenant, did, marker)
    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.json()["status"] == "failed"
    assert s.json()["error"]["error_code"] == "TIMEOUT"
    # persisted, not just computed: a second read agrees without recomputation
    marker2 = guest_uploads.read_marker(backend, tenant, did)
    assert marker2["status"] == "failed"


def test_dwg_at_aps_live_0_fails_honestly(client):
    r = _upload(client, data=b"AC1032rest-of-a-dwg-file", name="real.dwg")
    assert r.status_code == 202
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.json()["status"] == "failed"
    assert s.json()["error"]["error_code"] == "APS_UNAVAILABLE"
    # and the intake read fails CLOSED — no demo bootstrap
    i = client.get(f"/api/drawings/{did}/intake", headers={"X-Tenant-Id": tenant})
    assert i.status_code == 404


def test_marker_records_content_identity(client):
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    marker = guest_uploads.read_marker(backend, tenant, did)
    assert marker["bytes"] == len(DXF_BYTES)
    assert marker["filename"] == "mine.dxf"
    import hashlib
    assert marker["content_sha256"] == hashlib.sha256(DXF_BYTES).hexdigest()


def test_policy_endpoint_reads_the_one_constant(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "5.5")
    r = client.get("/api/site/guest-upload-policy")
    assert r.status_code == 200
    body = r.json()
    assert body["retention_hours"] == 5.5
    assert body["accepted"] == [".dwg", ".dxf"]
    assert body["enabled"] is True
    assert body["extract_live"] is False


def test_disabled_flag_503_and_policy_reports_it(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_ENABLED", "0")
    assert _upload(client).status_code == 503
    assert client.get("/api/site/guest-upload-policy").json()["enabled"] is False


def test_purge_daemon_starts_even_when_uploads_disabled(monkeypatch):
    """Round-1 MAJOR: disabling NEW uploads never strands already-stamped
    retention promises — the daemon runs regardless of the enable flag."""
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_ENABLED", "0")
    thread = guest_uploads.start_purge_daemon()
    assert thread is not None and thread.is_alive()


def test_invalid_upload_does_not_consume_guest_quota(client, monkeypatch):
    """Round-1 MAJOR: garbage requests must not drain the shared daily pool —
    quota is counted only after validation passes."""
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "1")
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_DAY", "1")
    assert _upload(client, name="junk.pdf").status_code == 400
    assert _upload(client, data=b"not a dwg", name="junk.dwg").status_code == 400
    # the one real slot is still available
    assert _upload(client).status_code == 202


def test_oversize_request_precheck_rejects_declared_length(client, monkeypatch):
    monkeypatch.setenv("LEAF_UPLOAD_MAX_BYTES", "64")
    big = b"0\nSECTION\n" + b"x" * 200_000
    r = _upload(client, data=big)
    assert r.status_code == 413
    assert "upload cap" in r.json()["error"]["message"]


def test_store_representation_raw_bytes_v1_plus_intake_cache(client):
    """Round-1 MAJOR: v1's version blob must hold the user's RAW bytes (what
    a live write would send to APS) and the parsed intake must live at the
    sibling cache key write_loop.read_intake prefers."""
    import store
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    vkey = store.drawing_version_key(tenant, did, 1)
    assert backend.get(vkey) == DXF_BYTES, "version blob must be the raw upload"
    ckey = write_loop.intake_cache_key(tenant, did, 1)
    assert backend.exists(ckey), "parsed intake must sit at the cache sibling"
    cached = json.loads(backend.get(ckey).decode("utf-8"))
    assert cached["layers"] == ["Panels"]


def test_session_route_serves_uploaded_drawing_not_demo(client):
    """Round-1 BLOCKER (session half): offline GET /api/session?dwg=<upload>
    must serve THEIR intake (or 404), never the cached rooftop demo."""
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    s = client.get(f"/api/session?dwg={did}", headers={"X-Tenant-Id": tenant})
    assert s.status_code == 200
    coords = [c for p in s.json()["intake"]["polylines"] for pt in p["pts"] for c in pt]
    assert DISTINCTIVE_COORD in coords
    assert ROOFTOP_COORD not in coords
    # unknown drawing under a guest tenant: honest 404, no bootstrap
    missing = client.get("/api/session?dwg=u-missing", headers={"X-Tenant-Id": tenant})
    assert missing.status_code == 404
    # regression: the default dwg still serves the cached demo intake
    default = client.get("/api/session", headers={"X-Tenant-Id": "acme-solar"})
    assert default.status_code == 200
    default_coords = [c for p in default.json()["intake"]["polylines"] for pt in p["pts"] for c in pt]
    assert ROOFTOP_COORD in default_coords
