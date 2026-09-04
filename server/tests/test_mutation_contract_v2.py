"""W4g-3a: the mutation contract v2 (the browser engine's saves through the
same closed data plan catalog tools use), end to end on the pure modules:
validation, plan lowering, the mock writer, effects verification, and the
intake shape's two additive lists (circles, arcs) across both parsers and the
synthesizer. No route, no APS."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

import dxf_intake
import intake_dxf
import write_loop
from mutation_plan import (
    emit_plan, plan_sha256, uses_v2, validate_mutations,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "da"))
import intake_parse  # noqa: E402


def _poly(handle, layer="Panels", closed=True, pts=None):
    return {"handle": handle, "layer": layer, "closed": closed, "xdata": None,
            "pts": pts or [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [2.0, 2.0, 0.0], [0.0, 2.0, 0.0]]}


def _line(handle, layer="0"):
    return _poly(handle, layer, closed=False, pts=[[5.0, 5.0, 0.0], [9.0, 5.0, 0.0]])


def _circle(handle, layer="0", c=(10.0, 10.0, 0.0), r=3.0, nrm=(0.0, 0.0, 1.0)):
    return {"handle": handle, "layer": layer, "c": list(c), "r": r, "nrm": list(nrm)}


def _arc(handle, layer="0"):
    return {"handle": handle, "layer": layer, "c": [20.0, 0.0, 0.0], "r": 2.0,
            "start_deg": 0.0, "end_deg": 90.0, "nrm": [0.0, 0.0, 1.0]}


def _base():
    return {"dwg": "source.dwg", "layers": ["Panels", "0"],
            "polylines": [_poly("A"), _line("B")],
            "circles": [_circle("C1")], "arcs": [_arc("D1")]}


# --- validation -------------------------------------------------------------

def test_v1_input_yields_byte_identical_canonical_data_and_plan():
    base = {"dwg": "s.dwg", "layers": ["Panels"], "polylines": [_poly("A")]}
    mutations = {"removed": ["A"], "added": [_poly("N", "Leaf Output", pts=[
        [10.0, 10.0, 0.0], [12.0, 10.0, 0.0], [12.0, 12.0, 0.0], [10.0, 12.0, 0.0]])]}
    canonical = validate_mutations(base, mutations)
    assert set(canonical) == {"added", "removed"}
    assert "kind" not in canonical["added"][0]
    assert uses_v2(canonical) is False
    plan = emit_plan(canonical, base_sha256="1" * 64)
    assert plan.startswith(b"LEAF_MUTATION_PLAN|1\n")
    assert b"REMOVE|A\n" in plan and b"ADD|Leaf Output|0,0,1|0|10,10;12,10;12,12;10,12\n" in plan


def test_every_v2_op_validates_and_lowers_to_its_line():
    canonical = validate_mutations(_base(), {
        "added": [
            {"handle": "n1", "kind": "LINE", "layer": "0", "pts": [[0, 0], [3, 4]]},
            {"handle": "n2", "kind": "CIRCLE", "layer": "0", "c": [1, 2], "r": 0.5},
            {"handle": "n3", "kind": "ARC", "layer": "Panels", "c": [0, 0, 1], "r": 2, "start_deg": 30, "end_deg": 120},
            {"handle": "n4", "layer": "0", "closed": False, "pts": [[0, 0], [1, 1], [2, 0]]},
        ],
        "removed": ["C1", "B"],
        "set_layer": [{"handle": "A", "layer": "Moved"}],
        "set_points": [{"handle": "A", "pts": [[0, 0], [4, 0], [4, 4], [0, 4]]}],
        "set_arc": [{"handle": "D1", "c": [20, 0], "r": 2.5, "start_deg": 10, "end_deg": 100}],
    })
    assert uses_v2(canonical) is True
    assert canonical["removed"] == ["B", "C1"]
    assert canonical["removed_kinds"] == {"C1": "CIRCLE"}
    assert canonical["set_points"][0]["closed"] is True  # inherited from the entity
    plan = emit_plan(canonical, base_sha256="2" * 64, base_intake=_base()).decode()
    lines = plan.splitlines()
    assert lines[0] == "LEAF_MUTATION_PLAN|2"
    assert "REMOVE|B" in lines and "REMOVE|C1" in lines
    assert "RELAYER|A|Moved" in lines
    assert "SETPOINTS|A|1|0,0,1|0|0,0;4,0;4,4;0,4" in lines
    assert "SETARC|D1|20,0,0|2.5|10|100" in lines
    assert "ADDLINE|0|0,0,0|3,4,0" in lines
    assert "ADDCIRCLE|0|1,2,0|0.5" in lines
    assert "ADDARC|Panels|0,0,1|2|30|120" in lines
    assert "ADDOPEN|0|0,0,1|0|0,0;1,1;2,0" in lines
    assert plan_sha256(plan.encode()) != plan_sha256(b"")


def test_set_points_on_a_line_shaped_polyline_lowers_through_the_up_plane():
    canonical = validate_mutations(_base(), {
        "set_points": [{"handle": "B", "pts": [[5, 5, 2], [9, 9, 2]]}],
    })
    plan = emit_plan(canonical, base_sha256="2" * 64, base_intake=_base()).decode()
    assert "SETPOINTS|B|0|0,0,1|2|5,5;9,9" in plan.splitlines()


@pytest.mark.parametrize("bad,needle", [
    ({"set_layer": [{"handle": "FF", "layer": "X"}]}, "unknown set_layer handle"),
    ({"set_layer": [{"handle": "A", "layer": "Panels"}]}, "no-op"),
    ({"set_layer": [{"handle": "A", "layer": "X"}, {"handle": "A", "layer": "Y"}]}, "duplicate set_layer"),
    ({"set_layer": [{"handle": "A", "layer": "X", "extra": 1}]}, "unknown or missing"),
    ({"removed": ["A"], "set_points": [{"handle": "A", "pts": [[0, 0], [1, 1], [2, 2]]}]}, "removed and replaced"),
    ({"set_points": [{"handle": "C1", "pts": [[0, 0], [1, 1]]}]}, "is a CIRCLE"),
    ({"set_points": [{"handle": "A", "pts": [[0, 0], [1, 1]]}]}, "invalid point count"),
    ({"set_points": [{"handle": "A", "pts": [[0, 0], [1, 1], [2, 0]]}, {"handle": "A", "pts": [[0, 0], [1, 1], [3, 0]]}]}, "more than one geometry"),
    ({"transforms": [{"handle": "A", "dx": 1, "dy": 0}], "set_points": [{"handle": "A", "pts": [[0, 0], [1, 1], [2, 0]]}]}, "more than one geometry"),
    ({"transforms": [{"handle": "C1", "dx": 1, "dy": 0}]}, "is not a polyline"),
    ({"set_circle": [{"handle": "D1", "c": [0, 0], "r": 1}]}, "is a ARC"),
    ({"set_circle": [{"handle": "C1", "c": [0, 0], "r": 0}]}, "must be positive"),
    ({"set_circle": [{"handle": "C1", "c": [0, float("nan")], "r": 1}]}, "must be finite"),
    ({"set_arc": [{"handle": "D1", "c": [0, 0], "r": 1, "start_deg": 10, "end_deg": 370}]}, "no sweep"),
    ({"added": [{"handle": "n", "kind": "CIRCLE", "layer": "0", "c": [0, 0], "r": -1}]}, "must be positive"),
    ({"added": [{"handle": "n", "kind": "LINE", "layer": "0", "pts": [[1, 1], [1, 1]]}]}, "zero length"),
    ({"added": [{"handle": "n", "kind": "LINE", "layer": "0", "pts": [[1, 1], [2, 2], [3, 3]]}]}, "invalid point count"),
    ({"added": [{"handle": "n", "kind": "ARC", "layer": "0", "c": [0, 0], "r": 1, "start_deg": 0, "end_deg": 360}]}, "no sweep"),
    ({"added": [{"handle": "n", "kind": "CIRCLE", "layer": "0", "c": [0, 0], "r": 1, "pts": [[0, 0]]}]}, "unknown fields"),
    ({"added": [{"handle": "n", "kind": "BLOB", "layer": "0"}]}, "unsupported kind"),
    ({"added": [{"handle": "n", "layer": "0", "closed": False, "pts": [[0, 0]]}]}, "invalid point count"),
    ({"added": [{"handle": "A", "kind": "CIRCLE", "layer": "0", "c": [0, 0], "r": 1}]}, "conflicting added handle"),
    ({"set_circle": [{"handle": "E1", "c": [0, 0], "r": 1}]}, "tilted"),
    # kimi on #1012: a replacement naming the current geometry is a no-op,
    # the same rule set_layer and set_points already apply.
    ({"set_circle": [{"handle": "C1", "c": [10, 10], "r": 3}]}, "no-op"),
    ({"set_arc": [{"handle": "D1", "c": [20, 0, 0], "r": 2, "start_deg": 0, "end_deg": 90}]}, "no-op"),
])
def test_v2_refusals_name_the_fault(bad, needle):
    base = _base()
    base["circles"].append(_circle("E1", nrm=(0.0, 0.7071, 0.7071)))
    with pytest.raises(ValueError, match=needle):
        validate_mutations(base, bad)


def test_ambiguous_handles_across_kinds_are_unusable():
    base = _base()
    base["arcs"].append({**_arc("C1")})  # the circle's handle reused by an arc
    with pytest.raises(ValueError, match="unknown removed handle"):
        validate_mutations(base, {"removed": ["C1"]})


# --- the mock writer --------------------------------------------------------

def test_apply_mutations_applies_every_v2_op_to_the_intake():
    canonical = validate_mutations(_base(), {
        "added": [
            {"handle": "n1", "kind": "LINE", "layer": "New", "pts": [[0, 0], [3, 4]]},
            {"handle": "n2", "kind": "CIRCLE", "layer": "0", "c": [1, 2], "r": 0.5},
            {"handle": "n3", "kind": "ARC", "layer": "0", "c": [0, 0], "r": 2, "start_deg": 30, "end_deg": 120},
        ],
        "removed": ["B"],
        "set_layer": [{"handle": "C1", "layer": "Moved"}],
        "set_circle": [{"handle": "C1", "c": [11, 11], "r": 4}],
        "set_points": [{"handle": "A", "closed": False, "pts": [[0, 0], [4, 0], [4, 4]]}],
        "set_arc": [{"handle": "D1", "c": [20, 0], "r": 2.5, "start_deg": 10, "end_deg": 100}],
    })
    out = write_loop.apply_mutations(_base(), canonical)
    by = {e["handle"]: e for field in ("polylines", "circles", "arcs") for e in out.get(field, [])}
    assert "B" not in by
    assert by["A"]["pts"] == [[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [4.0, 4.0, 0.0]] and by["A"]["closed"] is False
    assert by["C1"] == {"handle": "C1", "layer": "Moved", "c": [11.0, 11.0, 0.0], "r": 4.0, "nrm": [0.0, 0.0, 1.0]}
    assert by["D1"]["r"] == 2.5 and by["D1"]["start_deg"] == 10.0 and by["D1"]["end_deg"] == 100.0
    assert by["n1"] == {"handle": "n1", "layer": "New", "closed": False,
                        "pts": [[0.0, 0.0, 0.0], [3.0, 4.0, 0.0]], "xdata": None}
    assert by["n2"]["r"] == 0.5 and by["n3"]["end_deg"] == 120.0
    assert "New" in out["layers"] and "Moved" in out["layers"]
    # The base is untouched.
    assert _base()["circles"][0]["r"] == 3.0


# --- effects verification ---------------------------------------------------

def _actual_after(canonical, *, drift=None):
    """What the same-WorkItem inspection would report: the mock writer's
    result at the extractor's precision, new handles assigned by AutoCAD."""
    expected = write_loop.apply_mutations(_base(), canonical)
    actual = copy.deepcopy(expected)
    counter = 0
    for field in ("polylines", "circles", "arcs"):
        for entity in actual.get(field, []):
            if entity["handle"].startswith("n"):
                counter += 1
                entity["handle"] = f"APS{counter}"
            for key in ("pts", "c"):
                if key in entity:
                    if key == "pts":
                        entity["pts"] = [[round(v, 3) for v in p] for p in entity["pts"]]
                    else:
                        entity["c"] = [round(v, 3) for v in entity["c"]]
    if drift:
        drift(actual)
    return actual


def test_verify_accepts_the_exact_v2_effects():
    canonical = validate_mutations(_base(), {
        "added": [{"handle": "n2", "kind": "CIRCLE", "layer": "0", "c": [1, 2], "r": 0.5},
                  {"handle": "n1", "kind": "LINE", "layer": "0", "pts": [[0, 0], [3, 4]]}],
        "removed": ["C1"],
        "set_arc": [{"handle": "D1", "c": [20, 0], "r": 2.5, "start_deg": 10, "end_deg": 100}],
        "set_layer": [{"handle": "A", "layer": "Moved"}],
    })
    write_loop.verify_live_mutation_effects(_base(), _actual_after(canonical), canonical)


@pytest.mark.parametrize("drift,needle", [
    (lambda a: a["circles"].append(_circle("C1")), "removed handle 'C1' remains"),
    (lambda a: a["arcs"][0].update(r=2.6), "replaced handle 'D1'"),
    (lambda a: a["arcs"][0].update(end_deg=101.0), "replaced handle 'D1'"),
    (lambda a: a["circles"].append(_circle("EXTRA")), "unexpected new entities"),
    (lambda a: a["circles"].pop(), "added CIRCLE 'n2' is missing"),
    (lambda a: a["polylines"][0].update(layer="Elsewhere"), "replaced handle 'A'"),
])
def test_verify_refuses_every_drift_from_the_v2_plan(drift, needle):
    canonical = validate_mutations(_base(), {
        "added": [{"handle": "n2", "kind": "CIRCLE", "layer": "0", "c": [1, 2], "r": 0.5}],
        "removed": ["C1"],
        "set_arc": [{"handle": "D1", "c": [20, 0], "r": 2.5, "start_deg": 10, "end_deg": 100}],
        "set_layer": [{"handle": "A", "layer": "Moved"}],
    })
    with pytest.raises(ValueError, match=needle):
        write_loop.verify_live_mutation_effects(_base(), _actual_after(canonical, drift=drift), canonical)


# --- the intake shape across the parsers and the synthesizer ---------------

def test_inspection_records_for_the_three_kinds_parse_into_the_intake():
    text = "\n".join([
        "LAYER|0",
        "LN|0|1.000,2.000,0.000|3.000,4.000,0.000|1A",
        "CI|0|10.000,10.000,0.000|3.000|0.000000,0.000000,1.000000|1B",
        "AR|Panels|20.000,0.000,0.000|2.000|0.000000|90.000000|0.000000,0.000000,1.000000|1C",
    ])
    intake = intake_parse.parse_text(text, "x.dwg")
    assert intake["polylines"] == [{"layer": "0", "closed": False, "pts": [[1.0, 2.0, 0.0], [3.0, 4.0, 0.0]], "xdata": None, "handle": "1A"}]
    assert intake["circles"] == [{"layer": "0", "c": [10.0, 10.0, 0.0], "r": 3.0, "nrm": [0.0, 0.0, 1.0], "handle": "1B"}]
    assert intake["arcs"] == [{"layer": "Panels", "c": [20.0, 0.0, 0.0], "r": 2.0, "start_deg": 0.0, "end_deg": 90.0, "nrm": [0.0, 0.0, 1.0], "handle": "1C"}]
    assert not intake.get("parseErrors")


def test_dxf_circle_and_arc_round_trip_through_the_synthesizer():
    dxf = "\n".join([
        "0", "SECTION", "2", "ENTITIES",
        "0", "CIRCLE", "5", "2A", "8", "Round", "10", "10.5", "20", "-2", "30", "0", "40", "3.25",
        "0", "ARC", "5", "2B", "8", "Round", "10", "0", "20", "0", "30", "1", "40", "2", "50", "15", "51", "200",
        "0", "CIRCLE", "8", "Round", "10", "1", "20", "1", "30", "0", "40", "0",
        "0", "LINE", "5", "2C", "8", "Round", "10", "0", "20", "0", "30", "0", "11", "5", "21", "5", "31", "0",
        "0", "ENDSEC", "0", "EOF",
    ]).encode()
    intake = dxf_intake.parse_dxf_bytes(dxf, source_name="round.dxf")
    assert intake["circles"] == [{"layer": "Round", "c": [10.5, -2.0, 0.0], "r": 3.25, "nrm": [0.0, 0.0, 1.0], "handle": "2A"}]
    assert intake["arcs"] == [{"layer": "Round", "c": [0.0, 0.0, 1.0], "r": 2.0, "nrm": [0.0, 0.0, 1.0], "handle": "2B", "start_deg": 15.0, "end_deg": 200.0}]
    assert [p["handle"] for p in intake["polylines"]] == ["2C"]
    again = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(intake), source_name="round.dxf")
    assert again["circles"] == intake["circles"] and again["arcs"] == intake["arcs"]
    assert again["polylines"] == intake["polylines"]
    assert json.dumps(again["layers"]) == json.dumps(intake["layers"])


def test_a_tilted_circle_keeps_its_world_centre_through_both_directions():
    tilted = {"layers": ["0"], "polylines": [], "circles": [
        {"handle": "3A", "layer": "0", "c": [5.0, 6.0, 7.0], "r": 1.0, "nrm": [0.0, 0.7071, 0.7071]}]}
    back = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(tilted), source_name="t.dxf")
    circle = back["circles"][0]
    assert circle["c"] == pytest.approx([5.0, 6.0, 7.0], abs=1e-6)
    assert circle["nrm"] == pytest.approx([0.0, 0.7071, 0.7071], abs=1e-9)


@pytest.mark.parametrize("bad,needle", [
    ({"layers": [], "polylines": [], "circles": [{"layer": "0", "c": [0, 0], "r": 0}]}, "r must be positive"),
    ({"layers": [], "polylines": [], "arcs": [{"layer": "0", "c": [0, 0], "r": 1}]}, "start_deg"),
    ({"layers": [], "polylines": [], "circles": [{"layer": "0", "c": [0], "r": 1}]}, "c must be"),
    ({"layers": [], "polylines": [], "circles": "no"}, "circles and arcs must be lists"),
])
def test_synthesizer_refuses_malformed_round_entities(bad, needle):
    with pytest.raises(intake_dxf.IntakeDxfError, match=needle):
        intake_dxf.intake_to_dxf(bad)
