"""Card F-2: cad_upload disposition — writable-first dir resolution and the
fail-closed accept path.

Premise correction receipted on the board card: PR #774 mounted the dedicated
router on 2026-08-24 (the "flipped-inert / unmounted" state was stale), and
the REAL live defect was the accept path 500ing on staging (error_id
1d0930ab8e96381c) because the default receipt dir sits inside the read-only
container root while the deployment's one writable volume is only named via
LEAF_UPLOADS_DIR. These tests pin the two fixes:

  1. cad_upload_dir() resolves writable-first: explicit LEAF_CAD_UPLOAD_DIR
     wins; otherwise a sibling of LEAF_UPLOADS_DIR (the proven-writable
     volume); otherwise the local-dev default.
  2. A refused store write is a typed retryable 503 with ZERO orphan bytes —
     never an unhandled 500, and never a staged file without its receipt row.

Run:  cd server && python -m pytest tests/test_cad_upload_disposition.py -q
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from routers import cad_upload  # noqa: E402

# A minimal ASCII DXF that genuinely satisfies _sniff_reason (SECTION marker
# in the first 4 KB) — the same standard the e2e suite set: never toy bytes
# that only pass because the validator is weak.
VALID_DXF = (
    b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(cad_upload.FLAG_CAD_UPLOAD, "1")
    app = FastAPI()
    app.include_router(cad_upload.router)
    return TestClient(app)


def _post(client: TestClient, name: str = "probe.dxf") -> object:
    return client.post(
        "/api/cad/upload",
        files={"file": (name, io.BytesIO(VALID_DXF), "application/dxf")},
    )


def test_dir_resolution_is_writable_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # 1. Explicit override wins outright.
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "drawings" / "uploads"))
    assert cad_upload.cad_upload_dir() == tmp_path / "explicit"
    # 2. Without the override, a SIBLING of the deployment's writable uploads
    #    volume — /data/drawings/uploads -> /data/drawings/cad_uploads.
    monkeypatch.delenv("LEAF_CAD_UPLOAD_DIR")
    assert cad_upload.cad_upload_dir() == tmp_path / "drawings" / "cad_uploads"
    # 3. Local-dev default only when neither is set.
    monkeypatch.delenv("LEAF_UPLOADS_DIR")
    assert cad_upload.cad_upload_dir() == cad_upload.PROJECT_ROOT / "data" / "cad_uploads"


def test_accept_path_writes_via_the_uploads_sibling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("LEAF_CAD_UPLOAD_DIR", raising=False)
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "drawings" / "uploads"))
    resp = _post(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    sibling = tmp_path / "drawings" / "cad_uploads"
    assert (sibling / f"{body['upload_id']}.dxf").is_file()
    assert (sibling / "receipts.jsonl").is_file()


def test_unwritable_store_is_a_typed_503_with_zero_orphans(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # Point the store at a path whose parent is a FILE — every mkdir refuses,
    # deterministically, on every platform. The live staging shape (read-only
    # root fs) fails on the same OSError path.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(blocker / "cad_uploads"))
    resp = _post(client)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["retryable"] is True
    assert "unavailable" in body["error"]["message"]
    # No filesystem layout leaks onto the wire.
    assert str(tmp_path) not in resp.text
    # Zero orphan bytes: the blocker file is untouched and nothing appeared.
    assert blocker.read_text() == "not a directory"


def test_receipt_append_failure_unlinks_the_staged_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(tmp_path))

    def _refuse_append(directory: Path, receipt: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cad_upload, "_append_receipt", _refuse_append)
    with pytest.raises(cad_upload.CadUploadStoreUnavailable):
        cad_upload._write_accepted_upload("probe.dxf", ".dxf", VALID_DXF)
    # The staged bytes were removed: an upload without its receipt row must
    # not exist (zero-orphan invariant on the FAILURE path).
    assert list(tmp_path.glob("*.dxf")) == []
