"""CAD upload E2E: upload -> version receipt -> read round-trip (card C1-7).

Drives the REAL ``server/routers/cad_upload.py`` route through a real
multipart upload against a TestClient app mounted exactly as
``server/tests/test_cad_upload_validation.py`` mounts it (C1-3's own
precedent), using magic-byte-valid fixture bytes that genuinely satisfy the
route's own ``_sniff_reason`` validator -- not toy "fake-dwg-bytes" text.

Disposable-task shape: uploads land under a temp dir the test creates via
``tempfile.mkdtemp`` and explicitly removes with ``shutil.rmtree`` in a
``finally``, independent of pytest's own tmp_path retention -- nothing
durable leaks even if the process is killed mid-suite (the directory is
still scoped under the OS temp root for reclamation).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import cad_upload as cad_upload_router

VALID_DWG = b"AC1032" + os.urandom(512)
VALID_DXF = (
    b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n"
    b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
)


@pytest.fixture()
def disposable_upload_dir():
    directory = tempfile.mkdtemp(prefix="cad-upload-e2e-")
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture()
def client(monkeypatch, disposable_upload_dir):
    monkeypatch.setenv(cad_upload_router.FLAG_CAD_UPLOAD, "1")
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", disposable_upload_dir)
    app = FastAPI()
    app.include_router(cad_upload_router.router)
    return TestClient(app)


@pytest.mark.parametrize("filename,payload", [
    ("site-plan.dwg", VALID_DWG),
    ("layout.dxf", VALID_DXF),
])
def test_upload_version_receipt_read_round_trip(client, disposable_upload_dir,
                                                  filename, payload):
    expected_digest = hashlib.sha256(payload).hexdigest()

    resp = client.post(
        "/api/cad/upload",
        files={"file": (filename, payload, "application/octet-stream")})

    assert resp.status_code == 201
    receipt = resp.json()

    # Version receipt asserted from the RESPONSE.
    assert receipt["digest"] == expected_digest
    assert receipt["size"] == len(payload)
    assert receipt["version"] == 1
    upload_id = receipt["upload_id"]
    assert upload_id.startswith(expected_digest[:16])

    # Version receipt asserted from the STORE (the durable JSONL row), not
    # just trusted from the HTTP response.
    receipts_path = Path(disposable_upload_dir) / cad_upload_router._RECEIPTS_FILENAME
    stored_rows = [json.loads(line) for line in receipts_path.read_text().splitlines()]
    assert len(stored_rows) == 1
    stored = stored_rows[0]
    assert stored["digest"] == expected_digest
    assert stored["version"] == 1
    assert stored["upload_id"] == upload_id
    assert stored["size"] == len(payload)

    # Read round-trip: re-read the staged bytes off disk and compare against
    # the original payload, both by content and by recomputed digest.
    staged_path = Path(disposable_upload_dir) / f"{upload_id}{receipt['ext']}"
    read_back = staged_path.read_bytes()
    assert read_back == payload
    assert hashlib.sha256(read_back).hexdigest() == expected_digest


def test_reupload_of_identical_bytes_gets_the_next_version_receipt(
        client, disposable_upload_dir):
    first = client.post(
        "/api/cad/upload",
        files={"file": ("a.dwg", VALID_DWG, "application/octet-stream")}).json()
    second = client.post(
        "/api/cad/upload",
        files={"file": ("b.dwg", VALID_DWG, "application/octet-stream")}).json()

    assert first["digest"] == second["digest"]
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["upload_id"] != second["upload_id"]

    for receipt in (first, second):
        staged_path = Path(disposable_upload_dir) / f"{receipt['upload_id']}{receipt['ext']}"
        assert staged_path.read_bytes() == VALID_DWG
