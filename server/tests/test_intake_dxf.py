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


@pytest.mark.parametrize("bad,needle", [
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
