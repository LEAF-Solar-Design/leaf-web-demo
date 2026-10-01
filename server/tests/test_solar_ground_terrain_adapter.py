"""sf-w5-terrain: Ground terrain operations over the reopened physical state
(server/solar_ground_terrain_adapter.py): the frozen frame, units and current-state selection, the
slope mesh (LEAFTERRAINMESH), the tracker slope check and its clear (LEAFTRACKERSLOPEVIOLATIONS,
LEAFCLEARTRACKERSLOPEVIOLATIONS), each a Ground Physical preview published as a child of the
physical head. Every expected value below was measured by running the module against the
FilesystemBackend the physical-state tests seed. The real stored terrains are the licensed
LEAFLANDXMLDEMO capture imported by server/solar_landxml_import.py and the committed terrain
intake's t1 to t5 Studio chain, whose t2, t6 and t7 parity receipts are reproduced through the head."""
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_ground_terrain as terrain  # noqa: E402
import solar_ground_terrain_adapter as ta  # noqa: E402
import solar_landxml_import as lx  # noqa: E402
import solar_physical_head as ph  # noqa: E402
import solar_physical_state as ps  # noqa: E402
import write_loop  # noqa: E402
from test_solar_landxml_import import REAL, landxml, plugin_grid  # noqa: E402
from test_solar_physical_state import (  # noqa: E402
    DRAWING, GENERATE_SHA, PREFIX, PROJECT, REVISION, TENANT, TERRAIN_SHA, TINY, intake, producer, receipt)
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_solve_commit import seed, seed_graphless  # noqa: E402

MESH = "terrain-mesh-render"
SLOPE = "tracker-slope-violations"
CAPTURE_GRID_SHA = "1c78f602025556918336710801265c08a50a9e9098b48d2bda4a669bc9927452"
# The generate intake's TinyTest preset as the Studio evidence reads it (slope_limits).
TINYTEST = {"MaxNsSlopePct": 8.5, "MaxRowToRowEwSlopePct": 10.0, "MaxAxialSlopePct": 8.5,
            "MaxCrossAxisSlopePct": 10.0, "MaxRowToRowSlopeDeg": 4.0, "MaxSlopePercent": 15.0, "Columns": 1}
FRAME_M = {"coordinate_system": "world", "transform": "identity", "drawing_units": "m",
           "meters_per_unit": 1.0, "crs": "none", "elevation_datum": "unrecorded",
           "horizontal": "drawing-units", "elevation": "metres"}


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, DRAWING))


def refused(code, fn, *args, **kw):
    with pytest.raises(ps.PhysicalStateError) as exc:
        fn(*args, **kw)
    assert exc.value.code == code
    return exc.value


def imp(backend, data=REAL, drawing_units="m", **kw):
    return lx.import_landxml_terrain(backend, TENANT, DRAWING, data, drawing_units=drawing_units,
                                     crs="none", **kw)


def publish(backend, state, capability="frame-generate", source=GENERATE_SHA, drawing_units="m", frame=None):
    """Publish `state` as a child of the current head (the first state when there is none)."""
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    document = ps.physical_document(state, drawing_units=drawing_units, source_sha256=source,
                                    capability=capability, frame=frame,
                                    parent=None if head is None else head["state"]["artifact_id"])
    return ph.publish_physical_state(backend, TENANT, DRAWING, document)["head"]


def head_document(backend):
    return ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)[1]


def mesh(backend, **kw):
    return ta.render_mesh(backend, TENANT, DRAWING, **kw)


def slope(backend, **kw):
    return ta.check_tracker_slope(backend, TENANT, DRAWING, **kw)


def clear(backend, **kw):
    return ta.clear_tracker_slope(backend, TENANT, DRAWING, **kw)


def read(backend):
    return ta.read_terrain(backend, TENANT, DRAWING, project_id=PROJECT)


def steep_file():
    """A planar 10 % rise to the north (z = 0.10 x northing) on a 10 m lattice over the generate
    intake's boundary (easting 0..100, northing 0..200): 231 points, northing easting elevation."""
    return landxml(points=["%d %d %r" % (n, e, round(0.10 * n, 6))
                           for n in range(0, 201, 10) for e in range(0, 101, 10)])


@pytest.fixture(scope="module")
def g1_state():
    """The generate intake's g1 Studio state: 144 TinyTest frames on LEAF-TRACKERS."""
    ev = producer("solar_ground_studio_evidence")
    source = intake("generate")
    preset, pile_store = ev.load_stores(source)
    state = ev.new_state()
    ev.STEPS["generate"](state, {"intake": source, "preset": preset, "pile_store": pile_store, "mpu": 1.0})
    assert ev.slope_limits(preset) == TINYTEST
    return state


def steep_head(backend, g1_state):
    publish(backend, g1_state)
    imp(backend, steep_file())


# ------------------------------------------------------------------ contract --

def test_terrain_adapter_constants():
    assert (ta.VIEW_SCHEMA, ta.RESULT_SCHEMA, ta.RECORD_SCHEMA, ta.MATURITY) == (
        "leaf.solar-terrain-view.v1", "leaf.solar-terrain-operation.v1", "leaf.solar-terrain-preview.v1",
        "preview")
    assert (ta.MESH_CAPABILITY, ta.SLOPE_CAPABILITY) == (MESH, SLOPE)
    assert ta.OPERATIONS == {"mesh": MESH, "slope": SLOPE, "slope-clear": SLOPE}
    assert ta.RECORD_KEYS == (MESH, SLOPE)
    assert ta.IDENTITY_TRANSFORM == (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    assert (ta.MAX_MESH_NODES, ta.MAX_PROJECT_ID_CHARS, ta.MAX_LIMIT_PERCENT, ta.MAX_LIMIT_DEGREES,
            ta.MAX_COLUMNS) == (90_000, 100, 1000.0, 90.0, 10_000)
    assert ta.LIMIT_KEYS == ("MaxNsSlopePct", "MaxRowToRowEwSlopePct", "MaxAxialSlopePct",
                             "MaxCrossAxisSlopePct", "MaxRowToRowSlopeDeg", "MaxSlopePercent", "Columns")
    assert ta.BUCKETS == ("Green", "Yellow", "Red")
    assert len(ta.CODES) == 17 and not ta.CODES & (ps.CODES | ph.CODES | lx.CODES)
    error = ta.TerrainAdapterError("TERRAIN_GRID_MISSING")
    assert isinstance(error, ps.PhysicalStateError) and error.code == str(error) == "TERRAIN_GRID_MISSING"


def test_terrain_adapter_codes_closed():
    source = (SERVER / "solar_ground_terrain_adapter.py").read_text(encoding="utf-8")
    assert set(re.findall(r'"(TERRAIN_[A-Z_]+)"', source)) == ta.CODES


# ------------------------------------------------- frame, units and selection --

def test_terrain_adapter_reads_the_imported_capture(backend):
    imported = imp(backend)
    head, view = read(backend)
    assert head == imported["head"]
    assert view == {
        "schema": "leaf.solar-terrain-view.v1", "maturity": "preview", "frame": FRAME_M,
        "grid": {"rows": 30, "cols": 30, "x_min": -100.0, "x_max": 100.0, "y_min": -100.0, "y_max": 100.0,
                 "cell_x": 6.896551724137931, "cell_y": 6.896551724137931,
                 "cell_x_m": 6.896551724137931, "cell_y_m": 6.896551724137931,
                 "elevation_min_m": -1.7552671418084063, "elevation_max_m": 1.7552671418084052,
                 "grid_sha256": CAPTURE_GRID_SHA},
        "mesh_faces": 0, "slope_markers": 0,
        "previews": {MESH: {"state": "absent", "record": None}, SLOPE: {"state": "absent", "record": None}},
        "drawing_id": DRAWING, "project_id": PROJECT}
    assert canonical(view) == "6eb734c14d767657d9e4a5ae49fd50246d0a3d25aef660146db4aefa166a4815"
    assert canonical(plugin_grid(REAL)) == CAPTURE_GRID_SHA


def test_terrain_adapter_read_needs_a_project_and_a_head(backend):
    assert read(backend) == (None, None)
    refused("TERRAIN_PROJECT_ID_INVALID", ta.read_terrain, backend, TENANT, DRAWING, project_id="")
    refused("TERRAIN_PROJECT_ID_INVALID", ta.read_terrain, backend, TENANT, DRAWING, project_id=None)
    refused("TERRAIN_PROJECT_ID_INVALID", ta.read_terrain, backend, TENANT, DRAWING, project_id="p" * 101)


def test_terrain_adapter_reads_a_head_without_a_grid(backend):
    publish(backend, TINY)
    _, view = read(backend)
    assert view["grid"] is None and view["frame"] == FRAME_M
    assert (view["mesh_faces"], view["slope_markers"]) == (0, 0)


def test_terrain_adapter_feet_drawing_reads_metres_per_unit_from_the_document(backend):
    imp(backend, drawing_units="ft")
    result = mesh(backend)
    assert result["frame"] == dict(FRAME_M, drawing_units="ft", meters_per_unit=0.3048)
    assert result["grid"] == {
        "rows": 30, "cols": 30, "x_min": -328.0839895013123, "x_max": 328.0839895013123,
        "y_min": -328.0839895013123, "y_max": 328.0839895013123,
        "cell_x": 22.626482034573264, "cell_y": 22.626482034573264,
        "cell_x_m": 6.8965517241379315, "cell_y_m": 6.8965517241379315,
        "elevation_min_m": -1.755267141808407, "elevation_max_m": 1.7552671418084107,
        "grid_sha256": "a67a1a71ae3f8404398909e2a9dc836b2a83b02ec421b41e6c2dae8332b04fed"}
    record = result["record"]
    assert (record["meters_per_unit"], record["faces"], record["buckets"], record["max_slope_percent"],
            record["mesh_sha256"]) == (0.3048, 841, {"Green": 717, "Yellow": 124, "Red": 0},
                                       8.913350836855596,
                                       "3b2102067a88d3fd306fcc53eec5d053edde91cdc05413c8787508e1c042b9f5")
    document = head_document(backend)
    grid = document["state"]["grid"]
    faces = ta.mesh_of(document)
    args = (grid["elevations"], grid["rows"], grid["cols"], grid["x_min"], grid["x_max"], grid["y_min"],
            grid["y_max"])
    assert faces == terrain.draw_grid_mesh(*args, 0.3048)
    assert faces != terrain.draw_grid_mesh(*args, 1.0)        # the plugin's default Meters answer
    assert faces[0]["vertices"] == [(-328.0839895013123, -328.0839895013123, -0.0),
                                    (-305.45750746673906, -328.0839895013123, 1.636614720144202),
                                    (-305.45750746673906, -305.45750746673906, 1.4676402372913677),
                                    (-328.0839895013123, -305.45750746673906, 0.4843661846411674)]
    assert (faces[0]["slope_percent"], faces[0]["bucket"]) == (7.233182417149315, "Yellow")


def test_terrain_adapter_frame_transform_must_be_identity(backend):
    frame = dict(ps.DEFAULT_FRAME, transform=[2, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
    grid = {"rows": 3, "cols": 3, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 9}
    publish(backend, {"grid": grid}, frame=frame)
    before = keys(backend)
    for call in (read, mesh, slope, clear):
        refused("TERRAIN_FRAME_UNSUPPORTED", call, backend)
    assert keys(backend) == before


def test_terrain_adapter_float_identity_is_identity(backend):
    frame = dict(ps.DEFAULT_FRAME, transform=[1.0, 0.0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1.0])
    grid = {"rows": 3, "cols": 3, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 9}
    publish(backend, {"grid": grid}, frame=frame)
    assert read(backend)[1]["frame"] == FRAME_M


def test_terrain_adapter_crs_and_datum_are_reported_never_applied(backend):
    frame = dict(ps.DEFAULT_FRAME, crs="EPSG:32611", elevation_datum="EPSG:5703")
    publish(backend, {"grid": plugin_grid(REAL)}, capability="landxml-import", source=TERRAIN_SHA, frame=frame)
    result = mesh(backend)
    assert result["frame"] == dict(FRAME_M, crs="EPSG:32611", elevation_datum="EPSG:5703")
    assert result["record"]["mesh_sha256"] == "3c396f556cb26e26f98afcf2a0599bb4279c3529829ea7629a8f9e1b2b21a05e"
    assert head_document(backend)["frame"] == frame


def test_terrain_adapter_grid_carrying_an_affine_frame_is_refused(backend):
    grid = {"rows": 3, "cols": 3, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0,
            "elevations": [1.0] * 9, "frame": [0.0, 0.0, 2.0, 0.0, 0.0, 2.0]}
    publish(backend, {"grid": grid})
    for call in (read, mesh, slope):
        refused("TERRAIN_FRAME_UNSUPPORTED", call, backend)


@pytest.mark.parametrize("grid", [
    {"rows": 1, "cols": 3, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 3},
    {"rows": 3, "cols": 1, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 3},
    {"rows": 2, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 0.0, "y_max": 2.0, "elevations": [1.0] * 4},
    {"rows": 2, "cols": 2, "x_min": 0.0, "y_min": 3.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 4},
    {"rows": 2, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 3},
    {"rows": "2", "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 4},
    {"rows": 2, "cols": 2, "x_min": "0", "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0] * 4},
    {"rows": 2, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [1.0, 1.0, "a", 1.0]},
    {"rows": 2, "cols": 2, "x_min": 0.0, "y_max": 2.0, "x_max": 2.0, "elevations": [1.0] * 4},
], ids=["one-row", "one-col", "flat-x", "inverted-y", "short", "rows-str", "bound-str", "elevation-str",
        "no-y-min"])
def test_terrain_adapter_grid_invalid(backend, grid):
    publish(backend, {"grid": grid})
    before = keys(backend)
    for call in (read, mesh, slope):
        refused("TERRAIN_GRID_INVALID", call, backend)
    assert keys(backend) == before


def test_terrain_adapter_grid_slope_overflow_is_refused_by_the_mesh(backend):
    grid = {"rows": 2, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 1e-310, "y_max": 1.0,
            "elevations": [0.0, 1.0, 0.0, 1.0]}
    publish(backend, {"grid": grid})
    assert read(backend)[1]["grid"]["cell_x"] == 1e-310
    refused("TERRAIN_GRID_INVALID", mesh, backend)


def test_terrain_adapter_mesh_size_bound():
    def document(rows, cols):
        grid = {"rows": rows, "cols": cols, "x_min": 0.0, "y_min": 0.0, "x_max": 299.0, "y_max": 299.0,
                "elevations": [float((i % cols) * 0.01 + (i // cols) * 0.02) for i in range(rows * cols)]}
        return ps.physical_document({"grid": grid}, drawing_units="m", source_sha256=TERRAIN_SHA,
                                    capability="terrain-import")
    faces = ta.mesh_of(document(300, 300))
    assert (len(faces), faces[0]["bucket"], faces[0]["slope_percent"]) == (89401, "Green", 2.0)
    refused("TERRAIN_GRID_TOO_LARGE", ta.mesh_of, document(301, 300))
    refused("TERRAIN_GRID_TOO_LARGE", ta.terrain_view, document(300, 301))


# ----------------------------------------------------------------- the mesh --

def test_terrain_adapter_mesh_publishes_a_child(backend):
    imported = imp(backend)
    before = keys(backend)
    result = mesh(backend)
    state = result["head"]["state"]
    record = {"schema": "leaf.solar-terrain-preview.v1", "capability": MESH, "maturity": "preview",
              "grid_sha256": CAPTURE_GRID_SHA, "meters_per_unit": 1.0, "faces": 841,
              "buckets": {"Green": 717, "Yellow": 124, "Red": 0}, "max_slope_percent": 8.913350836855564,
              "mesh_sha256": "3c396f556cb26e26f98afcf2a0599bb4279c3529829ea7629a8f9e1b2b21a05e"}
    assert result == {
        "schema": "leaf.solar-terrain-operation.v1", "maturity": "preview", "operation": "mesh",
        "capability": MESH, "created": True, "drawing_id": DRAWING, "project_id": PROJECT, "frame": FRAME_M,
        "grid": read(backend)[1]["grid"], "record": record, "replaced": 0,
        "head": {"schema": ph.HEAD_SCHEMA, "drawing_id": DRAWING, "project_id": PROJECT, "index": 1,
                 "parent": imported["head"]["state"]["artifact_id"], "state": state}}
    assert (state["artifact_id"], state["content_sha256"], state["byte_length"]) == (
        "72d6db89f1ccad4ff9f197afb7f75e12a751e984dfc4b2443e6dd9aba96612f4",
        "34c1e8be8b5b78260b6cc9090f508350d1931cbce2ba572c5f279f6ee0b15f00", 18786)
    assert canonical(result) == "df79c13dd64740470878bef770a68059049de0ca5dad39e9f072270b6186179c"
    assert keys(backend) - before == {
        PREFIX + "artifacts/" + state["artifact_id"] + ".json",
        PREFIX + "artifacts/blobs/" + state["content_sha256"] + ".bin",
        PREFIX + "physical/head-0001.json"}
    document = head_document(backend)
    assert (document["capability"], document["source"], document["frame"], document["units"]) == (
        MESH, {"kind": "ground-intake", "sha256": imported["source"]["content_sha256"]}, dict(ps.DEFAULT_FRAME),
        {"drawing_units": "m", "meters_per_unit": 1.0})
    assert list(document["state"]) == ["grid", "mesh_faces", "status_records"]
    assert document["state"]["grid"] == plugin_grid(REAL)
    assert (document["state"]["mesh_faces"], document["state"]["status_records"]) == (841, {MESH: record})


def test_terrain_adapter_mesh_reopens_as_the_plugin_mesh(backend):
    imp(backend)
    record = mesh(backend)["record"]
    faces = ta.mesh_of(head_document(backend))
    grid = plugin_grid(REAL)
    assert faces == terrain.draw_grid_mesh(grid["elevations"], grid["rows"], grid["cols"], grid["x_min"],
                                           grid["x_max"], grid["y_min"], grid["y_max"], 1.0)
    assert canonical(faces) == record["mesh_sha256"]
    assert faces[0]["vertices"] == [(-100.0, -100.0, -0.0), (-93.10344827586206, -100.0, 0.4988401666999546),
                                    (-93.10344827586206, -93.10344827586206, 0.4473367443264088),
                                    (-100.0, -93.10344827586206, 0.14763481307862797)]
    assert (faces[0]["slope_percent"], faces[0]["bucket"], faces[0]["color"]["true_color"]) == (
        7.233182417149342, "Yellow", 16762880)
    _, view = read(backend)
    assert view["previews"][MESH] == {"state": "current", "record": record}
    assert view["mesh_faces"] == 841
    assert canonical(view) == "f35b97518e824169b9ccbaceb0c6b9aae5e703e3be6bb504aa7f9ae874a3f404"


def test_terrain_adapter_mesh_twice_writes_nothing(backend):
    imp(backend)
    first = mesh(backend)
    before = keys(backend)
    again = mesh(backend)
    assert again == dict(first, created=False, replaced=841)
    assert keys(backend) == before


def test_terrain_adapter_new_grid_makes_the_mesh_stale(backend):
    imp(backend)
    mesh(backend)
    imp(backend, target_cells=10)
    _, view = read(backend)
    assert (view["previews"][MESH]["state"], view["mesh_faces"]) == ("stale", 841)
    again = mesh(backend)
    assert (again["replaced"], again["record"]["faces"], again["record"]["buckets"],
            again["record"]["max_slope_percent"], again["head"]["index"]) == (
        841, 81, {"Green": 80, "Yellow": 1, "Red": 0}, 5.259965194838709, 3)
    assert read(backend)[1]["previews"][MESH]["state"] == "current"


def test_terrain_adapter_flat_grid_is_green(backend):
    """C23 on the head: a flat 3 x 3 grid draws four faces, each 0 % and Green."""
    grid = {"rows": 3, "cols": 3, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0, "elevations": [10.0] * 9}
    publish(backend, {"grid": grid}, capability="slope-heatmap", source=TERRAIN_SHA)
    record = mesh(backend)["record"]
    assert (record["faces"], record["buckets"], record["max_slope_percent"], record["grid_sha256"]) == (
        4, {"Green": 4, "Yellow": 0, "Red": 0}, 0.0,
        "94fbde9599d270ef9332263807d8dcd625cbf25d20fda4af43974db1c08b0b83")


def test_terrain_adapter_needs_a_grid(backend):
    publish(backend, TINY)
    before = keys(backend)
    refused("TERRAIN_GRID_MISSING", mesh, backend)
    refused("TERRAIN_GRID_MISSING", slope, backend)
    assert clear(backend)["created"] is False
    assert keys(backend) == before


# --------------------------------------------------------- the slope check --

def test_terrain_adapter_slope_on_a_steep_terrain(backend, g1_state):
    steep_head(backend, g1_state)
    before = keys(backend)
    result = slope(backend, limits=TINYTEST)
    assert result["report"] == {
        "tracker_count": 144, "axial_rows_checked": 144, "axial_violation_rows": 107,
        "cross_axis_pairs_checked": 132, "cross_axis_violation_pairs": 2, "row_to_row_pairs_checked": 132,
        "row_to_row_angle_violation_pairs": 7, "trackers_needing_terrain_following": 107,
        "has_violations": True,
        "status": "Tracker slope: 107/144 axial / 2/132 cross / 7/132 row-to-row deg exceed active preset limits."}
    assert result["record"] == {
        "schema": "leaf.solar-terrain-preview.v1", "capability": SLOPE, "maturity": "preview",
        "grid_sha256": "b84cd3b8a511775bad580bb9cf29206f7e51f5c85626d5844df2b3737d2ee2d3",
        "meters_per_unit": 1.0, "limits": TINYTEST, "frames": 144, "markers": 109,
        "status": result["report"]["status"],
        "report_sha256": "0256753360246769da5bc7a6ca8b6be8b6572a7df9503c6a5411838ce68f04ab"}
    assert (result["operation"], result["created"], result["replaced"], result["head"]["index"]) == (
        "slope", True, 0, 2)
    assert (result["grid"]["rows"], result["grid"]["cols"], result["grid"]["elevation_max_m"]) == (30, 15, 20.0)
    assert canonical(result) == "e1e2d57b467d264d0d5e06b58c3dba46089d55ecd9d2bdb12b1fa945a50016f7"
    assert len(keys(backend) - before) == 3
    markers = head_document(backend)["state"]["slope_markers"]
    assert len(markers) == 109
    assert markers[0] == {"role": "slope", "bbox": [3.9589999999999996, 0.0, 3.9589999999999996, 15.636000000000003]}
    assert markers[-1] == {"role": "slope", "bbox": [11.876999999999999, 0.0, 11.876999999999999, 15.636000000000003]}


def test_terrain_adapter_slope_is_the_kernel_answer(backend, g1_state):
    steep_head(backend, g1_state)
    record = slope(backend, limits=TINYTEST)["record"]
    document = head_document(backend)
    entities = [{"kind": "LWPOLYLINE", "layer": f["layer"], "vertices": f["vertices"],
                 "frame_cell": {"row": f["row"], "col": f["col"]}} for f in g1_state["frames"]]
    kernel = terrain.tracker_slope_violations(document["state"]["grid"], entities, TINYTEST, 1.0)
    assert canonical(kernel["report"]) == record["report_sha256"]
    boxes = []
    for overlay in kernel["overlays"]:
        (x0, y0), (x1, y1) = overlay["vertices"]
        boxes.append({"role": "slope", "bbox": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]})
    assert boxes == document["state"]["slope_markers"]
    assert document["state"]["frames"] == g1_state["frames"]


def test_terrain_adapter_slope_default_limits(backend, g1_state):
    steep_head(backend, g1_state)
    result = slope(backend)
    assert result["record"]["limits"] == dict(terrain.DEFAULT_PRESET_LIMITS, Columns=0)
    assert (result["report"]["status"], result["record"]["markers"]) == (
        "Tracker slope: 143/144 axial / 2/132 cross / 7/132 row-to-row deg exceed active preset limits.", 144)


def test_terrain_adapter_slope_again_writes_nothing_and_replaces(backend, g1_state):
    steep_head(backend, g1_state)
    slope(backend)
    second = slope(backend, limits=TINYTEST)
    assert (second["created"], second["replaced"]) == (True, 144)
    before = keys(backend)
    third = slope(backend, limits=TINYTEST)
    assert (third["created"], third["replaced"], third["head"]) == (False, 109, second["head"])
    assert keys(backend) == before


def test_terrain_adapter_new_grid_makes_the_slope_stale(backend, g1_state):
    steep_head(backend, g1_state)
    slope(backend, limits=TINYTEST)
    imp(backend, steep_file(), target_cells=20)
    _, view = read(backend)
    assert (view["previews"][SLOPE]["state"], view["slope_markers"]) == ("stale", 109)


def test_terrain_adapter_clear(backend, g1_state):
    steep_head(backend, g1_state)
    slope(backend, limits=TINYTEST)
    result = clear(backend)
    assert (result["operation"], result["capability"], result["created"], result["replaced"], result["grid"],
            result["record"], result["head"]["index"]) == ("slope-clear", SLOPE, True, 109, None, None, 3)
    assert canonical(result) == "6ab0874eb649573eb5e5c2d3db28d705c952e0f97dfe77fd127202a4341aecb8"
    state = head_document(backend)["state"]
    assert (state["slope_markers"], state["status_records"]) == ([], {})
    assert read(backend)[1]["previews"][SLOPE] == {"state": "absent", "record": None}
    before = keys(backend)
    assert (clear(backend)["created"], clear(backend)["replaced"]) == (False, 0)
    assert keys(backend) == before


def test_terrain_adapter_slope_needs_tracker_rows(backend):
    imp(backend)
    before = keys(backend)
    refused("TERRAIN_NO_TRACKER_ROWS", slope, backend)
    assert keys(backend) == before
    other_layer = [{"layer": "OTHER", "vertices": [(0.0, 0.0), (1.0, 0.0), (1.0, 2.0), (0.0, 2.0)], "row": 0, "col": 0}]
    publish(backend, dict(head_document(backend)["state"], frames=other_layer))
    before = keys(backend)
    refused("TERRAIN_NO_TRACKER_ROWS", slope, backend)
    assert keys(backend) == before


@pytest.mark.parametrize("frames", [
    [{"layer": "LEAF-TRACKERS", "vertices": [(0.0, 0.0), (1.0, 0.0), (1.0, 2.0), (0.0, 2.0)], "row": 0}],
    [{"layer": "LEAF-TRACKERS", "vertices": [(0.0, 0.0), (1.0, 0.0), (1.0, 2.0), (0.0, 2.0)], "row": True, "col": 0}],
    [{"layer": 7, "vertices": [(0.0, 0.0), (1.0, 0.0), (1.0, 2.0), (0.0, 2.0)], "row": 0, "col": 0}],
    [{"layer": "LEAF-TRACKERS", "vertices": "abcd", "row": 0, "col": 0}],
    [{"layer": "LEAF-TRACKERS", "vertices": [("a", 0.0), (1.0, 0.0), (1.0, 2.0), (0.0, 2.0)], "row": 0, "col": 0}],
    ["frame"],
], ids=["no-col", "bool-row", "layer-int", "vertices-str", "vertex-str", "not-a-dict"])
def test_terrain_adapter_frames_invalid(backend, frames):
    imp(backend)
    publish(backend, dict(head_document(backend)["state"], frames=frames))
    refused("TERRAIN_FRAMES_INVALID", slope, backend)


def test_terrain_adapter_tracker_row_bound(backend, g1_state, monkeypatch):
    steep_head(backend, g1_state)
    monkeypatch.setattr(terrain, "MAX_TRACKER_ROWS", 143)
    refused("TERRAIN_TOO_MANY_ROWS", slope, backend)
    monkeypatch.setattr(terrain, "MAX_TRACKER_ROWS", 144)
    assert slope(backend, limits=TINYTEST)["record"]["frames"] == 144


@pytest.mark.parametrize("limits", [
    {}, {"MaxAxialSlopePct": 1000}, {"MaxAxialSlopePct": 0}, {"Columns": 10_000}, {"MaxRowToRowSlopeDeg": 90},
], ids=["empty", "percent-top", "percent-zero", "columns-top", "degrees-top"])
def test_terrain_adapter_limits_accepted(limits):
    assert ta.resolve_limits(limits) == terrain.preset_limits(limits)


@pytest.mark.parametrize("limits", [
    [], {"Bogus": 1.0}, {"Columns": True}, {"Columns": -1}, {"Columns": 10_001}, {"Columns": 1.0},
    {"MaxAxialSlopePct": float("nan")}, {"MaxAxialSlopePct": float("inf")}, {"MaxAxialSlopePct": -0.1},
    {"MaxAxialSlopePct": 1000.5}, {"MaxRowToRowSlopeDeg": 90.5}, {"MaxAxialSlopePct": "8.5"},
    {"MaxAxialSlopePct": True},
], ids=["list", "unknown-key", "columns-bool", "columns-negative", "columns-over", "columns-float", "nan", "inf",
        "negative", "percent-over", "degrees-over", "string", "bool"])
def test_terrain_adapter_limits_refused(backend, limits):
    """Limits are checked before anything is read: no head is needed to refuse them."""
    before = keys(backend)
    refused("TERRAIN_LIMITS_INVALID", slope, backend, limits=limits)
    assert keys(backend) == before


def test_terrain_adapter_huge_integer_limit_is_refused_before_read(backend, monkeypatch):
    def unexpected_read(*args, **kw):
        pytest.fail("invalid limits must be refused before any store read")

    monkeypatch.setattr(ta, "_context", unexpected_read)
    before = keys(backend)
    refused("TERRAIN_LIMITS_INVALID", slope, backend, limits={"MaxAxialSlopePct": 10 ** 400})
    assert keys(backend) == before


def test_terrain_adapter_mesh_refuses_301_by_2_grid():
    grid = {"rows": 301, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 300.0,
            "elevations": [0.0] * 602}
    document = ps.physical_document({"grid": grid}, drawing_units="m", source_sha256=TERRAIN_SHA,
                                    capability="terrain-import")
    refused("TERRAIN_GRID_TOO_LARGE", ta.mesh_of, document)


def test_terrain_adapter_view_refuses_2_by_301_grid():
    grid = {"rows": 2, "cols": 301, "x_min": 0.0, "y_min": 0.0, "x_max": 300.0, "y_max": 1.0,
            "elevations": [0.0] * 602}
    document = ps.physical_document({"grid": grid}, drawing_units="m", source_sha256=TERRAIN_SHA,
                                    capability="terrain-import")
    refused("TERRAIN_GRID_TOO_LARGE", ta.terrain_view, document)


def test_terrain_adapter_grid_span_above_scalar_bound_is_refused(backend):
    grid = {"rows": 2, "cols": 2, "x_min": -1e15, "y_min": 0.0, "x_max": 1e15, "y_max": 1.0,
            "elevations": [0.0] * 4}
    publish(backend, {"grid": grid})
    before = keys(backend)
    for call in (read, mesh, slope):
        refused("TERRAIN_GRID_INVALID", call, backend)
    assert keys(backend) == before


def test_terrain_adapter_elevation_above_scalar_bound_is_refused():
    document = ps.physical_document(TINY, drawing_units="m", source_sha256=TERRAIN_SHA,
                                    capability="terrain-import")
    document["state"]["grid"] = {
        "rows": 2, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0,
        "elevations": [0.0, 2e15, 0.0, 0.0]}
    refused("TERRAIN_GRID_INVALID", ta.mesh_of, document)


def test_terrain_adapter_more_than_20000_frames_is_refused_before_report(backend, monkeypatch):
    grid = {"rows": 2, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 10.0, "y_max": 10.0,
            "elevations": [0.0] * 4}
    frame = {"layer": "LEAF-TRACKERS", "vertices": [(0.0, 0.0), (1.0, 0.0), (1.0, 2.0), (0.0, 2.0)],
             "row": 0, "col": 0}
    publish(backend, {"grid": grid, "frames": [frame] * 20_001})

    def unexpected_report(*args, **kw):
        pytest.fail("too many frames must be refused before building the report")

    monkeypatch.setattr(terrain, "build_report", unexpected_report)
    before = keys(backend)
    refused("TERRAIN_FRAMES_INVALID", slope, backend)
    assert keys(backend) == before


def test_terrain_adapter_trackers_outside_grid_check_nothing_and_write_nothing(backend):
    grid = {"rows": 2, "cols": 2, "x_min": 0.0, "y_min": 0.0, "x_max": 10.0, "y_max": 10.0,
            "elevations": [0.0] * 4}
    frames = [{"layer": "LEAF-TRACKERS", "vertices": [(20.0, 20.0), (21.0, 20.0),
                                                        (21.0, 30.0), (20.0, 30.0)],
               "row": 0, "col": 0}]
    publish(backend, {"grid": grid, "frames": frames})
    before = keys(backend)
    refused("TERRAIN_NO_TRACKER_ROWS", slope, backend)
    assert keys(backend) == before


def test_terrain_adapter_slope_preview_is_current_immediately_after_check(backend, g1_state):
    steep_head(backend, g1_state)
    result = slope(backend, limits=TINYTEST)
    assert result["created"] is True and result["record"]["markers"] > 0
    head, view = read(backend)
    assert head == result["head"]
    assert view["slope_markers"] == result["record"]["markers"]
    assert view["previews"][SLOPE] == {"state": "current", "record": result["record"]}


# ------------------------------------------------- receipts through the head --

def test_terrain_adapter_reproduces_the_t2_t6_t7_receipts(backend):
    """The committed terrain intake's Studio chain published through the head: the mesh, the slope
    check and the clear, run by the adapter on the reopened state, reproduce the studio
    output_sha256 of ground-terrain-t2, -t6 and -t7."""
    ev = producer("solar_ground_studio_evidence")
    source = intake("terrain")
    preset, pile_store = ev.load_stores(source)
    ctx = {"intake": source, "preset": preset, "pile_store": pile_store, "mpu": 1.0}
    state = ev.new_state()
    ev.STEPS["topo-import"](state, ctx)
    publish(backend, state, capability="terrain-import", source=TERRAIN_SHA)

    result = mesh(backend)
    assert (result["replaced"], result["record"]["faces"], result["record"]["buckets"]) == (
        13261, 13261, {"Green": 13243, "Yellow": 18, "Red": 0})
    document = head_document(backend)
    rows = ([ev.terrain_mesh_row(ta.document_grid(document), ta.mesh_of(document))]
            + ev.removed_rows({"terrain-mesh": result["replaced"]}))
    t2 = ev.build_document(source, "terrain", "t2", MESH, "mesh", rows, REVISION)
    assert t2["output_sha256"] == receipt(MESH, "ground-terrain-t2.json")

    for operation in ("mesh", "generate", "piling", "range"):
        ev.STEPS[operation](state, ctx)
    publish(backend, state, capability="pile-length-range-check", source=TERRAIN_SHA)
    result = slope(backend, limits=ev.slope_limits(preset))
    assert (result["report"]["status"], result["replaced"], result["record"]["markers"]) == (
        "Tracker slope: 0/1197 axial / 0/1178 cross - within ASCE 7-16 budget.", 0, 0)
    rows = (ev.marker_rows(head_document(backend)["state"]["slope_markers"])
            + ev.removed_rows({"marker": result["replaced"]}))
    t6 = ev.build_document(source, "terrain", "t6", SLOPE, "slope", rows, REVISION)
    assert t6["output_sha256"] == receipt(SLOPE, "ground-terrain-t6.json")

    result = clear(backend)
    assert (result["created"], result["replaced"], result["head"]["index"]) == (True, 0, 4)
    t7 = ev.build_document(source, "terrain", "t7", SLOPE, "slope-clear",
                           ev.removed_rows({"marker": result["replaced"]}), REVISION)
    assert t7["output_sha256"] == receipt(SLOPE, "ground-terrain-t7.json")


# ------------------------------------------------ selection and refusals --

def test_terrain_adapter_expected_head(backend):
    imported = imp(backend)
    current = imported["head"]["state"]["artifact_id"]
    before = keys(backend)
    refused("TERRAIN_HEAD_MOVED", mesh, backend, expected_head="0" * 64)
    refused("TERRAIN_EXPECTED_HEAD_INVALID", mesh, backend, expected_head="zz")
    refused("TERRAIN_EXPECTED_HEAD_INVALID", mesh, backend, expected_head=current.upper())
    assert keys(backend) == before
    assert mesh(backend, expected_head=current)["head"]["parent"] == current
    refused("TERRAIN_HEAD_MOVED", slope, backend, expected_head=current)
    refused("TERRAIN_HEAD_MOVED", clear, backend, expected_head=current)


def test_terrain_adapter_no_head(backend):
    before = keys(backend)
    for call in (mesh, slope, clear):
        refused("TERRAIN_STATE_NOT_FOUND", call, backend)
    assert keys(backend) == before


@pytest.mark.parametrize("project_id", ["", "p" * 101, 7])
def test_terrain_adapter_project_id_invalid(backend, project_id):
    imp(backend)
    for call in (mesh, slope, clear):
        refused("TERRAIN_PROJECT_ID_INVALID", call, backend, project_id=project_id)


def test_terrain_adapter_context(backend, tmp_path, monkeypatch):
    imp(backend)
    for call in (mesh, slope, clear):
        refused("TERRAIN_PROJECT_MISMATCH", call, backend, project_id="leaf:project:other")
    refused("TERRAIN_DRAWING_NOT_FOUND", ta.render_mesh, backend, TENANT, "nosuch")
    (tmp_path / "graphless").mkdir()
    graphless, _ = seed_graphless(tmp_path / "graphless", monkeypatch)
    refused("TERRAIN_GRAPH_REQUIRED", ta.render_mesh, graphless, TENANT, DRAWING)


def test_terrain_adapter_drained(backend, monkeypatch):
    imp(backend)
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    before = keys(backend)
    for call in (mesh, slope, clear):
        refused("TERRAIN_WRITES_DRAINED", call, backend)
    refused("TERRAIN_WRITES_DRAINED", slope, backend, limits={"Bogus": 1})
    assert keys(backend) == before
    assert read(backend)[1]["grid"]["grid_sha256"] == CAPTURE_GRID_SHA


def test_terrain_adapter_wide_grid_with_bounded_cells_checks_tracker_slope(backend):
    grid = {"rows": 2, "cols": 3, "x_min": -1e15, "x_max": 1e15,
            "y_min": 0.0, "y_max": 10.0, "elevations": [0.0] * 6}
    frames = [{"layer": "LEAF-TRACKERS", "vertices": [(0.0, 0.0), (1.0, 0.0),
                                                        (1.0, 2.0), (0.0, 2.0)],
               "row": 0, "col": 0}]
    head = publish(backend, {"grid": grid, "frames": frames})
    result = slope(backend)
    assert result["created"] is True
    assert result["head"]["index"] == head["index"] + 1
    assert result["report"]["axial_rows_checked"] == 1
    assert result["grid"]["cell_x"] == 1e15


def test_terrain_adapter_cell_width_above_scalar_bound_refuses_without_publish(backend):
    grid = {"rows": 2, "cols": 2, "x_min": -1e15, "x_max": 1e15,
            "y_min": 0.0, "y_max": 10.0, "elevations": [0.0] * 4}
    head = publish(backend, {"grid": grid})
    before = {key: backend.get(key) for key in keys(backend)}
    refused("TERRAIN_GRID_INVALID", ta.document_grid, head_document(backend))
    for call in (mesh, slope):
        refused("TERRAIN_GRID_INVALID", call, backend)
    assert ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT) == head
    assert {key: backend.get(key) for key in keys(backend)} == before


def test_terrain_adapter_feet_mesh_coordinates_within_scalar_bound_publish(backend):
    grid = {"rows": 2, "cols": 2, "x_min": 0.0, "x_max": 10.0,
            "y_min": 0.0, "y_max": 10.0, "elevations": [3e14] * 4}
    head = publish(backend, {"grid": grid}, drawing_units="ft")
    result = mesh(backend)
    assert result["created"] is True
    assert result["head"]["index"] == head["index"] + 1
    faces = ta.mesh_of(head_document(backend))
    assert len(faces) == 1
    assert all(vertex[2] == 3e14 / 0.3048 and abs(vertex[2]) <= 1e15
               for face in faces for vertex in face["vertices"])


def test_terrain_adapter_feet_mesh_coordinates_above_scalar_bound_refuse(backend):
    grid = {"rows": 2, "cols": 2, "x_min": 0.0, "x_max": 10.0,
            "y_min": 0.0, "y_max": 10.0, "elevations": [3.1e14] * 4}
    head = publish(backend, {"grid": grid}, drawing_units="ft")
    document = head_document(backend)
    before = {key: backend.get(key) for key in keys(backend)}
    refused("TERRAIN_GRID_INVALID", mesh, backend)
    refused("TERRAIN_GRID_INVALID", ta.mesh_of, document)
    assert ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT) == head
    assert {key: backend.get(key) for key in keys(backend)} == before


def test_terrain_adapter_concurrent_writer_conflicts(backend, monkeypatch):
    imported = imp(backend)
    real_publish = ph.publish_physical_state

    def racing(backend_, tenant, drawing, document, **kw):
        rival = ps.physical_document(dict(head_document(backend_)["state"], mesh_faces=1),
                                     drawing_units="m", source_sha256=GENERATE_SHA, capability="frame-generate",
                                     parent=imported["head"]["state"]["artifact_id"])
        real_publish(backend_, tenant, drawing, rival)
        return real_publish(backend_, tenant, drawing, document, **kw)

    monkeypatch.setattr(ta.ph, "publish_physical_state", racing)
    refused("PHYSICAL_HEAD_CONFLICT", mesh, backend)
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert head["index"] == 1 and head_document(backend)["capability"] == "frame-generate"
