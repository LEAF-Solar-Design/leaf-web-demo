"""CAD upload/edit fence: standalone negative controls (Lane C1, card C1-7).

Both controls target the REAL landed surfaces:

1. cad_upload OFF refuses everything. Drives the REAL
   ``server/routers/cad_upload.py`` route (``POST /api/cad/upload``) mounted
   on a TestClient app exactly as ``server/tests/test_cad_upload_validation.py``
   mounts it, with the real ``LEAF_CAD_UPLOAD_ENABLED`` boolean flag left off.
   Asserts the real flag-off envelope (503 INTERNAL, never 405/500) AND that
   the store saw zero writes -- inspected directly, not inferred from the
   status code alone.

2. cad_edit OFF never mounts the worker. ``cad_edit`` has no server-side
   route at all on this revision (C1-5/C1-6 landed it entirely client-side --
   ``web/src/cad/engineWorker.js``'s ``EngineBoundary`` + ``CadEntry.jsx`` /
   ``EditSurface.jsx``), so a FastAPI route-mount check here would be a toy
   proving nothing real. The genuine flip-time proof for this control lives
   in ``harness/tests/cad_upload.spec.ts``, which imports the real
   ``EngineBoundary`` and spies on the ``Worker`` constructor.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import cad_upload as cad_upload_router

VALID_DWG = b"AC1032" + os.urandom(64)


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


# --- 1. cad_upload OFF refuses everything, against the REAL route ----------

@pytest.mark.parametrize("flag_value", [None, "", "0", "false", "no", "off",
                                         "not-a-real-mode", "  "])
def test_cad_upload_off_refuses_the_real_route(monkeypatch, tmp_path, flag_value):
    if flag_value is None:
        monkeypatch.delenv(cad_upload_router.FLAG_CAD_UPLOAD, raising=False)
    else:
        monkeypatch.setenv(cad_upload_router.FLAG_CAD_UPLOAD, flag_value)
    upload_dir = tmp_path / "cad_uploads"
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(upload_dir))
    assert cad_upload_router.cad_upload_enabled() is False

    app = FastAPI()
    app.include_router(cad_upload_router.router)
    resp = TestClient(app).post(
        "/api/cad/upload",
        files={"file": ("plan.dwg", VALID_DWG, "application/octet-stream")})

    # The real flag-off envelope: a typed 503, never a routing 405/500.
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["error_code"] == "INTERNAL"

    # Nothing was written: the store directory itself was never even created.
    assert not upload_dir.exists()
    assert _staged_files(upload_dir) == []


def test_cad_upload_flip_time_on_then_off_against_the_real_route(monkeypatch, tmp_path):
    upload_dir = tmp_path / "cad_uploads"
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(upload_dir))
    app = FastAPI()
    app.include_router(cad_upload_router.router)
    client = TestClient(app)

    monkeypatch.setenv(cad_upload_router.FLAG_CAD_UPLOAD, "1")
    on_resp = _post(client, "plan.dwg", VALID_DWG)
    assert on_resp.status_code == 201
    assert len(_staged_files(upload_dir)) == 1

    monkeypatch.setenv(cad_upload_router.FLAG_CAD_UPLOAD, "0")
    off_resp = _post(client, "plan2.dwg", VALID_DWG)
    assert off_resp.status_code == 503
    assert off_resp.json()["error"]["error_code"] == "INTERNAL"
    # No second file landed once the flag flipped off, in the SAME process.
    assert len(_staged_files(upload_dir)) == 1


# --- 2. cad_edit OFF never mounts the worker --------------------------------
#
# No server-side route exists for cad_edit on this revision -- see module
# docstring. The real flip-time proof (Worker-constructor spy against the
# real EngineBoundary) lives in harness/tests/cad_upload.spec.ts.
