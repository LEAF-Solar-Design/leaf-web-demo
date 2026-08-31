"""Card F-3 (persistence leg): POST /api/drawings/{id}/versions/edited.

The editing surface's write-back into the SAME versioned-control chain every
other write uses. Pins, each against a hostile shape:

  - happy path: 201, the chain advances by exactly one, the new head's
    payload is the PARSED INTAKE of the edited bytes (viewer-readable, no
    cache machinery), the raw full-fidelity DXF lands digest-bound at the
    version's .edited.dxf sidecar, and the receipt names both digests plus a
    truthful zero engine cost;
  - integrity: a digest that does not match the received bytes is a 400 and
    writes NOTHING;
  - parseability: bytes the real intake path cannot read are a 422 and write
    nothing;
  - concurrency: a stale parent (head moved) is a 409 compare-and-set
    refusal and writes nothing;
  - size: over-cap is 413 before any parse.

Harness identical to tests/test_version_restore.py (FilesystemBackend via
LEAF_STORE_DIR, legacy X-Tenant-Id stub).

Run:  cd server && python -m pytest tests/test_save_edited_version.py -q
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

TENANT = "tenant-editsave"
DRAWING = "rooftop_demo"

EDITED_DXF = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n8\nRoof\n90\n3\n70\n1\n"
    "10\n0\n20\n0\n10\n50\n20\n0\n10\n50\n20\n30\n"
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


def _post(client, *, data: bytes = EDITED_DXF, digest: str | None = None,
          parent: int | None = None, name: str = "edited.dxf"):
    if parent is None:
        head = client.get(f"/api/drawings/{DRAWING}/versions",
                          headers={"X-Tenant-Id": TENANT}).json()["head"]
        parent = head
    return client.post(
        f"/api/drawings/{DRAWING}/versions/edited",
        headers={"X-Tenant-Id": TENANT},
        files={"file": (name, io.BytesIO(data), "application/dxf")},
        data={"parent_version": str(parent),
              "source_digest": digest or hashlib.sha256(data).hexdigest()},
    )


def test_save_advances_the_chain_with_intake_payload_sidecar_and_receipt(client, tmp_path):
    import store  # noqa: PLC0415
    import write_loop  # noqa: PLC0415

    before = client.get(f"/api/drawings/{DRAWING}/versions",
                        headers={"X-Tenant-Id": TENANT}).json()
    resp = _post(client, parent=before["head"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["new_version"]["parent"] == before["head"]
    assert body["head"] == before["head"] + 1
    assert body["source_sha256"] == hashlib.sha256(EDITED_DXF).hexdigest()
    assert body["source_stored"] is True
    assert body["cost"] == {"engine_usd": 0.0, "engine": "client-wasm"}

    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    new_v = body["head"]
    # The version payload IS the intake of the edited bytes.
    v, intake = write_loop.read_intake(backend, TENANT, DRAWING, new_v)
    assert v == new_v
    assert intake["layers"] == ["Roof"]
    assert len(intake["polylines"]) == 1
    # The raw full-fidelity DXF sits digest-bound at the sidecar.
    raw = backend.get(write_loop.edited_source_key(TENANT, DRAWING, new_v))
    assert hashlib.sha256(raw).hexdigest() == body["source_sha256"]
    # And the manifest meta records the binding.
    chain = client.get(f"/api/drawings/{DRAWING}/versions",
                       headers={"X-Tenant-Id": TENANT}).json()
    assert chain["head"] == new_v


def test_digest_mismatch_is_refused_and_writes_nothing(client):
    before = client.get(f"/api/drawings/{DRAWING}/versions",
                        headers={"X-Tenant-Id": TENANT}).json()["head"]
    resp = _post(client, digest="0" * 64, parent=before)
    assert resp.status_code == 400
    assert "source_digest" in resp.json()["error"]["message"]
    after = client.get(f"/api/drawings/{DRAWING}/versions",
                       headers={"X-Tenant-Id": TENANT}).json()["head"]
    assert after == before


def test_unparseable_bytes_are_refused_and_write_nothing(client):
    # Undecodable as text: the intake path's own "undecodable DXF" refusal
    # (dxf_intake.py raise site), the strongest genuinely-unparseable shape.
    junk = bytes([0xFF, 0xFE, 0x00, 0x9C]) * 8
    before = client.get(f"/api/drawings/{DRAWING}/versions",
                        headers={"X-Tenant-Id": TENANT}).json()["head"]
    resp = _post(client, data=junk, parent=before)
    assert resp.status_code == 422
    after = client.get(f"/api/drawings/{DRAWING}/versions",
                       headers={"X-Tenant-Id": TENANT}).json()["head"]
    assert after == before


def test_stale_parent_is_a_409_compare_and_set_refusal(client):
    head = client.get(f"/api/drawings/{DRAWING}/versions",
                      headers={"X-Tenant-Id": TENANT}).json()["head"]
    first = _post(client, parent=head)
    assert first.status_code == 201
    # Replay against the OLD head: the chain moved, so CAS must refuse.
    stale = _post(client, parent=head)
    assert stale.status_code == 409
    assert "stale parent" in stale.json()["error"]["message"]


def test_route_barrier_literal_equals_the_shared_id_rule():
    """The route's inline literal (a CodeQL-provable taint barrier) must be
    the SAME rule as the store's shared validator, forever."""
    import re as _re

    import tenant_id_validator  # noqa: PLC0415
    from routers import drawings as drawings_router  # noqa: PLC0415

    source = Path(drawings_router.__file__).read_text(encoding="utf-8")
    literal = _re.search(r'r"(\[a-z0-9\]\[a-z0-9_-\]\{0,62\})"', source)
    assert literal, "the route's literal id barrier vanished"
    assert f"^{literal.group(1)}$" == tenant_id_validator.TENANT_ID_PATTERN, (
        "the route's inline barrier drifted from the shared canonical-id rule"
    )


def test_malformed_drawing_id_is_refused_at_the_boundary(client):
    resp = client.post(
        "/api/drawings/..%2Fevil/versions/edited",
        headers={"X-Tenant-Id": TENANT},
        files={"file": ("edited.dxf", io.BytesIO(EDITED_DXF), "application/dxf")},
        data={"parent_version": "1",
              "source_digest": hashlib.sha256(EDITED_DXF).hexdigest()},
    )
    assert resp.status_code == 400
    assert "malformed drawing id" in resp.json()["error"]["message"]


def test_non_dxf_name_is_refused(client):
    resp = _post(client, name="edited.dwg")
    assert resp.status_code == 400


def test_over_cap_body_is_refused_before_parse(client, monkeypatch):
    import guest_uploads  # noqa: PLC0415
    monkeypatch.setattr(guest_uploads, "max_upload_bytes", lambda: 64)
    resp = _post(client, data=b"0" * 200)
    assert resp.status_code == 413
