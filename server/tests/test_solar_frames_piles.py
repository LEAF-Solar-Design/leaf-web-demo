"""sf-w5-frames-piles: native Ground frame and pile operations over the drawing's current
physical state (server/solar_frames_piles.py), published as children of the physical head and
reopened unchanged. Every expected value below was measured by running the module against the
FilesystemBackend the physical-state tests seed. The real fixtures are the committed G11 intakes
(docs/parity/evidence/ground/generate and terrain, run through Studio's own evidence producer,
scripts/solar_ground_studio_evidence.py) and the licensed LEAFLANDXMLDEMO capture stored through
server/solar_landxml_import.py."""
import copy
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

import solar_frames_piles as fp  # noqa: E402
import solar_ground_frames as frames  # noqa: E402
import solar_ground_terrain as terrain  # noqa: E402
import solar_landxml_import as lx  # noqa: E402
import solar_physical_head as ph  # noqa: E402
import solar_physical_state as ps  # noqa: E402
from test_solar_landxml_import import REAL  # noqa: E402
from test_solar_physical_state import (  # noqa: E402
    DRAWING, PREFIX, PROJECT, TENANT, TERRAIN_SHA, TINY, intake, producer)
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_solve_commit import seed, seed_graphless  # noqa: E402

GEN = intake("generate")
TER = intake("terrain")
PRESET = GEN["active_preset"]
TEMPLATE = GEN["pile_template"]
SQUARE_M = [[-90.0, -90.0], [90.0, -90.0], [90.0, 90.0], [-90.0, 90.0]]
SQUARE_FT = [[-295.0, -295.0], [295.0, -295.0], [295.0, 295.0], [-295.0, 295.0]]
OVERHANG_M = [[-90.0, -90.0], [130.0, -90.0], [130.0, 90.0], [-90.0, 90.0]]


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def ev():
    return producer("solar_ground_studio_evidence")


def evidence_states(kind, upto):
    """Studio's own evidence state after each step of the fixture's scenario, deep-copied."""
    module = ev()
    source = intake(kind)
    preset, pile_store = module.load_stores(source)
    ctx = {"intake": source, "preset": preset, "pile_store": pile_store, "mpu": 1.0}
    state = module.new_state()
    out = {}
    for step_id, _, operation in module.SCENARIOS[kind]:
        module.STEPS[operation](state, ctx)
        out[step_id] = copy.deepcopy(state)
        if step_id == upto:
            break
    return out


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    import write_loop
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, DRAWING))


def head_id(backend):
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    return None if head is None else head["state"]["artifact_id"]


def refused(code, fn, *args, **kw):
    with pytest.raises(ps.PhysicalStateError) as exc:
        fn(*args, **kw)
    assert exc.value.code == code
    return exc.value


def generate(backend, base=None, boundary=None, preset=PRESET, drawing_units="m", **kw):
    return fp.generate_frames(backend, TENANT, DRAWING, base=base, boundary=boundary or GEN["boundary"],
                              preset=preset, drawing_units=drawing_units, **kw)


def piles(backend, base, preset=PRESET, template=TEMPLATE, **kw):
    return fp.generate_piles(backend, TENANT, DRAWING, base=base, preset=preset, pile_template=template, **kw)


def seed_head(backend, state, drawing_units="m", capability="terrain-import"):
    document = ps.physical_document(state, drawing_units=drawing_units, source_sha256=TERRAIN_SHA,
                                    capability=capability)
    return ph.publish_physical_state(backend, TENANT, DRAWING, document)["head"]["state"]["artifact_id"]


def state_of(backend):
    return fp.load_frames_piles(backend, TENANT, DRAWING, project_id=PROJECT)[1]["state"]


def narrow(**piling):
    preset = copy.deepcopy(PRESET)
    preset["Piling"].update(piling)
    return preset


# ------------------------------------------------------------------ contract --

def test_frames_piles_constants():
    assert (fp.RESULT_SCHEMA, fp.PREVIEW_SCHEMA, fp.MATURITY) == (
        "leaf.solar-frames-piles.v1", "leaf.solar-frames-piles-preview.v1", "preview")
    assert fp.OPERATIONS == ("frame-generate", "frame-collision-detect", "piling-generate",
                             "pile-length-range-check")
    assert fp.OUTCOMES == ("published", "retry", "unchanged")
    assert (fp.MAX_PROJECT_ID_CHARS, fp.MAX_BOUNDARY_VERTICES, fp.MAX_ABS_COORDINATE, fp.MAX_PRESET_CHARS,
            fp.MAX_NAME_CHARS) == (100, 20_000, 1e9, 1_000_000, 256)
    assert fp.NEW_STATE == (("grid", None), ("mesh_faces", 0), ("frames", []), ("piles", []),
                            ("collision_layer", []), ("slope_markers", []), ("next_handle", 1))
    assert fp.GENERATE_CONSTRAINT_KEYS == ("restriction_outlines", "setback_rings", "road_lines")
    assert fp.PILING_CONSTRAINT_KEYS == ("road_lines",)
    assert len(fp.CODES) == 25 and not fp.CODES & (ps.CODES | ph.CODES | lx.CODES)
    error = fp.FramesPilesError("FRAMES_PILES_NO_FRAMES")
    assert isinstance(error, ps.PhysicalStateError) and error.code == str(error) == "FRAMES_PILES_NO_FRAMES"


def test_frames_piles_codes_closed():
    source = (SERVER / "solar_frames_piles.py").read_text(encoding="utf-8")
    assert set(re.findall(r'"(FRAMES_PILES_[A-Z_]+)"', source)) == fp.CODES


@pytest.mark.parametrize("code, expected", [
    ("frames_over_cap", "FRAMES_PILES_LIMIT_EXCEEDED"), ("tracker_frame_too_many_vertices", "FRAMES_PILES_LIMIT_EXCEEDED"),
    ("stations_too_many", "FRAMES_PILES_LIMIT_EXCEEDED"), ("frame_footprint_degenerate", "FRAMES_PILES_PRESET_INVALID"),
    ("piling_counts", "FRAMES_PILES_PRESET_INVALID"), ("template_counts", "FRAMES_PILES_PILE_TEMPLATE_INVALID"),
    ("terrain_sample_invalid", "FRAMES_PILES_TERRAIN_INVALID"),
    ("boundary_too_few_vertices", "FRAMES_PILES_BOUNDARY_INVALID"),
    ("frame_cell_malformed", "FRAMES_PILES_STATE_INVALID"), ("entities_malformed", "FRAMES_PILES_STATE_INVALID"),
])
def test_frames_piles_kernel_refusal_map(code, expected):
    assert fp._kernel_refusal(frames.GroundFramesError(code, "x")) == expected


# ------------------------------------------------- C17: the generate fixture --

def test_frames_piles_first_generate_is_the_evidence_g1(backend):
    before = keys(backend)
    result = generate(backend)
    assert (result["operation"], result["outcome"], result["created"], result["maturity"]) == (
        "frame-generate", "published", True, "preview")
    assert result["base"] is None and result["head"]["index"] == 0 and result["head"]["parent"] is None
    assert result["units"] == {"drawing_units": "m", "meters_per_unit": 1.0}
    assert result["terrain"] == {"present": False, "sampled": False, "rows": None, "cols": None,
                                 "grid_sha256": None}
    assert result["summary"] == {"frames_added": 144, "frames_off_terrain": 0, "preset_name": "TinyTest"}
    assert result["preview"] == {"schema": "leaf.solar-frames-piles-preview.v1", "maturity": "preview",
                                 "frames": 144, "piles": 0, "collision_markers": 0, "range_markers": 0,
                                 "terrain": False}
    g1 = evidence_states("generate", "g1")["g1"]
    view, document, summary = fp.load_frames_piles(backend, TENANT, DRAWING, project_id=PROJECT)
    assert view == result["head"] and summary == result["preview"]
    assert document["state"] == g1 and repr(document["state"]) == repr(g1)
    assert document["frame"] == ps.DEFAULT_FRAME and document["capability"] == "frame-generate"
    assert document["source"]["sha256"] == canonical({"operation": "frame-generate", "base": None,
                                                       "drawing_units": "m", "boundary": GEN["boundary"],
                                                       "preset": PRESET})
    assert document["state"]["frames"][0]["vertices"] == [(0.0, 0.0), (7.917999999999999, 0.0),
                                                           (7.917999999999999, 15.636000000000003),
                                                           (0.0, 15.636000000000003)]
    assert [f["handle"] for f in document["state"]["frames"][:3]] == ["1", "2", "3"]
    assert document["state"]["next_handle"] == 145
    assert (result["head"]["state"]["artifact_id"], result["head"]["state"]["byte_length"]) == MEASURED_G1_STATE
    assert canonical(result) == MEASURED_G1_RESULT
    assert len(keys(backend) - before) == 3


MEASURED_G1_STATE = ("4431effa54c7630c12269f5ae9ee2b9a9edd54d5b152ba810bf409e5ca9bfc3b", 50469)
MEASURED_G1_RESULT = "b2a79f4436980d9d4b15a12c6d752b7727874e09a38e5c767d2d95c9ac61da4c"


def test_frames_piles_retry_returns_the_same_head(backend):
    first = generate(backend)
    before = keys(backend)
    again = generate(backend)
    assert again["outcome"] == "retry" and again["created"] is False and again["summary"] is None
    assert again["head"] == first["head"] and again["preview"] == first["preview"]
    assert keys(backend) == before


def test_frames_piles_stale_base_is_refused(backend):
    generate(backend)
    before = keys(backend)
    refused("FRAMES_PILES_STALE_BASE", generate, backend, boundary=[[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [0.0, 50.0]])
    refused("FRAMES_PILES_STALE_BASE", fp.detect_collisions, backend, TENANT, DRAWING, base="0" * 64)
    assert keys(backend) == before


def test_frames_piles_generate_chain_matches_the_evidence(backend):
    """g1, g2 (no overlaps: unchanged), g5 (a second generate on top) and g6 (144 collisions)."""
    g1 = generate(backend)
    base = g1["head"]["state"]["artifact_id"]
    before = keys(backend)
    g2 = fp.detect_collisions(backend, TENANT, DRAWING, base=base)
    assert (g2["outcome"], g2["created"], g2["summary"]) == ("unchanged", False, {"frames_checked": 144,
                                                                                  "collisions": 0})
    assert keys(backend) == before and g2["head"] == g1["head"]
    g5 = generate(backend, base=base)
    assert g5["head"]["index"] == 1 and g5["head"]["parent"] == base and g5["summary"]["frames_added"] == 144
    g6 = fp.detect_collisions(backend, TENANT, DRAWING, base=g5["head"]["state"]["artifact_id"])
    assert g6["summary"] == {"frames_checked": 288, "collisions": 144} and g6["head"]["index"] == 2
    evidence = evidence_states("generate", "g6")["g6"]
    state = state_of(backend)
    assert state["frames"] == evidence["frames"] and repr(state["frames"]) == repr(evidence["frames"])
    assert state["collision_layer"] == evidence["collision_layer"]
    assert state["next_handle"] == 289 and state["piles"] == []
    assert g6["preview"]["collision_markers"] == 144


def test_frames_piles_piling_needs_terrain(backend):
    base = generate(backend)["head"]["state"]["artifact_id"]
    before = keys(backend)
    refused("FRAMES_PILES_TERRAIN_REQUIRED", piles, backend, base)
    assert keys(backend) == before


# ------------------------------------------- the terrain fixture (t3 to t5) --

@pytest.fixture(scope="module")
def terrain_chain():
    return evidence_states("terrain", "t5")


def test_frames_piles_terrain_chain_matches_the_evidence(backend, terrain_chain):
    base = seed_head(backend, copy.deepcopy(terrain_chain["t2"]))
    t3 = fp.generate_frames(backend, TENANT, DRAWING, base=base, boundary=TER["boundary"],
                            preset=TER["active_preset"], drawing_units="m")
    assert t3["summary"] == {"frames_added": 1197, "frames_off_terrain": 0, "preset_name": "TinyTest"}
    assert t3["terrain"]["present"] and t3["terrain"]["sampled"] and (t3["terrain"]["rows"],
                                                                       t3["terrain"]["cols"]) == (90, 150)
    assert t3["terrain"]["grid_sha256"] == canonical(terrain.neutral_grid(terrain_chain["t2"]["grid"]))
    state = state_of(backend)
    assert state == terrain_chain["t3"] and repr(state) == repr(terrain_chain["t3"])
    t4 = fp.generate_piles(backend, TENANT, DRAWING, base=t3["head"]["state"]["artifact_id"],
                           preset=TER["active_preset"], pile_template=TER["pile_template"])
    assert t4["summary"] == MEASURED_T4_SUMMARY
    state = state_of(backend)
    assert state == terrain_chain["t4"] and repr(state) == repr(terrain_chain["t4"])
    assert len(state["piles"]) == 9576
    t5 = fp.check_pile_lengths(backend, TENANT, DRAWING, base=t4["head"]["state"]["artifact_id"],
                               preset=TER["active_preset"])
    assert (t5["outcome"], t5["summary"]) == ("unchanged", {"total_piles": 9576, "out_of_range": 0,
                                                            "min_m": 1.0, "max_m": 6.0})
    assert (t4["head"]["state"]["artifact_id"], t4["head"]["state"]["byte_length"]) == MEASURED_T4_STATE


MEASURED_T4_SUMMARY = {"native_frames": 1197, "piles": 9576, "piles_replaced": 0, "grid_piles": 9576,
                       "joint_piles": 0, "station_piles": 0, "short_trackers": 0, "template_name": "Default"}
MEASURED_T4_STATE = ("a142b37358df425668c2b483ac1134169b34041444816e9872ce417f33fb75e9", 6082384)


def test_frames_piles_piling_twice_is_unchanged(backend, terrain_chain):
    base = seed_head(backend, copy.deepcopy(terrain_chain["t3"]))
    first = piles(backend, base, TER["active_preset"], TER["pile_template"])
    before = keys(backend)
    second = piles(backend, first["head"]["state"]["artifact_id"], TER["active_preset"], TER["pile_template"])
    assert second["outcome"] == "unchanged" and second["summary"]["piles_replaced"] == 9576
    assert keys(backend) == before and second["head"] == first["head"]


def test_frames_piles_range_window_markers(backend, terrain_chain):
    base = seed_head(backend, copy.deepcopy(terrain_chain["t4"]))
    result = fp.check_pile_lengths(backend, TENANT, DRAWING, base=base, preset=narrow(MaxPileLengthM=1.5))
    assert result["summary"] == {"total_piles": 9576, "out_of_range": 9576, "min_m": 1.0, "max_m": 1.5}
    assert result["preview"]["range_markers"] == 9576 and result["head"]["index"] == 1
    marker = state_of(backend)["collision_layer"][0]
    assert marker["role"] == "pile-range" and marker == MEASURED_RANGE_MARKER
    refused("FRAMES_PILES_RANGE_INVALID", fp.check_pile_lengths, backend, TENANT, DRAWING,
            base=result["head"]["state"]["artifact_id"], preset=narrow(MaxPileLengthM=0.0))
    refused("FRAMES_PILES_RANGE_INVALID", fp.check_pile_lengths, backend, TENANT, DRAWING,
            base=result["head"]["state"]["artifact_id"], preset=narrow(MaxPileLengthM="1e400"))


MEASURED_RANGE_MARKER = {"role": "pile-range", "bbox": [1.5086, 5.1370000000000005, 1.6585999999999999, 5.287000000000001]}


# ---------------------------------------------- the stored LandXML terrain --

def landxml_head(backend, drawing_units="m"):
    result = lx.import_landxml_terrain(backend, TENANT, DRAWING, REAL, drawing_units=drawing_units, crs="none")
    return result["head"]["state"]["artifact_id"]


def test_frames_piles_on_the_landxml_terrain_in_metres(backend):
    base = landxml_head(backend)
    made = generate(backend, base=base, boundary=SQUARE_M)
    assert made["summary"] == MEASURED_LX_M_GENERATE
    state = state_of(backend)
    assert list(state) == ["grid", "frames", "next_handle"]
    placed = piles(backend, made["head"]["state"]["artifact_id"])
    assert placed["summary"] == MEASURED_LX_M_PILES
    assert placed["terrain"] == {"present": True, "sampled": True, "rows": 30, "cols": 30,
                                 "grid_sha256": "1c78f602025556918336710801265c08a50a9e9098b48d2bda4a669bc9927452"}
    interp = terrain.terrain_interpolator(state_of(backend)["grid"], 1.0)
    for ent in state_of(backend)["piles"]:
        x, y, bottom = ent["center"]
        assert abs((bottom + 1.5) - interp.interpolate_z(x, y)) < 1e-9


MEASURED_LX_M_GENERATE = {"frames_added": 242, "frames_off_terrain": 0, "preset_name": "TinyTest"}
MEASURED_LX_M_PILES = {"native_frames": 242, "piles": 1936, "piles_replaced": 0, "grid_piles": 1936, "joint_piles": 0,
                       "station_piles": 0, "short_trackers": 0, "template_name": "Default"}


def test_frames_piles_on_the_landxml_terrain_in_feet(backend):
    """The unit boundary: frames and piles in feet, the grid's elevations in metres."""
    base = landxml_head(backend, "ft")
    made = generate(backend, base=base, boundary=SQUARE_FT, drawing_units="ft")
    assert made["units"] == {"drawing_units": "ft", "meters_per_unit": 0.3048}
    assert made["summary"] == MEASURED_LX_FT_GENERATE
    first = state_of(backend)["frames"][0]["vertices"]
    assert first == MEASURED_LX_FT_FIRST_FRAME
    assert abs((first[1][0] - first[0][0]) * 0.3048 - 7.917999999999999) < 1e-9
    placed = piles(backend, made["head"]["state"]["artifact_id"])
    assert placed["summary"] == MEASURED_LX_FT_PILES
    assert placed["terrain"]["grid_sha256"] == "a67a1a71ae3f8404398909e2a9dc836b2a83b02ec421b41e6c2dae8332b04fed"
    state = state_of(backend)
    interp = terrain.terrain_interpolator(state["grid"], 1.0)
    for ent in state["piles"]:
        x, y, bottom = ent["center"]
        assert abs((bottom + 1.5 / 0.3048) * 0.3048 - interp.interpolate_z(x, y)) < 1e-9
        assert ent["pile"]["depth_m"] == 2.0 and abs(ent["thickness"] - 2.0 / 0.3048) < 1e-12
    refused("FRAMES_PILES_UNITS_MISMATCH", generate, backend, base=placed["head"]["state"]["artifact_id"],
            boundary=SQUARE_FT, drawing_units="m")


MEASURED_LX_FT_GENERATE = MEASURED_LX_M_GENERATE
MEASURED_LX_FT_FIRST_FRAME = [(-295.0, -295.0), (-269.0223097112861, -295.0), (-269.0223097112861, -243.70078740157481),
                              (-295.0, -243.70078740157481)]
MEASURED_LX_FT_PILES = MEASURED_LX_M_PILES


def test_frames_piles_off_terrain_is_refused(backend):
    base = landxml_head(backend)
    made = generate(backend, base=base, boundary=OVERHANG_M)
    assert made["summary"] == MEASURED_LX_OVERHANG
    before = keys(backend)
    refused("FRAMES_PILES_OFF_TERRAIN", piles, backend, made["head"]["state"]["artifact_id"])
    assert keys(backend) == before


MEASURED_LX_OVERHANG = {"frames_added": 297, "frames_off_terrain": 33, "preset_name": "TinyTest"}


def test_frames_piles_no_frames(backend):
    base = landxml_head(backend)
    refused("FRAMES_PILES_NO_FRAMES", fp.detect_collisions, backend, TENANT, DRAWING, base=base)
    refused("FRAMES_PILES_NO_FRAMES", piles, backend, base)
    refused("FRAMES_PILES_NO_PILES", fp.check_pile_lengths, backend, TENANT, DRAWING, base=base, preset=PRESET)
    refused("FRAMES_PILES_NO_FRAMES_FIT", generate, backend, base=base,
            boundary=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])


@pytest.mark.parametrize("call", ["collision", "piling", "range"])
def test_frames_piles_state_required(backend, call):
    fn = {"collision": lambda: fp.detect_collisions(backend, TENANT, DRAWING, base=None),
          "piling": lambda: piles(backend, None),
          "range": lambda: fp.check_pile_lengths(backend, TENANT, DRAWING, base=None, preset=PRESET)}[call]
    refused("FRAMES_PILES_STATE_REQUIRED", fn)
    assert not any("/physical/" in key for key in keys(backend))


# -------------------------------------------------- refused, not approximated --

def test_frames_piles_pvcase_source_is_refused(backend):
    state = copy.deepcopy(TINY)
    state["frames"].append({"type": "INSERT", "layer": "PVCASE-TRACKERS", "name": "TRK"})
    base = seed_head(backend, state, capability="frame-generate")
    refused("FRAMES_PILES_PVCASE_UNSUPPORTED", piles, backend, base)


@pytest.mark.parametrize("key", ["restriction_outlines", "setback_rings", "road_lines"])
def test_frames_piles_site_constraints_are_refused(backend, key):
    base = landxml_head(backend)
    made = generate(backend, base=base, boundary=SQUARE_M)
    state = dict(state_of(backend))
    state[key] = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]]
    document = ps.physical_document(state, drawing_units="m", source_sha256=TERRAIN_SHA, capability="road-add",
                                    parent=made["head"]["state"]["artifact_id"])
    base = ph.publish_physical_state(backend, TENANT, DRAWING, document)["head"]["state"]["artifact_id"]
    refused("FRAMES_PILES_SITE_CONSTRAINTS_UNSUPPORTED", generate, backend, base=base, boundary=SQUARE_M)
    if key == "road_lines":
        refused("FRAMES_PILES_SITE_CONSTRAINTS_UNSUPPORTED", piles, backend, base)
    else:
        assert piles(backend, base)["outcome"] == "published"


@pytest.mark.parametrize("grid", [
    {"elevations": [1.0], "rows": 1, "cols": 1, "x_min": 0.0, "x_max": 1.0, "y_min": 0.0, "y_max": 1.0},
    {"elevations": [1.0, 2.0], "rows": 2, "cols": 2, "x_min": 0.0, "x_max": 1.0, "y_min": 0.0, "y_max": 1.0},
])
def test_frames_piles_invalid_terrain(backend, grid):
    base = seed_head(backend, {"grid": grid})
    refused("FRAMES_PILES_TERRAIN_INVALID", generate, backend, base=base)
    refused("FRAMES_PILES_TERRAIN_INVALID", piles, backend, base)


def test_frames_piles_limit_exceeded(backend, monkeypatch):
    assert fp.frames is frames  # the evidence producer loads its own copy; the module's is the one patched
    monkeypatch.setattr(frames, "MAX_FRAMES", 10)
    refused("FRAMES_PILES_LIMIT_EXCEEDED", generate, backend)
    assert not any("/physical/" in key for key in keys(backend))


# ------------------------------------------------------- parameter refusals --

def _preset(**changes):
    preset = copy.deepcopy(PRESET)
    preset.update(changes)
    return preset


@pytest.mark.parametrize("kw, code", [
    ({"base": "zz"}, "FRAMES_PILES_BASE_INVALID"),
    ({"base": "A" * 64}, "FRAMES_PILES_BASE_INVALID"),
    ({"drawing_units": "in"}, "FRAMES_PILES_DRAWING_UNITS_INVALID"),
    ({"drawing_units": None}, "FRAMES_PILES_DRAWING_UNITS_INVALID"),
    ({"boundary": [[0.0, 0.0], [1.0, 0.0]]}, "FRAMES_PILES_BOUNDARY_INVALID"),
    ({"boundary": [[0.0, 0.0], [1.0, "a"], [1.0, 1.0]]}, "FRAMES_PILES_BOUNDARY_INVALID"),
    ({"boundary": [[0.0, 0.0], [1.0, float("nan")], [1.0, 1.0]]}, "FRAMES_PILES_BOUNDARY_INVALID"),
    ({"boundary": [[0.0, 0.0], [1e10, 0.0], [1.0, 1.0]]}, "FRAMES_PILES_BOUNDARY_INVALID"),
    ({"boundary": [[0.0, 0.0], [True, 0.0], [1.0, 1.0]]}, "FRAMES_PILES_BOUNDARY_INVALID"),
    ({"boundary": [[0.0, 0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]}, "FRAMES_PILES_BOUNDARY_INVALID"),
    ({"boundary": [[float(i), 0.0] for i in range(20_001)]}, "FRAMES_PILES_BOUNDARY_INVALID"),
    ({"preset": "TinyTest"}, "FRAMES_PILES_PRESET_INVALID"),
    ({"preset": _preset(Name="")}, "FRAMES_PILES_PRESET_INVALID"),
    ({"preset": _preset(Name="x" * 257)}, "FRAMES_PILES_PRESET_INVALID"),
    ({"preset": _preset(PileTemplateName="x" * 257)}, "FRAMES_PILES_PRESET_INVALID"),
    ({"preset": _preset(TiltDegrees=float("inf"))}, "FRAMES_PILES_PRESET_INVALID"),
    ({"preset": _preset(Pad="x" * 1_000_000)}, "FRAMES_PILES_PRESET_INVALID"),
    ({"preset": _preset(Rows=0)}, "FRAMES_PILES_PRESET_INVALID"),
    ({"project_id": ""}, "FRAMES_PILES_PROJECT_ID_INVALID"),
    ({"project_id": "p" * 101}, "FRAMES_PILES_PROJECT_ID_INVALID"),
])
def test_frames_piles_parameter_refusals(backend, kw, code):
    before = keys(backend)
    refused(code, generate, backend, **kw)
    assert keys(backend) == before


@pytest.mark.parametrize("template", ["Default", [], {"Name": "x" * 257}, {"Name": "T", "PileDiameterM": float("nan")}])
def test_frames_piles_template_refusals(backend, template):
    refused("FRAMES_PILES_PILE_TEMPLATE_INVALID", piles, backend, None, template=template)


def test_frames_piles_context_refusals(backend, tmp_path, monkeypatch):
    refused("FRAMES_PILES_PROJECT_MISMATCH", generate, backend, project_id="leaf:project:other")
    refused("FRAMES_PILES_DRAWING_NOT_FOUND", fp.generate_frames, backend, TENANT, "nosuch", base=None,
            boundary=GEN["boundary"], preset=PRESET, drawing_units="m")
    (tmp_path / "graphless").mkdir()
    graphless, _ = seed_graphless(tmp_path / "graphless", monkeypatch)
    refused("FRAMES_PILES_GRAPH_REQUIRED", generate, graphless)
    assert not any("/physical/" in key for key in keys(graphless))


def test_frames_piles_drained(backend, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    before = keys(backend)
    refused("FRAMES_PILES_WRITES_DRAINED", generate, backend)
    refused("FRAMES_PILES_WRITES_DRAINED", fp.detect_collisions, backend, TENANT, DRAWING, base=None)
    assert keys(backend) == before


def test_frames_piles_concurrent_writer_conflicts(backend, monkeypatch):
    real_publish = ph.publish_physical_state

    def racing(backend_, tenant, drawing, document, **kw):
        rival = ps.physical_document(TINY, drawing_units="m", source_sha256=TERRAIN_SHA,
                                     capability="frame-generate")
        real_publish(backend_, tenant, drawing, rival)
        return real_publish(backend_, tenant, drawing, document, **kw)

    monkeypatch.setattr(fp.ph, "publish_physical_state", racing)
    refused("PHYSICAL_HEAD_CONFLICT", generate, backend)
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert head["index"] == 0 and fp.load_frames_piles(backend, TENANT, DRAWING, project_id=PROJECT)[2][
        "frames"] == 1


def test_frames_piles_load_empty_and_project(backend):
    assert fp.load_frames_piles(backend, TENANT, DRAWING, project_id=PROJECT) == (None, None, None)
    refused("FRAMES_PILES_PROJECT_ID_INVALID", fp.load_frames_piles, backend, TENANT, DRAWING, project_id="")
    generate(backend)
    refused("PHYSICAL_HEAD_PROJECT_MISMATCH", fp.load_frames_piles, backend, TENANT, DRAWING,
            project_id="leaf:project:other")


def test_frames_piles_non_string_marker_role_reopens_after_generate(backend):
    state = copy.deepcopy(TINY)
    state["collision_layer"] = [{"role": []}]
    base = seed_head(backend, state)
    result = generate(backend, base=base)
    assert result["outcome"] == "published"
    head, document, summary = fp.load_frames_piles(backend, TENANT, DRAWING, project_id=PROJECT)
    assert head == result["head"] and summary == result["preview"]
    assert summary["frames"] == len(state["frames"]) + 144
    assert summary["collision_markers"] == 0 and summary["range_markers"] == 0
    assert document["state"]["collision_layer"] == [{"role": []}]


def test_frames_piles_preset_null_pile_template_name_refused(backend):
    before = keys(backend)
    refused("FRAMES_PILES_PRESET_INVALID", generate, backend, preset=_preset(PileTemplateName=None))
    assert keys(backend) == before


def test_frames_piles_template_null_name_refused(backend):
    before = keys(backend)
    refused("FRAMES_PILES_PILE_TEMPLATE_INVALID", piles, backend, None, template={"Name": None})
    assert keys(backend) == before


def test_frames_piles_lowercase_preset_name_member_bounded(backend):
    preset = copy.deepcopy(PRESET)
    preset.pop("PileTemplateName", None)
    preset["piletemplatename"] = "x" * 257
    before = keys(backend)
    refused("FRAMES_PILES_PRESET_INVALID", generate, backend, preset=preset)
    assert keys(backend) == before


def test_frames_piles_lowercase_template_name_bounded(backend):
    before = keys(backend)
    refused("FRAMES_PILES_PILE_TEMPLATE_INVALID", piles, backend, None, template={"name": "x" * 257})
    assert keys(backend) == before


def test_frames_piles_duplicate_case_insensitive_preset_name_refused(backend):
    before = keys(backend)
    refused("FRAMES_PILES_PRESET_INVALID", generate, backend, preset=_preset(name=PRESET["Name"]))
    assert keys(backend) == before


def test_frames_piles_preset_mixed_key_types_refused(backend):
    before = keys(backend)
    refused("FRAMES_PILES_PRESET_INVALID", generate, backend, preset=_preset(extra={1: "a", "b": "c"}))
    assert keys(backend) == before


def test_frames_piles_boundary_overflow_refused(backend):
    before = keys(backend)
    refused("FRAMES_PILES_BOUNDARY_INVALID", generate, backend,
            boundary=[[0.0, 0.0], [10**400, 0.0], [1.0, 1.0]])
    assert keys(backend) == before


def test_frames_piles_collision_invalid_stored_terrain_writes_nothing(backend):
    state = copy.deepcopy(evidence_states("generate", "g1")["g1"])
    state["frames"] = state["frames"] + copy.deepcopy(state["frames"])
    state["grid"] = {"elevations": [1.0], "rows": 1, "cols": 1,
                     "x_min": 0.0, "x_max": 1.0, "y_min": 0.0, "y_max": 1.0}
    base = seed_head(backend, state)
    before = keys(backend)
    refused("FRAMES_PILES_TERRAIN_INVALID", fp.detect_collisions, backend, TENANT, DRAWING, base=base)
    assert keys(backend) == before and head_id(backend) == base


def test_frames_piles_collision_and_range_report_generate_terrain_digest(backend, terrain_chain):
    base = seed_head(backend, copy.deepcopy(terrain_chain["t2"]))
    made = fp.generate_frames(backend, TENANT, DRAWING, base=base, boundary=TER["boundary"],
                              preset=TER["active_preset"], drawing_units="m")
    digest = made["terrain"]["grid_sha256"]
    assert digest == canonical(terrain.neutral_grid(terrain_chain["t2"]["grid"]))
    collision = fp.detect_collisions(backend, TENANT, DRAWING, base=made["head"]["state"]["artifact_id"])
    assert collision["terrain"]["grid_sha256"] == digest and collision["terrain"]["sampled"] is False
    placed = piles(backend, collision["head"]["state"]["artifact_id"], TER["active_preset"], TER["pile_template"])
    checked = fp.check_pile_lengths(backend, TENANT, DRAWING, base=placed["head"]["state"]["artifact_id"],
                                    preset=TER["active_preset"])
    assert checked["terrain"]["grid_sha256"] == digest and checked["terrain"]["sampled"] is False


def test_frames_piles_preset_numeric_overflow_refused(backend):
    preset = copy.deepcopy(PRESET)
    preset["ModuleLengthM"] = 10**400
    before = keys(backend)
    refused("FRAMES_PILES_PRESET_INVALID", generate, backend, preset=preset)
    assert keys(backend) == before


def test_frames_piles_template_numeric_overflow_refused(backend):
    base = landxml_head(backend)
    made = generate(backend, base=base, boundary=SQUARE_M)
    base = made["head"]["state"]["artifact_id"]
    template = copy.deepcopy(TEMPLATE)
    template["PileDiameterM"] = 10**400
    before = keys(backend)
    refused("FRAMES_PILES_PILE_TEMPLATE_INVALID", piles, backend, base, template=template)
    assert keys(backend) == before and head_id(backend) == base


def test_frames_piles_collision_malformed_stored_vertices_refused(backend):
    base = seed_head(backend, {"frames": [{"type": "LWPOLYLINE", "layer": "LEAF-TRACKERS",
                                         "closed": True, "vertices": 1, "row": 0, "col": 0}]})
    before = keys(backend)
    refused("FRAMES_PILES_STATE_INVALID", fp.detect_collisions, backend, TENANT, DRAWING, base=base)
    assert keys(backend) == before and head_id(backend) == base


def test_frames_piles_range_malformed_stored_extents_refused(backend):
    base = seed_head(backend, {"piles": [{"type": "CIRCLE", "layer": "LEAF-PILING", "extents": []}]})
    before = keys(backend)
    refused("FRAMES_PILES_STATE_INVALID", fp.check_pile_lengths, backend, TENANT, DRAWING,
            base=base, preset=PRESET)
    assert keys(backend) == before and head_id(backend) == base


@pytest.mark.parametrize("call", ["collision", "range"])
def test_frames_piles_unreadable_terrain_precedes_missing_entities(backend, call):
    base = seed_head(backend, {"grid": {"elevations": [1.0], "rows": 1, "cols": 1,
                                      "x_min": 0.0, "x_max": 1.0, "y_min": 0.0, "y_max": 1.0}})
    before = keys(backend)
    fn = {"collision": lambda: fp.detect_collisions(backend, TENANT, DRAWING, base=base),
          "range": lambda: fp.check_pile_lengths(backend, TENANT, DRAWING, base=base, preset=PRESET)}[call]
    refused("FRAMES_PILES_TERRAIN_INVALID", fn)
    assert keys(backend) == before and head_id(backend) == base


@pytest.mark.parametrize("error_type, code", [
    (fp.FramesPilesError, "FRAMES_PILES_NO_FRAMES"),
    (ps.PhysicalStateError, "PHYSICAL_STATE_INVALID"),
])
def test_frames_piles_typed_refusal_inside_a_run_keeps_its_code(backend, monkeypatch, error_type, code):
    base = seed_head(backend, copy.deepcopy(TINY))
    before = keys(backend)

    def typed_refusal(state, mpu, inputs):
        raise error_type(code)

    monkeypatch.setitem(fp._RUN, fp.COLLISION, typed_refusal)
    refused(code, fp.detect_collisions, backend, TENANT, DRAWING, base=base)
    assert keys(backend) == before and head_id(backend) == base


# ------------------------------- terrain standing (sf-w5-piles-terrain-staleness) --

def standing(frames_state, frames_checked, frames_stale, piles_state, piles_checked, piles_stale, grid_sha256):
    return {"schema": "leaf.solar-frames-piles-terrain-standing.v1", "maturity": "preview",
            "grid_sha256": grid_sha256,
            "frames": {"state": frames_state, "checked": frames_checked, "stale": frames_stale},
            "piles": {"state": piles_state, "checked": piles_checked, "stale": piles_stale}}


def read_standing(backend):
    return fp.read_terrain_standing(backend, TENANT, DRAWING, project_id=PROJECT)


LX_M_GRID = "1c78f602025556918336710801265c08a50a9e9098b48d2bda4a669bc9927452"
LX_M_GRID_10 = "256ae101729b837f56251969b9dcf252af45cdd1126b92e9bf5dc3caac7a9213"
LX_FT_GRID = "a67a1a71ae3f8404398909e2a9dc836b2a83b02ec421b41e6c2dae8332b04fed"
LX_FT_GRID_10 = "b647f3d440f6fc0d80087553aef55268abb0ba7a017b912aa2dd9bc666214e1c"
T4_GRID = "fccde59f5f7e53f6d92bf88a03ffb97c00323406765c7678bcb5bdb0e5393524"


def test_frames_piles_standing_constants():
    assert fp.STANDING_SCHEMA == "leaf.solar-frames-piles-terrain-standing.v1"
    assert fp.STANDINGS == ("absent", "current", "stale")
    assert fp.STANDING_TOLERANCE == 1e-6
    assert len(fp.CODES) == 25


def test_frames_piles_standing_follows_the_grid_in_metres(backend):
    base = landxml_head(backend)
    made = generate(backend, base=base, boundary=SQUARE_M)
    assert made["standing"] == standing("current", 242, 0, "absent", 0, 0, LX_M_GRID)
    placed = piles(backend, made["head"]["state"]["artifact_id"])
    assert placed["standing"] == standing("current", 242, 0, "current", 1936, 0, LX_M_GRID)
    before = keys(backend)
    assert read_standing(backend) == (placed["head"], placed["standing"])
    assert keys(backend) == before
    moved = lx.import_landxml_terrain(backend, TENANT, DRAWING, REAL, drawing_units="m", crs="none",
                                      target_cells=10)
    assert moved["created"] is True
    stale = standing("stale", 242, 242, "stale", 1936, 1936, LX_M_GRID_10)
    assert read_standing(backend) == (moved["head"], stale)
    collision = fp.detect_collisions(backend, TENANT, DRAWING, base=moved["head"]["state"]["artifact_id"])
    assert collision["outcome"] == "unchanged" and collision["standing"] == stale
    assert collision["terrain"]["grid_sha256"] == LX_M_GRID_10
    window = fp.check_pile_lengths(backend, TENANT, DRAWING, base=moved["head"]["state"]["artifact_id"],
                                   preset=PRESET)
    assert window["outcome"] == "unchanged" and window["standing"] == stale
    again = piles(backend, moved["head"]["state"]["artifact_id"])
    assert again["outcome"] == "published" and again["summary"]["piles_replaced"] == 1936
    assert again["standing"] == standing("stale", 242, 242, "current", 1936, 0, LX_M_GRID_10)
    back = lx.import_landxml_terrain(backend, TENANT, DRAWING, REAL, drawing_units="m", crs="none")
    assert read_standing(backend) == (back["head"], standing("current", 242, 0, "stale", 1936, 1936, LX_M_GRID))


def test_frames_piles_standing_follows_the_grid_in_feet(backend):
    base = landxml_head(backend, "ft")
    made = generate(backend, base=base, boundary=SQUARE_FT, drawing_units="ft")
    placed = piles(backend, made["head"]["state"]["artifact_id"])
    assert placed["standing"] == standing("current", 242, 0, "current", 1936, 0, LX_FT_GRID)
    lx.import_landxml_terrain(backend, TENANT, DRAWING, REAL, drawing_units="ft", crs="none", target_cells=10)
    assert read_standing(backend)[1] == standing("stale", 242, 242, "stale", 1936, 1936, LX_FT_GRID_10)


def test_frames_piles_standing_flat_frames_go_stale_under_a_new_grid(backend):
    made = generate(backend)
    assert made["standing"] == standing("current", 144, 0, "absent", 0, 0, None)
    lx.import_landxml_terrain(backend, TENANT, DRAWING, REAL, drawing_units="m", crs="none")
    assert read_standing(backend)[1] == standing("stale", 144, 72, "absent", 0, 0, LX_M_GRID)


def test_frames_piles_standing_of_the_evidence_t4(backend, terrain_chain):
    seed_head(backend, copy.deepcopy(terrain_chain["t4"]))
    assert read_standing(backend)[1] == standing("current", 1197, 0, "current", 9576, 0, T4_GRID)


@pytest.mark.parametrize("delta, stale", [(5e-7, 0), (2e-6, 1), (-2e-6, 1)])
def test_frames_piles_standing_tolerance(backend, delta, stale):
    base = landxml_head(backend)
    made = generate(backend, base=base, boundary=SQUARE_M)
    piles(backend, made["head"]["state"]["artifact_id"])
    state = copy.deepcopy(state_of(backend))
    state["frames"][0]["elevation"] += delta
    x, y, z = state["piles"][0]["center"]
    state["piles"][0]["center"] = (x, y, z + delta)
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    document = ps.physical_document(state, drawing_units="m", source_sha256=TERRAIN_SHA, capability="hand-edit",
                                    parent=head["state"]["artifact_id"])
    ph.publish_physical_state(backend, TENANT, DRAWING, document)
    word = "stale" if stale else "current"
    assert read_standing(backend)[1] == standing(word, 242, stale, word, 1936, stale, LX_M_GRID)


def test_frames_piles_standing_ignores_entities_it_did_not_draw(backend):
    state = copy.deepcopy(TINY)
    state["frames"].append({"type": "INSERT", "layer": "PVCASE-TRACKERS", "name": "TRK"})
    state["piles"].append({"type": "CIRCLE", "layer": "OTHER", "center": (0.0, 0.0, 9.0)})
    seed_head(backend, state, capability="frame-generate")
    assert read_standing(backend)[1] == standing("absent", 0, 0, "absent", 0, 0, None)


NATIVE_FRAME = {"type": "LWPOLYLINE", "layer": "LEAF-TRACKERS", "closed": True,
                "vertices": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)], "row": 0, "col": 0}
NATIVE_PILE = {"type": "CIRCLE", "layer": "LEAF-PILING", "center": (0.5, 0.5, -1.5), "pile": {"embedment_m": 1.5}}


@pytest.mark.parametrize("key, change", [
    ("frames", {"vertices": 1}),
    ("frames", {"vertices": []}),
    ("frames", {"vertices": [(0.0, 0.0, 0.0)]}),
    ("frames", {"vertices": [(0.0, "1")]}),
    ("frames", {"elevation": "0"}),
    ("frames", {"elevation": True}),
    ("piles", {"center": (0.5, 0.5)}),
    ("piles", {"center": None}),
    ("piles", {"pile": None}),
    ("piles", {"pile": {"embedment_m": "1.5"}}),
    ("piles", {"pile": {}}),
])
def test_frames_piles_standing_refuses_a_malformed_native_entity(backend, key, change):
    entity = dict(NATIVE_FRAME if key == "frames" else NATIVE_PILE, **change)
    state = {"grid": None, "frames": [], "piles": []}
    state[key] = [entity]
    base = seed_head(backend, state)
    refused("FRAMES_PILES_STATE_INVALID", fp.read_terrain_standing, backend, TENANT, DRAWING, project_id=PROJECT)
    before = keys(backend)
    refused("FRAMES_PILES_STATE_INVALID", generate, backend, base=base, boundary=SQUARE_M)
    assert keys(backend) == before and head_id(backend) == base


@pytest.mark.parametrize("change", [
    {"pile": {"embedment_m": "1.5"}},
    {"pile": None},
    {"center": None},
])
def test_frames_piles_standing_piling_refuses_a_malformed_existing_pile(backend, change):
    base = landxml_head(backend)
    made = generate(backend, base=base, boundary=SQUARE_M)
    placed = piles(backend, made["head"]["state"]["artifact_id"])
    again = piles(backend, placed["head"]["state"]["artifact_id"])
    assert again["outcome"] in ("published", "unchanged")
    assert again["standing"] == standing("current", 242, 0, "current", 1936, 0, LX_M_GRID)
    state = copy.deepcopy(state_of(backend))
    state["piles"][0].update(change)
    document = ps.physical_document(state, drawing_units="m", source_sha256=TERRAIN_SHA, capability="hand-edit",
                                    parent=head_id(backend))
    child = ph.publish_physical_state(backend, TENANT, DRAWING, document)
    base = child["head"]["state"]["artifact_id"]
    before = keys(backend)
    refused("FRAMES_PILES_STATE_INVALID", piles, backend, base)
    assert keys(backend) == before and head_id(backend) == base


def test_frames_piles_standing_native_entities_on_a_flat_drawing_are_current(backend):
    seed_head(backend, {"grid": None, "frames": [dict(NATIVE_FRAME)], "piles": [dict(NATIVE_PILE)]})
    assert read_standing(backend)[1] == standing("current", 1, 0, "current", 1, 0, None)


def test_frames_piles_standing_refuses_an_unreadable_grid(backend):
    seed_head(backend, {"grid": {"elevations": [1.0], "rows": 1, "cols": 1, "x_min": 0.0, "x_max": 1.0,
                                 "y_min": 0.0, "y_max": 1.0}})
    refused("FRAMES_PILES_TERRAIN_INVALID", fp.read_terrain_standing, backend, TENANT, DRAWING, project_id=PROJECT)


def test_frames_piles_standing_read_empty_and_project(backend):
    assert read_standing(backend) == (None, None)
    refused("FRAMES_PILES_PROJECT_ID_INVALID", fp.read_terrain_standing, backend, TENANT, DRAWING, project_id="")
    refused("FRAMES_PILES_PROJECT_ID_INVALID", fp.read_terrain_standing, backend, TENANT, DRAWING,
            project_id="p" * 101)
    generate(backend)
    refused("PHYSICAL_HEAD_PROJECT_MISMATCH", fp.read_terrain_standing, backend, TENANT, DRAWING,
            project_id="leaf:project:other")
