"""Studio's G23 build-out evidence (b1, b2, b15, b16, b17).

A small synthetic terrain intake authored here runs the whole chain (t1 to t7, the a-steps
the b-steps read, then the five b-steps) so every row kind is built on every runner: the
b1 `file` and `report` rows and the read-only rule, `tube`, `grade-pad`, `road-line` and
`label` rows, the parameters' answers, the comparator accepting every document against
itself, and refusals. The committed terrain intake (inputs only) runs the chain once more
to prove the documents carry the licensed outputs: the BOM CSV (byte for byte, as the G20
normalized text) and its six reported values, 1434 tubes, the pad at 0.26 m, and the two
roads the capture drew.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bev = _load("solar_ground_buildout_evidence")
compare = bev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"
INTAKE = Path(__file__).resolve().parent.parent / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"
# The b1 CSV as the plugin wrote it, after the G20 normalization (no BOM, LF line ends).
LICENSED_BOM_TEXT = (
    "Category,Description,Unit,Quantity\n"
    "Module,PV Module — 2.100 m × 1.000 m portrait,ea,69678\n"
    "Torque Tube,\"Torque tube / tracker rail (1434 sections, 89811.6 m total)\",m,89811.55\n"
    "Torque Tube,Torque tube section count,ea,1434\n"
    "Pile,\"Ground pile / foundation (at 5.0 m c/c spacing, estimated)\",ea,19008\n"
    "Drive Unit,Single-axis tracker drive unit (one per table section),ea,1434\n"
    "DC Capacity,Total DC nameplate (69678 × 400 Wp),kWp,27871.20\n"
)

PRESET = {"Name": "Small", "ModuleLengthM": 2.0, "ModuleWidthM": 1.0, "Rows": 1, "Columns": 2,
          "HorizontalGapM": 0.0, "VerticalGapM": 0.0, "ColorIndex": 5, "PileTemplateName": "Two",
          "MaxSlopePercent": 80.0, "MaxCrossAxisSlopePct": 5.0,
          "Piling": {"MinPileLengthM": 3.0, "MaxPileLengthM": 6.0}}
TEMPLATE = {"Name": "Two", "HorizontalPoleCount": 1, "VerticalPoleCount": 2, "PileDiameterM": 0.2,
            "PileRevealM": 1.0, "PileEmbedmentM": 1.5}
BOUNDARY = [[0.0, 0.0], [3.0, 0.0], [3.0, 8.0], [0.0, 8.0]]


def terrain_intake():
    """A 30 x 9 m plane rising 0.3 m per metre east, as 3 x 3 m faces: a 45 x 150 grid."""
    faces = []
    for i in range(10):
        for j in range(3):
            x0, x1, y0, y1 = 3.0 * i, 3.0 * i + 3.0, 3.0 * j, 3.0 * j + 3.0
            faces.append([[x0, y0, 0.3 * x0], [x1, y0, 0.3 * x1], [x1, y1, 0.3 * x1], [x0, y1, 0.3 * x0]])
    return {"units": "m", "boundary": deepcopy(BOUNDARY), "active_preset": deepcopy(PRESET),
            "pile_template": deepcopy(TEMPLATE), "terrain_faces": faces}


@pytest.fixture(scope="module")
def run():
    return bev.run_steps(terrain_intake(), REVISION)


@pytest.fixture(scope="module")
def licensed():
    docs, _ = bev.run_steps(json.loads(INTAKE.read_text(encoding="utf-8")), REVISION)
    return docs


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def ids(rows):
    return [r["id"]["entity_id"] for r in rows]


def xy(point):
    return point["value"]


def one_tracker_state():
    """A state holding one 10 m frame polyline on the tracker layer and nothing else."""
    return bev.with_buildout_store({"grid": None, "frames": [
        {"layer": "LEAF-TRACKERS", "vertices": [[0.0, 0.0], [1.0, 0.0], [1.0, 10.0], [0.0, 10.0]]}]})


# ------------------------------------------------------------------ steps --

def test_every_step_in_order_with_its_capability_and_answers(run):
    docs, _ = run
    assert list(docs) == ["b1", "b2", "b15", "b16", "b17"]
    assert [d["provenance"]["capability"] for d in docs.values()] == \
        ["bill-of-materials", "torque-tube-3d-draw", "pad-grading-batch", "draw-road", "road-design"]
    assert [d["parameters"]["answers"] for d in docs.values()] == \
        [[], [], ["Auto", "select:LEAF-BOUNDARY"], ["20,150", "480,150"], ["250,20", "250,280"]]
    for doc in docs.values():
        assert set(doc["parameters"]) == {"units_keyword", "grid_cells_long_axis", "active_preset", "answers"}


def test_b1_file_and_report_rows_for_one_frame():
    state = one_tracker_state()
    rows = bev.step_rows("b1", state, {"boundary": []})
    assert [r["type"] for r in rows] == ["file"] + ["report"] * 5
    (file_row,) = [r for r in rows if r["type"] == "file"]
    text = "".join(file_row["chunks"])
    assert file_row["role"] == "bom-csv" and file_row["lines"] == 6 and "\r" not in text
    assert text.startswith("Category,Description,Unit,Quantity\nModule,PV Module — 2.100 m × 1.000 m")
    assert "Torque Tube,\"Torque tube / tracker rail (1 sections, 10.0 m total)\",m,10.00\n" in text
    report = {r["name"]: r["value"] for r in rows if r["type"] == "report"}
    assert report == {"tracker-sections": 1, "module-slots": 0, "estimated-piles": 2, "drives": 1,
                      "tube-length": {"kind": "length", "value": 10.0, "unit": "m"}}
    assert state["last_bom"].startswith(b"\xef\xbb\xbf")


def test_b1_flags_a_read_only_step_that_moved_the_state(monkeypatch):
    state = one_tracker_state()
    real = bev.bo.bom_command

    def mutating(entities, module, **kwargs):
        entities[0]["vertices"].append([0.0, 5.0])
        return real(entities, module, **kwargs)
    monkeypatch.setattr(bev.bo, "bom_command", mutating)
    rows = bev.step_rows("b1", state, {"boundary": []})
    assert rows[-1]["type"] == "unexpected-change"


def test_b1_with_no_tracker_rows_writes_no_file():
    state = bev.with_buildout_store({"grid": None, "frames": []})
    assert bev.step_rows("b1", state, {"boundary": []}) == []
    assert state["last_bom"] is None


def test_b2_tube_rows_carry_3d_boxes(run):
    docs, state = run
    tubes = rows_of(docs["b2"], "tube")
    assert len(tubes) == len(state["tubes"])
    assert ids(tubes) == [f"tube-{n}" for n in range(1, len(tubes) + 1)]
    for row in tubes:
        assert len(xy(row["bbox_min"])) == 3 and len(xy(row["bbox_max"])) == 3
        assert all(lo < hi for lo, hi in zip(xy(row["bbox_min"]), xy(row["bbox_max"])))


def test_b15_grades_the_boundary_pad(run):
    docs, state = run
    (pad,) = rows_of(docs["b15"], "grade-pad")
    assert [xy(p) for p in pad["boundary"]] == [[0.0, 0.0], [3.0, 0.0], [3.0, 8.0], [0.0, 8.0]]
    assert pad["label"].startswith("PAD 1\\P") and xy(pad["label_at"]) == [1.5, 4.0]
    assert state["grading_settings"]["GradingElevationM"] == pad["elevation"]["value"]


def test_b15_writes_a_setting_row_only_when_the_value_moves():
    state = bev.with_buildout_store({"grid": None, "frames": []})
    intake = {"boundary": deepcopy(BOUNDARY)}
    rows = bev.step_rows("b15", state, intake)
    assert [r["type"] for r in rows] == ["grade-pad"]          # Manual 0 with no terrain: the defaults
    state["grading_settings"] = {"GradingElevationM": 1.25, "GradingMode": 0}
    rows = bev.step_rows("b15", state, intake)
    (setting,) = [r for r in rows if r["type"] == "setting"]
    assert setting["id"] == "setting-GradingElevationM"
    assert setting["value"] == {"kind": "length", "value": 0.0, "unit": "m"}


def test_b16_and_b17_road_lines_and_label(run):
    docs, _ = run
    lines = rows_of(docs["b16"], "road-line")
    assert [r["role"] for r in lines] == ["offset", "edge", "centerline", "edge", "offset"]
    assert [xy(r["vertices"][0]) for r in lines] == [[20.0, 147.0], [20.0, 148.0], [20.0, 150.0], [20.0, 152.0],
                                                     [20.0, 153.0]]
    assert all(r["bulges"] == [0.0, 0.0] and r["closed"] is False for r in lines)
    lines = rows_of(docs["b17"], "road-line")
    assert [r["role"] for r in lines] == ["cross-section", "edge", "edge", "centerline", "edge", "edge"]
    assert [xy(r["vertices"][0])[0] for r in lines] == [246.0, 246.0, 247.0, 250.0, 253.0, 254.0]
    (label,) = rows_of(docs["b17"], "label")
    assert label["text"] == "CROSS-SECTION (1:10 V.E.)\\PRoad: 6.0 m  |  Shoulder: 1.0 m  |  Xslope: 2.0%"
    assert xy(label["at"]) == [250.0, 132.5]


# --------------------------------------------------------- documents --

def test_documents_validate_and_hash(run):
    docs, _ = run
    intake = terrain_intake()
    for doc in docs.values():
        compare.validate_evidence(doc, "exports")
        assert doc["fixture_sha256"] == compare.semantic_hash(intake)
        assert doc["input_sha256"] == compare.semantic_hash(
            {"fixture_sha256": doc["fixture_sha256"], "parameters": doc["parameters"]})
        assert doc["output_sha256"] == compare.semantic_hash(doc["after"])
        assert doc["after"]["format"] == "ground-v1" and doc["state"] == "committed"
        assert set(doc["entity_mapping"]) == set(ids(doc["after"]["rows"]))


def test_comparator_accepts_every_document_against_itself(run):
    docs, _ = run
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["versions"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_one_step_equals_the_same_step_of_a_full_run(run):
    docs, _ = run
    only, _ = bev.run_steps(terrain_intake(), REVISION, only="b16")
    assert list(only) == ["b16"] and only["b16"] == docs["b16"]


def test_refusals():
    state = bev.with_buildout_store({"grid": None, "frames": []})
    with pytest.raises(bev.EvidenceError):
        bev.step_rows("b3", state, terrain_intake())
    with pytest.raises(bev.EvidenceError):
        bev.run_steps(terrain_intake(), REVISION, only="a13")
    with pytest.raises(bev.EvidenceError):
        bev.step_rows("b1", {"frames": []}, terrain_intake())
    with pytest.raises(bev.EvidenceError, match="revision"):
        bev.build_document(terrain_intake(), "b16", [], "HEAD")
    with pytest.raises(bev.EvidenceError):
        bev._answer_point("20;150")
    with pytest.raises(bev.EvidenceError):
        bev.step_rows("b15", state, {"boundary": [[0.0, 0.0], [1.0, float("nan")], [1.0, 1.0]]})


def test_cli_refuses_an_untracked_intake(tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(terrain_intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert bev.main(["--intake", str(intake), "--out-dir", str(out)]) == 2
    assert not out.exists()


# ------------------------------------------- the licensed b-steps --

def test_b1_from_the_committed_intake_is_the_licensed_bom(licensed):
    rows = rows_of(licensed["b1"])
    (file_row,) = [r for r in rows if r["type"] == "file"]
    assert "".join(file_row["chunks"]) == LICENSED_BOM_TEXT and file_row["lines"] == 7
    report = {r["name"]: r["value"] for r in rows if r["type"] == "report"}
    assert report == {"tracker-sections": 1434, "module-slots": 69678,
                      "tube-length": {"kind": "length", "value": 89811.6, "unit": "m"},
                      "estimated-piles": 19008, "drives": 1434, "dc-capacity-kwp": 27871.2}
    assert not [r for r in rows if r["type"] == "unexpected-change"]


# Contract G25's report table: b1's report names, shapes and the values the plugin
# printed on the terrain capture. Test data only; the producer computes every value. The other steps of
# this slice print no report values.
G25 = {"b1": {"dc-capacity-kwp": ("float", 27871.2), "drives": ("int", 1434), "estimated-piles": ("int", 19008),
              "module-slots": ("int", 69678), "tracker-sections": ("int", 1434),
              "tube-length": ("quantity:length:m", 89811.6)}}


def _shape(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return f"quantity:{value['kind']}:{value['unit']}"
    return type(value).__name__


def test_report_rows_from_the_committed_intake_are_the_g25_table(licensed):
    for step in bev.STEP_IDS:
        rows = rows_of(licensed[step], "report")
        want = G25.get(step, {})
        assert ids(rows) == [f"report-{name}" for name in sorted(want)], step
        for row in rows:
            shape, value = want[row["name"]]
            assert _shape(row["value"]) == shape, row["name"]
            if isinstance(row["value"], dict):
                assert row["value"]["value"] == pytest.approx(value, abs=1e-3), row["name"]
            else:
                assert row["value"] == value, row["name"]


def test_b2_from_the_committed_intake_draws_1434_tubes(licensed):
    tubes = rows_of(licensed["b2"], "tube")
    assert len(tubes) == 1434
    centres_y = [(xy(r["bbox_min"])[1] + xy(r["bbox_max"])[1]) / 2 for r in tubes]
    assert centres_y == sorted(centres_y) or all(round(a, 6) <= round(b, 6) for a, b in zip(centres_y, centres_y[1:]))


def test_b15_from_the_committed_intake_is_the_captured_pad(licensed):
    rows = rows_of(licensed["b15"])
    assert [r["type"] for r in rows] == ["grade-pad"]          # a10 already stored this elevation
    (pad,) = rows
    assert pad["label"] == "PAD 1\\P0.26 m" and xy(pad["label_at"]) == [250.0, 150.0]
    assert pad["elevation"]["value"] == pytest.approx(0.26064200685635874, abs=1e-12)


def test_b16_and_b17_from_the_committed_intake(licensed):
    assert len(rows_of(licensed["b16"], "road-line")) == 5
    assert len(rows_of(licensed["b17"], "road-line")) == 6 and len(rows_of(licensed["b17"], "label")) == 1
