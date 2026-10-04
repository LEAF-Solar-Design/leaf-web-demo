"""Acceptance for sf-w4-landxml-terrain-read: read the terrain stored for a drawing.

landxml-import, the read after the import. The terrain lives in the drawing's Ground physical head
(server/solar_physical_head.py), not in the design graph, so the read adapter
(server/solar_local_read.py) hands a builtin that declares READS_PHYSICAL_HEAD the head it read,
and its terminal proof re-reads the exact head-log entry the output names. The read never writes.
Ground Physical PREVIEW: every answer says maturity "preview". Every frozen value below was
measured by running this contract with python -B from server/ against the licensed LEAFLANDXMLDEMO
capture imported by server/solar_landxml_import.py.
"""
import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import broker_client  # noqa: E402
import catalog  # noqa: E402
import deps  # noqa: E402
import entitlements  # noqa: E402
import jobs  # noqa: E402
import product_capability_availability as availability  # noqa: E402
import solar_ground_terrain_adapter as ta  # noqa: E402
import solar_local_graph as local  # noqa: E402
import solar_local_read as read  # noqa: E402
import solar_physical_head as ph  # noqa: E402
import solar_physical_state as ps  # noqa: E402
import solar_tools  # noqa: E402
import store  # noqa: E402
import write_loop  # noqa: E402
from leaf_cloud_client import canonical_bytes  # noqa: E402
from solar_design_graph import GraphValidationError  # noqa: E402
from solar_sizing_client import digest  # noqa: E402
from test_solar_ground_terrain_adapter import (  # noqa: E402,F401
    CAPTURE_GRID_SHA, FRAME_M, TINYTEST, backend, g1_state, imp, kernel_report_sha256, mesh, publish, slope,
    steep_file, steep_head)
from test_solar_landxml_import import REAL_SHA  # noqa: E402
from test_solar_physical_state import DRAWING, GENERATE_SHA, PROJECT, TENANT  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_local_graph_adapter import held, run as commit  # noqa: E402
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: E402,F401

TOOL = "solar-terrain-read"
JOB = "read-job"
SCHEMA = "leaf.solar-terrain-read.v1"
REQUEST = "7ceef78300cccfeb9c5ee3014ef87cad0f3d125cf850a750e2c03f92af378d04"   # {} at version 1
GRAPH = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
STEEP_SHA = "135b1f5e15ba0247b8bec7b97b78d04e04814d2cd033d666c705dd1a7ffe50e7"
STEEP_GRID_SHA = "b84cd3b8a511775bad580bb9cf29206f7e51f5c85626d5844df2b3737d2ee2d3"
FEET_GRID_SHA = "a67a1a71ae3f8404398909e2a9dc836b2a83b02ec421b41e6c2dae8332b04fed"
CAPTURE_STATE = "56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"
MESH_STATE = "72d6db89f1ccad4ff9f197afb7f75e12a751e984dfc4b2443e6dd9aba96612f4"
ABSENT = {"schema": SCHEMA, "maturity": "preview", "stored": False, "head": None, "state": None,
          "terrain": None}
NO_PREVIEWS = {"terrain-mesh-render": {"record": None, "state": "absent"},
               "tracker-slope-violations": {"record": None, "state": "absent"}}
CAPTURE_GRID = {"rows": 30, "cols": 30, "x_min": -100.0, "x_max": 100.0, "y_min": -100.0, "y_max": 100.0,
                "cell_x": 6.896551724137931, "cell_y": 6.896551724137931,
                "cell_x_m": 6.896551724137931, "cell_y_m": 6.896551724137931,
                "elevation_min_m": -1.7552671418084063, "elevation_max_m": 1.7552671418084052,
                "grid_sha256": CAPTURE_GRID_SHA}
MESH_RECORD = {"schema": "leaf.solar-terrain-preview.v1", "capability": "terrain-mesh-render",
               "maturity": "preview", "grid_sha256": CAPTURE_GRID_SHA, "meters_per_unit": 1.0,
               "faces": 841, "buckets": {"Green": 717, "Yellow": 124, "Red": 0},
               "max_slope_percent": 8.913350836855564,
               "mesh_sha256": "3c396f556cb26e26f98afcf2a0599bb4279c3529829ea7629a8f9e1b2b21a05e"}
# (output_bytes, output_sha256) of each measured read.
OUTPUTS = {
    "absent": (115, "a91514b8727aebd7cf56d0eaa81afe6f635e02681575d98cde43bc9b75c95bf8"),
    "capture": (1605, "b14c17e4868ea1e2b672635fb43778a3c28d2bc4c2ff4a9a758700d9eab01854"),
    "mesh": (2110, "3f18e5f670a6092ad6857749177aa78f217aa4cd9acc69934b3475806ccff7ac"),
    "stale": (2066, "4ede878f70420c135388f2833cd8e37da65ad553e117259877ccc4fd5825d5f6"),
    "steep": (1693, "2400ab7ac62f41aef9c6ecc2a3fd7559ff08a7eeed224e1e6eca4151c029054b"),
    "feet":(1660, "9ca40cda51c6ddd8807826bfe4695a9768b8063dd69325c37e52de9dc2f1bb1a"),
    "nogrid": (1260, "859905f2c3797e38662c43394ddf3f090369a68f4d097cea1761fff9ca0dcc60"),
}
# The slope reading's preview record carries the kernel's report digest, and that report holds floats
# from the terrain kernel's trigonometry that differ in the last bit between platform math libraries
# (see test_solar_ground_terrain_adapter.kernel_report_sha256), so the head and the whole-output digest
# chain through a platform-dependent value: one literal cannot hold on Windows and Linux. The slope row
# derives the report digest from the kernel, pins the head by relation, and pins everything else by a
# digest over the output with the head and the report digest removed.
SLOPE_OUTPUT_BYTES = 2296
SLOPE_REST_SHA = "9cf6836819e8b2beb7e63843a6b44141890df206f5efe5d3381d4452a778a966"


# ---------------------------------------------------------------- helpers


def builtin():
    return read._load_builtin(TOOL)


def run_read(backend, request=None, version=1, job=JOB):
    return read.run_local_graph_read(backend, TENANT, TOOL, dict(request or {}, drawing_id=DRAWING),
                                     drawing_id=DRAWING, source_version=version, job_id=job)


def proof(result, backend, version=1):
    return read.graph_read_provenance(result, {"drawing_id": DRAWING}, TENANT, JOB, TOOL, version,
                                      backend=backend)


def rejected(result, backend):
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        proof(result, backend)


def pinned(result, name):
    assert (result["output_bytes"], result["output_sha256"]) == OUTPUTS[name]
    assert result["request_sha256"] == REQUEST
    assert (result["source_version"], result["graph_sha256"]) == (1, GRAPH)
    assert result["drawing_changed"] is False
    assert len(canonical_bytes(result["output"])) == result["output_bytes"]
    assert digest(result["output"]) == result["output_sha256"]
    return result["output"]


def resealed(result, output):
    """The receipt a forger would send: a changed output with its own matching digest and size."""
    forged = copy.deepcopy(result)
    forged.update(output=output, output_sha256=digest(output), output_bytes=len(canonical_bytes(output)))
    return forged


def refused(code, fn, *args, **kw):
    with pytest.raises(GraphValidationError) as exc:
        fn(*args, **kw)
    assert exc.value.code == code


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, DRAWING))


def document(state, frame=None, drawing_units="m"):
    return ps.physical_document(state, drawing_units=drawing_units, source_sha256=GENERATE_SHA,
                                capability="frame-generate", frame=frame)


def handed(doc, view=None):
    """What the adapter hands the builtin for a stored head."""
    return {"head": {"index": 0} if view is None else view, "document": doc}


# ---------------------------------------------------------------- contract


def test_terrain_read_constants():
    module = builtin()
    assert (module.TOOL, module.OUTPUT_SCHEMA, module.INVALID, module.MATURITY) == (
        TOOL, SCHEMA, "INVALID_TERRAIN_READ_REQUEST", "preview")
    assert module.READS_PHYSICAL_HEAD is True
    assert not hasattr(module, "READS_VERSION_HISTORY")
    assert not hasattr(module, "input_readiness")


def test_terrain_read_declaration():
    path = SERVER / "solar_tools" / "solar_terrain_read.json"
    declared = json.loads(path.read_text(encoding="utf-8"))
    description = declared["record"].pop("description")
    assert description == (
        "Show the terrain stored for this drawing: the grid's rows, columns, extents, cell size and "
        "elevation range, its units, CRS and elevation datum, its digest and source file digest, and "
        "whether the terrain mesh and slope check previews still match it. Reads only; a drawing "
        "with no stored terrain answers that plainly.")
    assert 1 <= len(description) <= 1024
    assert declared == {
        "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_terrain_read.py",
        "family": "terrain", "adapter": "local-graph-read", "entitlement": "run_read",
        "requires_persisted_graph": True, "seedable": False,
        "invalid_request_code": "INVALID_TERRAIN_READ_REQUEST",
        "readiness": {"kind": "facets", "facets": []},
        "engine": "server-builtin", "interaction": {"mode": "form"}, "record_store": "registry",
        "record": {
            "name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "terrain",
            "engine_op": "solar_terrain_read", "entry": "builtins/solar_terrain_read.py",
            "params": {"type": "object", "properties": {
                "drawing_id": {"type": "string", "maxLength": 128}},
                "required": [], "additionalProperties": False},
            "returns": {"type": "object"}, "capabilities": ["drawing.read"],
            "allow_local_fallback": False},
        "ledger": ["landxml-import"], "trusted_inputs": [],
        "maturity": "preview", "wave": 4, "order": 40, "scenario": "w4-landxml"}
    assert solar_tools.get(TOOL)["record"] == solar_tools.trusted_record(TOOL)
    assert solar_tools.trusted_record(TOOL)["description"] == description
    legacy = json.loads((SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]
    assert TOOL not in {tool["name"] for tool in legacy}


@pytest.mark.parametrize("value,valid", [
    ({}, True),
    ({"drawing_id": "solar"}, True),
    ({"drawing_id": "d" * 128}, True),
    ({"drawing_id": "d" * 129}, False),
    ({"drawing_id": 7}, False),
    ({"drawing_id": None}, False),
    ({"section": "grid"}, False),
    ({"expected_rev": 0}, False),
    ({"expected_head": "0" * 64}, False),
    ({"project_id": PROJECT}, False),
])
def test_terrain_read_params_schema(value, valid):
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(value) is valid


def test_terrain_read_registry_and_catalog(monkeypatch):
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    assert TOOL in solar_tools.local_graph_read_tools() and TOOL not in solar_tools.local_graph_tools()
    assert TOOL in read.local_graph_read_tools() and TOOL not in local.local_graph_tools()
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert availability.capability_adapter(TOOL) == "local-graph-read"
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_read"
    found = [(family, row) for family in catalog.build_catalog(deps.all_tools(TENANT))
             for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "terrain"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": TOOL, "family": "terrain", "wave": 4, "order": 40,
        "maturity": "preview", "engine": "server-builtin", "adapter": "local-graph-read",
        "entitlement": "run_read", "interaction": {"mode": "form"}, "ledger": ["landxml-import"]}
    assert row["params_schema"] == solar_tools.trusted_record(TOOL)["params"]
    # The terrain reads sort after the SolarEdge pair, with physical shade after terrain read.
    wave4 = [(entry["order"], entry["name"]) for entry in solar_tools.entries() if entry["wave"] == 4]
    assert wave4 == [(30, "solar-solaredge-accept"), (31, "solar-solaredge-tracking-read"),
                     (40, TOOL), (50, "solar-physical-shade")]


def test_terrain_read_readiness(graph):
    # Facets are empty: the only readiness code this tool can carry is the shared unresolved_units.
    ready = {"input_ready": True, "input_reason": None}
    assert availability.w1_local_commit_inputs(graph)[TOOL] == ready
    feet = copy.deepcopy(graph)
    feet["project"]["units"]["meters_per_unit"] = 0.3048
    assert availability.w1_local_commit_inputs(feet)[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


# ---------------------------------------------------------------- reads through the adapter


def test_terrain_read_absent(backend):
    before = keys(backend)
    result = run_read(backend)
    assert pinned(result, "absent") == ABSENT
    assert proof(result, backend)["output_sha256"] == OUTPUTS["absent"][1]
    assert keys(backend) == before


def test_terrain_read_imported_capture(backend):
    imported = imp(backend)
    before = keys(backend)
    output = pinned(run_read(backend), "capture")
    assert output == {
        "schema": SCHEMA, "maturity": "preview", "stored": True, "head": imported["head"],
        "state": {"capability": "landxml-import", "parent": None,
                  "source": {"kind": "ground-intake", "sha256": REAL_SHA}},
        "terrain": {"schema": "leaf.solar-terrain-view.v1", "maturity": "preview", "frame": FRAME_M,
                    "grid": CAPTURE_GRID, "mesh_faces": 0, "slope_markers": 0, "previews": NO_PREVIEWS}}
    assert (output["head"]["index"], output["head"]["state"]["artifact_id"]) == (0, CAPTURE_STATE)
    assert output["head"]["state"]["byte_length"] == 18283
    assert keys(backend) == before


def test_terrain_read_is_the_adapter_view(backend):
    # The reading is solar_ground_terrain_adapter.terrain_view of the head document, unchanged.
    imp(backend)
    mesh(backend)
    head, view = ta.read_terrain(backend, TENANT, DRAWING, project_id=PROJECT)
    output = run_read(backend)["output"]
    assert output["head"] == head
    assert dict(output["terrain"], drawing_id=DRAWING, project_id=PROJECT) == view


def test_terrain_read_mesh_preview_current_then_stale(backend):
    imp(backend)
    mesh(backend)
    output = pinned(run_read(backend), "mesh")
    assert (output["head"]["index"], output["head"]["parent"]) == (1, CAPTURE_STATE)
    assert output["head"]["state"]["artifact_id"] == MESH_STATE
    assert output["state"] == {"capability": "terrain-mesh-render", "parent": CAPTURE_STATE,
                               "source": {"kind": "ground-intake", "sha256": REAL_SHA}}
    assert output["terrain"]["mesh_faces"] == 841
    assert output["terrain"]["previews"]["terrain-mesh-render"] == {"record": MESH_RECORD, "state": "current"}
    assert output["terrain"]["previews"]["tracker-slope-violations"] == {"record": None, "state": "absent"}
    # A new grid leaves the mesh record behind: the read reports it stale, never current.
    imp(backend, steep_file())
    output = pinned(run_read(backend), "stale")
    assert (output["head"]["index"], output["head"]["parent"]) == (2, MESH_STATE)
    assert output["state"]["capability"] == "landxml-import"
    assert output["state"]["source"]["sha256"] == STEEP_SHA
    grid = output["terrain"]["grid"]
    assert (grid["rows"], grid["cols"], grid["grid_sha256"]) == (30, 15, STEEP_GRID_SHA)
    assert (grid["x_min"], grid["x_max"], grid["y_min"], grid["y_max"]) == (0.0, 100.0, 0.0, 200.0)
    assert (grid["cell_x"], grid["cell_y"]) == (7.142857142857143, 6.896551724137931)
    assert (grid["elevation_min_m"], grid["elevation_max_m"]) == (0.0, 20.0)
    assert output["terrain"]["previews"]["terrain-mesh-render"] == {"record": MESH_RECORD, "state": "stale"}


def test_terrain_read_slope_preview(backend, g1_state):
    steep_head(backend, g1_state)
    output = pinned(run_read(backend), "steep")
    assert output["terrain"]["previews"] == NO_PREVIEWS
    slope(backend, limits=TINYTEST)
    result = run_read(backend)
    output = result["output"]
    assert result["output_bytes"] == SLOPE_OUTPUT_BYTES
    assert result["request_sha256"] == REQUEST
    assert (result["source_version"], result["graph_sha256"]) == (1, GRAPH)
    assert result["drawing_changed"] is False
    assert len(canonical_bytes(output)) == result["output_bytes"]
    assert digest(output) == result["output_sha256"]
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert (output["head"]["index"], output["head"]["state"]["artifact_id"]) == (2, head["state"]["artifact_id"])
    rest = copy.deepcopy(output)
    del rest["head"]
    del rest["terrain"]["previews"]["tracker-slope-violations"]["record"]["report_sha256"]
    assert digest(rest) == SLOPE_REST_SHA
    assert output["state"]["capability"] == "tracker-slope-violations"
    assert output["terrain"]["slope_markers"] == 109
    preview = output["terrain"]["previews"]["tracker-slope-violations"]
    assert preview["state"] == "current"
    assert preview["record"] == {
        "schema": "leaf.solar-terrain-preview.v1", "capability": "tracker-slope-violations",
        "maturity": "preview", "grid_sha256": STEEP_GRID_SHA, "meters_per_unit": 1.0, "limits": TINYTEST,
        "frames": 144, "markers": 109,
        "status": "Tracker slope: 107/144 axial / 2/132 cross / 7/132 row-to-row deg exceed active preset limits.",
        "report_sha256": kernel_report_sha256(backend, g1_state)}
    assert proof(run_read(backend), backend)["output_sha256"] == result["output_sha256"]


def test_terrain_read_feet_drawing(backend):
    imp(backend, drawing_units="ft")
    output = pinned(run_read(backend), "feet")
    assert output["terrain"]["frame"] == dict(FRAME_M, drawing_units="ft", meters_per_unit=0.3048)
    grid = output["terrain"]["grid"]
    assert (grid["rows"], grid["cols"], grid["grid_sha256"]) == (30, 30, FEET_GRID_SHA)
    assert (grid["x_min"], grid["x_max"]) == (-328.0839895013123, 328.0839895013123)
    assert (grid["cell_x"], grid["cell_x_m"]) == (22.626482034573264, 6.8965517241379315)
    assert (grid["elevation_min_m"], grid["elevation_max_m"]) == (-1.755267141808407, 1.7552671418084107)


def test_terrain_read_crs_and_datum_are_reported(backend):
    frame = json.loads(json.dumps(ps.DEFAULT_FRAME))
    frame.update(crs="EPSG:26915", elevation_datum="EPSG:5703")
    publish(backend, {"grid": {"elevations": [1.0, 2.0, 3.0, 4.0], "rows": 2, "cols": 2,
                               "x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 20.0}}, frame=frame)
    output = run_read(backend)["output"]
    assert output["terrain"]["frame"] == dict(FRAME_M, crs="EPSG:26915", elevation_datum="EPSG:5703")
    grid = output["terrain"]["grid"]
    assert (grid["rows"], grid["cols"], grid["cell_x"], grid["cell_y"]) == (2, 2, 10.0, 20.0)
    assert (grid["elevation_min_m"], grid["elevation_max_m"]) == (1.0, 4.0)


def test_terrain_read_state_without_a_grid(backend):
    publish(backend, {"frames": []})
    result = run_read(backend)
    output = pinned(result, "nogrid")
    assert output["stored"] is True and output["terrain"]["grid"] is None
    assert output["state"] == {"capability": "frame-generate", "parent": None,
                               "source": {"kind": "ground-intake", "sha256": GENERATE_SHA}}
    assert output["terrain"]["previews"] == NO_PREVIEWS
    assert proof(result, backend)["output_sha256"] == OUTPUTS["nogrid"][1]


def test_terrain_read_reads_the_current_head_at_any_graph_version(backend):
    # The physical head is per drawing, not per drawing version: a read bound to version 1 after
    # the graph moved to version 2 still reports the current head.
    imp(backend)
    with held(backend) as fence:
        commit(backend, fence=fence)
    assert store.load_manifest(backend, TENANT, DRAWING)["head"] == 2
    old = run_read(backend, version=1)
    assert old["output"] == run_read(backend, version=2)["output"]
    assert (old["output_bytes"], old["output_sha256"]) == OUTPUTS["capture"]
    assert proof(old, backend)["source_version"] == 1


def test_terrain_read_never_writes_and_ignores_the_drain(backend, monkeypatch):
    imp(backend)
    before = keys(backend)
    manifest = store.load_manifest(backend, TENANT, DRAWING)

    def forbidden(*args, **kwargs):
        pytest.fail("a terrain read must not write")

    monkeypatch.setattr(ph, "publish_physical_state", forbidden)
    monkeypatch.setattr(ps, "store_physical_state", forbidden)
    monkeypatch.setattr(backend, "put", forbidden)
    monkeypatch.setattr(backend, "put_if_absent_or_verify", forbidden)
    monkeypatch.setattr(write_loop, "drawing_mutations_refusal", lambda: "drained")
    result = run_read(backend)
    assert (result["output_bytes"], result["output_sha256"]) == OUTPUTS["capture"]
    assert proof(result, backend)["output_sha256"] == OUTPUTS["capture"][1]
    assert keys(backend) == before
    assert store.load_manifest(backend, TENANT, DRAWING) == manifest


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize("request_value,code", [
    ({"section": "grid"}, "INVALID_TERRAIN_READ_REQUEST"),
    ({"expected_head": None}, "INVALID_TERRAIN_READ_REQUEST"),
    ({"": 1}, "INVALID_TERRAIN_READ_REQUEST"),
    ({"initialize": {}}, "READ_SEED_UNSUPPORTED"),
    ({"x": 1e15}, "INVALID_NUMERIC_PARAM"),
])
def test_terrain_read_request_refusals(backend, request_value, code):
    imp(backend)
    refused(code, run_read, backend, request_value)


@pytest.mark.parametrize("params", [None, [], "x", 0, {"x": 1}, {"drawing_id": "solar"}])
def test_terrain_read_builtin_request_refusals(params):
    # The adapter pops drawing_id before the builtin runs, so the builtin accepts only {}.
    refused("INVALID_TERRAIN_READ_REQUEST", builtin().run, {}, params)
    refused("INVALID_TERRAIN_READ_REQUEST", builtin().run, {}, params, physical_head=None)


def test_terrain_read_adapter_refuses_a_non_object_request(backend):
    with pytest.raises(GraphValidationError) as exc:
        read.run_local_graph_read(backend, TENANT, TOOL, [], drawing_id=DRAWING, source_version=1,
                                  job_id=JOB)
    assert exc.value.code == "INVALID_TERRAIN_READ_REQUEST"


@pytest.mark.parametrize("case,code", [
    ("transform", "TERRAIN_FRAME_UNSUPPORTED"),
    ("affine-grid", "TERRAIN_FRAME_UNSUPPORTED"),
    ("one-row", "TERRAIN_GRID_INVALID"),
    ("flat-bounds", "TERRAIN_GRID_INVALID"),
    ("short-elevations", "TERRAIN_GRID_INVALID"),
    ("too-wide", "TERRAIN_GRID_TOO_LARGE"),
])
def test_terrain_read_refuses_a_state_it_cannot_read_truthfully(backend, case, code):
    grid = {"elevations": [1.0, 2.0, 3.0, 4.0], "rows": 2, "cols": 2,
            "x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 20.0}
    frame = None
    if case == "transform":
        frame = json.loads(json.dumps(ps.DEFAULT_FRAME))
        frame["transform"][3] = 5
    elif case == "affine-grid":
        grid["frame"] = {"origin": [0.0, 0.0]}
    elif case == "one-row":
        grid.update(rows=1, elevations=[1.0, 2.0])
    elif case == "flat-bounds":
        grid.update(x_max=0.0)
    elif case == "short-elevations":
        grid.update(elevations=[1.0, 2.0, 3.0])
    elif case == "too-wide":
        grid.update(rows=2, cols=301, elevations=[0.0] * 602)
    publish(backend, {"grid": grid}, frame=frame)
    before = keys(backend)
    refused(code, run_read, backend)
    assert keys(backend) == before


@pytest.mark.parametrize("value", [
    "x", 7, [], {}, {"head": {}}, {"document": None}, {"head": {}, "document": None},
    {"head": {}, "document": {}}, {"head": {}, "document": {"state": []}},
])
def test_terrain_read_builtin_refuses_a_malformed_head(value):
    refused("PHYSICAL_HEAD_CORRUPT", builtin().run, {}, {}, physical_head=value)


def test_terrain_read_builtin_is_pure():
    doc = document({"grid": {"elevations": [1.0, 2.0, 3.0, 4.0], "rows": 2, "cols": 2,
                             "x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 20.0}})
    given = handed(doc, {"index": 3, "state": {"artifact_id": "a" * 64}})
    before = copy.deepcopy(given)
    first = builtin().run({}, {}, physical_head=given)
    second = builtin().run({}, {}, physical_head=given)
    assert first == second and given == before
    assert first["head"] == {"index": 3, "state": {"artifact_id": "a" * 64}}
    first["state"]["source"]["sha256"] = "changed"
    first["terrain"]["grid"]["rows"] = 99
    assert given == before
    assert builtin().run({}, {}, physical_head=given) == second
    assert builtin().run({"anything": 1}, {}) == ABSENT


@pytest.mark.parametrize("case,code", [
    ("state-missing", "PHYSICAL_HEAD_CORRUPT"),
    ("entry-garbled", "PHYSICAL_HEAD_CORRUPT"),
    ("store-down", "PHYSICAL_HEAD_STORE_UNAVAILABLE"),
])
def test_terrain_read_head_store_refusals_pass_through(backend, monkeypatch, case, code):
    imported = imp(backend)
    entry = ph.entry_key(TENANT, DRAWING, 0)
    if case == "state-missing":
        original = backend.get
        state_keys = [key for key in keys(backend) if imported["head"]["state"]["artifact_id"] in key]
        assert state_keys

        def get(key):
            if key in state_keys:
                raise KeyError(key)
            return original(key)

        monkeypatch.setattr(backend, "get", get)
    elif case == "entry-garbled":
        original = backend.get
        monkeypatch.setattr(backend, "get", lambda key: b"{}" if key == entry else original(key))
    else:
        original = backend.get

        def get(key):
            if "/physical/" in key:
                raise OSError("store down")
            return original(key)

        monkeypatch.setattr(backend, "get", get)
    refused(code, run_read, backend)


# ---------------------------------------------------------------- the terminal proof


def test_terrain_read_proof_holds_after_the_head_moves(backend):
    # The head log is append-only: the proof re-reads the entry the output names, so a receipt
    # stays provable after a later operation or import moves the current head.
    imp(backend)
    first = run_read(backend)
    expected = {"execution_mode": "local_graph_read", "adapter": "local-graph-read",
                "request_sha256": REQUEST, "graph_sha256": GRAPH, "source_version": 1,
                "output_sha256": OUTPUTS["capture"][1]}
    assert proof(first, backend) == expected
    mesh(backend)
    second = run_read(backend)
    imp(backend, steep_file())
    assert proof(first, backend) == expected
    assert proof(second, backend)["output_sha256"] == OUTPUTS["mesh"][1]
    assert proof(run_read(backend), backend)["output_sha256"] == OUTPUTS["stale"][1]


def test_terrain_read_absent_is_provable_only_while_the_log_is_empty(backend):
    absent = run_read(backend)
    assert proof(absent, backend)["output_sha256"] == OUTPUTS["absent"][1]
    imp(backend)
    rejected(absent, backend)
    # A forged "nothing stored" for a drawing that has a head is rejected the same way.
    rejected(resealed(run_read(backend), ABSENT), backend)


@pytest.mark.parametrize("case", [
    "grid-rows", "preview-state", "source-sha", "capability", "stored-flag", "terrain-null",
    "head-index", "head-index-bool", "head-index-str", "head-index-negative", "head-index-4096",
    "head-index-absent", "head-parent", "head-artifact", "head-content", "head-byte-length", "head-download",
    "head-source-version", "head-project", "head-schema", "head-missing", "head-list", "head-state-null",
    "head-extra-key", "output-list",
])
def test_terrain_read_proof_rejects_a_resealed_forgery(backend, case):
    imp(backend)
    mesh(backend)
    result = run_read(backend)
    output = copy.deepcopy(result["output"])
    head = output["head"]
    if case == "grid-rows":
        output["terrain"]["grid"]["rows"] = 31
    elif case == "preview-state":
        output["terrain"]["previews"]["terrain-mesh-render"]["state"] = "stale"
    elif case == "source-sha":
        output["state"]["source"]["sha256"] = "0" * 64
    elif case == "capability":
        output["state"]["capability"] = "landxml-import"
    elif case == "stored-flag":
        output["stored"] = False
    elif case == "terrain-null":
        output["terrain"] = None
    elif case == "head-index":
        head["index"] = 0            # the first import's entry: a real entry, another state
    elif case == "head-index-bool":
        head["index"] = True
    elif case == "head-index-str":
        head["index"] = "1"
    elif case == "head-index-negative":
        head["index"] = -1
    elif case == "head-index-4096":
        head["index"] = 4096
    elif case == "head-index-absent":
        head["index"] = 2            # in range, and the log holds only entries 0 and 1
    elif case == "head-parent":
        head["parent"] = None
    elif case == "head-artifact":
        head["state"]["artifact_id"] = CAPTURE_STATE
    elif case == "head-content":
        head["state"]["content_sha256"] = "0" * 64
    elif case == "head-byte-length":
        head["state"]["byte_length"] += 1
    elif case == "head-download":
        head["state"]["download"] = "/api/drawings/other/artifacts/" + MESH_STATE
    elif case == "head-source-version":
        head["state"]["source_version"] = 2
    elif case == "head-project":
        head["project_id"] = "other"
    elif case == "head-schema":
        head["schema"] = "other"
    elif case == "head-missing":
        del output["head"]
    elif case == "head-list":
        output["head"] = [1]
    elif case == "head-state-null":
        head["state"] = None
    elif case == "head-extra-key":
        head["extra"] = 1
    elif case == "output-list":
        output = [output]
    assert proof(result, backend)["output_sha256"] == OUTPUTS["mesh"][1]
    rejected(resealed(result, output), backend)


def test_terrain_read_proof_rejects_an_older_head_presented_whole(backend):
    # A whole, genuine earlier reading is a true reading of an earlier head, so it proves as such;
    # what it cannot do is pass for another receipt: its output digest is its own.
    imp(backend)
    earlier = run_read(backend)
    mesh(backend)
    later = run_read(backend)
    assert proof(earlier, backend)["output_sha256"] == OUTPUTS["capture"][1]
    rejected(dict(later, output=earlier["output"]), backend)


def test_terrain_read_proof_reads_the_entry_its_predecessor_and_one_state(backend, monkeypatch):
    imp(backend)
    mesh(backend)
    result = run_read(backend)
    monkeypatch.setattr(ph, "load_physical_head", lambda *a, **k: pytest.fail("the proof re-reads by index"))
    monkeypatch.setattr(ph, "physical_head", lambda *a, **k: pytest.fail("the proof re-reads by index"))
    original = backend.get
    physical = []

    def get(key):
        if "/physical/" in key:
            physical.append(key)
        return original(key)

    monkeypatch.setattr(backend, "get", get)
    assert proof(result, backend)["output_sha256"] == OUTPUTS["mesh"][1]
    assert physical == [ph.entry_key(TENANT, DRAWING, 1), ph.entry_key(TENANT, DRAWING, 0)]


def _rewrite_entry(backend, index, make):
    key = ph.entry_key(TENANT, DRAWING, index)
    path = Path(backend._path(key))
    entry = json.loads(path.read_bytes())
    data = make(entry)
    if data is None:
        path.unlink()
    else:
        path.write_bytes(data)


BROKEN_PREDECESSORS = {
    # The entry's own index field is wrong.
    "wrong-index": lambda e: ph.entry_bytes(17, e["project_id"], None, e["state"], e["content_sha256"]),
    "missing-key": lambda e: canonical_bytes({k: v for k, v in e.items() if k != "content_sha256"}),
    "non-canonical": lambda e: json.dumps(e, indent=2).encode("utf-8"),
    # A well-formed entry that names another state: the recorded entry's parent no longer chains.
    "other-state": lambda e: ph.entry_bytes(0, e["project_id"], None, "0" * 64, e["content_sha256"]),
    "absent": lambda e: None,
}


@pytest.mark.parametrize("case", sorted(BROKEN_PREDECESSORS))
def test_terrain_read_proof_rejects_a_broken_chain(backend, case):
    # The head reader refuses a log whose head does not chain to a valid predecessor; the proof of a
    # reading taken before the damage must refuse it too, not accept what the reader calls corrupt.
    imp(backend)
    mesh(backend)
    result = run_read(backend)
    assert result["output"]["head"]["index"] == 1
    assert proof(result, backend)["output_sha256"] == OUTPUTS["mesh"][1]
    _rewrite_entry(backend, 0, BROKEN_PREDECESSORS[case])
    with pytest.raises(ph.PhysicalHeadError, match="^PHYSICAL_HEAD_CORRUPT$"):
        ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    rejected(result, backend)


def test_terrain_read_proof_rejects_a_first_entry_that_names_a_parent(backend):
    # Entry 0 rewritten canonically with a parent, and the receipt resealed to match it: the bytes
    # agree, the chain does not (the first entry of a log names no parent).
    imp(backend)
    result = run_read(backend)
    assert proof(result, backend)["output_sha256"] == OUTPUTS["capture"][1]
    _rewrite_entry(backend, 0, lambda e: ph.entry_bytes(0, e["project_id"], "0" * 64, e["state"],
                                                        e["content_sha256"]))
    with pytest.raises(ph.PhysicalHeadError, match="^PHYSICAL_HEAD_CORRUPT$"):
        ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    rejected(result, backend)
    output = copy.deepcopy(result["output"])
    output["head"]["parent"] = "0" * 64
    rejected(resealed(result, output), backend)


def test_terrain_read_absent_proof_rejects_when_the_head_log_cannot_be_read(backend, monkeypatch):
    absent = run_read(backend)
    original = backend.get

    def get(key):
        if "/physical/" in key:
            raise OSError("store down")
        return original(key)

    monkeypatch.setattr(backend, "get", get)
    rejected(absent, backend)


def test_terrain_read_proof_rejects_when_the_recorded_state_is_gone(backend, monkeypatch):
    imp(backend)
    result = run_read(backend)
    original = backend.get

    def get(key):
        if CAPTURE_STATE in key:
            raise KeyError(key)
        return original(key)

    monkeypatch.setattr(backend, "get", get)
    rejected(result, backend)


# ---------------------------------------------------------------- the job rail


def _read_api(backend, tmp_path, monkeypatch):
    """The select-by-zone read harness (test_solar_tool_select_by_zone._api) for this tool."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *a, **k: pytest.fail("dynamic dispatch"))
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "absent-authored.json")
    record = deps.find_tool(TOOL, TENANT)
    assert record == solar_tools.trusted_record(TOOL)

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(record, False): InlineExecutor()})
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)
    mode = {"tamper": False, "requests": []}

    def transport(url, *, json, headers, timeout):
        mode["requests"].append(copy.deepcopy(json))
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                reply = __import__("json").loads(response.body)
                if mode["tamper"] and reply.get("ok") is True:
                    output = copy.deepcopy(reply["result"]["output"])
                    output["terrain"]["grid"]["rows"] = 31
                    reply["result"].update(output=output, output_sha256=digest(output),
                                           output_bytes=len(canonical_bytes(output)))
                return reply

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    tenant = route.deps.TenantContext(TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    app.dependency_overrides[route.deps.require_tenant] = lambda: tenant
    with TestClient(app) as client:
        yield client, backend, record, tenant, mode


@pytest.fixture
def read_api(isolated_jobs, no_network, backend, tmp_path, monkeypatch):
    imp(backend)
    yield from _read_api(backend, tmp_path, monkeypatch)


def post(api, request):
    return api[0].post("/api/run?wait=1", json={
        "tool": TOOL, "dwg": DRAWING, "params": request, "catalog_digest": deps.catalog_tool_digest(api[2])})


def test_terrain_read_api_end_to_end(read_api):
    before = keys(read_api[1])
    response = post(read_api, {})
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete" and rec["dwg_version"] == 1
    assert rec["params"] == {"drawing_id": DRAWING}
    assert result["output"]["terrain"]["grid"] == CAPTURE_GRID
    assert (result["output_bytes"], result["output_sha256"]) == OUTPUTS["capture"]
    assert result["request_sha256"] == REQUEST
    provenance = env["execution_provenance"]
    assert provenance["execution_path"] == "local"
    receipt = read.graph_read_provenance(result, rec["params"], TENANT, rec["job_id"], TOOL, 1,
                                         backend=read_api[1])
    for key, value in receipt.items():
        assert provenance[key] == rec["provenance"][key] == value
    assert len(read_api[4]["requests"]) == 1
    assert keys(read_api[1]) == before


def test_terrain_read_api_tampered_receipt(read_api):
    read_api[4]["tamper"] = True
    response = post(read_api, {})
    assert response.status_code == 500, response.text
    assert response.json()["error"]["message"] == "graph read terminal proof rejected"
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1 and jobs.get_job(rows[0]["job_id"])["status"] == "failed"


def test_terrain_read_api_schema_refusal(read_api):
    response = post(read_api, {"section": "grid"})
    env = response.json()
    assert env.get("ok") is not True, response.text
    rows = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(row.get("error") or {}).get("reason_code") for row in rows]
    assert "tool_params_invalid" in reasons, response.text
    assert all(row["status"] != "complete" for row in rows)


def test_terrain_read_api_builtin_refusal_is_named(read_api, monkeypatch):
    # A stored state the read cannot report truthfully fails the job with the terrain code.
    backend = read_api[1]
    frame = json.loads(json.dumps(ps.DEFAULT_FRAME))
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    child = ps.physical_document({"grid": {"elevations": [1.0, 2.0], "rows": 1, "cols": 2, "x_min": 0.0,
                                           "x_max": 1.0, "y_min": 0.0, "y_max": 1.0}},
                                 drawing_units="m", source_sha256=GENERATE_SHA, capability="frame-generate",
                                 frame=frame, parent=head["state"]["artifact_id"])
    ph.publish_physical_state(backend, TENANT, DRAWING, child)
    response = post(read_api, {})
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "TERRAIN_GRID_INVALID"
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1 and jobs.get_job(rows[0]["job_id"])["status"] == "failed"
