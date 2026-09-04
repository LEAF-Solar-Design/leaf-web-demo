"""W4g-1 (engine reach): GET /api/drawings/{id}/dxf serves a version as DXF.

Pins, each leg on its own terms:
  - a mock version (payload = intake JSON) is synthesized (X-Leaf-Dxf-Source
    intake-synth), parses back to the head's own layers and polylines, and
    carries ETag = sha256(body), X-Leaf-Version, X-Leaf-Head;
  - a browser-edited version serves its sidecar byte for byte
    (edited-sidecar) when the sidecar is bound to the payload, and falls to
    the payload synth when the sidecar was swapped underneath;
  - If-None-Match with the current ETag answers 304 and no body;
  - a raw DWG version converts through dwg_convert (dwg2dxf) exactly once and
    is served from the digest-bound cache afterwards; a cache whose proof does
    not bind is refused (503), never served; a deployment without the
    converter answers 503 with the honest sentence;
  - over the engine ceiling is 413; unknown version 404; malformed id 400;
    a guest tenant's unknown drawing 404 (no demo bootstrap for guests).

Harness identical to tests/test_save_edited_version.py (FilesystemBackend via
LEAF_STORE_DIR, legacy X-Tenant-Id stub).

Run:  cd server && python -m pytest tests/test_drawing_dxf_route.py -q
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

TENANT = "tenant-dxfroute"
DRAWING = "rooftop_demo"
H = {"X-Tenant-Id": TENANT}

EDITED_DXF = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n5\n1A\n8\nRoof\n90\n3\n70\n1\n"
    "10\n0\n20\n0\n10\n50\n20\n0\n10\n50\n20\n30\n"
    "0\nENDSEC\n0\nEOF\n"
).encode("ascii")

CONVERTED_DXF = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLINE\n5\n2B\n8\nWalls\n10\n0\n20\n0\n11\n10\n21\n10\n"
    "0\nENDSEC\n0\nEOF\n"
).encode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("APS_LIVE", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)

    from routers import drawings as drawings_router  # noqa: PLC0415
    from envelopes import install_error_handlers  # noqa: PLC0415

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _backend(tmp_path):
    import store  # noqa: PLC0415
    return store.FilesystemBackend(str(tmp_path / "drawings"))


def _get(client, drawing=DRAWING, headers=None, **params):
    return client.get(f"/api/drawings/{drawing}/dxf", headers=headers or H, params=params)


def _save_edited(client, data=EDITED_DXF):
    head = client.get(f"/api/drawings/{DRAWING}/versions", headers=H).json()["head"]
    resp = client.post(
        f"/api/drawings/{DRAWING}/versions/edited", headers=H,
        files={"file": ("edited.dxf", io.BytesIO(data), "application/dxf")},
        data={"parent_version": str(head), "source_digest": hashlib.sha256(data).hexdigest()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["head"]


def test_mock_version_is_synthesized_and_parses_back_to_its_own_intake(client):
    import dxf_intake  # noqa: PLC0415

    resp = _get(client)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/dxf")
    assert resp.headers["x-leaf-dxf-source"] == "intake-synth"
    assert resp.headers["x-leaf-version"] == "1"
    assert resp.headers["x-leaf-head"] == "1"
    assert resp.headers["etag"] == '"' + hashlib.sha256(resp.content).hexdigest() + '"'
    assert resp.headers["cache-control"] == "private, no-cache"
    intake = client.get(f"/api/drawings/{DRAWING}/intake", headers=H).json()["intake"]
    back = dxf_intake.parse_dxf_bytes(resp.content, source_name=intake["dwg"])
    assert back["layers"] == intake["layers"]
    assert back["polylines"] == intake["polylines"]
    assert len(back["polylines"]) > 2000


def test_edited_version_serves_its_bound_sidecar_byte_for_byte(client):
    new_v = _save_edited(client)
    resp = _get(client)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-leaf-dxf-source"] == "edited-sidecar"
    assert resp.headers["x-leaf-version"] == str(new_v)
    assert resp.content == EDITED_DXF
    # An explicit older version still resolves to its own leg.
    old = _get(client, version="1")
    assert old.status_code == 200
    assert old.headers["x-leaf-dxf-source"] == "intake-synth"
    assert old.headers["x-leaf-head"] == str(new_v)


def test_swapped_sidecar_is_never_served_the_payload_wins(client, tmp_path):
    import write_loop  # noqa: PLC0415

    new_v = _save_edited(client)
    backend = _backend(tmp_path)
    swapped = EDITED_DXF.replace(b"\n50\n20\n30\n", b"\n50\n20\n99\n")
    backend.put(write_loop.edited_source_key(TENANT, DRAWING, new_v), swapped)
    resp = _get(client)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-leaf-dxf-source"] == "intake-synth"
    assert resp.content != swapped
    assert b"\n20\n30.0\n" in resp.content  # the payload's own geometry


def test_if_none_match_answers_304_without_a_body(client):
    first = _get(client)
    etag = first.headers["etag"]
    again = client.get(f"/api/drawings/{DRAWING}/dxf", headers={**H, "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    stale = client.get(f"/api/drawings/{DRAWING}/dxf", headers={**H, "If-None-Match": '"0"'})
    assert stale.status_code == 200


def _ingest_dwg(tmp_path, drawing_id, payload=b"AC1032" + b"\x00" * 64):
    import store  # noqa: PLC0415

    backend = _backend(tmp_path)
    p = tmp_path / "x.dwg"
    p.write_bytes(payload)
    store.ingest_drawing(backend, TENANT, str(p), drawing_id=drawing_id)
    return backend


def _fake_converter(monkeypatch, calls):
    import dwg_convert  # noqa: PLC0415

    @contextlib.contextmanager
    def fake(source):
        calls.append(Path(source).read_bytes())
        out = Path(source).parent / "converted.dxf"
        out.write_bytes(CONVERTED_DXF)
        try:
            yield out
        finally:
            out.unlink(missing_ok=True)

    monkeypatch.setattr(dwg_convert, "converted_dxf", fake)


def test_dwg_version_converts_once_then_serves_the_digest_bound_cache(client, tmp_path, monkeypatch):
    import write_loop  # noqa: PLC0415

    backend = _ingest_dwg(tmp_path, "dwgdoc")
    calls: list = []
    _fake_converter(monkeypatch, calls)
    first = _get(client, drawing="dwgdoc")
    assert first.status_code == 200, first.text
    assert first.headers["x-leaf-dxf-source"] == "dwg2dxf"
    assert first.content == CONVERTED_DXF
    assert len(calls) == 1 and calls[0].startswith(b"AC1032")
    # Cache + proof landed beside the version.
    ckey = write_loop.dxf_cache_key(TENANT, "dwgdoc", 1)
    assert backend.get(ckey) == CONVERTED_DXF
    proof = json.loads(backend.get(write_loop.dxf_cache_proof_key(TENANT, "dwgdoc", 1)))
    assert proof["dxf_sha256"] == hashlib.sha256(CONVERTED_DXF).hexdigest()
    assert proof["source_sha256"] == hashlib.sha256(b"AC1032" + b"\x00" * 64).hexdigest()
    second = _get(client, drawing="dwgdoc")
    assert second.status_code == 200
    assert second.headers["x-leaf-dxf-source"] == "dwg2dxf-cache"
    assert second.content == CONVERTED_DXF
    assert len(calls) == 1  # no second conversion


def test_unbound_cache_is_refused_not_served(client, tmp_path, monkeypatch):
    import write_loop  # noqa: PLC0415

    backend = _ingest_dwg(tmp_path, "dwgdoc")
    calls: list = []
    _fake_converter(monkeypatch, calls)
    assert _get(client, drawing="dwgdoc").status_code == 200
    # Swap the cached blob: the proof no longer binds it.
    backend.put(write_loop.dxf_cache_key(TENANT, "dwgdoc", 1), CONVERTED_DXF.replace(b"Walls", b"Other"))
    resp = _get(client, drawing="dwgdoc")
    assert resp.status_code == 503
    assert "not bound" in resp.json()["error"]["message"]
    assert len(calls) == 1


def test_no_converter_on_this_deployment_is_a_503_with_the_sentence(client, tmp_path, monkeypatch):
    import dwg_convert  # noqa: PLC0415

    _ingest_dwg(tmp_path, "dwgdoc")
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    resp = _get(client, drawing="dwgdoc")
    assert resp.status_code == 503
    assert "not available on this deployment" in resp.json()["error"]["message"]


def test_payload_that_is_neither_intake_nor_dwg_is_422(client, tmp_path):
    _ingest_dwg(tmp_path, "junkdoc", payload=b"\x00\x01\x02 not a drawing")
    resp = _get(client, drawing="junkdoc")
    assert resp.status_code == 422
    assert "neither intake JSON nor a DWG" in resp.json()["error"]["message"]


def test_over_the_engine_ceiling_is_413(client, monkeypatch):
    import write_loop  # noqa: PLC0415

    monkeypatch.setattr(write_loop, "MAX_DXF_BYTES", 1024)
    resp = _get(client)
    assert resp.status_code == 413
    assert "ceiling" in resp.json()["error"]["message"]


def test_unknown_version_malformed_id_and_guest_unknown_drawing(client):
    assert _get(client, version="42").status_code == 404
    assert _get(client, version="abc").status_code == 404
    assert client.get("/api/drawings/Bad..Id/dxf", headers=H).status_code == 400
    guest = client.get("/api/drawings/nothere/dxf", headers={"X-Tenant-Id": "guest-abc123"})
    assert guest.status_code == 404
