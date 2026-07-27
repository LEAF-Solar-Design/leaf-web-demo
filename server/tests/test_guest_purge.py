"""
The retention promise-keeper (§19): purge_expired deletes guest drawings at
their STAMPED expiry — drawing dir, staged upload file, and the empty tenant
dir — and logs every deletion to purge.log.jsonl. Proven with a short
retention override, exactly as the definition of done demands.

Run:  cd server && python -m pytest tests/test_guest_purge.py -q
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import guest_uploads
import write_loop

DXF = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\nAB\n8\nL1\n70\n1\n"
       "10\n1.0\n20\n2.0\n10\n3.0\n20\n4.0\n0\nENDSEC\n0\nEOF\n").encode()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    guest_uploads._reset_rate_state()
    monkeypatch.setattr(
        guest_uploads, "start_extraction_thread",
        lambda tenant_id, drawing_id, ext: guest_uploads.run_extraction(
            tenant_id, drawing_id, ext))
    return TestClient(app_module.app)


def _upload(client):
    r = client.post("/api/drawings/upload",
                    files={"file": ("p.dxf", io.BytesIO(DXF))})
    assert r.status_code == 202
    return r.json()["tenant_id"], r.json()["drawing_id"]


def _drawing_dir(tenant, did) -> Path:
    return Path(write_loop.guest_store_dir()) / "tenants" / tenant / "drawings" / did


def test_expired_guest_drawing_is_purged_with_log(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")  # ~72 ms
    tenant, did = _upload(client)
    ddir = _drawing_dir(tenant, did)
    staged = guest_uploads.staged_path(tenant, did, ".dxf")
    assert ddir.is_dir() and staged.is_file()

    time.sleep(0.2)  # let the stamped expiry pass
    result = guest_uploads.purge_expired()

    assert result["count"] == 1
    assert result["purged"] == [{"tenant_id": tenant, "drawing_id": did}]
    assert result["freed_bytes"] > 0
    assert not ddir.exists(), "drawing dir must be deleted"
    assert not staged.exists(), "staged upload file must be deleted"
    assert not (Path(write_loop.guest_store_dir()) / "tenants" / tenant).exists(), \
        "empty tenant dir must be dropped"

    log = (Path(write_loop.guest_store_dir()) / "purge.log.jsonl").read_text()
    entry = json.loads(log.strip().splitlines()[-1])
    assert entry["tenant_id"] == tenant and entry["drawing_id"] == did
    assert entry["freed_bytes"] > 0

    # the surface is now honestly gone
    assert client.get(f"/api/drawings/{did}/upload-status",
                      headers={"X-Tenant-Id": tenant}).status_code == 404
    assert client.get(f"/api/drawings/{did}/intake",
                      headers={"X-Tenant-Id": tenant}).status_code == 404


def test_unexpired_guest_drawing_survives(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "24")
    tenant, did = _upload(client)
    result = guest_uploads.purge_expired()
    assert result["count"] == 0
    assert _drawing_dir(tenant, did).is_dir()
    assert client.get(f"/api/drawings/{did}/intake",
                      headers={"X-Tenant-Id": tenant}).status_code == 200


def test_purge_is_idempotent(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")
    _upload(client)
    time.sleep(0.2)
    assert guest_uploads.purge_expired()["count"] == 1
    assert guest_uploads.purge_expired()["count"] == 0


def test_purge_honors_stamp_not_current_env(client, monkeypatch):
    """The STAMPED expiry rules: a drawing uploaded under a long window is NOT
    purged just because the env later shrank — the promise shown at upload
    time is the promise kept."""
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "24")
    tenant, did = _upload(client)
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00001")
    assert guest_uploads.purge_expired()["count"] == 0
    assert _drawing_dir(tenant, did).is_dir()


def test_account_uploads_untouched_by_purge(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")
    r = client.post("/api/drawings/upload",
                    files={"file": ("p.dxf", io.BytesIO(DXF))},
                    headers={"X-Tenant-Id": "acme-solar"})
    assert r.status_code == 202
    did = r.json()["drawing_id"]
    time.sleep(0.2)
    guest_uploads.purge_expired()
    # account drawing (default store) survives — no retention promise was made
    assert client.get(f"/api/drawings/{did}/intake",
                      headers={"X-Tenant-Id": "acme-solar"}).status_code == 200


def test_purge_failure_is_logged_honestly_never_as_a_kill(client, monkeypatch):
    """A failed deletion must NEVER produce a success log line — the purge
    log is the retention promise's receipt (round 1, MAJOR)."""
    import shutil as _shutil
    real_rmtree = _shutil.rmtree  # capture BEFORE patching: guest_uploads.shutil
    # IS this same module object, so restoring via _shutil.rmtree later would
    # re-assign the lambda (aliasing).
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")
    tenant, did = _upload(client)
    time.sleep(0.2)
    monkeypatch.setattr(guest_uploads.shutil, "rmtree", lambda *a, **k: None)
    result = guest_uploads.purge_expired()
    assert result["count"] == 0, "a surviving dir must not count as purged"
    assert _drawing_dir(tenant, did).is_dir()
    log = (Path(write_loop.guest_store_dir()) / "purge.log.jsonl").read_text()
    entry = json.loads(log.strip().splitlines()[-1])
    assert entry["status"] == "failed" and entry["drawing_id"] == did
    monkeypatch.setattr(guest_uploads.shutil, "rmtree", real_rmtree)
    # and the NEXT sweep (deletion working again) keeps the promise
    result2 = guest_uploads.purge_expired()
    assert result2["count"] == 1
    assert not _drawing_dir(tenant, did).exists()


def test_staged_file_deletion_failure_is_honest_too(client, monkeypatch):
    """Round-2 MAJOR: a surviving staged RAW file must block the success
    receipt exactly like a surviving drawing dir — and because staged files
    delete FIRST, the drawing dir (and its marker) survive for a full retry."""
    real_unlink = guest_uploads._unlink_quiet  # captured for explicit restore
    # (monkeypatch.undo() would also revert the fixture's store isolation)
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")
    tenant, did = _upload(client)
    staged = guest_uploads.staged_path(tenant, did, ".dxf")
    assert staged.is_file()
    time.sleep(0.2)
    monkeypatch.setattr(guest_uploads, "_unlink_quiet", lambda p: None)
    result = guest_uploads.purge_expired()
    assert result["count"] == 0
    assert staged.exists(), "staged file survives the simulated failure"
    assert _drawing_dir(tenant, did).is_dir(), \
        "dir must NOT be deleted while its staged file survives (full-retry invariant)"
    log = (Path(write_loop.guest_store_dir()) / "purge.log.jsonl").read_text()
    assert json.loads(log.strip().splitlines()[-1])["status"] == "failed"
    monkeypatch.setattr(guest_uploads, "_unlink_quiet", real_unlink)
    result2 = guest_uploads.purge_expired()
    assert result2["count"] == 1
    assert not staged.exists() and not _drawing_dir(tenant, did).exists()


def test_orphan_staged_file_swept_by_mtime(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")
    updir = guest_uploads.uploads_dir()
    updir.mkdir(parents=True, exist_ok=True)
    orphan = updir / "guest-x--u-orphan.dxf"
    orphan.write_bytes(b"leftover")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    guest_uploads.purge_expired()
    assert not orphan.exists()


def test_retry_landing_between_expiry_read_and_lock_survives(client, monkeypatch):
    """Round-5 MAJOR: purge reads the expiry BEFORE acquiring the drawing
    lock. If a failed-retry replaces the marker (fresh quota-charged attempt,
    fresh expiry) in that window, the in-lock expiry re-check must veto the
    stale deletion verdict — the fresh attempt survives, no receipt is
    logged."""
    import dxf_intake
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")  # ~72 ms

    # First attempt fails terminally (real path: the extractor raises), so
    # the marker is 'failed' and expired shortly after.
    real_parse = dxf_intake.parse_dxf_file
    monkeypatch.setattr(
        dxf_intake, "parse_dxf_file",
        lambda *a, **k: (_ for _ in ()).throw(
            guest_uploads._ExtractError("INTERNAL", "boom", retryable=True)))
    tenant, did = _upload(client)
    time.sleep(0.2)  # marker now expired

    # The retry lands INSIDE purge's read->lock window: hook _read_expiry so
    # that, right after purge reads the STALE expiry for this drawing, the
    # same-bytes retry replaces the marker with a fresh 24h attempt.
    monkeypatch.setattr(dxf_intake, "parse_dxf_file", real_parse)
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "24")
    real_read = guest_uploads._read_expiry
    fired = {"done": False}

    def read_then_retry(marker_path):
        expires = real_read(marker_path)
        if not fired["done"] and marker_path.parent.name == did:
            fired["done"] = True
            retry = client.post(
                "/api/drawings/upload",
                files={"file": ("p.dxf", io.BytesIO(DXF))},
                headers={"X-Tenant-Id": tenant})
            assert retry.status_code == 202
            assert retry.json()["drawing_id"] == did
        return expires

    monkeypatch.setattr(guest_uploads, "_read_expiry", read_then_retry)
    result = guest_uploads.purge_expired()

    assert result["count"] == 0, "stale verdict must not delete the fresh attempt"
    assert _drawing_dir(tenant, did).exists()
    marker = guest_uploads.read_marker(
        write_loop.backend_for_tenant(tenant, aps_live=False, da=None), tenant, did)
    assert marker is not None and marker["status"] == "ready"


def test_the_extraction_hands_the_store_a_marker_check_it_can_run_in_the_lock(
        client, monkeypatch):
    """The CREATING writer's half of the retention promise, at its wiring.

    Every other legacy writer proves the drawing survived by loading its
    manifest inside the store's checkout lock. `ingest_drawing` cannot: an
    absent drawing is its normal case. So `run_extraction` hands it a callable
    that re-reads the upload marker, and the store runs that INSIDE the lock —
    the only place where the answer cannot go stale before the write. Checked
    outside, it is a read with an unbounded wait after it, and a purge sweep in a
    second process fits through that window and writes a receipt.

    Two assertions, because passing a callable that always says yes would satisfy
    the first one alone: it is really wired through, and it really flips once the
    purge has taken the drawing.
    """
    import store

    seen = {}
    real_ingest = store.ingest_drawing

    def spy(backend, tenant_id, local_path, drawing_id=None, **kwargs):
        check = kwargs.get("precondition")
        seen["precondition"] = check
        # Sampled HERE, the only moment it means "this extraction is still the
        # live attempt". By the time the upload call returns, the marker has
        # moved on to `ready` and the honest answer is already no.
        seen["at_ingest"] = check() if callable(check) else None
        return real_ingest(backend, tenant_id, local_path, drawing_id, **kwargs)

    monkeypatch.setattr(store, "ingest_drawing", spy)
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")  # ~72 ms
    tenant, did = _upload(client)

    check = seen.get("precondition")
    assert callable(check), (
        "run_extraction ingested without a precondition; the creating writer "
        "then has nothing to stop it recreating a purged drawing")
    assert seen["at_ingest"] is True, (
        "the check said no for a live extraction that WAS still its drawing's "
        "own attempt; a check that never says yes blocks real uploads")

    time.sleep(0.2)
    assert guest_uploads.purge_expired()["count"] == 1
    assert not _drawing_dir(tenant, did).exists()

    assert check() is False, (
        "the check still says yes after the purge wrote its 'deleted' receipt, "
        "so it would wave an in-flight extraction through and resurrect the "
        "drawing behind that receipt")
