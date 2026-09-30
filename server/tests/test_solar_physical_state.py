"""sf-w5-physical-state: the Ground physical design state serialized and reopened unchanged
through the revision-bound artifact store (cases C17 to C23, measured on the committed
generate and terrain intakes and the committed parity receipts)."""
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_artifacts as artifacts  # noqa: E402
import solar_import_sources as sources  # noqa: E402
import solar_physical_state as ps  # noqa: E402
import write_loop  # noqa: E402
from solar_graph_context import resolve_graph_context  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_solve_commit import seed, seed_graphless  # noqa: E402
from test_w1_local_graph_adapter import held, run as commit  # noqa: E402

TENANT = "fixture-tenant"
DRAWING = "solar"
PROJECT = "leaf:project:00000000-0000-4000-8000-000000000001"
GRAPH_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
PREFIX = "tenants/fixture-tenant/drawings/solar/"
REVISION = "0" * 40
EVIDENCE = ROOT / "docs" / "parity" / "evidence" / "ground"
RECEIPTS = ROOT / "docs" / "parity" / "receipts"
GENERATE_SHA = "0bea92f131cc13d054e3931f33f395f974795a6ec5661895e337cbe3f420c0b6"
TERRAIN_SHA = "28ff7d712df4c92ce9e832694064dd4601fce217d24c98300d1e7922399193ae"


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def producer(name):
    return _load(name, ROOT / "scripts" / f"{name}.py")


def intake(kind):
    return json.loads((EVIDENCE / kind / "intake.json").read_text(encoding="utf-8"))


def receipt(capability, name):
    data = json.loads((RECEIPTS / capability / name).read_text(encoding="utf-8"))
    return data["comparison"]["studio"]["output_sha256"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def document(state, source, capability, **kw):
    return ps.physical_document(state, drawing_units="m", source_sha256=source, capability=capability, **kw)


def reopen(state, source, capability):
    """encode, decode; returns (bytes, reopened state) and checks exact equality, order included."""
    doc = document(state, source, capability)
    data = ps.encode_document(doc)
    back = ps.decode_document(data)
    assert back == doc and repr(back) == repr(doc)
    assert back["state"] == state and repr(back["state"]) == repr(state)
    return data, back["state"]


# ------------------------------------------------------------------ chains --

@pytest.fixture(scope="module")
def generate_g1():
    ev = producer("solar_ground_studio_evidence")
    source = intake("generate")
    preset, pile_store = ev.load_stores(source)
    ctx = {"intake": source, "preset": preset, "pile_store": pile_store, "mpu": 1.0}
    state = ev.new_state()
    rows = ev.STEPS["generate"](state, ctx)
    return state, rows, ctx


@pytest.fixture(scope="module")
def terrain_t7():
    return producer("solar_ground_layout_evidence").terrain_state(intake("terrain"))


@pytest.fixture(scope="module")
def analysis_t7():
    return producer("solar_ground_analysis_evidence").terrain_state(intake("terrain"))


@pytest.fixture(scope="module")
def buildout_a13():
    return producer("solar_ground_buildout_evidence").a13_state(intake("terrain"))


STAMP = "2026-09-30T00:00:00.0000000Z"


@pytest.fixture(scope="module")
def shade_a13():
    """The shade chain runs a7 (LEAFEXPORTSCENE), whose DAE text carries DateTime.UtcNow
    (solar_ground_scene.utc_stamp) into `last_scene`; the chain runs under one fixed stamp so
    the state bytes are reproducible."""
    sh = producer("solar_ground_shade_evidence")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sh.scene_ev.scene, "utc_stamp", lambda moment=None: STAMP)
        return sh.studio_state(intake("terrain"))


@pytest.fixture(scope="module")
def scene_t7():
    return producer("solar_ground_scene_evidence").terrain_state(intake("terrain"))


# ------------------------------------------------------------------ backend --

@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def written(backend):
    return {key for key in backend.drawing_object_keys(TENANT, DRAWING) if "/artifacts/" in key}


TINY = {
    "frames": [{"layer": "LEAF-TRACKERS", "vertices": [(0.0, 0.0), (1.5, 0.0), (1.5, 2.0), (0.0, 2.0)],
                "row": 1, "col": 2, "color": {"index": 256}, "handle": "1"}],
    "piles": [{"center": (0.5, 1.0, -0.0), "normal": (0, 0, 1), "pile": {"diameter_m": 0.15}}],
    "settings": {"TrackerModuleCrossAxisM": 2.1, "DrawingUnitIsFeet": False, "Zeta": None, "Alpha": "\u00e9"},
    "grid": None,
    "mesh_faces": 0,
    "next_handle": 2,
    "last_bom": b"Item,Qty\r\nPile,12\r\n",
    "shading_last": {"kind": "Tree", "nested": [[(1, 2)], []]},
    "area_record": None,
    "shade_files": {},
}


# --------------------------------------------------------------- contract --

def test_physical_state_constants():
    assert ps.DOCUMENT_SCHEMA == "leaf.solar-physical-state.v1"
    assert ps.RESULT_SCHEMA == "leaf.solar-physical-state-result.v1"
    assert (ps.TOOL, ps.MEDIA_TYPE, ps.FILENAME, ps.SOURCE_KIND) == (
        "solar-physical-state", "application/json", "physical-state.json", "ground-intake")
    assert ps.MAX_DOCUMENT_BYTES == artifacts.MAX_ARTIFACT_BYTES == 16_777_216
    assert (ps.MAX_NODES, ps.MAX_DEPTH, ps.MAX_STRING_CHARS, ps.MAX_BYTES_VALUE) == (
        1_000_000, 32, 8_388_608, 4_194_304)
    assert (ps.MAX_ABS_FLOAT, ps.MAX_ABS_INT, ps.MAX_COUNTER) == (1e15, 2 ** 53 - 1, 1_000_000_000)
    assert ps.UNITS == {"m": 1.0, "ft": 0.3048}
    assert ps.DOCUMENT_ORDER == ("schema", "units", "frame", "source", "capability", "parent", "state")
    assert len(ps.STATE_KEYS) == 34 and ps.COUNTER_KEYS == {"mesh_faces", "next_handle"}
    assert len(ps.CODES) == 26
    error = ps.PhysicalStateError("PHYSICAL_STATE_INVALID")
    assert isinstance(error, ValueError) and str(error) == "PHYSICAL_STATE_INVALID"


def test_physical_state_tiny_round_trip():
    before = copy.deepcopy(TINY)
    data, back = reopen(TINY, GENERATE_SHA, "frame-generate")
    assert TINY == before and repr(TINY) == repr(before)
    assert type(back["frames"][0]["vertices"][0]) is tuple and type(back["last_bom"]) is bytes
    assert str(back["piles"][0]["center"][2]) == "-0.0" and back["settings"]["DrawingUnitIsFeet"] is False
    assert list(back["settings"]) == ["TrackerModuleCrossAxisM", "DrawingUnitIsFeet", "Zeta", "Alpha"]
    assert (len(data), sha(data)) == MEASURED_TINY


MEASURED_TINY = (966, "fd565c6abb1421d7261d9269d42daa568d7e45e677069b69d4bbfad3875a31de")


def test_physical_state_encoded_form():
    encoded = ps.encode_state({"frames": [{"vertices": [(1.0, 2)]}], "last_bom": b"a,b\r\n"})
    assert encoded == {"frames": [{"vertices": [{"$tuple": [1.0, 2]}]}], "last_bom": {"$bytes": "YSxiDQo="}}
    assert ps.decode_state(encoded) == {"frames": [{"vertices": [(1.0, 2)]}], "last_bom": b"a,b\r\n"}
    assert ps.encode_state({}) == {} and ps.decode_state({}) == {}


def test_physical_state_key_order_is_kept():
    first = ps.encode_document(document({"settings": {"b": 1, "a": 2}}, GENERATE_SHA, "frame-generate"))
    second = ps.encode_document(document({"settings": {"a": 2, "b": 1}}, GENERATE_SHA, "frame-generate"))
    assert first != second
    assert list(ps.decode_document(first)["state"]["settings"]) == ["b", "a"]


def test_physical_state_envelope_bytes():
    doc = document({}, GENERATE_SHA, "frame-generate")
    assert ps.encode_document(doc) == (
        b'{"schema":"leaf.solar-physical-state.v1","units":{"drawing_units":"m","meters_per_unit":1.0},'
        b'"frame":{"coordinate_system":"world","transform":[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],'
        b'"elevation_datum":"unrecorded","crs":"none"},"source":{"kind":"ground-intake","sha256":"'
        + GENERATE_SHA.encode() + b'"},"capability":"frame-generate","parent":null,"state":{}}')
    shuffled = {key: doc[key] for key in reversed(ps.DOCUMENT_ORDER)}
    shuffled["frame"] = dict(reversed(list(doc["frame"].items())))
    assert ps.encode_document(shuffled) == ps.encode_document(doc)


def test_physical_state_feet_and_epsg():
    frame = dict(ps.DEFAULT_FRAME, elevation_datum="EPSG:5703", crs="EPSG:32615",
                 transform=[1.0, 0, 0, 10.5, 0, 1, 0, -2, 0, 0, 1, 0, 0, 0, 0, 1])
    doc = ps.physical_document({}, drawing_units="ft", source_sha256=TERRAIN_SHA, capability="terrain-import",
                               parent="a" * 64, frame=frame)
    assert doc["units"] == {"drawing_units": "ft", "meters_per_unit": 0.3048}
    back = ps.decode_document(ps.encode_document(doc))
    assert back == doc and back["frame"]["crs"] == "EPSG:32615" and back["parent"] == "a" * 64


def test_physical_state_source_identity():
    for kind, expected in (("generate", GENERATE_SHA), ("terrain", TERRAIN_SHA)):
        assert ps.canonical_sha256(intake(kind)) == expected
    data = json.loads((RECEIPTS / "frame-generate" / "ground-generate-g1.json").read_text(encoding="utf-8"))
    assert data["comparison"]["studio"]["fixture_sha256"] == GENERATE_SHA
    data = json.loads((RECEIPTS / "shade-sim" / "ground-terrain-b8.json").read_text(encoding="utf-8"))
    assert data["comparison"]["studio"]["fixture_sha256"] == TERRAIN_SHA
    assert data["comparison"]["studio"]["units"] == "m"
    assert data["comparison"]["studio"]["frame"] == ps.DEFAULT_FRAME


# --------------------------------------------------------- envelope refusals --

def _envelope(**change):
    doc = document({}, GENERATE_SHA, "frame-generate")
    for key, value in change.items():
        if "." in key:
            outer, inner = key.split(".")
            doc[outer] = dict(doc[outer], **{inner: value})
        else:
            doc[key] = value
    return doc


@pytest.mark.parametrize("change,code", [
    ({"schema": "leaf.solar-physical-state.v2"}, "PHYSICAL_STATE_INVALID"),
    ({"units.drawing_units": "in"}, "PHYSICAL_STATE_UNITS_UNSUPPORTED"),
    ({"units.meters_per_unit": 0.3048}, "PHYSICAL_STATE_UNITS_UNSUPPORTED"),
    ({"units.meters_per_unit": 1}, "PHYSICAL_STATE_UNITS_UNSUPPORTED"),
    ({"frame.coordinate_system": "ucs"}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.transform": [1] * 15}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.transform": [True] + [0] * 15}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.transform": [float("nan")] + [0] * 15}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.transform": [1e16] + [0] * 15}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.crs": "EPSG:0"}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.crs": "WGS84"}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.crs": None}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"frame.elevation_datum": "EPSG:1234567"}, "PHYSICAL_STATE_FRAME_INVALID"),
    ({"source.kind": "terrain-import"}, "PHYSICAL_STATE_SOURCE_INVALID"),
    ({"source.sha256": GENERATE_SHA.upper()}, "PHYSICAL_STATE_SOURCE_INVALID"),
    ({"capability": "Frame_Generate"}, "PHYSICAL_STATE_CAPABILITY_INVALID"),
    ({"capability": "a" * 65}, "PHYSICAL_STATE_CAPABILITY_INVALID"),
    ({"capability": ""}, "PHYSICAL_STATE_CAPABILITY_INVALID"),
    ({"parent": "abc"}, "PHYSICAL_STATE_PARENT_INVALID"),
    ({"parent": "A" * 64}, "PHYSICAL_STATE_PARENT_INVALID"),
    ({"extra": {}}, "PHYSICAL_STATE_INVALID"),
])
def test_physical_state_envelope_refused(change, code):
    doc = _envelope(**change)
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.encode_document(doc)
    assert exc.value.code == code and str(exc.value) == code


# ------------------------------------------------------------ state refusals --

class _Float(float):
    pass


@pytest.mark.parametrize("state,code", [
    ("not a dict", "PHYSICAL_STATE_INVALID"),
    ({"layers": []}, "PHYSICAL_STATE_KEY_UNKNOWN"),
    ({"frames": {}}, "PHYSICAL_STATE_TYPE_MISMATCH"),
    ({"frames": ()}, "PHYSICAL_STATE_TYPE_MISMATCH"),
    ({"grid": []}, "PHYSICAL_STATE_TYPE_MISMATCH"),
    ({"last_bom": "text"}, "PHYSICAL_STATE_TYPE_MISMATCH"),
    ({"mesh_faces": True}, "PHYSICAL_STATE_TYPE_MISMATCH"),
    ({"mesh_faces": 1.0}, "PHYSICAL_STATE_TYPE_MISMATCH"),
    ({"mesh_faces": -1}, "PHYSICAL_STATE_LIMIT_EXCEEDED"),
    ({"next_handle": 1_000_000_001}, "PHYSICAL_STATE_LIMIT_EXCEEDED"),
    ({"frames": [{1, 2}]}, "PHYSICAL_STATE_UNSUPPORTED_VALUE"),
    ({"frames": [{1: "a"}]}, "PHYSICAL_STATE_UNSUPPORTED_VALUE"),
    ({"frames": [_Float(1.0)]}, "PHYSICAL_STATE_UNSUPPORTED_VALUE"),
    ({"frames": [{"$tuple": [1]}]}, "PHYSICAL_STATE_RESERVED_KEY"),
    ({"settings": {"$ref": 1}}, "PHYSICAL_STATE_RESERVED_KEY"),
    ({"frames": [float("nan")]}, "PHYSICAL_STATE_NONFINITE"),
    ({"frames": [float("-inf")]}, "PHYSICAL_STATE_NONFINITE"),
    ({"frames": [1e16]}, "PHYSICAL_STATE_LIMIT_EXCEEDED"),
    ({"frames": [2 ** 53]}, "PHYSICAL_STATE_LIMIT_EXCEEDED"),
])
def test_physical_state_state_refused(state, code):
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.encode_document(dict(_envelope(), state=state))
    assert exc.value.code == code


@pytest.mark.parametrize("bound,value,state", [
    ("MAX_STRING_CHARS", 11, {"area_record": "x" * 12}),
    ("MAX_STRING_CHARS", 4, {"settings": {"abcde": 1}}),
    ("MAX_BYTES_VALUE", 4, {"last_bom": b"abcde"}),
    ("MAX_NODES", 3, {"frames": [1, 2, 3]}),
    ("MAX_DEPTH", 2, {"frames": [[[1]]]}),
    ("MAX_DEPTH", 2, {"frames": [(1,)]}),
    ("MAX_DOCUMENT_BYTES", 300, {"area_record": "x" * 200}),
])
def test_physical_state_bounds(monkeypatch, bound, value, state):
    ok = ps.encode_document(dict(_envelope(), state=state))
    monkeypatch.setattr(ps, bound, value)
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.encode_document(dict(_envelope(), state=state))
    assert exc.value.code == "PHYSICAL_STATE_LIMIT_EXCEEDED"
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.decode_document(ok)
    assert exc.value.code == "PHYSICAL_STATE_LIMIT_EXCEEDED"


# ----------------------------------------------------------- decode refusals --

def _good():
    return ps.encode_document(document({"frames": [{"vertices": [(1.0, 2)]}], "last_bom": b"ab"},
                                       GENERATE_SHA, "frame-generate"))


def _swap(old, new):
    data = _good()
    assert old in data
    return data.replace(old, new, 1)


@pytest.mark.parametrize("data,code", [
    ("text", "PHYSICAL_STATE_INVALID"),
    (b"", "PHYSICAL_STATE_INVALID"),
    (b"\xff\xfe", "PHYSICAL_STATE_INVALID"),
    (b"[1]", "PHYSICAL_STATE_INVALID"),
    (b'{"schema":NaN}', "PHYSICAL_STATE_INVALID"),
    ("space", "PHYSICAL_STATE_NOT_CANONICAL"),
    ("float", "PHYSICAL_STATE_NOT_CANONICAL"),
    ("escape", "PHYSICAL_STATE_NOT_CANONICAL"),
    ("order", "PHYSICAL_STATE_NOT_CANONICAL"),
    ("duplicate", "PHYSICAL_STATE_NOT_CANONICAL"),
    ("tag-two-keys", "PHYSICAL_STATE_TAG_INVALID"),
    ("tag-not-list", "PHYSICAL_STATE_TAG_INVALID"),
    ("tag-unknown", "PHYSICAL_STATE_TAG_INVALID"),
    ("bytes-bad", "PHYSICAL_STATE_TAG_INVALID"),
    ("bytes-unpadded", "PHYSICAL_STATE_TAG_INVALID"),
    ("unknown-key", "PHYSICAL_STATE_KEY_UNKNOWN"),
    ("missing-key", "PHYSICAL_STATE_INVALID"),
    ("schema", "PHYSICAL_STATE_INVALID"),
])
def test_physical_state_decode_refused(data, code):
    if isinstance(data, str) and data != "text":
        data = {
            "space": _swap(b'"units":{', b'"units": {'),
            "float": _swap(b'"meters_per_unit":1.0', b'"meters_per_unit":1e0'),
            "escape": _swap(b'"frame-generate"', b'"frame\\u002dgenerate"'),
            "order": _swap(b'"elevation_datum":"unrecorded","crs":"none"',
                           b'"crs":"none","elevation_datum":"unrecorded"'),
            "duplicate": _swap(b'"parent":null', b'"parent":null,"parent":null'),
            "tag-two-keys": _swap(b'{"$tuple":[1.0,2]}', b'{"$tuple":[1.0,2],"x":1}'),
            "tag-not-list": _swap(b'{"$tuple":[1.0,2]}', b'{"$tuple":1}'),
            "tag-unknown": _swap(b'{"$tuple":[1.0,2]}', b'{"$set":[1.0,2]}'),
            "bytes-bad": _swap(b'{"$bytes":"YWI="}', b'{"$bytes":"Y*I="}'),
            "bytes-unpadded": _swap(b'{"$bytes":"YWI="}', b'{"$bytes":"YWI"}'),
            "unknown-key": _swap(b'"frames":', b'"layers":'),
            "missing-key": _swap(b'"parent":null,', b''),
            "schema": _swap(b'physical-state.v1', b'physical-state.v2'),
        }[data]
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.decode_document(data)
    assert exc.value.code == code


# ------------------------------------------------------------ C17 frames/piles --

def test_physical_state_c17_frames_reopen(generate_g1):
    state, rows, ctx = generate_g1
    ev = producer("solar_ground_studio_evidence")
    data, back = reopen(state, GENERATE_SHA, "frame-generate")
    assert (len(data), sha(data)) == MEASURED_C17_G1
    assert len(back["frames"]) == 144 and back["piles"] == [] and back["next_handle"] == 145
    assert back["frames"][0]["vertices"] == [(0.0, 0.0), (7.917999999999999, 0.0),
                                             (7.917999999999999, 15.636000000000003), (0.0, 15.636000000000003)]
    rows_again = ev.frame_rows(back["frames"])
    doc = ev.build_document(ctx["intake"], "generate", "g1", "frame-generate", "generate", rows_again, REVISION)
    assert doc["output_sha256"] == receipt("frame-generate", "ground-generate-g1.json")


def test_physical_state_c17_piling_from_reopened(generate_g1):
    state, rows, ctx = generate_g1
    ev = producer("solar_ground_studio_evidence")
    _, back = reopen(state, GENERATE_SHA, "frame-generate")
    ev.STEPS["collision"](back, ctx)
    rows3 = ev.STEPS["piling"](back, ctx)
    doc = ev.build_document(ctx["intake"], "generate", "g3", "piling-generate", "piling", rows3, REVISION)
    assert doc["output_sha256"] == receipt("piling-generate", "ground-generate-g3.json")
    assert len([row for row in rows3 if row["type"] == "pile-set"]) == 144 and len(back["piles"]) == 1152
    data, again = reopen(back, GENERATE_SHA, "piling-generate")
    assert (len(data), sha(data)) == MEASURED_C17_G3
    assert type(again["piles"][0]["center"]) is tuple


MEASURED_C17_G1 = (50469, "0b3ae83814014ef601f931b77f89e693aa927b05151ab45945efc22d19ad9fec")
MEASURED_C17_G3 = (631053, "c0f3a69d23f8e0efc2bc44136d5d9bf2d0fd25dcd95e8318ef6ccffec2bede18")


# ---------------------------------------------------------- C18 layout settings --

def test_physical_state_c18_layout_settings(terrain_t7):
    lay = producer("solar_ground_layout_evidence")
    tin = intake("terrain")
    data, t7 = reopen(terrain_t7, TERRAIN_SHA, "tracker-slope-violations")
    assert (len(data), sha(data)) == MEASURED_C18_T7
    for step, capability in (("a3", "tracker-module-spec"), ("a4", "row-spacing-calculator")):
        doc = lay.build_document(tin, step, lay.step_rows(step, t7, tin), REVISION)
        assert doc["output_sha256"] == receipt(capability, f"ground-terrain-{step}.json")
    data, a4 = reopen(t7, TERRAIN_SHA, "row-spacing-calculator")
    assert (len(data), sha(data)) == MEASURED_C18_A4
    settings = a4["settings"]
    assert (settings["TrackerModuleCrossAxisM"], settings["TrackerModuleAlongAxisM"], settings["TrackerModuleGapM"],
            settings["TrackerRailOverhangM"], settings["TrackerModulePmaxW"], settings["TorqueTubeHeightM"],
            settings["TrackerTorqueTubeRadiusM"], settings["LeafSpacingMinPitchM"]) == (
        2.1, 1.0, 0.02, 0.05, 400.0, 1.5, 0.08, 4.2279994425599625)
    doc = lay.build_document(tin, "a8", lay.step_rows("a8", a4, tin), REVISION)
    assert doc["output_sha256"] == receipt("tracker-layout", "ground-terrain-a8.json")


MEASURED_C18_T7 = (6082331, "0612c06882008b827423c65436cd82849a6db70f5d30530d1eaa65509b8bd684")
MEASURED_C18_A4 = (6083417, "3b873b5378c14ee1d6ef2230d2cdff92c7e15403fabea2422ae7febb5cab9d3a")


# ---------------------------------------------------------------- C19 grading --

def test_physical_state_c19_grading(analysis_t7):
    ana = producer("solar_ground_analysis_evidence")
    tin = intake("terrain")
    _, t7 = reopen(analysis_t7, TERRAIN_SHA, "tracker-slope-violations")
    rows = ana.step_rows("a10", t7, tin)
    doc = ana.build_document(tin, "a10", rows, REVISION)
    assert doc["output_sha256"] == receipt("pad-grading", "ground-terrain-a10.json")
    pad = next(row for row in rows if row["type"] == "grade-pad")
    assert pad["elevation"]["value"] == 0.2606420068563595 and pad["label"] == "GRADE PAD\\P0.26 m"
    assert pad["label_at"]["value"] == [250.0, 150.0]
    data, a10 = reopen(t7, TERRAIN_SHA, "pad-grading")
    assert (len(data), sha(data)) == MEASURED_C19_A10
    assert a10["settings"] == {"GradingElevationM": 0.2606420068563595, "GradingMode": 0}


MEASURED_C19_A10 = (6082413, "9f1ee8ab73e819b6f6accd1d57f765219490928dca1c26fb3cbc636c05ca29fe")


# ----------------------------------------------------------- C20 physical BOM --

def test_physical_state_c20_bom(buildout_a13):
    bo = producer("solar_ground_buildout_evidence")
    tin = intake("terrain")
    _, a13 = reopen(buildout_a13, TERRAIN_SHA, "setback-boundary")
    doc = bo.build_document(tin, "b1", bo.step_rows("b1", a13, tin), REVISION)
    assert doc["output_sha256"] == receipt("bill-of-materials", "ground-terrain-b1.json")
    data, b1 = reopen(a13, TERRAIN_SHA, "bill-of-materials")
    assert (len(data), sha(data)) == MEASURED_C20_B1
    bom = b1["last_bom"]
    assert type(bom) is bytes and len(bom) == 445 and b"89811.55" in bom
    assert sha(bom) == "18c553bee66bb219aaabe3772a9e79c0bc524aba8ee2f5730f1e6df8e333b6cd"


MEASURED_C20_B1 = (6221378, "0bf9c501193619ce18813f1bdf18f72ec0e563881b13de77f3ef95beb720718a")


# ------------------------------------------------------------------ C21 shade --

def test_physical_state_c21_shade(shade_a13):
    sh = producer("solar_ground_shade_evidence")
    tin = intake("terrain")
    _, a13 = reopen(shade_a13, TERRAIN_SHA, "delete-array")
    doc = sh.build_document(tin, "b8", "shade-sim", "shade-sim", sh.step_rows("b8", a13, tin), REVISION)
    assert doc["output_sha256"] == receipt("shade-sim", "ground-terrain-b8.json")
    data, b8 = reopen(a13, TERRAIN_SHA, "shade-sim")
    assert (len(data), sha(data)) == MEASURED_C21_B8
    assert len(b8["shade_heatmap"]) == 1197 and type(b8["shade_heatmap"][0]["rgb"]) is tuple
    assert STAMP in b8["last_scene"]["scene-dae"]
    assert {key: len(value) for key, value in b8["shade_files"].items()} == {
        "shade-azal-matrix": 1653, "shade-per-panel": 4816417, "shade-sam": 3211}


MEASURED_C21_B8 = (12005611, "a5c743cabdfce001395b3cc2e6246b07526fadfc365e2b6b2091505a7f5eae3d")


# ----------------------------------------------------------------- C22 arrays --

def test_physical_state_c22_arrays(scene_t7):
    scn = producer("solar_ground_scene_evidence")
    tin = intake("terrain")
    _, t7 = reopen(scene_t7, TERRAIN_SHA, "tracker-slope-violations")
    doc = scn.build_document(tin, "a5", "define-array", "array-define", scn.step_rows("a5", t7, tin), REVISION)
    assert doc["output_sha256"] == receipt("define-array", "ground-terrain-a5.json")
    data, a5 = reopen(t7, TERRAIN_SHA, "define-array")
    assert (len(data), sha(data)) == MEASURED_C22_A5
    array = a5["arrays"][0]
    assert (array["key"], array["centre_x"], array["centre_y"], array["modules_x"], array["modules_y"],
            array["orientation"], array["module_width_m"], array["module_height_m"]) == (
        "array_0", 250.0, 250.0, 100, 85, 0, 0.992, 1.64)


MEASURED_C22_A5 = (6082870, "15e2ccbc991f605a1784506a27216efce1c32a9e477a2da3d8c7791b61080ac9")


# -------------------------------------------------------- C23 terrain overlay --

def test_physical_state_c23_overlay(analysis_t7):
    ana = producer("solar_ground_analysis_evidence")
    tin = intake("terrain")
    _, t7 = reopen(analysis_t7, TERRAIN_SHA, "tracker-slope-violations")
    doc = ana.build_document(tin, "a1", ana.step_rows("a1", t7, tin), REVISION)
    assert doc["output_sha256"] == receipt("slope-heatmap", "ground-terrain-a1.json")


def test_physical_state_c23_flat_grid():
    analysis = _load("solar_ground_analysis", SERVER / "solar_ground_analysis.py")
    grid = {"rows": 3, "cols": 3, "x_min": 0.0, "y_min": 0.0, "x_max": 2.0, "y_max": 2.0,
            "elevations": [10.0] * 9}
    _, back = reopen({"grid": grid}, TERRAIN_SHA, "slope-heatmap")
    cells = analysis.compute_slope_cells(back["grid"])
    assert len(cells) == 4 and all(c["slope_percent"] == 0.0 and c["bucket"] == "Green" for c in cells)


# ------------------------------------------------------------- store and load --

def _store(backend, state=None, **kw):
    doc = document(TINY if state is None else state, GENERATE_SHA, kw.pop("capability", "frame-generate"),
                   parent=kw.pop("parent", None))
    return ps.store_physical_state(backend, TENANT, DRAWING, doc, **kw)


def test_physical_state_store_contract(backend):
    before = {key: backend.get(key) for key in backend.drawing_object_keys(TENANT, DRAWING)
              if "/artifacts/" not in key}
    result = _store(backend)
    after = {key: backend.get(key) for key in backend.drawing_object_keys(TENANT, DRAWING)
             if "/artifacts/" not in key}
    assert after == before
    data = ps.encode_document(document(TINY, GENERATE_SHA, "frame-generate"))
    ref = result["state"]
    assert result == {"schema": ps.RESULT_SCHEMA, "drawing_id": DRAWING, "project_id": PROJECT,
                      "source_version": 1, "graph_sha256": GRAPH_SHA, "capability": "frame-generate",
                      "parent": None, "state": ref}
    assert ref["content_sha256"] == sha(data) and ref["byte_length"] == len(data)
    assert (ref["media_type"], ref["filename"], ref["source_version"]) == (
        "application/json", "physical-state.json", 1)
    context = resolve_graph_context(backend, TENANT, DRAWING, 1)
    binding = artifacts.artifact_binding(TENANT, DRAWING, context, ps.TOOL, sha(data))
    assert ref["artifact_id"] == hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"),
                                                           allow_nan=False).encode()).hexdigest()
    assert ref == MEASURED_STORE_REF
    assert written(backend) == {PREFIX + "artifacts/" + ref["artifact_id"] + ".json",
                                PREFIX + "artifacts/blobs/" + sha(data) + ".bin"}
    meta, doc = ps.load_physical_state(backend, TENANT, DRAWING, ref["artifact_id"], project_id=PROJECT)
    assert meta["tool"] == ps.TOOL and meta["request_sha256"] == meta["content_sha256"] == sha(data)
    assert doc["state"] == TINY and repr(doc["state"]) == repr(TINY)


MEASURED_STORE_REF = {
    "schema": "leaf.solar-artifact-ref.v1",
    "artifact_id": "804b7d619d8cbacc36e38fff644611f40778569ffaa804e996b150e8f015c7c7",
    "media_type": "application/json", "filename": "physical-state.json", "byte_length": 966,
    "content_sha256": "fd565c6abb1421d7261d9269d42daa568d7e45e677069b69d4bbfad3875a31de", "source_version": 1,
    "download": "/api/drawings/solar/artifacts/804b7d619d8cbacc36e38fff644611f40778569ffaa804e996b150e8f015c7c7"}


def test_physical_state_store_duplicate(backend):
    first = _store(backend)
    before = written(backend)
    assert _store(backend, project_id=PROJECT) == first
    assert written(backend) == before


def test_physical_state_store_new_revision(backend):
    first = _store(backend)
    with held(backend) as fence:
        commit(backend, fence=fence)
    second = _store(backend)
    assert second["source_version"] == 2 and second["state"]["artifact_id"] != first["state"]["artifact_id"]
    assert second["state"]["content_sha256"] == first["state"]["content_sha256"]
    meta, doc = ps.load_physical_state(backend, TENANT, DRAWING, first["state"]["artifact_id"], project_id=PROJECT)
    assert meta["source_version"] == 1 and doc["state"] == TINY


def test_physical_state_store_parent(backend):
    first = _store(backend)
    child_state = dict(TINY, next_handle=3)
    child = _store(backend, child_state, capability="piling-generate", parent=first["state"]["artifact_id"])
    assert child["parent"] == first["state"]["artifact_id"]
    _, doc = ps.load_physical_state(backend, TENANT, DRAWING, child["state"]["artifact_id"], project_id=PROJECT)
    assert doc["parent"] == first["state"]["artifact_id"] and doc["state"]["next_handle"] == 3


def _foreign_artifact(backend, tool="solar-select-by-zone", media="text/csv", filename="strings.csv",
                      content=b"panel,string\r\nP1,S1\r\n", request=None):
    context = resolve_graph_context(backend, TENANT, DRAWING, 1)
    sink = artifacts.ArtifactSink(backend, TENANT, DRAWING, context, tool,
                                  request or hashlib.sha256(content).hexdigest(), False)
    return sink.finish(sink.prepare(artifacts.ArtifactOutput({}, media, filename, content)))["artifact_id"]


@pytest.mark.parametrize("case,code", [
    ("huge-transform", "PHYSICAL_STATE_FRAME_INVALID"),
    ("list-units", "PHYSICAL_STATE_UNITS_UNSUPPORTED"),
    ("dict-units", "PHYSICAL_STATE_UNITS_UNSUPPORTED")])
def test_physical_state_malformed_envelope_encode_decode_load(backend, case, code):
    doc = document({}, GENERATE_SHA, "frame-generate")
    if case == "huge-transform":
        doc["frame"]["transform"][0] = 10 ** 400
    else:
        doc["units"]["drawing_units"] = [] if case == "list-units" else {}
    data = json.dumps(doc, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.encode_document(doc)
    assert exc.value.code == code
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.decode_document(data)
    assert exc.value.code == code
    artifact_id = _foreign_artifact(backend, ps.TOOL, ps.MEDIA_TYPE, ps.FILENAME, data)
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.load_physical_state(backend, TENANT, DRAWING, artifact_id, project_id=PROJECT)
    assert exc.value.code == "PHYSICAL_STATE_CORRUPT"


def test_physical_state_decode_state_key_length_bound():
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.decode_state({"settings": {"x" * 8_388_609: 1}})
    assert exc.value.code == "PHYSICAL_STATE_LIMIT_EXCEEDED"


def test_physical_state_load_valid_envelope_noncanonical_bytes(backend):
    doc = document({}, GENERATE_SHA, "frame-generate")
    canonical = ps.encode_document(doc)
    data = b" " + canonical
    assert json.loads(data) == doc
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.decode_document(data)
    assert exc.value.code == "PHYSICAL_STATE_NOT_CANONICAL"
    artifact_id = _foreign_artifact(backend, ps.TOOL, ps.MEDIA_TYPE, ps.FILENAME, data)
    meta, content = artifacts.read_artifact(backend, TENANT, DRAWING, artifact_id)
    assert content == data and meta["request_sha256"] == meta["content_sha256"] == sha(data)
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.load_physical_state(backend, TENANT, DRAWING, artifact_id, project_id=PROJECT)
    assert exc.value.code == "PHYSICAL_STATE_CORRUPT"


@pytest.mark.parametrize("case,code", [("missing", "PHYSICAL_STATE_PARENT_NOT_FOUND"),
                                       ("foreign", "PHYSICAL_STATE_PARENT_INVALID")])
def test_physical_state_store_parent_refused(backend, case, code):
    parent = "0" * 64 if case == "missing" else _foreign_artifact(backend)
    before = written(backend)
    with pytest.raises(ps.PhysicalStateError) as exc:
        _store(backend, parent=parent)
    assert exc.value.code == code
    assert written(backend) == before


@pytest.mark.parametrize("case,code", [
    ("csv", "PHYSICAL_STATE_KIND_MISMATCH"), ("project", "PHYSICAL_STATE_PROJECT_MISMATCH"),
    ("missing", "PHYSICAL_STATE_NOT_FOUND"), ("id", "PHYSICAL_STATE_ID_INVALID"),
    ("blob", "PHYSICAL_STATE_CORRUPT"), ("request", "PHYSICAL_STATE_CORRUPT"),
    ("noncanonical", "PHYSICAL_STATE_CORRUPT"), ("project-id", "PHYSICAL_STATE_PROJECT_ID_INVALID"),
    ("tenant", "PHYSICAL_STATE_NOT_FOUND")])
def test_physical_state_load_refused(backend, case, code):
    ref = _store(backend)["state"]
    artifact_id, project, tenant = ref["artifact_id"], PROJECT, TENANT
    if case == "csv":
        artifact_id = _foreign_artifact(backend)
    elif case == "project":
        project = "leaf:project:other"
    elif case == "missing":
        artifact_id = "0" * 64
    elif case == "id":
        artifact_id = "E" * 64
    elif case == "blob":
        backend.put(PREFIX + "artifacts/blobs/" + ref["content_sha256"] + ".bin", b"{}")
    elif case == "request":
        artifact_id = _foreign_artifact(backend, ps.TOOL, ps.MEDIA_TYPE, ps.FILENAME,
                                        ps.encode_document(document({}, GENERATE_SHA, "frame-generate")),
                                        request="1" * 64)
    elif case == "noncanonical":
        data = json.dumps(document({}, GENERATE_SHA, "frame-generate")).encode("utf-8")
        artifact_id = _foreign_artifact(backend, ps.TOOL, ps.MEDIA_TYPE, ps.FILENAME, data)
    elif case == "project-id":
        project = "p" * 101
    elif case == "tenant":
        tenant = "other-tenant"
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.load_physical_state(backend, tenant, DRAWING, artifact_id, project_id=project)
    assert exc.value.code == code


@pytest.mark.parametrize("case,code", [("graphless", "PHYSICAL_STATE_GRAPH_REQUIRED"),
                                       ("missing", "PHYSICAL_STATE_DRAWING_NOT_FOUND"),
                                       ("project", "PHYSICAL_STATE_PROJECT_MISMATCH"),
                                       ("long-project", "PHYSICAL_STATE_PROJECT_ID_INVALID")])
def test_physical_state_store_context_refused(backend, tmp_path, monkeypatch, case, code):
    if case == "graphless":
        (tmp_path / "graphless").mkdir()
        backend, _ = seed_graphless(tmp_path / "graphless", monkeypatch)
    project = {"project": "leaf:project:other", "long-project": "p" * 101}.get(case)
    doc = document(TINY, GENERATE_SHA, "frame-generate")
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.store_physical_state(backend, TENANT, "nosuch" if case == "missing" else DRAWING, doc,
                                project_id=project)
    assert exc.value.code == code
    assert written(backend) == set()


def test_physical_state_store_drained(backend, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    with pytest.raises(ps.PhysicalStateError) as exc:
        _store(backend)
    assert exc.value.code == "PHYSICAL_STATE_WRITES_DRAINED"
    assert written(backend) == set()


def test_physical_state_store_unavailable(backend):
    class Unavailable:
        def __getattr__(self, name):
            return getattr(backend, name)

        def put_if_absent_or_verify(self, key, data):
            raise OSError("unavailable")

    with pytest.raises(ps.PhysicalStateError) as exc:
        _store(Unavailable())
    assert exc.value.code == "PHYSICAL_STATE_STORE_UNAVAILABLE"
    assert written(backend) == set()


def test_physical_state_store_invalid_document_writes_nothing(backend):
    doc = document(TINY, GENERATE_SHA, "frame-generate")
    doc["state"] = {"layers": []}
    with pytest.raises(ps.PhysicalStateError) as exc:
        ps.store_physical_state(backend, TENANT, DRAWING, doc)
    assert exc.value.code == "PHYSICAL_STATE_KEY_UNKNOWN"
    assert written(backend) == set()


def test_physical_state_store_c21_through_the_store(backend, shade_a13):
    sh = producer("solar_ground_shade_evidence")
    tin = intake("terrain")
    _, state = reopen(shade_a13, TERRAIN_SHA, "delete-array")
    sh.step_rows("b8", state, tin)
    doc = document(state, TERRAIN_SHA, "shade-sim")
    result = ps.store_physical_state(backend, TENANT, DRAWING, doc, project_id=PROJECT)
    assert result["state"]["byte_length"] == MEASURED_C21_B8[0]
    assert result["state"]["content_sha256"] == MEASURED_C21_B8[1]
    _, back = ps.load_physical_state(backend, TENANT, DRAWING, result["state"]["artifact_id"], project_id=PROJECT)
    assert back == doc and repr(back["state"]) == repr(state)


def test_physical_state_codes_closed():
    source = (SERVER / "solar_physical_state.py").read_text(encoding="utf-8")
    import re
    assert set(re.findall(r'"(PHYSICAL_STATE_[A-Z_]+)"', source)) == ps.CODES
