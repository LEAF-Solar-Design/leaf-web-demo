"""Compact Ground tracker slots: the codec, its bounds, and the exact expanded v1 interface."""
from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import struct
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solar_ground_buildout  # noqa: E402
import solar_design_graph as sdg  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
from solar_design_graph import GraphValidationError, load_schema, serialize_graph, validate_graph  # noqa: E402
from solar_solve_results import sync_assignments  # noqa: E402
from test_w1_design_graph import app_id, entity, graph  # noqa: E402,F401 (fixture)

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
COLLECTIONS = ("electrical_zones", "frames", "panels", "strings", "inverters", "routes", "schedules")
METRES = {
    "drawing_units": "m", "meters_per_unit": 1.0, "source": "explicit", "compute_units": "m",
    "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], "elevation_datum": "local ground",
    "crs": None, "drawing_unit_is_feet": False, "warnings": [],
}
VALID = {"state": "valid", "reasons": []}


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def canon_sha(value):
    return sha256(canon(value)).hexdigest()


def uid(kind, key):
    """A deterministic application id (test scaffolding for the future conversion writer)."""
    return f"leaf:{kind}:{uuid.UUID(bytes=sha256(key.encode('utf-8')).digest()[:16], version=4)}"


def refused(code, fn, *args):
    with pytest.raises(GraphValidationError) as error:
        fn(*args)
    assert error.value.code == code


@pytest.fixture(scope="module")
def b18():
    """The b18 Ground terrain site: 237 LEAFSAT trackers of 294 slots (conversion evidence)."""
    ds = _load("solar_ground_dsteps", SERVER / "solar_ground_dsteps.py")
    evidence = _load("solar_ground_dsteps_evidence", ROOT / "scripts" / "solar_ground_dsteps_evidence.py")
    intake = json.loads((ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json")
                        .read_text(encoding="utf-8"))
    state = evidence.b18_state(intake)
    ents = evidence.bev.tracker_entities(state)
    mpu = evidence.bev.MPU
    layout = ds.tracker_panel_layout(ents, mpu, state.get("settings"))
    rows = [solar_ground_buildout.read_tracker_rows([ents[t["entity_index"]]], mpu)[0]
            for t in layout["trackers"]]
    return {"ents": ents, "mpu": mpu, "layout": layout, "rows": rows}


def ground_base(w1):
    g = deepcopy(w1)
    g["project"]["installation_design"] = "Ground"
    g["project"]["units"] = deepcopy(METRES)
    for key in COLLECTIONS:
        g[key] = []
    return g


def slot_ids(g, k, slots):
    return [uid("panel", f"{g['source_hash']}:ground-panel:{k}:{i}") for i in range(slots)]


def ground_graph(w1, b18, n):
    """Compact frames for the first n b18 trackers, one frame per tracker (test scaffolding)."""
    g = ground_base(w1)
    for k, t in enumerate(b18["layout"]["trackers"][:n]):
        ent, row, slots = b18["ents"][t["entity_index"]], b18["rows"][k], t["module_slots"]
        tracker = {
            "tracker_model": "single_axis_tracker", "axis_start": list(t["axis_start"]),
            "axis_end": list(t["axis_end"]), "axis_units": "drawing", "module_slots": slots,
            "row_index": row["row_index"], "length_m": row["length_m"],
            "rail_overhang_m": row["rail_overhang_m"],
            "cross_axis_width_m": ent["cross_axis_width_du"] * b18["mpu"],
            "max_tilt_deg": ent["max_tilt_deg"],
            "source": {"entity_kind": t["entity_kind"], "handle": None, "layer": None,
                       "block_name": ent["block"], "source_command": ent["source_command"]},
        }
        frame = entity(
            "frame", 1, name=f"Group {t['group_number']}", insertion_point=list(t["center"]),
            installation_design="Ground", panel_refs=[], module_rows=1, module_columns=slots,
            module_slots=slots, module_power_watts=400, module_width_along_row=t["slot_length_du"],
            module_height_across_row=t["panel_height_du"], electrical_zone_ref=None, matrix=[],
            sequences=[], panel_assignments=[], tracker=tracker)
        frame["id"] = uid("frame", f"{g['source_hash']}:ground-frame:{k}")
        frame["ground_slots"] = codec.encode_slots(
            slot_ids(g, k, slots), t["panels"], t["row_angle_rad"],
            {"rev": 0, "provenance": deepcopy(frame["provenance"]), "validity": deepcopy(VALID),
             "extra": {}})
        g["frames"].append(frame)
    return g


def strung(w1, b18):
    """Two compact trackers carrying three strings: S1 on frame A slots 0-2 (inverter input 0),
    S2 on frame A slots 3-4 then frame B slots 0-1 (input 1), S3 on frame B slots 10-11 with no
    inverter."""
    g = ground_graph(w1, b18, 2)
    a, b = slot_ids(g, 0, 294), slot_ids(g, 1, 294)
    inverter = app_id("inverter", 1)
    members = {1: a[0:3], 2: a[3:5] + b[0:2], 3: b[10:12]}
    for n, refs in members.items():
        g["strings"].append(entity(
            "string", n, circuit_tag=f"S{n}", circuit_kind="String", ordered_panel_refs=list(refs),
            module_count=len(refs), from_ref=refs[0], to_ref=refs[-1], tag_text_ref=None,
            wire_gauge="10 AWG", length_ft=10, route=[[0, 0], [1, 0]],
            inverter_ref=inverter if n < 3 else None))
    g["inverters"].append(entity(
        "inverter", 1, number=1, type_key="A", is_l2=False, position=[5, 0], model="fixture-inverter",
        mppt_count=1, total_dc_inputs=2, max_dc_voltage=600, max_ac_power_kw=1, is_solaredge=False,
        input_assignments=[{"string_ref": app_id("string", n), "mppt_letter": "A", "input_number": n - 1}
                           for n in (1, 2)]))
    return g


# A three-slot block pinned by value: ids 00..01, 00..02, 00..03 of kind panel, centres include
# -0.0 and a large magnitude, which the float64 rows keep bit for bit.
SMALL_IDS = [f"leaf:panel:00000000-0000-4000-8000-{n:012d}" for n in (1, 2, 3)]
SMALL_CENTRES = [[0.5, -0.0], [1.5, 1e300], [-2.25, 3.0]]
SMALL_TEMPLATE = {"rev": 2, "provenance": {"created_by": "fixture", "created_at": "2026-09-17T00:00:00Z",
                                           "last_writer": "fixture", "source_rev": 1},
                  "validity": {"state": "valid", "reasons": []}, "extra": {"note": "kept"}}


def small_block():
    return codec.encode_slots(SMALL_IDS, SMALL_CENTRES, 1.5707963267948966, SMALL_TEMPLATE)


def test_ground_graph_codec_w1_fixture_is_byte_identical(graph):
    assert (sdg.MAX_NODES, sdg.MAX_BYTES) == (500000, 16 * 1024 * 1024)
    before = deepcopy(graph)
    assert validate_graph(graph) == graph
    assert canon_sha(graph) == "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
    payload = serialize_graph(graph).encode("utf-8")
    assert len(payload) == 11244
    assert sha256(payload).hexdigest() == "1fd29adc97543a32e739c14bcad1257284b6fcf15eb08d9d3a1407df0b9c84fc"
    assert codec.compact_frame_ids(graph) == []
    assert codec.decode_graph_slots(graph) == {}
    expanded = codec.expand_graph(graph)
    assert canon(expanded) == canon(graph) and expanded is not graph
    assert canon(codec.compact_graph(graph, [])) == canon(graph)
    assert graph == before


def test_ground_graph_codec_bounds_follow_the_schema():
    schema = load_schema()
    assert codec.CODEC == "leaf.solar-ground-slots.v1"
    assert codec.SLOTS_KEY == "ground_slots"
    assert codec.MAX_FRAME_SLOTS == schema["$defs"]["frame"]["properties"]["matrix"]["items"]["maxItems"] == 10000
    assert codec.MAX_GRAPH_SLOTS == schema["properties"]["panels"]["maxItems"] == 100000
    assert codec.SLOT_FLOOR_NODES == 59
    assert codec.row_string_length(1) == 24
    assert codec.row_string_length(294) == 6272
    assert codec.row_string_length(codec.MAX_FRAME_SLOTS) == 213336


def test_ground_graph_codec_json_cost_is_the_validator_accounting(graph, monkeypatch, b18):
    for value in (graph, ground_graph(graph, b18, 2)):
        nodes, size = codec.json_cost(value)
        monkeypatch.setattr(sdg, "MAX_NODES", nodes)
        sdg._bounded_json(value)
        monkeypatch.setattr(sdg, "MAX_NODES", nodes - 1)
        refused("GRAPH_LIMIT_EXCEEDED", sdg._bounded_json, value)
        monkeypatch.setattr(sdg, "MAX_NODES", 500000)
        monkeypatch.setattr(sdg, "MAX_BYTES", size)
        sdg._bounded_json(value)
        monkeypatch.setattr(sdg, "MAX_BYTES", size - 1)
        refused("GRAPH_LIMIT_EXCEEDED", sdg._bounded_json, value)
        monkeypatch.setattr(sdg, "MAX_BYTES", 16 * 1024 * 1024)
    assert codec.json_cost(graph) == (914, 12218)


def test_ground_graph_codec_small_block_is_pinned():
    block = small_block()
    assert block == {
        "codec": "leaf.solar-ground-slots.v1", "count": 3,
        "panel_ids": "AAAAAAAAQACAAAAAAAAAAQAAAAAAAEAAgAAAAAAAAAIAAAAAAABAAIAAAAAAAAAD",
        "centres": "AAAAAAAA4D8AAAAAAAAAgAAAAAAAAPg/nHUAiDzkN34AAAAAAAACwAAAAAAAAAhA",
        "angle": 1.5707963267948966, "panel": SMALL_TEMPLATE,
    }
    assert block["panel"] is not SMALL_TEMPLATE
    table = codec.decode_slots(block)
    assert table.ids == tuple(SMALL_IDS)
    assert table.centres == (0.5, -0.0, 1.5, 1e300, -2.25, 3.0)
    assert [struct.pack("<d", v) for v in table.centres[1:2]] == [struct.pack("<d", -0.0)]
    assert table.angle == 1.5707963267948966 and table.panel is block["panel"]
    assert codec.encode_slots(SMALL_IDS, SMALL_CENTRES, 1.5707963267948966, SMALL_TEMPLATE) == block


def _ids(n):
    return [f"leaf:panel:00000000-0000-4000-8000-{i:012d}" for i in range(n)]


# (id, change to the (ids, centres, angle, template) arguments, refusal code)
ENCODE_REFUSED = [
    ("no-slots", lambda a: a.update(ids=[], centres=[]), "INVALID_GROUND_SLOTS"),
    ("over-frame-bound", lambda a: a.update(ids=_ids(10001), centres=[[0.0, 0.0]] * 10001), "GRAPH_LIMIT_EXCEEDED"),
    ("ids-not-a-list", lambda a: a.update(ids="leaf:panel:x"), "INVALID_GROUND_SLOTS"),
    ("frame-kind-id", lambda a: a["ids"].__setitem__(0, "leaf:frame:00000000-0000-4000-8000-000000000001"), "INVALID_GROUND_SLOTS"),
    ("upper-case-id", lambda a: a["ids"].__setitem__(0, "leaf:panel:00000000-0000-4000-8000-00000000000A"), "INVALID_GROUND_SLOTS"),
    ("uuid-version-1", lambda a: a["ids"].__setitem__(0, "leaf:panel:00000000-0000-1000-8000-000000000001"), "INVALID_GROUND_SLOTS"),
    ("uuid-variant", lambda a: a["ids"].__setitem__(0, "leaf:panel:00000000-0000-4000-c000-000000000001"), "INVALID_GROUND_SLOTS"),
    ("id-not-a-string", lambda a: a["ids"].__setitem__(0, 7), "INVALID_GROUND_SLOTS"),
    ("duplicate-id", lambda a: a["ids"].__setitem__(2, a["ids"][0]), "DUPLICATE_APPLICATION_ID"),
    ("centre-count", lambda a: a["centres"].pop(), "INVALID_GROUND_SLOTS"),
    ("integer-centre", lambda a: a["centres"].__setitem__(0, [1, 0.0]), "INVALID_GROUND_SLOTS"),
    ("boolean-centre", lambda a: a["centres"].__setitem__(0, [True, 0.0]), "INVALID_GROUND_SLOTS"),
    ("three-d-centre", lambda a: a["centres"].__setitem__(0, [0.0, 0.0, 0.0]), "INVALID_GROUND_SLOTS"),
    ("nan-centre", lambda a: a["centres"].__setitem__(0, [float("nan"), 0.0]), "INVALID_GROUND_SLOTS"),
    ("infinite-centre", lambda a: a["centres"].__setitem__(0, [0.0, float("inf")]), "INVALID_GROUND_SLOTS"),
    ("boolean-angle", lambda a: a.update(angle=True), "INVALID_GROUND_SLOTS"),
    ("nan-angle", lambda a: a.update(angle=float("nan")), "INVALID_GROUND_SLOTS"),
    ("huge-int-angle", lambda a: a.update(angle=10**1000), "INVALID_GROUND_SLOTS"),
    ("huge-negative-int-angle", lambda a: a.update(angle=-(10**1000)), "INVALID_GROUND_SLOTS"),
    ("string-angle", lambda a: a.update(angle="0"), "INVALID_GROUND_SLOTS"),
    ("template-missing-key", lambda a: a["template"].pop("extra"), "INVALID_GROUND_SLOTS"),
    ("template-unknown-key", lambda a: a["template"].update(kind="panel"), "INVALID_GROUND_SLOTS"),
    ("template-boolean-rev", lambda a: a["template"].update(rev=True), "INVALID_GROUND_SLOTS"),
    ("template-negative-rev", lambda a: a["template"].update(rev=-1), "INVALID_GROUND_SLOTS"),
    ("template-rev-over-bound", lambda a: a["template"].update(rev=1000001), "INVALID_GROUND_SLOTS"),
    ("template-provenance-list", lambda a: a["template"].update(provenance=[]), "INVALID_GROUND_SLOTS"),
]


@pytest.mark.parametrize("name,change,code", ENCODE_REFUSED, ids=[row[0] for row in ENCODE_REFUSED])
def test_ground_graph_codec_encode_refused(name, change, code):
    args = {"ids": list(SMALL_IDS), "centres": deepcopy(SMALL_CENTRES), "angle": 0.25,
            "template": deepcopy(SMALL_TEMPLATE)}
    change(args)
    before = deepcopy(args) if name != "nan-centre" else None
    refused(code, codec.encode_slots, args["ids"], args["centres"], args["angle"], args["template"])
    if before is not None:
        assert args == before


def _flip_id_byte(block, index, value):
    raw = bytearray(base64.b64decode(block["panel_ids"]))
    raw[index] = value
    block["panel_ids"] = base64.b64encode(bytes(raw)).decode("ascii")


def _centre_bytes(block, data):
    raw = bytearray(base64.b64decode(block["centres"]))
    raw[0:8] = data
    block["centres"] = base64.b64encode(bytes(raw)).decode("ascii")


def _last_char(key, char):
    def change(block):
        block[key] = block[key][:-1] + char
    return change


# (id, change to a valid three-slot block, refusal code)
DECODE_REFUSED = [
    ("not-an-object-none", None, "INVALID_GROUND_SLOTS"),
    ("not-an-object-list", [], "INVALID_GROUND_SLOTS"),
    ("not-an-object-string", "block", "INVALID_GROUND_SLOTS"),
    ("not-an-object-int", 7, "INVALID_GROUND_SLOTS"),
    ("unknown-key", lambda b: b.update(future=1), "INVALID_GROUND_SLOTS"),
    ("missing-key", lambda b: b.pop("angle"), "INVALID_GROUND_SLOTS"),
    ("wrong-codec", lambda b: b.update(codec="leaf.solar-ground-slots.v2"), "INVALID_GROUND_SLOTS"),
    ("zero-count", lambda b: b.update(count=0), "INVALID_GROUND_SLOTS"),
    ("boolean-count", lambda b: b.update(count=True), "INVALID_GROUND_SLOTS"),
    ("float-count", lambda b: b.update(count=3.0), "INVALID_GROUND_SLOTS"),
    ("count-over-frame-bound", lambda b: b.update(count=10001), "GRAPH_LIMIT_EXCEEDED"),
    ("count-above-rows", lambda b: b.update(count=4), "INVALID_GROUND_SLOTS"),
    ("count-below-rows", lambda b: b.update(count=2), "INVALID_GROUND_SLOTS"),
    ("rows-not-a-string", lambda b: b.update(panel_ids=[]), "INVALID_GROUND_SLOTS"),
    ("non-alphabet-char", _last_char("panel_ids", "*"), "INVALID_GROUND_SLOTS"),
    ("non-ascii-char", _last_char("centres", "\u00e9"), "INVALID_GROUND_SLOTS"),
    ("url-safe-alphabet", lambda b: b.update(panel_ids=b["panel_ids"][:-2] + "-_"), "INVALID_GROUND_SLOTS"),
    ("uuid-version-nibble", lambda b: _flip_id_byte(b, 6, 0x10), "INVALID_GROUND_SLOTS"),
    ("uuid-variant-bits", lambda b: _flip_id_byte(b, 8, 0xC0), "INVALID_GROUND_SLOTS"),
    ("duplicate-row-id", lambda b: b.update(panel_ids=base64.b64encode(
        base64.b64decode(b["panel_ids"])[:16] * 3).decode("ascii")), "DUPLICATE_APPLICATION_ID"),
    ("nan-centre-row", lambda b: _centre_bytes(b, struct.pack("<d", float("nan"))), "INVALID_GROUND_SLOTS"),
    ("infinite-centre-row", lambda b: _centre_bytes(b, struct.pack("<d", float("-inf"))), "INVALID_GROUND_SLOTS"),
    ("boolean-angle", lambda b: b.update(angle=False), "INVALID_GROUND_SLOTS"),
    ("infinite-angle", lambda b: b.update(angle=float("inf")), "INVALID_GROUND_SLOTS"),
    ("huge-int-angle-block", lambda b: b.update(angle=10**1000), "INVALID_GROUND_SLOTS"),
    ("template-unknown-key", lambda b: b["panel"].update(id="x"), "INVALID_GROUND_SLOTS"),
    ("template-validity-null", lambda b: b["panel"].update(validity=None), "INVALID_GROUND_SLOTS"),
]


@pytest.mark.parametrize("name,change,code", DECODE_REFUSED, ids=[row[0] for row in DECODE_REFUSED])
def test_ground_graph_codec_decode_refused(name, change, code):
    block = small_block()
    if callable(change):
        change(block)
    else:
        block = deepcopy(change)
    before = deepcopy(block) if "nan" not in name else None
    refused(code, codec.decode_slots, block)
    if before is not None:
        assert block == before


def test_ground_graph_codec_one_slot_rows_are_canonical_base64():
    block = codec.encode_slots(SMALL_IDS[:1], SMALL_CENTRES[:1], 0.0, SMALL_TEMPLATE)
    assert (block["panel_ids"], block["centres"]) == ("AAAAAAAAQACAAAAAAAAAAQ==", "AAAAAAAA4D8AAAAAAAAAgA==")
    assert codec.decode_slots(block).ids == (SMALL_IDS[0],)
    for key, text in (("panel_ids", "AAAAAAAAQACAAAAAAAAAAR=="), ("centres", "AAAAAAAA4D8AAAAAAAAAgB==")):
        assert base64.b64decode(text) == base64.b64decode(block[key])
        refused("INVALID_GROUND_SLOTS", codec.decode_slots, {**block, key: text})
    refused("INVALID_GROUND_SLOTS", codec.decode_slots, {**block, "panel_ids": "AAAAAAAAQACAAAAAAAAAAQ=A"})


def _nested_extra(depth):
    extra = {}
    for _ in range(depth):
        extra = {"nested": extra}
    return extra


def _template_snapshot(panel):
    # Copy the chain iteratively: deepcopy itself cannot copy the 600-level bomb.
    snapshot = deepcopy({key: value for key, value in panel.items() if key != "extra"})
    source = panel["extra"]
    snapshot["extra"] = target = {}
    while source:
        source = source["nested"]
        target["nested"] = {}
        target = target["nested"]
    return snapshot


def _assert_template_unchanged(panel, before):
    assert {key: value for key, value in panel.items() if key != "extra"} == {
        key: value for key, value in before.items() if key != "extra"}
    left, right = panel["extra"], before["extra"]
    while left or right:
        assert type(left) is dict and type(right) is dict
        assert set(left) == set(right) == {"nested"}
        left, right = left["nested"], right["nested"]
    assert left == right == {}


def test_ground_graph_codec_deep_template_encode_is_bounded():
    ids, centres, angle = SMALL_IDS[:1], [[0.0, 0.0]], 0.0
    panel = deepcopy(SMALL_TEMPLATE)
    panel["extra"] = _nested_extra(600)
    before_ids, before_centres = deepcopy(ids), deepcopy(centres)
    before_panel = _template_snapshot(panel)
    refused("GRAPH_LIMIT_EXCEEDED", codec.encode_slots, ids, centres, angle, panel)
    assert ids == before_ids and centres == before_centres and angle == 0.0
    _assert_template_unchanged(panel, before_panel)


def test_ground_graph_codec_deep_template_decode_is_bounded(monkeypatch):
    block = codec.encode_slots(SMALL_IDS[:1], [[0.0, 0.0]], 0.0, SMALL_TEMPLATE)
    block["panel"]["extra"] = _nested_extra(600)
    before = deepcopy({key: value for key, value in block.items() if key != "panel"})
    before["panel"] = _template_snapshot(block["panel"])

    def never(*args, **kwargs):
        raise AssertionError("a row was decoded")

    monkeypatch.setattr(codec.base64, "b64decode", never)
    refused("GRAPH_LIMIT_EXCEEDED", codec.decode_slots, block)
    assert {key: value for key, value in block.items() if key != "panel"} == {
        key: value for key, value in before.items() if key != "panel"}
    _assert_template_unchanged(block["panel"], before["panel"])


def test_ground_graph_codec_template_depth_boundary_encodes():
    # _bounded_json starts the panel at depth 0: extra is depth 1, so 31 nested
    # dictionaries put the empty leaf at the admitted depth 32 (MAX_DEPTH).
    panel = deepcopy(SMALL_TEMPLATE)
    panel["extra"] = _nested_extra(31)
    before = deepcopy(panel)
    sdg._bounded_json(panel)
    block = codec.encode_slots(SMALL_IDS[:1], [[0.0, 0.0]], 0.0, panel)
    assert block["panel"] == before and block["panel"] is not panel
    assert panel == before
    panel["extra"] = _nested_extra(32)
    refused("GRAPH_LIMIT_EXCEEDED", codec.encode_slots, SMALL_IDS[:1], [[0.0, 0.0]], 0.0, panel)


def test_ground_graph_codec_decoded_row_length_is_exact(monkeypatch):
    block = codec.encode_slots(SMALL_IDS[:1], [[0.0, 0.0]], 0.0, SMALL_TEMPLATE)
    raw = base64.b64decode(block["panel_ids"]) + b"\x00"
    block["panel_ids"] = base64.b64encode(raw).decode("ascii")
    assert len(raw) == 17 and len(block["panel_ids"]) == codec.row_string_length(1) == 24
    before = deepcopy(block)
    decode = codec.base64.b64decode
    calls = []

    def tracked(text, **kwargs):
        calls.append(text)
        return decode(text, **kwargs)

    def never(*args, **kwargs):
        raise AssertionError("slot centres were unpacked")

    monkeypatch.setattr(codec.base64, "b64decode", tracked)
    monkeypatch.setattr(codec.struct, "unpack", never)
    refused("INVALID_GROUND_SLOTS", codec.decode_slots, block)
    assert calls == [block["panel_ids"].encode("ascii")]
    assert block == before


def test_ground_graph_codec_oversized_rows_are_refused_before_decoding(monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a row was decoded")
    monkeypatch.setattr(codec.base64, "b64decode", never)
    block = small_block()
    block["panel_ids"] = "A" * (10 * 1024 * 1024)
    refused("INVALID_GROUND_SLOTS", codec.decode_slots, block)
    block = small_block()
    block.update(count=10001, panel_ids="A" * 213336, centres="A" * 213336)
    refused("GRAPH_LIMIT_EXCEEDED", codec.decode_slots, block)


def _compact_frame(w1, b18, **changes):
    g = ground_graph(w1, b18, 1)
    frame = g["frames"][0]
    for key, value in changes.items():
        if key == "tracker_slots":
            frame["tracker"]["module_slots"] = value
        else:
            frame[key] = value
    return g


# (id, frame change on a one-tracker compact graph, refusal code)
FRAME_REFUSED = [
    ("roof-frame", {"installation_design": "Roof"}, "INVALID_GROUND_SLOTS"),
    ("no-tracker", {"tracker": None}, "INVALID_GROUND_SLOTS"),
    ("two-rows", {"module_rows": 2}, "INVALID_GROUND_SLOTS"),
    ("boolean-rows", {"module_rows": True}, "INVALID_GROUND_SLOTS"),
    ("columns-disagree", {"module_columns": 293}, "INVALID_GROUND_SLOTS"),
    ("slots-disagree", {"module_slots": 295}, "INVALID_GROUND_SLOTS"),
    ("tracker-slots-disagree", {"tracker_slots": 293}, "INVALID_GROUND_SLOTS"),
    ("explicit-panel-refs", {"panel_refs": ["leaf:panel:00000000-0000-4000-8000-000000000001"]}, "INVALID_GROUND_SLOTS"),
    ("explicit-matrix", {"matrix": [[]]}, "INVALID_GROUND_SLOTS"),
    ("explicit-assignments", {"panel_assignments": [{}]}, "INVALID_GROUND_SLOTS"),
    ("explicit-sequences", {"sequences": [{}]}, "INVALID_GROUND_SLOTS"),
    ("sequences-null", {"sequences": None}, "INVALID_GROUND_SLOTS"),
]


@pytest.mark.parametrize("name,changes,code", FRAME_REFUSED, ids=[row[0] for row in FRAME_REFUSED])
def test_ground_graph_codec_frame_must_agree_with_its_block(graph, b18, name, changes, code):
    g = _compact_frame(graph, b18, **changes)
    before = deepcopy(g)
    refused(code, codec.decode_graph_slots, g)
    refused(code, codec.expand_graph, g)
    assert g == before


def test_ground_graph_codec_repeated_ids_across_frames_are_refused(graph, b18):
    g = ground_graph(graph, b18, 2)
    g["frames"][1]["ground_slots"]["panel_ids"] = g["frames"][0]["ground_slots"]["panel_ids"]
    refused("DUPLICATE_APPLICATION_ID", codec.decode_graph_slots, g)
    g = ground_graph(graph, b18, 2)
    g["frames"][1]["id"] = g["frames"][0]["id"]
    refused("DUPLICATE_APPLICATION_ID", codec.decode_graph_slots, g)


def test_ground_graph_codec_graph_slot_bound_is_checked_before_decoding(graph, b18, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a row was decoded")
    g = ground_graph(graph, b18, 1)
    frame = g["frames"][0]
    frame.update(module_columns=10000, module_slots=10000)
    frame["tracker"]["module_slots"] = 10000
    frame["ground_slots"].update(count=10000, panel_ids="A" * 213336, centres="A" * 213336)
    g["frames"] = [deepcopy(frame) for _ in range(11)]
    for k, item in enumerate(g["frames"]):
        item["id"] = uid("frame", f"bomb:{k}")
    monkeypatch.setattr(codec.base64, "b64decode", never)
    refused("GRAPH_LIMIT_EXCEEDED", codec.decode_graph_slots, g)
    refused("GRAPH_LIMIT_EXCEEDED", codec.expand_graph, g)


def test_ground_graph_codec_template_bomb_is_refused_before_expansion(graph, b18, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a row was decoded")
    g = ground_graph(graph, b18, 1)
    g["frames"][0]["ground_slots"]["panel"]["extra"] = {"payload": list(range(2000))}
    assert codec.expansion_floor(g)[0] > sdg.MAX_NODES
    monkeypatch.setattr(codec.base64, "b64decode", never)
    before = deepcopy(g)
    refused("GRAPH_LIMIT_EXCEEDED", codec.expand_graph, g)
    assert g == before


def test_ground_graph_codec_two_trackers_expand_to_valid_v1(graph, b18):
    g = ground_graph(graph, b18, 2)
    before = deepcopy(g)
    assert codec.compact_frame_ids(g) == [frame["id"] for frame in g["frames"]]
    assert canon_sha(g) == "da0365699a167f319128e57639f5700e5e473c3280e660471a47de740f70682d"
    expanded = codec.expand_graph(g)
    assert g == before
    assert validate_graph(expanded) == expanded
    assert canon_sha(expanded) == "ef1bae34e7de39a6f3902e389b84f98f154c5dc45c901f4c8ac18d4278316b4c"
    assert len(expanded["panels"]) == 588 and "ground_slots" not in expanded["frames"][0]
    assert codec.json_cost(expanded) == (48630, 558980)
    first = expanded["panels"][0]
    assert first == {
        "id": slot_ids(g, 0, 1)[0], "kind": "panel", "rev": 0,
        "provenance": g["frames"][0]["provenance"], "extra": {}, "validity": VALID,
        "frame_ref": g["frames"][0]["id"], "matrix_cell": {"row": 0, "col": 0},
        "centre": b18["layout"]["trackers"][0]["panels"][0],
        "angle": b18["layout"]["trackers"][0]["row_angle_rad"],
        "assignment": {"string_ref": None, "seq": None},
    }
    assert list(first) == ["id", "kind", "rev", "provenance", "extra", "validity", "frame_ref",
                           "matrix_cell", "centre", "angle", "assignment"]
    back = codec.compact_graph(expanded, codec.compact_frame_ids(g))
    assert canon(back) == canon(g)


def test_ground_graph_codec_unassigned_expansion_costs_exactly_the_floor(graph, b18):
    for trackers in (2, 20):
        g = ground_graph(graph, b18, trackers)
        nodes, size = codec.json_cost(codec.expand_graph(g))
        floor_nodes, floor_size = codec.expansion_floor(g)
        assert floor_nodes == nodes
        assert floor_size <= size


def test_ground_graph_codec_expansion_at_the_node_limit_is_accepted(graph, b18):
    g = ground_graph(graph, b18, 20)
    g["frames"][0]["extra"]["padding"] = [None] * 15588
    before = deepcopy(g)
    frame_ids = codec.compact_frame_ids(g)
    expanded = codec.expand_graph(g)
    assert codec.json_cost(expanded)[0] == sdg.MAX_NODES == 500000
    assert validate_graph(expanded) == expanded
    assert canon(codec.compact_graph(expanded, frame_ids)) == canon(g)
    assert g == before
    g["frames"][0]["extra"]["padding"].append(None)
    expanded["frames"][0]["extra"]["padding"].append(None)
    compact_before, expanded_before = deepcopy(g), deepcopy(expanded)
    refused("GRAPH_LIMIT_EXCEEDED", codec.expand_graph, g)
    refused("INVALID_GROUND_SLOTS", codec.compact_graph, expanded, frame_ids)
    assert g == compact_before and expanded == expanded_before


def test_ground_graph_codec_strings_derive_every_slot_view(graph, b18):
    g = strung(graph, b18)
    expanded = codec.expand_graph(g)
    assert validate_graph(expanded) == expanded
    assert canon_sha(expanded) == "e56b1f2c2254ebbbfda3b42bc199d66ab2ae222f018db4a9573cd678a32f6f92"
    synced = deepcopy(expanded)
    sync_assignments(synced)
    assert canon(synced) == canon(expanded)
    a, b = expanded["frames"]
    assert [s["ordered_panel_refs"] for s in a["sequences"]] == [slot_ids(g, 0, 5)[0:3], slot_ids(g, 0, 5)[3:5]]
    assert [s["string_ref"] for s in b["sequences"]] == [app_id("string", 2), app_id("string", 3)]
    cell = a["matrix"][0][4]
    assert (cell["seq"], cell["inverter_id"], cell["string_input_number"]) == (1, app_id("inverter", 1), 1)
    cell = b["matrix"][0][11]
    assert (cell["seq"], cell["inverter_id"], cell["string_input_number"]) == (1, None, None)
    assert b["panel_assignments"][1] == {"panel_ref": slot_ids(g, 1, 2)[1], "string_ref": app_id("string", 2),
                                         "seq": 3, "inverter_id": app_id("inverter", 1),
                                         "string_input_number": 1}
    assert canon(codec.compact_graph(expanded, codec.compact_frame_ids(g))) == canon(g)


def test_ground_graph_codec_twenty_trackers_fit_the_unchanged_validator(graph, b18):
    g = ground_graph(graph, b18, 20)
    assert (codec.json_cost(g), len(canon(g))) == ((3070, 286335), 284813)
    expanded = codec.expand_graph(g)
    assert validate_graph(expanded) == expanded
    assert len(expanded["panels"]) == 5880
    assert (codec.json_cost(expanded), len(canon(expanded))) == ((484410, 5566195), 5915320)
    assert canon_sha(expanded) == "8f1ba0024fb00a0d5d807dc4b9ecc413809389e7138c5a71fa05892aee59f582"
    assert canon(codec.compact_graph(expanded, codec.compact_frame_ids(g))) == canon(g)


def test_ground_graph_codec_twenty_one_trackers_exceed_the_node_limit(graph, b18, monkeypatch):
    g = ground_graph(graph, b18, 21)
    before = deepcopy(g)
    removed_size = sum(len(codec.SLOTS_KEY.encode("utf-8")) + codec.json_cost(frame["ground_slots"])[1]
                       for frame in g["frames"])
    assert codec.expansion_floor(g) == (508620, 1776107 - removed_size)

    def never(*args, **kwargs):
        raise AssertionError("a row was decoded")
    monkeypatch.setattr(codec.base64, "b64decode", never)
    refused("GRAPH_LIMIT_EXCEEDED", codec.expand_graph, g)
    assert g == before


def test_ground_graph_codec_full_b18_site(graph, b18, monkeypatch):
    g = ground_graph(graph, b18, 237)
    assert len(g["frames"]) == 237
    assert sum(frame["ground_slots"]["count"] for frame in g["frames"]) == 69678
    data = canon(g)
    assert (codec.json_cost(g), len(data)) == ((34101, 3364599), 3350782)
    assert sha256(data).hexdigest() == "a7e92f0cedec74b369d232918e12cf4bcd25e98e92409726648295c311b27a0b"
    tables = codec.decode_graph_slots(g)
    assert list(tables) == codec.compact_frame_ids(g)
    for k, (table, tracker) in enumerate(zip(tables.values(), b18["layout"]["trackers"])):
        assert table.ids == tuple(slot_ids(g, k, tracker["module_slots"]))
        assert [list(table.centres[i:i + 2]) for i in range(0, len(table.centres), 2)] == tracker["panels"]
        assert table.angle == tracker["row_angle_rad"]
    assert len({panel_id for table in tables.values() for panel_id in table.ids}) == 69678
    removed_size = sum(len(codec.SLOTS_KEY.encode("utf-8")) + codec.json_cost(frame["ground_slots"])[1]
                       for frame in g["frames"])
    assert codec.expansion_floor(g) == (5737980, 20017641 - removed_size)

    def never(*args, **kwargs):
        raise AssertionError("a row was decoded")
    monkeypatch.setattr(codec.base64, "b64decode", never)
    refused("GRAPH_LIMIT_EXCEEDED", codec.expand_graph, g)


def test_ground_graph_codec_existing_ground_fixture_is_not_compactable(graph):
    """test_solar_ground_graph.py's single-tracker Ground graph has integer centres and strings, so
    compaction would change a value; it is refused and nothing changes."""
    value = deepcopy(graph)
    value["project"]["installation_design"] = "Ground"
    value["frames"][0]["installation_design"] = "Ground"
    value["frames"][0]["tracker"] = {
        "tracker_model": "single_axis_tracker", "axis_start": [1, 0], "axis_end": [3, 0],
        "axis_units": "drawing", "module_slots": 3, "row_index": 0, "length_m": 0.0508,
        "rail_overhang_m": 0.05, "cross_axis_width_m": 2.1, "max_tilt_deg": 60.0,
        "source": {"entity_kind": "tracker", "handle": "2A0", "layer": "LEAF-TRACKERS",
                   "block_name": "LEAFSAT", "source_command": "LEAFSAT"}}
    assert validate_graph(value) == value
    before = deepcopy(value)
    refused("INVALID_GROUND_SLOTS", codec.compact_graph, value, [app_id("frame", 1)])
    assert value == before


def _expanded_two(w1, b18):
    g = ground_graph(w1, b18, 2)
    return g, codec.expand_graph(g)


def _swap_runs(e):
    e["panels"] = e["panels"][294:] + e["panels"][:294]


def _provenance(e):
    e["panels"][300]["provenance"]["last_writer"] = "solar-panel-remove"


def _cell_key(e):
    e["frames"][0]["matrix"][0][5]["future"] = True


def _sequence_key(e):
    e["frames"][0]["sequences"].append({"string_ref": None, "ordered_panel_refs": []})


def _integer_centre(e):
    e["panels"][10]["centre"] = [2, 3.0]


def _moved_cell(e):
    e["frames"][1]["matrix"][0][7]["x"] = 99.0


def _explicit_after(e):
    e["panels"].append(deepcopy(e["panels"][0]) | {"id": "leaf:panel:00000000-0000-4000-8000-000000000999"})


# (id, change to the expanded two-tracker graph, frame ids named, refusal code)
COMPACT_REFUSED = [
    ("unknown-frame", None, lambda g: [app_id("frame", 9)], "INVALID_GROUND_SLOTS"),
    ("repeated-name", None, lambda g: [g["frames"][0]["id"]] * 2, "INVALID_GROUND_SLOTS"),
    ("names-not-a-list", None, lambda g: g["frames"][0]["id"], "INVALID_GROUND_SLOTS"),
    ("runs-swapped", _swap_runs, lambda g: [f["id"] for f in g["frames"]], "INVALID_GROUND_SLOTS"),
    ("run-not-at-tail", None, lambda g: [g["frames"][0]["id"]], "INVALID_GROUND_SLOTS"),
    ("explicit-panel-after-runs", _explicit_after, lambda g: [f["id"] for f in g["frames"]], "INVALID_GROUND_SLOTS"),
    ("one-panel-provenance", _provenance, lambda g: [f["id"] for f in g["frames"]], "INVALID_GROUND_SLOTS"),
    ("matrix-cell-unknown-key", _cell_key, lambda g: [f["id"] for f in g["frames"]], "INVALID_GROUND_SLOTS"),
    ("extra-sequence", _sequence_key, lambda g: [f["id"] for f in g["frames"]], "INVALID_GROUND_SLOTS"),
    ("integer-centre", _integer_centre, lambda g: [f["id"] for f in g["frames"]], "INVALID_GROUND_SLOTS"),
    ("cell-off-centre", _moved_cell, lambda g: [f["id"] for f in g["frames"]], "INVALID_GROUND_SLOTS"),
]


@pytest.mark.parametrize("name,change,names,code", COMPACT_REFUSED, ids=[row[0] for row in COMPACT_REFUSED])
def test_ground_graph_codec_compact_refused(graph, b18, name, change, names, code):
    g, expanded = _expanded_two(graph, b18)
    target, frame_ids = expanded, names(expanded)
    if change is not None:
        change(target)
    before = deepcopy(target)
    refused(code, codec.compact_graph, target, frame_ids)
    assert target == before


def test_ground_graph_codec_a_compact_frame_is_not_compacted_again(graph, b18):
    g = ground_graph(graph, b18, 2)
    before = deepcopy(g)
    refused("INVALID_GROUND_SLOTS", codec.compact_graph, g, [g["frames"][0]["id"]])
    assert g == before


def test_ground_graph_codec_last_frame_alone_compacts(graph, b18):
    g, expanded = _expanded_two(graph, b18)
    partial = codec.compact_graph(expanded, [g["frames"][1]["id"]])
    assert codec.compact_frame_ids(partial) == [g["frames"][1]["id"]]
    assert len(partial["panels"]) == 294
    assert canon(codec.expand_graph(partial)) == canon(expanded)
    assert validate_graph(codec.expand_graph(partial)) == expanded


def test_ground_graph_codec_results_never_alias_the_input(graph, b18):
    g = ground_graph(graph, b18, 1)
    expanded = codec.expand_graph(g)
    expanded["panels"][0]["provenance"]["last_writer"] = "changed"
    expanded["panels"][0]["validity"]["reasons"].append("changed")
    expanded["panels"][1]["extra"]["note"] = "changed"
    assert g["frames"][0]["ground_slots"]["panel"]["provenance"]["last_writer"] == "fixture"
    assert g["frames"][0]["ground_slots"]["panel"]["validity"] == VALID
    assert g["frames"][0]["ground_slots"]["panel"]["extra"] == {}
    assert expanded["panels"][1]["validity"] == VALID
    assert expanded["panels"][0]["extra"] == {}
    again = codec.expand_graph(g)
    back = codec.compact_graph(again, codec.compact_frame_ids(g))
    back["frames"][0]["ground_slots"]["panel"]["extra"]["note"] = "changed"
    assert again["panels"][0]["extra"] == {}
