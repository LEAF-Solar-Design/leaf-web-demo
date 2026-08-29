"""Tests for the hardened CAD upload validation router (card C1-3).

Exercises the acceptance oracle directly:
  - extension AND magic-byte check (DWG/DXF only)
  - size cap enforced before buffering (streamed, bounded)
  - zip/entity-bomb heuristics reject pathological files with a typed 4xx
  - nothing malformed is stored -> oversized/malformed uploads leave zero
    orphan bytes on disk
  - every accepted upload writes a content digest + version receipt row
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from routers import cad_upload as cad_upload_router  # noqa: E402
from route_flatten import iter_leaf_routes  # noqa: E402

VALID_DWG = b"AC1032" + os.urandom(200)
VALID_DXF = (
    b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n"
    b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
)


@pytest.fixture()
def cad_env(monkeypatch, tmp_path):
    upload_dir = tmp_path / "cad_uploads"
    monkeypatch.setenv(cad_upload_router.FLAG_CAD_UPLOAD, "1")
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("LEAF_CAD_UPLOAD_MAX_BYTES", str(64 * 1024))
    return upload_dir


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(cad_upload_router.router)
    return TestClient(app)


def _staged_files(upload_dir):
    if not upload_dir.exists():
        return []
    return [
        p for p in upload_dir.iterdir()
        if p.is_file() and p.name != cad_upload_router._RECEIPTS_FILENAME
    ]


def _post(client, filename: str, data: bytes):
    return client.post(
        "/api/cad/upload", files={"file": (filename, data, "application/octet-stream")})


# --------------------------------------------------------------------------- #
# flag gate
# --------------------------------------------------------------------------- #
def test_flag_off_returns_503(client, monkeypatch, tmp_path):
    monkeypatch.delenv(cad_upload_router.FLAG_CAD_UPLOAD, raising=False)
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(tmp_path / "cad_uploads"))
    resp = _post(client, "drawing.dwg", VALID_DWG)
    assert resp.status_code == 503
    assert resp.json()["error"]["error_code"] == "INTERNAL"


# --------------------------------------------------------------------------- #
# extension + magic-byte checks
# --------------------------------------------------------------------------- #
def test_rejects_disallowed_extension(client, cad_env):
    resp = _post(client, "payload.txt", b"not a cad file")
    assert resp.status_code == 400
    assert resp.json()["error"]["error_code"] == "BAD_PARAMS"
    assert _staged_files(cad_env) == []


def test_rejects_dwg_extension_with_wrong_magic(client, cad_env):
    resp = _post(client, "fake.dwg", b"not-really-a-dwg" + os.urandom(64))
    assert resp.status_code == 400
    assert "AC1" in resp.json()["error"]["message"]
    assert _staged_files(cad_env) == []


def test_rejects_dxf_with_no_group_code_structure(client, cad_env):
    resp = _post(client, "fake.dxf", os.urandom(128))
    assert resp.status_code == 400
    assert _staged_files(cad_env) == []


def test_rejects_empty_file(client, cad_env):
    resp = _post(client, "empty.dwg", b"")
    assert resp.status_code == 400
    assert "empty" in resp.json()["error"]["message"]
    assert _staged_files(cad_env) == []


# --------------------------------------------------------------------------- #
# size cap: streamed + bounded, enforced before buffering
# --------------------------------------------------------------------------- #
def test_read_bounded_never_reads_past_cap_plus_one():
    """The streaming reader must stop at cap+1 bytes total no matter how much
    more data the underlying stream actually holds -- proves the cap is
    enforced by bounded chunked reads, not by buffering the whole body first."""

    class _HugeStream:
        def __init__(self, total_bytes: int):
            self.total_bytes = total_bytes
            self.served = 0
            self.max_chunk_requested = 0

        def read(self, n: int) -> bytes:
            self.max_chunk_requested = max(self.max_chunk_requested, n)
            if self.served >= self.total_bytes:
                return b""
            chunk = b"A" * min(n, self.total_bytes - self.served)
            self.served += len(chunk)
            return chunk

    cap = 1000
    stream = _HugeStream(total_bytes=10**9)
    data = cad_upload_router._read_bounded(stream, cap)
    assert len(data) == cap + 1
    assert stream.served == cap + 1


def test_oversized_upload_is_rejected_with_413_and_zero_orphan_bytes(client, cad_env):
    cap = 64 * 1024
    resp = _post(client, "big.dwg", b"AC1032" + os.urandom(cap))
    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert "byte upload cap" in body["error"]["message"]
    assert _staged_files(cad_env) == []


# --------------------------------------------------------------------------- #
# zip / entity-bomb heuristics
# --------------------------------------------------------------------------- #
def test_rejects_embedded_zip_signature(client, cad_env):
    payload = b"AC1032" + b"PK\x03\x04" + os.urandom(64)
    resp = _post(client, "polyglot.dwg", payload)
    assert resp.status_code == 422
    assert "zip" in resp.json()["error"]["message"]
    assert _staged_files(cad_env) == []


def test_rejects_pathological_compression_ratio(client, cad_env):
    # 20 KB of a single repeated byte: compresses down to almost nothing --
    # not plausible real CAD geometry, and above the min-size threshold.
    payload = b"AC1032" + (b"A" * 20000)
    resp = _post(client, "bomb.dwg", payload)
    assert resp.status_code == 422
    assert "compression" in resp.json()["error"]["message"]
    assert _staged_files(cad_env) == []


def test_rejects_dxf_entity_expansion_bomb(client, cad_env, monkeypatch):
    # Lower the threshold so the heuristic is exercised without needing a
    # multi-hundred-thousand-entity fixture on disk.
    monkeypatch.setattr(cad_upload_router, "_DXF_MAX_ENTITY_MARKERS", 3)
    payload = VALID_DXF + (b"\nINSERT" * 10)
    resp = _post(client, "entity_bomb.dxf", payload)
    assert resp.status_code == 422
    assert "entity" in resp.json()["error"]["message"]
    assert _staged_files(cad_env) == []


# --------------------------------------------------------------------------- #
# accepted uploads: digest + version receipt row, bytes staged verbatim
# --------------------------------------------------------------------------- #
def test_accepts_valid_dwg_and_writes_digest_and_receipt(client, cad_env):
    resp = _post(client, "site-plan.dwg", VALID_DWG)
    assert resp.status_code == 201
    body = resp.json()
    assert body["digest"] == hashlib.sha256(VALID_DWG).hexdigest()
    assert body["version"] == 1
    assert body["ext"] == ".dwg"
    assert body["size"] == len(VALID_DWG)

    staged = _staged_files(cad_env)
    assert len(staged) == 1
    assert staged[0].read_bytes() == VALID_DWG

    receipts_path = cad_env / cad_upload_router._RECEIPTS_FILENAME
    rows = [json.loads(line) for line in receipts_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["digest"] == body["digest"]
    assert rows[0]["version"] == 1
    assert rows[0]["upload_id"] == body["upload_id"]


def test_accepts_valid_dxf(client, cad_env):
    resp = _post(client, "layout.dxf", VALID_DXF)
    assert resp.status_code == 201
    body = resp.json()
    assert body["ext"] == ".dxf"
    assert body["digest"] == hashlib.sha256(VALID_DXF).hexdigest()


def test_reupload_of_identical_content_gets_next_version(client, cad_env):
    first = _post(client, "site-plan.dwg", VALID_DWG).json()
    second = _post(client, "site-plan-copy.dwg", VALID_DWG).json()

    assert first["digest"] == second["digest"]
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["upload_id"] != second["upload_id"]

    staged = _staged_files(cad_env)
    assert len(staged) == 2

    receipts_path = cad_env / cad_upload_router._RECEIPTS_FILENAME
    rows = [json.loads(line) for line in receipts_path.read_text().splitlines()]
    assert len(rows) == 2
    assert {row["version"] for row in rows} == {1, 2}


# --------------------------------------------------------------------------- #
# MOUNTED on the real server app (not a per-test FastAPI() shim)
#
# Every test above builds its own FastAPI and includes the router, so all of
# them pass just as well while server/app.py never mounts it -- which is
# exactly the inert-by-construction state this section exists to refute. The
# negative control is the STATUS CODE both ways against ``app_module.app``:
#   flag OFF -> 503 (the router's own fail-closed refusal; a 404 here would
#               mean the route is not mounted at all)
#   flag ON  -> the route is reachable AND still performs its validation
#               (201 for admissible bytes, typed 4xx for a bad payload).
# --------------------------------------------------------------------------- #
import app as app_module  # noqa: E402


@pytest.fixture()
def mounted_client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(tmp_path / "mounted_cad_uploads"))
    monkeypatch.setenv("LEAF_CAD_UPLOAD_MAX_BYTES", str(64 * 1024))
    return TestClient(app_module.app, raise_server_exceptions=False)


def test_mounted_route_exists_on_the_real_app():
    assert any(path == "/api/cad/upload"
               and "POST" in (getattr(route, "methods", None) or set())
               for path, route in iter_leaf_routes(app_module.app.routes))


def test_mounted_flag_off_is_503_not_404(mounted_client, monkeypatch):
    monkeypatch.delenv(cad_upload_router.FLAG_CAD_UPLOAD, raising=False)
    resp = _post(mounted_client, "drawing.dwg", VALID_DWG)
    assert resp.status_code == 503, "404 here means the router is not mounted in app.py"
    assert resp.json()["error"]["error_code"] == "INTERNAL"


def test_mounted_flag_on_is_reachable_and_still_validates(mounted_client, monkeypatch):
    monkeypatch.setenv(cad_upload_router.FLAG_CAD_UPLOAD, "1")

    accepted = _post(mounted_client, "site-plan.dwg", VALID_DWG)
    assert accepted.status_code == 201
    assert accepted.json()["digest"] == hashlib.sha256(VALID_DWG).hexdigest()

    # Reachable is not enough: the mounted route must still run every gate.
    rejected = _post(mounted_client, "payload.txt", b"not a cad file")
    assert rejected.status_code == 400
    assert rejected.json()["error"]["error_code"] == "BAD_PARAMS"

    bomb = _post(mounted_client, "polyglot.dwg", b"AC1032" + b"PK\x03\x04" + os.urandom(64))
    assert bomb.status_code == 422
