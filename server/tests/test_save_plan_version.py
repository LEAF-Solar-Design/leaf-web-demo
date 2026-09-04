"""W4g-3b (one head): POST /api/drawings/{id}/versions/plan, the browser
edit's save through the commit leg the SERVER picks.

Pins, each against a hostile shape:

  - an intake-backed head (the demo) takes the DXF sidecar leg: 201,
    `commit: "dxf-sidecar"`, the chain +1, the payload is the parsed intake
    of the bytes, the sidecar stored, the note says why;
  - a DWG-backed head at APS_LIVE=0 takes the plan leg: `commit: "dwg-plan"`,
    the plan validated against the head's intake and applied by the mock
    writer, the payload is the applied intake (the engine reopens it through
    the synthesizer), `plan_sha256` names the lowered plan, no sidecar;
  - a DWG-backed head at APS_LIVE=1 takes the sidecar until W4g-3c and says so;
  - a plan naming no operation takes the sidecar and says so;
  - a plan the contract refuses (an unknown handle) is a 422 that writes
    NOTHING; a plan that is not JSON is a 400; an oversized one a 413;
  - a stale parent is a 409 that writes nothing; the F-3 integrity half
    (digest mismatch 400, unparseable 422) holds verbatim.

Run:  cd server && python -m pytest tests/test_save_plan_version.py -q
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

TENANT = "tenant-plansave"
DEMO = "rooftop_demo"
DWG_DRAWING = "dwgbacked"

EDITED_DXF = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n5\nA\n8\nRoof\n90\n3\n70\n1\n"
    "10\n0\n20\n0\n10\n50\n20\n0\n10\n50\n20\n30\n"
    "0\nLINE\n5\n1F\n8\nNew\n10\n1\n20\n2\n30\n0\n11\n4\n21\n6\n31\n0\n"
    "0\nENDSEC\n0\nEOF\n"
).encode("ascii")


def _entity(handle, layer="Panels", z=0.0):
    return {
        "handle": handle, "layer": layer, "closed": True, "xdata": None,
        "pts": [[0.0, 0.0, z], [2.0, 0.0, z], [2.0, 2.0, z], [0.0, 2.0, z]],
    }


def _base_intake():
    return {"dwg": "source.dwg", "layers": ["Panels"],
            "polylines": [_entity("A"), _entity("B", z=3.0)]}


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


def _seed_dwg_backed(tmp_path):
    """A DWG-backed drawing at v1 with its intake cache published (the shape a
    live ingest leaves), under the same store dir the router resolves."""
    import store  # noqa: PLC0415
    import write_loop  # noqa: PLC0415

    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    source = tmp_path / "base.dwg"
    source.write_bytes(b"AC1032" + b"\x00" * 64)
    store.ingest_drawing(backend, TENANT, str(source), drawing_id=DWG_DRAWING)
    write_loop.publish_intake_cache(
        backend, TENANT, DWG_DRAWING, 1, source.read_bytes(), _base_intake())
    return backend


def _head(client, drawing):
    return client.get(f"/api/drawings/{drawing}/versions",
                      headers={"X-Tenant-Id": TENANT}).json()["head"]


def _post(client, drawing, mutations, *, data: bytes = EDITED_DXF,
          digest: str | None = None, parent: int | None = None,
          plan: str | None = None, name: str = "edited.dxf"):
    if parent is None:
        parent = _head(client, drawing)
    return client.post(
        f"/api/drawings/{drawing}/versions/plan",
        headers={"X-Tenant-Id": TENANT},
        files={"file": (name, io.BytesIO(data), "application/dxf")},
        data={"parent_version": str(parent),
              "source_digest": digest or hashlib.sha256(data).hexdigest(),
              "plan": plan if plan is not None else json.dumps({"mutations": mutations})},
    )


def test_intake_backed_head_takes_the_sidecar_leg_and_says_why(client, tmp_path):
    import store  # noqa: PLC0415
    import write_loop  # noqa: PLC0415

    before = _head(client, DEMO)
    resp = _post(client, DEMO, {"added": [{"handle": "n1", "kind": "LINE", "layer": "New", "pts": [[1, 2], [4, 6]]}]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit"] == "dxf-sidecar"
    assert "no DWG source" in body["commit_note"]
    assert body["plan_sha256"] is None
    assert body["new_version"] == {"drawing_id": DEMO, "version": before + 1, "parent": before}
    assert body["head"] == before + 1
    assert body["source_sha256"] == hashlib.sha256(EDITED_DXF).hexdigest()
    assert body["source_stored"] is True
    assert body["cost"] == {"engine_usd": 0.0, "engine": "client-wasm"}
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    assert backend.exists(write_loop.edited_source_key(TENANT, DEMO, before + 1))
    # The payload is the PARSED intake of the bytes: the LINE as a 2-point
    # open polyline beside the closed one.
    view = client.get(f"/api/drawings/{DEMO}/intake?version=head",
                      headers={"X-Tenant-Id": TENANT}).json()
    handles = sorted(p["handle"] for p in view["intake"]["polylines"])
    assert handles == ["1F", "A"]


def test_dwg_backed_head_takes_the_plan_leg_through_the_mock_writer(client, tmp_path):
    import store  # noqa: PLC0415
    import write_loop  # noqa: PLC0415

    backend = _seed_dwg_backed(tmp_path)
    assert _head(client, DWG_DRAWING) == 1
    resp = _post(client, DWG_DRAWING, {
        "added": [{"handle": "n1", "kind": "LINE", "layer": "New", "pts": [[1, 2], [4, 6]]},
                  {"handle": "n2", "kind": "CIRCLE", "layer": "Round", "c": [10, 10], "r": 3}],
        "set_layer": [{"handle": "A", "layer": "Moved"}],
        "removed": ["B"],
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit"] == "dwg-plan"
    assert "mock writer" in body["commit_note"]
    assert len(body["plan_sha256"]) == 64
    assert body["new_version"] == {"drawing_id": DWG_DRAWING, "version": 2, "parent": 1}
    assert body["source_stored"] is False
    assert body["cost"] == {"engine_usd": 0.0, "engine": "mock-writer"}
    assert not backend.exists(write_loop.edited_source_key(TENANT, DWG_DRAWING, 2))
    view = client.get(f"/api/drawings/{DWG_DRAWING}/intake?version=head",
                      headers={"X-Tenant-Id": TENANT}).json()["intake"]
    by = {p["handle"]: p for p in view["polylines"]}
    assert set(by) == {"A", "n1"}
    assert by["A"]["layer"] == "Moved"
    assert by["n1"]["pts"] == [[1.0, 2.0, 0.0], [4.0, 6.0, 0.0]] and by["n1"]["closed"] is False
    assert view["circles"] == [{"handle": "n2", "layer": "Round", "c": [10.0, 10.0, 0.0], "r": 3.0, "nrm": [0.0, 0.0, 1.0]}]
    assert "New" in view["layers"] and "Round" in view["layers"] and "Moved" in view["layers"]
    # The engine reopens the new head through the synthesizer, circle included.
    dxf = client.get(f"/api/drawings/{DWG_DRAWING}/dxf?version=head",
                     headers={"X-Tenant-Id": TENANT})
    assert dxf.status_code == 200 and dxf.headers["x-leaf-dxf-source"] == "intake-synth"
    # The synthesizer writes every polyline (the LINE included) as LWPOLYLINE.
    assert b"\nCIRCLE\n" in dxf.content and dxf.content.count(b"\nLWPOLYLINE\n") == 2


def test_dwg_backed_head_at_aps_live_takes_the_sidecar_until_3c(client, tmp_path, monkeypatch):
    import deps  # noqa: PLC0415

    _seed_dwg_backed(tmp_path)
    monkeypatch.setattr(deps, "APS_LIVE", True)
    resp = _post(client, DWG_DRAWING, {"set_layer": [{"handle": "A", "layer": "Moved"}]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit"] == "dxf-sidecar"
    assert "W4g-3c" in body["commit_note"]
    assert body["source_stored"] is True


def test_a_plan_naming_no_operation_takes_the_sidecar_and_says_so(client, tmp_path):
    _seed_dwg_backed(tmp_path)
    resp = _post(client, DWG_DRAWING, {})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit"] == "dxf-sidecar"
    assert "names no operation" in body["commit_note"]


def test_a_refused_plan_is_a_422_that_writes_nothing(client, tmp_path):
    _seed_dwg_backed(tmp_path)
    resp = _post(client, DWG_DRAWING, {"removed": ["FF"]})
    assert resp.status_code == 422, resp.text
    assert "the edit plan was refused" in resp.json()["error"]["message"]
    assert "unknown removed handle" in resp.json()["error"]["message"]
    assert _head(client, DWG_DRAWING) == 1


@pytest.mark.parametrize("plan,status,needle", [
    pytest.param("not json", 400, "plan is not JSON", id="not-json"),
    pytest.param(json.dumps(["mutations"]), 400, "mutations object", id="a-list"),
    pytest.param(json.dumps({"mutations": "x"}), 400, "mutations object", id="mutations-not-an-object"),
    # A short id: pytest exports the test id as an environment variable, which
    # Windows caps at 32 K, so a 600 KB string must not name the case. 600 KB
    # is over the route's cap and under the multipart parser's 1024 KB part
    # cap, so the route's typed 413 is the answer, never the parser's 400.
    pytest.param("{" + " " * (600 * 1024) + "}", 413, "byte cap", id="oversized"),
])
def test_a_malformed_plan_field_is_refused_before_any_store_call(client, plan, status, needle):
    before = _head(client, DEMO)
    resp = _post(client, DEMO, {}, plan=plan)
    assert resp.status_code == status, resp.text
    assert needle in resp.json()["error"]["message"]
    assert _head(client, DEMO) == before


def test_stale_parent_is_a_409_that_writes_nothing(client, tmp_path):
    _seed_dwg_backed(tmp_path)
    resp = _post(client, DWG_DRAWING, {"set_layer": [{"handle": "A", "layer": "Moved"}]}, parent=7)
    assert resp.status_code == 409, resp.text
    assert "stale parent" in resp.json()["error"]["message"]
    assert _head(client, DWG_DRAWING) == 1


def test_the_f3_integrity_half_holds_verbatim(client):
    before = _head(client, DEMO)
    bad_digest = _post(client, DEMO, {}, digest="0" * 64)
    assert bad_digest.status_code == 400 and "source_digest" in bad_digest.json()["error"]["message"]
    unparseable = _post(client, DEMO, {}, data=b"not a dxf at all")
    assert unparseable.status_code == 422
    wrong_name = _post(client, DEMO, {}, name="edited.dwg")
    assert wrong_name.status_code == 400
    assert _head(client, DEMO) == before
