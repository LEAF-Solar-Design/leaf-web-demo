"""W4g-1: intake JSON -> DXF, the inverse of dxf_intake over the intake subset.

Pins: the round trip parse(emit(intake)) reproduces layers and polylines
exactly on the shipped demo intake (2,345 polylines) and on a mixed-z
polyline (classic POLYLINE path); handles are kept verbatim when real and
replaced uniquely when synthetic; every malformed shape is refused before a
byte is emitted; a control character in a layer or text is refused (a newline
would inject entities into the pair grammar).

Run:  cd server && python -m pytest tests/test_intake_dxf.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import dxf_intake  # noqa: E402
import intake_dxf  # noqa: E402

DEMO_INTAKE = SERVER_DIR.parent / "data" / "rooftop_demo.intake.json"


def _subset(intake):
    return {"layers": intake["layers"], "polylines": intake["polylines"]}


def _roundtrip(intake):
    data = intake_dxf.intake_to_dxf(intake)
    return dxf_intake.parse_dxf_bytes(data, source_name=intake.get("dwg", "x")), data


def _bulged_square(bulges):
    return {"layers": ["A"], "polylines": [
        {"layer": "A", "closed": True, "handle": "10", "xdata": None,
         "pts": [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]],
         "bulges": bulges}]}


def test_bulged_polyline_round_trips_with_42_after_its_vertex():
    intake = _bulged_square([1, 0, 0, 0])
    back, data = _roundtrip(intake)
    lines = data.decode().splitlines()
    pairs = list(zip(lines[::2], lines[1::2]))
    assert [p for p in pairs if p[0] == "42"] == [("42", "1.0")]
    first = pairs.index(("10", "0.0"))
    assert pairs[first:first + 3] == [("10", "0.0"), ("20", "0.0"), ("42", "1.0")]
    assert back["polylines"][0]["bulges"] == [1.0, 0.0, 0.0, 0.0]
    assert _subset(back) == _subset(intake)


def test_sparse_bulge_after_third_vertex_keeps_its_index():
    data = (b"0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\n10\n8\nA\n"
            b"90\n4\n70\n1\n10\n0\n20\n0\n10\n10\n20\n0\n"
            b"10\n10\n20\n10\n42\n0.5\n10\n0\n20\n10\n"
            b"0\nENDSEC\n0\nEOF\n")
    back = dxf_intake.parse_dxf_bytes(data)
    assert back["polylines"][0]["bulges"] == [0.0, 0.0, 0.5, 0.0]


def test_sparse_bulge_after_last_vertex_of_closed_polyline_keeps_its_index():
    data = (b"0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\n10\n8\nA\n"
            b"90\n4\n70\n1\n10\n0\n20\n0\n10\n10\n20\n0\n"
            b"10\n10\n20\n10\n10\n0\n20\n10\n42\n-0.5\n"
            b"0\nENDSEC\n0\nEOF\n")
    back = dxf_intake.parse_dxf_bytes(data)
    assert back["polylines"][0]["closed"] is True
    assert back["polylines"][0]["bulges"] == [0.0, 0.0, 0.0, -0.5]


def test_zero_bulges_emit_no_groups_and_parse_as_absent():
    back, data = _roundtrip(_bulged_square([0, 0, 0, 0]))
    assert b"\n42\n" not in data
    assert "bulges" not in back["polylines"][0]


def test_inspection_bulge_flag_emits_no_groups_and_parses_as_absent():
    back, data = _roundtrip(_bulged_square([1]))
    assert b"\n42\n" not in data
    assert "bulges" not in back["polylines"][0]


@pytest.mark.parametrize("bulges", [None, (1, 0, 0, 0),
    ["1"], [float("nan")], [float("inf")],
    ["1", 0, 0, 0], [float("nan"), 0, 0, 0], [float("inf"), 0, 0, 0]])
def test_invalid_bulges_are_refused_with_the_field_named(bulges):
    with pytest.raises(intake_dxf.IntakeDxfError, match=r"polylines\[0\]\.bulges"):
        intake_dxf.intake_to_dxf(_bulged_square(bulges))


def test_mixed_z_polyline_cannot_carry_bulges():
    intake = _bulged_square([1, 0, 0, 0])
    intake["polylines"][0]["pts"][1][2] = 2
    back, data = _roundtrip(intake)
    assert b"\nPOLYLINE\n" in data
    assert b"\n42\n" not in data
    assert "bulges" not in back["polylines"][0]


def test_demo_intake_round_trips_exactly():
    intake = json.loads(DEMO_INTAKE.read_text(encoding="utf-8"))
    back, data = _roundtrip(intake)
    assert back["layers"] == intake["layers"]
    assert back["polylines"] == intake["polylines"]
    assert len(intake["polylines"]) > 2000  # the real thing, not a toy
    # Real handles travel verbatim as group 5.
    assert b"\n5\n9462\n" in data


def test_synthetic_handles_become_unique_hex_above_the_real_ones():
    intake = {"layers": ["A"], "polylines": [
        {"layer": "A", "closed": True, "pts": [[0, 0, 0], [1, 0, 0], [1, 1, 0]], "xdata": None, "handle": "L1"},
        {"layer": "A", "closed": False, "pts": [[0, 0, 0], [2, 2, 0]], "xdata": None, "handle": "1F"},
        {"layer": "A", "closed": False, "pts": [[0, 0, 0], [3, 3, 0]], "xdata": None, "handle": ""},
    ]}
    back, data = _roundtrip(intake)
    handles = [p["handle"] for p in back["polylines"]]
    assert handles[1] == "1F"
    assert handles[0] != handles[2]
    assert all(int(h, 16) > 0xFF for h in (handles[0], handles[2]))
    assert len(set(handles)) == 3
    # Geometry and layers unchanged by the handle swap.
    assert [p["pts"] for p in back["polylines"]] == [p["pts"] for p in intake["polylines"]]


def test_properties_round_trip_62_6_370_and_are_absent_for_an_untouched_entity():
    # W4g-7b-03s: colour/linetype/lineweight travel through 62/6/370 (rgb,
    # when present, also travels through 420 as of w4g-7b-03s-d D2; the
    # write contract itself only ever sets an ACI, never rgb) and only for a
    # handle `properties` actually names; an untouched entity carries none
    # of the four groups, so it round-trips with no `properties` entry.
    intake = {"layers": ["A"], "polylines": [
        {"layer": "A", "closed": False, "pts": [[0, 0, 0], [1, 0, 0]], "xdata": None, "handle": "10"},
        {"layer": "A", "closed": False, "pts": [[0, 0, 0], [2, 0, 0]], "xdata": None, "handle": "11"},
    ], "properties": {"10": {"aci": 1, "rgb": None, "linetype": "Continuous", "lineweight": 25}}}
    back, data = _roundtrip(intake)
    assert b"\n62\n1\n" in data and b"\n6\nContinuous\n" in data and b"\n370\n25\n" in data
    assert b"\n420\n" not in data
    assert back["properties"] == {"10": {"aci": 1, "rgb": None, "linetype": "Continuous", "lineweight": 25}}
    assert "11" not in back["properties"]


def test_true_colour_rgb_round_trips_through_420():
    # w4g-7b-03s-d D2: a record carrying a non-null rgb emits 420 (immediately
    # after 62), and the reader's own 420 -> rgb mapping reads it back.
    intake = {"layers": ["A"], "polylines": [
        {"layer": "A", "closed": False, "pts": [[0, 0, 0], [1, 0, 0]], "xdata": None, "handle": "10"},
    ], "properties": {"10": {"aci": 1, "rgb": [10, 20, 30], "linetype": "ByLayer", "lineweight": -1}}}
    back, data = _roundtrip(intake)
    assert b"\n420\n660510\n" in data
    assert data.index(b"\n420\n") > data.index(b"\n62\n1\n")
    assert back["properties"]["10"] == {"aci": 1, "rgb": [10, 20, 30], "linetype": "ByLayer", "lineweight": -1}


def test_negative_62_is_the_layer_off_flag_and_stores_its_magnitude():
    # w4g-7b-03s-d D1: a negative colour is AutoCAD's "layer off" flag; the
    # ACI the entity actually carries is the magnitude.
    text = "\n".join([
        "0", "SECTION", "2", "ENTITIES",
        "0", "LINE", "8", "0",
        "10", "0.0", "20", "0.0", "30", "0.0",
        "11", "1.0", "21", "0.0", "31", "0.0",
        "62", "-7", "5", "3B",
        "0", "ENDSEC", "0", "EOF", "",
    ])
    out = dxf_intake.parse_dxf_bytes(text.encode("utf-8"))
    assert out["properties"]["3B"]["aci"] == 7
    assert "propertiesDropped" not in out


def test_out_of_range_62_and_370_are_dropped_and_counted():
    # w4g-7b-03s-d D1: a value the writer would refuse (intake_dxf's 0..256
    # aci bound, its lineweight enumeration) is never stored; it is dropped
    # (reads as absent/default) and counted in propertiesDropped so an
    # uploaded DXF carrying either never becomes unopenable on its own next
    # intake_to_dxf leg.
    text = "\n".join([
        "0", "SECTION", "2", "ENTITIES",
        "0", "LINE", "8", "0",
        "10", "0.0", "20", "0.0", "30", "0.0",
        "11", "1.0", "21", "0.0", "31", "0.0",
        "62", "300", "370", "26", "5", "3B",
        "0", "ENDSEC", "0", "EOF", "",
    ])
    out = dxf_intake.parse_dxf_bytes(text.encode("utf-8"))
    assert out["properties"]["3B"]["aci"] == 256
    assert out["properties"]["3B"]["lineweight"] == -1
    assert out["propertiesDropped"] == 2


def test_every_reader_property_branch_round_trips_through_the_writer_without_raising():
    # w4g-7b-03s-d D1 pin: intake_to_dxf must accept every intake the reader
    # emits, including the normalized/dropped branches above, so a DXF that
    # exercised all of them stays openable on its own next write.
    long_linetype = "L" * 300
    text = "\n".join([
        "0", "SECTION", "2", "ENTITIES",
        "0", "LINE", "8", "0",  # negative aci -> layer-off magnitude
        "10", "0.0", "20", "0.0", "30", "0.0",
        "11", "1.0", "21", "0.0", "31", "0.0",
        "62", "-7", "5", "10",
        "0", "LINE", "8", "0",  # out-of-range aci and lineweight -> dropped
        "10", "0.0", "20", "0.0", "30", "0.0",
        "11", "2.0", "21", "0.0", "31", "0.0",
        "62", "300", "370", "26", "5", "11",
        "0", "LINE", "8", "0",  # oversized linetype -> dropped
        "10", "0.0", "20", "0.0", "30", "0.0",
        "11", "3.0", "21", "0.0", "31", "0.0",
        "6", long_linetype, "5", "12",
        "0", "LINE", "8", "0",  # valid linetype/lineweight + true colour
        "10", "0.0", "20", "0.0", "30", "0.0",
        "11", "4.0", "21", "0.0", "31", "0.0",
        "6", "DASHED", "370", "25", "62", "1", "420", "660510", "5", "13",
        "0", "ENDSEC", "0", "EOF", "",
    ])
    first = dxf_intake.parse_dxf_bytes(text.encode("utf-8"))
    assert first["propertiesDropped"] == 3
    data = intake_dxf.intake_to_dxf(first)
    second = dxf_intake.parse_dxf_bytes(data, source_name=first["dwg"])
    assert second["properties"] == first["properties"]
    assert second["polylines"] == first["polylines"]


def test_mixed_z_polyline_takes_the_3d_polyline_path_and_keeps_every_z():
    intake = {"layers": ["Z"], "polylines": [
        {"layer": "Z", "closed": False, "pts": [[0, 0, 1.5], [1, 0, 2.5], [1, 1, -3.25]], "xdata": None, "handle": "2A"},
    ]}
    back, data = _roundtrip(intake)
    assert b"\n0\nPOLYLINE\n" in data and b"\n0\nVERTEX\n" in data
    assert back["polylines"] == intake["polylines"]


def test_entity_layer_missing_from_the_layer_list_is_appended_in_first_seen_order():
    intake = {"layers": ["B"], "polylines": [
        {"layer": "C", "closed": False, "pts": [[0, 0, 0], [1, 1, 0]], "xdata": None, "handle": "10"},
        {"layer": "B", "closed": False, "pts": [[0, 0, 0], [1, 1, 0]], "xdata": None, "handle": "11"},
    ]}
    back, _ = _roundtrip(intake)
    assert back["layers"] == ["B", "C"]


def test_texts_round_trip_and_empty_texts_are_dropped_like_the_parser_does():
    intake = {"layers": ["T"], "polylines": [], "texts": [
        {"kind": "TEXT", "layer": "T", "pt": [1.0, 2.0], "text": "ROOF  PLAN", "handle": "30"},
        {"kind": "MTEXT", "layer": "T", "pt": [3.0, 4.0], "text": "north", "handle": "31"},
        {"kind": "TEXT", "layer": "T", "pt": [5.0, 6.0], "text": "   ", "handle": "32"},
    ]}
    back, _ = _roundtrip(intake)
    assert [t["text"] for t in back["texts"]] == ["ROOF PLAN", "north"]
    assert [t["handle"] for t in back["texts"]] == ["30", "31"]
    assert [t["pt"] for t in back["texts"]] == [[1.0, 2.0], [3.0, 4.0]]


def test_dimension_round_trips_exactly_and_dimstyle_table_is_conditional():
    # W4g-7b-04s: one LINEAR and one ALIGNED dimension, plus the loaded
    # dimstyle catalogue; byte-identical output when neither is carried.
    intake = {
        "layers": ["0", "DIMS"], "polylines": [],
        "dimensions": [
            {"type": "LINEAR", "layer": "DIMS", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
             "dimline": [1.5, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
             "nrm": [0.0, 0.0, 1.0], "measurement": 3.0, "handle": "A1"},
            {"type": "ALIGNED", "layer": "0", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
             "dimline": [1.5, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
             "nrm": [0.0, 0.0, 1.0], "measurement": 5.0, "handle": "A2"},
        ],
        "dimstyles": ["Standard"],
    }
    back, data = _roundtrip(intake)
    assert back["dimensions"] == intake["dimensions"]
    assert back["dimstyles"] == intake["dimstyles"]
    assert b"\n0\nDIMENSION\n" in data and b"\n0\nDIMSTYLE\n" in data
    plain = intake_dxf.intake_to_dxf({"layers": ["0"], "polylines": []})
    assert b"DIMSTYLE" not in plain
    empty = intake_dxf.intake_to_dxf(
        {"layers": ["0"], "polylines": [], "dimensions": [], "dimstyles": []})
    assert empty == plain


def test_dimension_with_a_tilted_normal_keeps_def_points_as_wcs_not_ocs():
    # F3 (opus round-one read of PR #1119): per the DXF spec, DIMENSION
    # groups 13/14/10 are WCS points; only 11/12/16 are OCS. Re-pinned from
    # the old (mistaken) OCS-round-trip fixture: a non-default normal must
    # NOT transform p1/p2/dimline, so def2 (3,4,0) reads back UNCHANGED as
    # (3,4,0), never rotated into some other OCS-projected point.
    normal = [0.0, 0.0, -1.0]
    intake = {
        "layers": ["0"], "polylines": [],
        "dimensions": [
            {"type": "ALIGNED", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
             "dimline": [1.5, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
             "nrm": normal, "measurement": 5.0, "handle": "A3"},
        ],
        "dimstyles": ["Standard"],
    }
    back, _ = _roundtrip(intake)
    entity = back["dimensions"][0]
    assert entity["nrm"] == normal
    assert entity["p1"] == [0.0, 0.0, 0.0]
    assert entity["p2"] == [3.0, 4.0, 0.0]
    assert entity["dimline"] == [1.5, 6.0, 0.0]


def test_dimension_emits_group_11_as_the_canonical_dimline_point():
    # F4: the text middle point (group 11, OCS like 12/16) was omitted
    # entirely; the planar contract puts the text on the dimension line, so
    # it is always the canonical dimline point, written after group 10.
    intake = {
        "layers": ["0"], "polylines": [],
        "dimensions": [
            {"type": "LINEAR", "layer": "0", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
             "dimline": [3.0, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
             "nrm": [0.0, 0.0, 1.0], "measurement": 3.0, "handle": "A1"},
        ],
        "dimstyles": ["Standard"],
    }
    data = intake_dxf.intake_to_dxf(intake)
    text = data.decode("ascii")
    ten_idx = text.index("\n10\n3.0\n20\n6.0\n30\n0.0\n")
    eleven_idx = text.index("\n11\n3.0\n21\n6.0\n31\n0.0\n")
    assert ten_idx < eleven_idx
    # The parser has no field for group 11: it is silently ignored on read.
    back = dxf_intake.parse_dxf_bytes(data)
    assert back["dimensions"] == intake["dimensions"]


def test_unsupported_dimension_subtype_is_counted_not_refused():
    # A real DIMENSION subtype this contract does not carry (2 = angular)
    # is skipped, counted, and never refuses the parse.
    raw = (
        "0\nSECTION\n2\nENTITIES\n"
        "0\nDIMENSION\n5\n1A\n8\n0\n70\n2\n"
        "10\n0.0\n20\n0.0\n30\n0.0\n"
        "13\n0.0\n23\n0.0\n33\n0.0\n"
        "14\n1.0\n24\n0.0\n34\n0.0\n"
        "0\nENDSEC\n0\nEOF\n"
    ).encode("ascii")
    parsed = dxf_intake.parse_dxf_bytes(raw)
    assert parsed.get("dimensions_unsupported") == 1
    assert "dimensions" not in parsed


@pytest.mark.parametrize("bad,needle", [
    ({"layers": ["0"], "polylines": [], "dimensions": [{"type": "ANGULAR", "p1": [0, 0, 0],
      "p2": [1, 0, 0], "dimline": [0, 1, 0], "style": "Standard", "measurement": 1}]}, "type"),
    ({"layers": ["0"], "polylines": [], "dimensions": [{"type": "LINEAR", "p1": [0, 0, 0],
      "p2": [1, 0, 0], "dimline": [0, 1, 0], "style": "", "measurement": 1}]}, "dimstyle name"),
    ({"layers": "A", "polylines": []}, "layers"),
    ({"layers": ["A", "A"], "polylines": []}, "duplicate layer"),
    ({"layers": ["A\nB"], "polylines": []}, "control character"),
    ({"layers": [], "polylines": [{"layer": "A", "closed": True, "pts": [[0, 0]], "handle": "1"}]}, "two points"),
    ({"layers": [], "polylines": [{"layer": "A", "closed": "yes", "pts": [[0, 0], [1, 1]], "handle": "1"}]}, "boolean"),
    ({"layers": [], "polylines": [{"layer": "A", "closed": True, "pts": [[0, float("nan")], [1, 1]], "handle": "1"}]}, "finite"),
    ({"layers": [], "polylines": [{"layer": "A", "closed": True, "pts": [[0, True], [1, 1]], "handle": "1"}]}, "not a number"),
    ({"layers": [], "polylines": [{"layer": "A", "closed": True, "pts": [[0, 0, 0, 0], [1, 1]], "handle": "1"}]}, "a point is"),
    ({"layers": [], "polylines": [
        {"layer": "A", "closed": True, "pts": [[0, 0], [1, 1]], "handle": "ab"},
        {"layer": "A", "closed": True, "pts": [[0, 0], [1, 1]], "handle": "AB"}]}, "duplicate handle"),
    ({"layers": [], "polylines": [{"layer": "A", "closed": True, "pts": [[0, 0], [1, 1]], "handle": 12}]}, "handle is not a string"),
    ({"layers": [], "polylines": [], "texts": [{"kind": "TEXT", "layer": "A", "pt": [0, 0], "text": "a\x00b", "handle": "1"}]}, "control character"),
    ({"layers": [], "polylines": [], "texts": [{"kind": "LABEL", "layer": "A", "pt": [0, 0], "text": "x", "handle": "1"}]}, "kind"),
    ("nope", "not an object"),
])
def test_malformed_intakes_are_refused_before_any_byte(bad, needle):
    with pytest.raises(intake_dxf.IntakeDxfError) as exc:
        intake_dxf.intake_to_dxf(bad)
    assert needle in str(exc.value)


def test_bounds_hold(monkeypatch):
    monkeypatch.setattr(intake_dxf, "MAX_ENTITIES", 2)
    three = {"layers": [], "polylines": [
        {"layer": "A", "closed": False, "pts": [[0, 0], [1, 1]], "handle": str(i)} for i in range(3)]}
    with pytest.raises(intake_dxf.IntakeDxfError):
        intake_dxf.intake_to_dxf(three)
    monkeypatch.setattr(intake_dxf, "MAX_ENTITIES", 200_000)
    monkeypatch.setattr(intake_dxf, "MAX_POINTS_PER_ENTITY", 2)
    with pytest.raises(intake_dxf.IntakeDxfError):
        intake_dxf.intake_to_dxf({"layers": [], "polylines": [
            {"layer": "A", "closed": False, "pts": [[0, 0], [1, 1], [2, 2]], "handle": "1"}]})
