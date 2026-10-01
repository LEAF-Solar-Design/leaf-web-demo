"""A browser CAD save keeps the drawing's solar design (sf-w2-combiner-intake-survives-saves).

POST /api/drawings/{id}/versions/edited and the dxf-sidecar leg of POST /api/drawings/{id}/versions/plan store
the parse of the edited DXF as the new version's intake. That parse never holds the solar keys, so before this
record a save over a solar drawing dropped the embedded design graph, its digest, and the combiner intake and
panel-group outlines the solar-combiners tool reads. server/routers/drawings.py _carry_solar_state now copies
every SOLAR_CARRIED_KEYS key the replaced version holds onto the new payload, unchanged, and refuses (nothing
written) when the replaced version cannot be read, does not match its manifest digest, or the carried intake
passes the design bound. Every expected value below was measured by running the routes on a FilesystemBackend.
"""
import copy
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop  # noqa: E402  (first: puts da/ on sys.path for store)
import store  # noqa: E402
import dxf_intake  # noqa: E402
import solar_design_graph  # noqa: E402
import solar_graph_context  # noqa: E402
from solar_sizing_client import digest  # noqa: E402
from test_solar_combiner_intake_import import encode, i4_inputs  # noqa: E402,F401
from test_solar_tool_combiners import C5_TOOL_SHA, I4_SHA, dispatch, request, sha  # noqa: E402
from test_solar_w2_registration import head_graph, latest  # noqa: E402

TENANT = "fixture-tenant"
DRAWING = "solar"
CARRIED = ("solar_design_graph", "solar_design_graph_sha256", "combiner_intake", "panel_groups")
DXF = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n8\nRoof\n90\n3\n70\n1\n"
       "10\n0\n20\n0\n10\n50\n20\n0\n10\n50\n20\n30\n0\nENDSEC\n0\nEOF\n").encode("ascii")
CAD = {"dwg": {}, "layers": [], "polylines": [], "inserts": [], "faces3d": [], "blockdefs": [], "geodata": None}
STALE = "stale parent: the named parent_version is no longer the head; refresh the drawing and re-apply the edit"
UNREADABLE = ("the version this save replaces could not be read, so its solar design could not be carried; "
              "nothing was saved")
CORRUPT = "the version this save replaces does not match its stored record; nothing was saved"
TOO_LARGE = "the edited drawing and its solar design together exceed the stored design bound; nothing was saved"
# Measured: the payload a carried save stores (raw bytes as the route encodes them, and canonical), and the
# payload a plain drawing's save stores.
CARRIED_PAYLOAD_SHA = "8e185c4ac4940f9671034dc91150823721b8d0fdd846caf7230145049e29e173"
CARRIED_CANONICAL_SHA = "58a1acc4a077814a8829d55132ccd14c9185a64d2fa75771763eac4456149473"
PLAIN_PAYLOAD_SHA = "50782dd84aa7f392dfe15a11e44e0d3284edf4baa79b8b2e98fd6934c31f83ff"
MERGED_NODES = 185668  # solar_design_graph._bounded_json node count of the carried v3 intake (i4 fixture)
PLAN_LEGS = [({"mutations": {}}, "the plan names no operation; the DXF carries this save"),
             ({"mutations": {"removed": ["AB"]}}, "the head has no DWG source; the DXF carries this save")]


def ingest(tmp_path, monkeypatch, text):
    """A drawing whose version 1 is exactly `text` (test_solar_tool_combiners.stored, with raw bytes)."""
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    path = tmp_path / "seed.json"
    path.write_bytes(text.encode("utf-8"))
    store.ingest_drawing(backend, TENANT, str(path), drawing_id=DRAWING)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def solar_seed(g, **extra):
    return json.dumps(dict(CAD, **extra, solar_design_graph=g, solar_design_graph_sha256=digest(g)))


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers
    from routers import drawings
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("APS_LIVE", raising=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def imported(i4_inputs, tmp_path, monkeypatch, client):
    """(backend, graph, combiner intake, groups): the i4 drawing at version 1 with the combiner intake imported
    through the real route as version 2."""
    g, intake, groups = copy.deepcopy(i4_inputs)
    backend = ingest(tmp_path, monkeypatch, solar_seed(g))
    response = client.post("/api/drawings/" + DRAWING + "/imports/combiner-intake", content=encode(intake, groups),
                           headers={"X-Tenant-Id": TENANT, "Content-Type": "application/json"})
    assert response.status_code == 200 and response.json()["version"] == 2
    return backend, g, intake, groups


def save(client, kind, parent, plan=None, data=DXF):
    form = {"parent_version": str(parent), "source_digest": hashlib.sha256(data).hexdigest()}
    if plan is not None:
        form["plan"] = json.dumps(plan)
    return client.post(f"/api/drawings/{DRAWING}/versions/{kind}", headers={"X-Tenant-Id": TENANT},
                       files={"file": ("edited.dxf", io.BytesIO(data), "application/dxf")}, data=form)


def stored_bytes(backend, version):
    _, key = store.resolve_version(backend, TENANT, DRAWING, version)
    return backend.get(key)


def stored_intake(backend, version):
    return json.loads(stored_bytes(backend, version))


def parsed():
    intake = dxf_intake.parse_dxf_bytes(DXF, source_name="edited.dxf")
    intake["polylineWidthCovered"] = True
    return intake


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, DRAWING))


def refused(response):
    error = response.json()["error"]
    return response.status_code, error["message"], error["retryable"]


def assert_carried(backend, client, response, parent):
    """The saved version keeps every solar key of `parent` and the edit's own CAD, and opens as the edit."""
    body = response.json()
    assert (response.status_code, body["new_version"]) == (201, {"drawing_id": DRAWING, "version": parent + 1,
                                                                 "parent": parent})
    before, after = stored_intake(backend, parent), stored_intake(backend, parent + 1)
    assert {name: after[name] for name in CARRIED} == {name: before[name] for name in CARRIED}
    assert {name: value for name, value in after.items() if name not in CARRIED} == parsed()
    raw = stored_bytes(backend, parent + 1)
    entry = store.resolve_version_entry(backend, TENANT, DRAWING, parent + 1)[2]
    assert body["intake_sha256"] == entry["sha256"] == hashlib.sha256(raw).hexdigest() == CARRIED_PAYLOAD_SHA
    assert sha(after) == CARRIED_CANONICAL_SHA
    context = solar_graph_context.resolve_graph_context(backend, TENANT, DRAWING, "head")
    assert (context["representation"], context["resolved_version"], context["graph_sha256"],
            context["local_commit_ready"]) == ("intake", parent + 1, I4_SHA, True)
    # The browser engine reopens the saved version as the exact edited bytes: the sidecar proof binds them to
    # the payload actually stored.
    dxf = client.get(f"/api/drawings/{DRAWING}/dxf?version={parent + 1}", headers={"X-Tenant-Id": TENANT})
    assert (dxf.status_code, dxf.content) == (200, DXF)


def test_solar_design_survives_constants():
    from routers import drawings
    assert drawings.SOLAR_CARRIED_KEYS == CARRIED


def test_solar_design_survives_edited_save_and_the_tool_runs(imported, client):
    backend, _, _, _ = imported
    assert_carried(backend, client, save(client, "edited", 2), 2)
    receipt = dispatch(backend, request(), source_version=3, job_id="combiners-after-edit")
    assert receipt["new_version"] == {"drawing_id": DRAWING, "version": 4, "parent": 3}
    assert (sha(head_graph(backend)), latest(backend)) == (C5_TOOL_SHA, 4)


@pytest.mark.parametrize("plan,note", PLAN_LEGS, ids=["no-op-plan", "no-dwg-source"])
def test_solar_design_survives_plan_save(imported, client, plan, note):
    backend, _, _, _ = imported
    response = save(client, "plan", 2, plan)
    assert (response.json()["commit"], response.json()["commit_note"]) == ("dxf-sidecar", note)
    assert_carried(backend, client, response, 2)


def test_solar_design_survives_two_edits(imported, client):
    backend, _, _, _ = imported
    assert save(client, "edited", 2).status_code == 201
    assert_carried(backend, client, save(client, "edited", 3), 3)


def test_solar_design_survives_graph_only(i4_inputs, tmp_path, monkeypatch, client):
    g, _, _ = copy.deepcopy(i4_inputs)
    backend = ingest(tmp_path, monkeypatch, solar_seed(g))
    assert save(client, "edited", 1).status_code == 201
    after = stored_intake(backend, 2)
    assert (after["solar_design_graph"], after["solar_design_graph_sha256"]) == (g, digest(g))
    assert "combiner_intake" not in after and "panel_groups" not in after
    assert {name: value for name, value in after.items() if name not in CARRIED} == parsed()


def test_solar_design_survives_plain_drawing_payload_unchanged(tmp_path, monkeypatch, client):
    backend = ingest(tmp_path, monkeypatch, json.dumps(CAD))
    response = save(client, "edited", 1)
    assert response.status_code == 201
    raw = stored_bytes(backend, 2)
    assert raw == json.dumps(parsed(), separators=(",", ":")).encode("utf-8")
    assert response.json()["intake_sha256"] == hashlib.sha256(raw).hexdigest() == PLAIN_PAYLOAD_SHA


def test_solar_design_survives_escaped_key_spelling(i4_inputs, tmp_path, monkeypatch, client):
    """The parent is read as JSON, not searched as text: an escaped key name is the same key."""
    g, _, _ = copy.deepcopy(i4_inputs)
    # Both key names spelled with the JSON escape of "s" (chr(92) is the backslash), so no byte search for a
    # key name finds either of them.
    escaped = '"' + chr(92) + 'u0073olar_design_graph'
    text = solar_seed(g).replace('"solar_design_graph', escaped)
    assert 'solar_design_graph' not in text and text.count(escaped) == 2
    backend = ingest(tmp_path, monkeypatch, text)
    assert solar_graph_context.resolve_graph_context(backend, TENANT, DRAWING, 1)["graph_sha256"] == I4_SHA
    assert save(client, "edited", 1).status_code == 201
    assert stored_intake(backend, 2)["solar_design_graph"] == g


def test_solar_design_survives_values_verbatim(tmp_path, monkeypatch, client):
    """A carried value is copied as the parent holds it, whatever its type; the save does not judge it."""
    backend = ingest(tmp_path, monkeypatch, json.dumps(dict(CAD, combiner_intake="x", panel_groups=7)))
    assert save(client, "edited", 1).status_code == 201
    after = stored_intake(backend, 2)
    assert (after["combiner_intake"], after["panel_groups"]) == ("x", 7)
    assert "solar_design_graph" not in after and "solar_design_graph_sha256" not in after


@pytest.mark.parametrize("fault", [OSError("disk"), KeyError("gone")], ids=["oserror", "keyerror"])
def test_solar_design_survives_unreadable_parent(imported, client, monkeypatch, fault):
    backend, _, _, _ = imported
    _, parent_key = store.resolve_version(backend, TENANT, DRAWING, 2)
    real_get = backend.get

    def get(key):
        if key == parent_key:
            raise fault
        return real_get(key)

    before = keys(backend)
    monkeypatch.setattr(backend, "get", get)
    assert refused(save(client, "edited", 2)) == (503, UNREADABLE, True)
    monkeypatch.setattr(backend, "get", real_get)
    assert (latest(backend), keys(backend)) == (2, before)


@pytest.mark.parametrize("kind,plan", [("edited", None), ("plan", {"mutations": {}})], ids=["edited", "plan"])
def test_solar_design_survives_corrupt_parent(imported, client, monkeypatch, kind, plan):
    backend, _, _, _ = imported
    real = store.resolve_version_entry

    def resolve(backend_, tenant_id, drawing_id, version="head"):
        v, key, entry = real(backend_, tenant_id, drawing_id, version)
        return v, key, dict(entry, sha256="0" * 64) if v == 2 else entry

    before = keys(backend)
    monkeypatch.setattr(store, "resolve_version_entry", resolve)
    assert refused(save(client, kind, 2, plan)) == (500, CORRUPT, False)
    monkeypatch.setattr(store, "resolve_version_entry", real)
    assert (latest(backend), keys(backend)) == (2, before)


@pytest.mark.parametrize("kind,plan", [("edited", None), ("plan", {"mutations": {}})], ids=["edited", "plan"])
@pytest.mark.parametrize("corruption", ["truncated", "cad_only"])
def test_solar_design_survives_corrupt_parent_bytes(imported, client, monkeypatch, kind, plan, corruption):
    backend, _, _, _ = imported
    _, parent_key = store.resolve_version(backend, TENANT, DRAWING, 2)
    real_get = backend.get
    raw = real_get(parent_key)
    corrupt = raw[:-1] if corruption == "truncated" else json.dumps(parsed()).encode()

    def get(key):
        if key == parent_key:
            return corrupt
        return real_get(key)

    before = keys(backend)
    monkeypatch.setattr(backend, "get", get)
    assert refused(save(client, kind, 2, plan)) == (500, CORRUPT, False)
    monkeypatch.setattr(backend, "get", real_get)
    assert (latest(backend), keys(backend)) == (2, before)


@pytest.mark.parametrize("kind,plan", [("edited", None), ("plan", {"mutations": {}})], ids=["edited", "plan"])
@pytest.mark.parametrize("limit,status", [(MERGED_NODES - 1, 413), (MERGED_NODES, 201)], ids=["over", "at"])
def test_solar_design_survives_bound(imported, client, monkeypatch, kind, plan, limit, status):
    backend, _, _, _ = imported
    before = keys(backend)
    monkeypatch.setattr(solar_design_graph, "MAX_NODES", limit)
    response = save(client, kind, 2, plan)
    assert response.status_code == status
    if status == 413:
        assert refused(response) == (413, TOO_LARGE, False)
        assert (latest(backend), keys(backend)) == (2, before)
    else:
        assert latest(backend) == 3 and stored_intake(backend, 3)["panel_groups"] == stored_intake(backend, 2)["panel_groups"]


@pytest.mark.parametrize("parent", [99, 1], ids=["unknown", "older"])
def test_solar_design_survives_parent_not_head(imported, client, parent):
    backend, _, _, _ = imported
    before = keys(backend)
    assert refused(save(client, "edited", parent)) == (409, STALE, True)
    assert (latest(backend), keys(backend)) == (2, before)
