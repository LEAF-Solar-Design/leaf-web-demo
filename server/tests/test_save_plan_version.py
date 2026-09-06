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
  - a DWG-backed head at APS_LIVE=1 takes the sidecar while the live leg is
    gated off; when enabled, a matching DXF and a sufficient checkout lease
    submit a plan job without writing a version, unless the payload is over cap;
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
    monkeypatch.delenv("LEAF_PLAN_LIVE_LEG", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)

    from routers import drawings as drawings_router  # noqa: PLC0415
    from envelopes import install_error_handlers  # noqa: PLC0415

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _seed_dwg_backed(tmp_path, *, intake=None):
    """A DWG-backed drawing at v1 with its intake cache published (the shape a
    live ingest leaves), under the same store dir the router resolves."""
    import store  # noqa: PLC0415
    import write_loop  # noqa: PLC0415

    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    source = tmp_path / "base.dwg"
    source.write_bytes(b"AC1032" + b"\x00" * 64)
    store.ingest_drawing(backend, TENANT, str(source), drawing_id=DWG_DRAWING)
    write_loop.publish_intake_cache(
        backend, TENANT, DWG_DRAWING, 1, source.read_bytes(),
        _base_intake() if intake is None else intake)
    return backend


def _live_base_intake():
    """The uploaded DXF's entities before its LINE endpoint edit."""
    return {
        "dwg": "source.dwg", "layers": ["New", "Roof"],
        "polylines": [
            {"handle": "A", "layer": "Roof", "closed": True, "xdata": None,
             "pts": [[0.0, 0.0, 0.0], [50.0, 0.0, 0.0], [50.0, 30.0, 0.0]]},
            {"handle": "1F", "layer": "New", "closed": False, "xdata": None,
             "pts": [[1.0, 2.0, 0.0], [3.0, 5.0, 0.0]]},
        ],
    }


def _live_mutations():
    return {"set_points": [{"handle": "1F", "pts": [[1, 2], [4, 6]]}]}


@pytest.fixture()
def plan_submissions(client, monkeypatch):
    import jobs  # noqa: PLC0415

    calls = []

    def record(tenant_id, plan, dwg, *, checkout_holder, checkout_fence):
        calls.append({"tenant_id": tenant_id, "plan": plan, "dwg": dwg,
                      "checkout_holder": checkout_holder, "checkout_fence": checkout_fence})
        return "live-plan-job"

    monkeypatch.setattr(jobs, "submit_plan_job", record)
    return calls


@pytest.fixture()
def live(client, tmp_path, monkeypatch, plan_submissions, request):
    import deps  # noqa: PLC0415
    import jobs  # noqa: PLC0415

    monkeypatch.setattr(deps, "APS_LIVE", True)
    monkeypatch.setenv("LEAF_PLAN_LIVE_LEG", "1")
    monkeypatch.setattr(jobs, "job_max_s", lambda: 540)
    intake = request.param if hasattr(request, "param") else _live_base_intake()
    backend = _seed_dwg_backed(tmp_path, intake=intake)
    return client, backend, plan_submissions


def _checkout(client):
    resp = client.post(
        f"/api/drawings/{DWG_DRAWING}/checkout",
        headers={"X-Tenant-Id": TENANT},
        json={"holder": "plan-editor", "ttl_s": 3600},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["acquired"] is True
    return resp.json()["checkout_capability"]


def _head(client, drawing):
    return client.get(f"/api/drawings/{drawing}/versions",
                      headers={"X-Tenant-Id": TENANT}).json()["head"]


def _post(client, drawing, mutations, *, data: bytes = EDITED_DXF,
          digest: str | None = None, parent: int | None = None,
          plan: str | None = None, name: str = "edited.dxf",
          capability: str | None = None):
    if parent is None:
        parent = _head(client, drawing)
    headers = {"X-Tenant-Id": TENANT}
    if capability is not None:
        headers["X-Checkout-Capability"] = capability
    return client.post(
        f"/api/drawings/{drawing}/versions/plan",
        headers=headers,
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


def test_dwg_backed_head_at_aps_live_takes_the_sidecar_while_the_live_leg_is_gated_off(
        client, tmp_path, monkeypatch, plan_submissions):
    import deps  # noqa: PLC0415

    _seed_dwg_backed(tmp_path)
    monkeypatch.setattr(deps, "APS_LIVE", True)
    monkeypatch.delenv("LEAF_PLAN_LIVE_LEG", raising=False)
    resp = _post(client, DWG_DRAWING, {"set_layer": [{"handle": "A", "layer": "Moved"}]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit"] == "dxf-sidecar"
    assert body["commit_note"] == (
        "live plan commit is gated off (LEAF_PLAN_LIVE_LEG); the DXF carries this save")
    assert body["source_stored"] is True
    assert plan_submissions == []


def test_live_leg_submits_a_plan_job_and_writes_nothing(live):
    import mutation_plan  # noqa: PLC0415
    import store  # noqa: PLC0415
    import write_loop  # noqa: PLC0415

    client, backend, submissions = live
    capability = _checkout(client)
    before = store.load_manifest(backend, TENANT, DWG_DRAWING)
    co = before["checkout"]
    mutations = _live_mutations()
    _, vkey = store.resolve_version(backend, TENANT, DWG_DRAWING, 1)
    canonical = mutation_plan.validate_mutations(
        _live_base_intake(), mutations, allow_transforms=True, allow_xdata=False)
    plan_digest = mutation_plan.plan_sha256(mutation_plan.emit_plan(
        canonical, base_sha256=hashlib.sha256(backend.get(vkey)).hexdigest(),
        base_intake=_live_base_intake()))

    resp = _post(client, DWG_DRAWING, mutations, capability=capability)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["drawing_id"] == DWG_DRAWING
    assert body["job_id"] == "live-plan-job"
    assert body["status"] == "submitted"
    assert body["commit"] == "dwg-plan"
    assert body["commit_note"] == "live APS WorkItem; the job carries the receipt"
    assert body["parent"] == 1
    assert body["plan_sha256"] == plan_digest
    assert body["source_sha256"] == hashlib.sha256(EDITED_DXF).hexdigest()
    assert body["source_stored"] is False
    assert body["cost"] == {"engine": "aps-workitem", "engine_usd": None}
    assert submissions == [{
        "tenant_id": TENANT, "dwg": DWG_DRAWING,
        "plan": {"drawing_id": DWG_DRAWING, "parent_version": 1,
                 "mutations": canonical, "plan_sha256": plan_digest,
                 "source_sha256": hashlib.sha256(EDITED_DXF).hexdigest()},
        "checkout_holder": co["holder"], "checkout_fence": co["fence"],
    }]
    assert _head(client, DWG_DRAWING) == 1
    assert store.load_manifest(backend, TENANT, DWG_DRAWING) == before
    assert not backend.exists(write_loop.edited_source_key(TENANT, DWG_DRAWING, 2))


def test_live_leg_accepts_extractor_quantum_without_changing_the_plan(live):
    client, _backend, submissions = live
    capability = _checkout(client)
    mutations = {"set_points": [{"handle": "1F", "pts": [[1, 2], [4.0004, 6]]}]}
    data = EDITED_DXF.replace(b"11\n4\n21\n6\n", b"11\n4.0004\n21\n6\n")
    resp = _post(client, DWG_DRAWING, mutations, data=data, capability=capability)
    assert resp.status_code == 202, resp.text
    assert len(submissions) == 1
    points = submissions[0]["plan"]["mutations"]["set_points"][0]["pts"]
    assert points == [[1.0, 2.0, 0.0], [4.0004, 6.0, 0.0]]
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_quantizes_upload_through_the_plan_number_pipeline(live):
    client, _backend, submissions = live
    capability = _checkout(client)
    mutations = {"set_points": [{"handle": "1F", "pts": [[1, 2, 0], [4.0004999999996, 6, 0]]}]}
    data = EDITED_DXF.replace(b"11\n4\n21\n6\n", b"11\n4.0004999999996\n21\n6\n")
    resp = _post(client, DWG_DRAWING, mutations, data=data, capability=capability)
    assert resp.status_code == 202, resp.text
    assert len(submissions) == 1
    assert submissions[0]["plan"]["mutations"]["set_points"][0]["pts"] == mutations["set_points"][0]["pts"]
    assert _head(client, DWG_DRAWING) == 1


@pytest.mark.parametrize("live,kind,angles", [
    pytest.param({**_live_base_intake(), "circles": [
        {"handle": "2A", "layer": "New", "c": [0.0, 0.0, 0.0], "r": 3.0},
    ]}, "CIRCLE", "", id="circle"),
    pytest.param({**_live_base_intake(), "arcs": [
        {"handle": "2A", "layer": "New", "c": [0.0, 0.0, 0.0], "r": 3.0,
         "start_deg": 0.0, "end_deg": 90.0},
    ]}, "ARC", "50\n0\n51\n90\n", id="arc"),
], indirect=["live"])
@pytest.mark.parametrize("radius,status", [("3", 202), ("3.0004", 202), ("3.0014", 422)])
def test_live_leg_unchanged_round_entity_matches_head_at_quantum(live, kind, angles, radius, status):
    client, _backend, submissions = live
    capability = _checkout(client)
    entity = (f"0\n{kind}\n5\n2A\n8\nNew\n10\n0\n20\n0\n40\n{radius}\n" + angles).encode("ascii")
    data = EDITED_DXF.replace(b"0\nENDSEC\n", entity + b"0\nENDSEC\n")
    resp = _post(client, DWG_DRAWING, _live_mutations(), data=data, capability=capability)
    assert resp.status_code == status, resp.text
    if status == 202:
        assert len(submissions) == 1
    else:
        assert resp.json()["error"]["message"] == (
            "the uploaded DXF does not carry the plan's result: an unchanged entity differs from the head")
        assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


@pytest.mark.parametrize("live", [{**_live_base_intake(), "arcs": [
    {"handle": "2A", "layer": "New", "c": [0.0, 0.0, 0.0], "r": 3.0,
     "start_deg": 0.0, "end_deg": 90.0},
]}], indirect=True)
@pytest.mark.parametrize("angles", [b"50\n0.0006\n51\n90\n", b"50\n0\n51\n90.0006\n"])
def test_live_leg_unchanged_arc_angles_match_head_at_quantum(live, angles):
    client, _backend, submissions = live
    capability = _checkout(client)
    arc = b"0\nARC\n5\n2A\n8\nNew\n10\n0\n20\n0\n40\n3\n" + angles
    data = EDITED_DXF.replace(b"0\nENDSEC\n", arc + b"0\nENDSEC\n")
    resp = _post(client, DWG_DRAWING, _live_mutations(), data=data, capability=capability)
    assert resp.status_code == 422, resp.text
    assert "an unchanged entity differs from the head" in resp.json()["error"]["message"]
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_refuses_a_contradiction_beyond_the_extractor_quantum(live):
    client, _backend, submissions = live
    mutations = {"set_points": [{"handle": "1F", "pts": [[1, 2], [4.0004, 6]]}]}
    data = EDITED_DXF.replace(b"11\n4\n21\n6\n", b"11\n4.002\n21\n6\n")
    resp = _post(client, DWG_DRAWING, mutations, data=data)
    assert resp.status_code == 422, resp.text
    assert "does not carry the plan's result" in resp.json()["error"]["message"]
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_skips_text_comparison_when_head_does_not_list_texts(live):
    client, _backend, submissions = live
    capability = _checkout(client)
    text = b"0\nTEXT\n5\n2A\n8\nNew\n10\n0\n20\n0\n40\n1\n1\nEXTRA\n"
    data = EDITED_DXF.replace(b"0\nENDSEC\n", text + b"0\nENDSEC\n")
    resp = _post(client, DWG_DRAWING, _live_mutations(), data=data, capability=capability)
    assert resp.status_code == 202, resp.text
    assert len(submissions) == 1
    assert _head(client, DWG_DRAWING) == 1


@pytest.mark.parametrize("live", [{**_live_base_intake(), "texts": []}], indirect=True)
def test_live_leg_refuses_text_entities_not_carried_by_the_plan(live):
    client, _backend, submissions = live
    text = b"0\nTEXT\n5\n2A\n8\nNew\n10\n0\n20\n0\n40\n1\n1\nEXTRA\n"
    data = EDITED_DXF.replace(b"0\nENDSEC\n", text + b"0\nENDSEC\n")
    resp = _post(client, DWG_DRAWING, _live_mutations(), data=data)
    assert resp.status_code == 422, resp.text
    assert "text entities differ from the head" in resp.json()["error"]["message"]
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


@pytest.mark.parametrize("live", [{**_live_base_intake(), "texts": [
    {"kind": "TEXT", "layer": "New", "pt": [0.0, 0.0], "text": "OLD", "handle": "2A"},
]}], indirect=True)
def test_live_leg_refuses_a_change_from_text_to_mtext(live):
    client, _backend, submissions = live
    capability = _checkout(client)
    text = b"0\nMTEXT\n5\n2A\n8\nNew\n10\n0\n20\n0\n40\n1\n1\nOLD\n"
    data = EDITED_DXF.replace(b"0\nENDSEC\n", text + b"0\nENDSEC\n")
    resp = _post(client, DWG_DRAWING, _live_mutations(), data=data, capability=capability)
    assert resp.status_code == 422, resp.text
    assert "text entities differ from the head" in resp.json()["error"]["message"]
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_refuses_a_circle_outside_the_drawing_plane(live):
    client, _backend, submissions = live
    capability = _checkout(client)
    normal = b"210\n1\n220\n0\n230\n0\n"
    circle = b"0\nCIRCLE\n5\nC1\n8\nNew\n10\n10\n20\n10\n40\n2\n" + normal
    data = EDITED_DXF.replace(b"0\nENDSEC\n", circle + b"0\nENDSEC\n")
    mutations = {**_live_mutations(), "added": [
        {"kind": "CIRCLE", "handle": "C1", "layer": "New", "c": [10, 10], "r": 2},
    ]}
    resp = _post(client, DWG_DRAWING, mutations, data=data, capability=capability)
    assert resp.status_code == 422, resp.text
    assert "outside the drawing plane" in resp.json()["error"]["message"]
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1

    resp = _post(client, DWG_DRAWING, mutations, data=data.replace(normal, b""),
                 capability=capability)
    assert resp.status_code == 202, resp.text
    assert len(submissions) == 1
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_refuses_a_plan_the_dxf_contradicts(live):
    client, _backend, submissions = live
    contradictory = EDITED_DXF.replace(b"11\n4\n21\n6\n", b"11\n9\n21\n6\n")
    resp = _post(client, DWG_DRAWING, _live_mutations(), data=contradictory)
    assert resp.status_code == 422, resp.text
    assert "does not carry the plan's result" in resp.json()["error"]["message"]
    assert resp.json()["error"]["retryable"] is False
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_refuses_an_unlowerable_plan_synchronously(live):
    client, _backend, submissions = live
    resp = _post(client, DWG_DRAWING, {"set_points": [{
        "handle": "A", "closed": True,
        "pts": [[0, 0, 0], [2, 0, 0], [2, 2, 1], [0, 2, 0]],
    }]})
    assert resp.status_code == 422, resp.text
    assert "the edit plan was refused" in resp.json()["error"]["message"]
    assert "planar" in resp.json()["error"]["message"]
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_takes_the_sidecar_when_the_plan_exceeds_the_job_cap(live, monkeypatch):
    import jobs  # noqa: PLC0415
    import write_loop  # noqa: PLC0415

    client, backend, submissions = live
    monkeypatch.setattr(jobs, "MAX_PARAMS_BYTES", 64)
    resp = _post(client, DWG_DRAWING, _live_mutations())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit"] == "dxf-sidecar"
    assert body["commit_note"] == (
        "the plan exceeds the job payload cap; the DXF carries this save")
    assert body["plan_sha256"] is None
    assert body["source_stored"] is True
    assert body["cost"] == {"engine_usd": 0.0, "engine": "client-wasm"}
    assert backend.get(write_loop.edited_source_key(TENANT, DWG_DRAWING, 2)) == EDITED_DXF
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 2


def test_live_leg_refuses_a_short_lease(live, monkeypatch):
    import jobs  # noqa: PLC0415

    client, _backend, submissions = live
    capability = _checkout(client)
    monkeypatch.setattr(jobs, "job_max_s", lambda: 7200)
    resp = _post(client, DWG_DRAWING, _live_mutations(), capability=capability)
    assert resp.status_code == 409, resp.text
    assert "edit lock has" in resp.json()["error"]["message"]
    assert resp.json()["error"]["retryable"] is True
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


def test_live_leg_stale_parent_is_still_a_409_before_anything(live):
    client, _backend, submissions = live
    resp = _post(client, DWG_DRAWING, _live_mutations(), parent=0)
    assert resp.status_code == 409, resp.text
    assert "stale parent" in resp.json()["error"]["message"]
    assert submissions == []
    assert _head(client, DWG_DRAWING) == 1


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
