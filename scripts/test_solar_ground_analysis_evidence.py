"""Studio G20 evidence for a1, a2, a10 and a12 (slope map, terrain CSV, pad grading,
survey colours).

Synthetic intakes are authored here (the same small terrain the ground producer's own
suite uses); the terrain fixture (docs/parity/evidence/ground/terrain/intake.json,
inputs only) is run once through the whole chain to prove the rows reproduce the
licensed outputs: 13227 green and 34 yellow slope cells, the a2 CSV file byte for byte
(its SHA-256 only; the file stays private), the balanced grade 0.26064200685635874 m
and 700 green survey faces. Nothing skips except the CLI case that needs a git
executable to commit its intake.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = _load("solar_ground_analysis_evidence")
compare = ev.compare
analysis = ev.analysis
REVISION = "0123456789abcdef0123456789abcdef01234567"
FIXTURE = Path(__file__).resolve().parent.parent / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"
A2_CSV_SHA256 = "1d72bde87f122458fba3fc8549d43873d6dcd879910b01d0c57a348f0c0b0f16"
GREEN, YELLOW, RED = 0x00C800, 0xFFC800, 0xDC0000

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
def rows():
    return ev.run_steps(terrain_intake())


@pytest.fixture(scope="module")
def fixture_rows():
    return ev.run_steps(json.loads(FIXTURE.read_text(encoding="utf-8")))


def one(rows_, row_type):
    [row] = [r for r in rows_ if r["type"] == row_type]
    return row


def values(points):
    return [p["value"] for p in points]


# ------------------------------------------------------------ synthetic rows --

def test_every_owned_step_emits_its_g20_rows(rows):
    assert list(rows) == ["a1", "a2", "a10", "a12"]
    assert [r["type"] for r in rows["a1"]] == ["slope-map"]
    assert [r["type"] for r in rows["a2"]] == ["file"]
    assert [r["type"] for r in rows["a10"]] == ["grade-pad", "setting"]
    assert [r["type"] for r in rows["a12"]] == ["survey-colors"]
    for step_rows in rows.values():
        for row in step_rows:
            assert (row["quantity"], row["unit"]) == (1, "each")


def test_a1_slope_map_is_one_row_over_studios_own_grid(rows):
    row = one(rows["a1"], "slope-map")
    state = ev.terrain_state(terrain_intake())
    grid = state["grid"]
    assert row["id"] == "slope-map-1"
    assert (row["rows"], row["cols"]) == (grid["rows"], grid["cols"]) == (45, 150)
    assert row["extent"]["min"]["value"] == [grid["x_min"], grid["y_min"]]
    assert row["extent"]["max"]["value"] == [grid["x_max"], grid["y_max"]]
    expected = [c["color_index"] for c in analysis.compute_slope_cells(grid, 1.0)]
    assert row["cell_colors"] == expected and len(expected) == 44 * 149
    assert set(expected) <= {1, 2, 3}


def test_a2_file_row_is_the_csv_text_normalized(rows):
    row = one(rows["a2"], "file")
    assert (row["id"], row["role"]) == ("file-1", "terrain-csv")
    assert "\r" not in row["text"] and not row["text"].startswith("﻿")
    lines = row["text"].split("\n")
    assert lines[0] == "X,Y,Z" and lines[-1] == ""
    assert row["lines"] == 45 * 150 + 1 == len(lines) - 1
    grid = ev.terrain_state(terrain_intake())["grid"]
    raw = analysis.terrain_csv_file_bytes(grid, 1.0)
    assert raw == analysis.UTF8_BOM + row["text"].replace("\n", "\r\n").encode("utf-8")


def test_a10_grade_pad_and_the_grading_setting(rows):
    pad = one(rows["a10"], "grade-pad")
    setting = one(rows["a10"], "setting")
    assert pad["id"] == "grade-pad-1"
    assert values(pad["boundary"]) == BOUNDARY
    elevation = pad["elevation"]
    assert elevation["kind"] == "length" and elevation["unit"] == "m"
    assert pad["label"] == "GRADE PAD\\P" + analysis.format_fixed(elevation["value"], 2) + " m"
    assert pad["label_at"]["value"] == [1.5, 4.0]
    assert setting == {"id": "setting-GradingElevationM", "type": "setting", "quantity": 1, "unit": "each",
                       "name": "GradingElevationM", "value": elevation}


def test_a12_survey_colours_follow_the_intake_face_order(rows):
    row = one(rows["a12"], "survey-colors")
    assert row["id"] == "survey-colors-1"
    # Every 3 m face rises 0.9 m eastward: 30 %, red.
    assert row["colors"] == [RED] * 30


def test_steps_update_studios_state():
    intake = terrain_intake()
    state = ev.terrain_state(intake)
    assert state["settings"] == {} and state["terrain_face_colors"] is None
    ev.step_rows("a10", state, intake)
    assert set(state["settings"]) == {"GradingElevationM", "GradingMode"}
    # Grading again at the same elevation changes no setting.
    assert [r["type"] for r in ev.step_rows("a10", state, intake)] == ["grade-pad"]
    ev.step_rows("a12", state, intake)
    assert state["terrain_face_colors"] == [RED] * 30


def test_setting_rows_read_absent_as_the_declared_default():
    assert ev.setting_rows({}, {"GradingElevationM": 0.0, "GradingMode": 0}) == []
    [row] = ev.setting_rows({}, {"GradingElevationM": 1.25, "GradingMode": 0})
    assert row["value"] == {"kind": "length", "value": 1.25, "unit": "m"}
    [row] = ev.setting_rows({"GradingMode": 0}, {"GradingMode": 1})
    assert (row["id"], row["value"]) == ("setting-GradingMode", 1)
    with pytest.raises(ev.EvidenceError):
        ev.setting_rows({}, {"SomethingElse": 1})


def test_setting_values_by_name_suffix():
    assert ev.setting_value("ShadeLimitAngleDeg", 29.5) == {"kind": "angle", "value": 29.5, "unit": "deg"}
    assert ev.setting_value("PitchM", 4) == {"kind": "length", "value": 4.0, "unit": "m"}
    assert ev.setting_value("Mode", "Auto") == "Auto"
    assert ev.setting_value("Nested", {"b": 1, "a": [2]}) == '{"a":[2],"b":1}'


def test_steps_without_terrain_draw_nothing():
    state = {"grid": None, "settings": {}, "terrain_face_colors": None}
    intake = terrain_intake()
    assert ev.step_rows("a1", state, intake) == []
    assert ev.step_rows("a2", state, intake) == []
    # With no terrain LEAFGRADE still draws the pad at the default 0 m, which is no
    # setting change.
    assert [r["type"] for r in ev.step_rows("a10", state, intake)] == ["grade-pad"]


def test_unknown_steps_and_malformed_intakes_refuse():
    with pytest.raises(ev.EvidenceError):
        ev.step_rows("a3", {}, terrain_intake())
    with pytest.raises(ev.EvidenceError):
        ev.run_steps(terrain_intake(), only="t1")
    bad = terrain_intake()
    bad["terrain_faces"][0] = [[0.0, 0.0, 0.0]]
    with pytest.raises(ev.EvidenceError):
        ev.run_steps(bad)
    grid_state = {"grid": {"elevations": [0.0] * 4, "rows": 2, "cols": 2, "x_min": 0.0, "x_max": 1.0,
                           "y_min": 0.0, "y_max": float("nan")}, "settings": {}}
    with pytest.raises(ev.EvidenceError):
        ev.step_rows("a1", grid_state, terrain_intake())


# --------------------------------------------------------------- documents --

def small_state():
    return {"grid": {"elevations": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "rows": 2, "cols": 3, "x_min": 0.0,
                     "x_max": 200.0, "y_min": 0.0, "y_max": 100.0},
            "settings": {}, "terrain_face_colors": None}


def test_documents_carry_the_producer_envelope_and_self_compare(rows):
    intake = terrain_intake()
    for step_id in ("a1", "a10", "a12"):
        doc = ev.build_document(intake, step_id, rows[step_id], REVISION)
        assert doc["parameters"] == {"units_keyword": "Meters", "grid_cells_long_axis": 150,
                                     "active_preset": "Small", "answers": ev.STEP_ANSWERS[step_id]}
        assert doc["after"]["source_revision"] == step_id and doc["after"]["format"] == "ground-v1"
        assert doc["provenance"]["step"] == step_id and doc["provenance"]["side"] == "studio"
        assert doc["fixture_sha256"] == compare.semantic_hash(intake)
        assert set(doc["entity_mapping"]) == {r["id"]["entity_id"] for r in doc["after"]["rows"]}
        capability = doc["provenance"]["capability"]
        assert compare.compare(doc, doc, "exports", capability=capability)["verdict"] == "pass"


def test_rows_are_emitted_in_type_then_number_order(rows):
    doc = ev.build_document(terrain_intake(), "a10", list(reversed(rows["a10"])), REVISION)
    assert [r["id"]["entity_id"] for r in doc["after"]["rows"]] == ["grade-pad-1", "setting-GradingElevationM"]


def test_a_small_csv_builds_a_file_document():
    state = small_state()
    rows_ = ev.step_rows("a2", state, terrain_intake())
    assert rows_[0]["text"] == ("X,Y,Z\n0.000,0.000,1.000\n100.000,0.000,2.000\n200.000,0.000,3.000\n"
                                "0.000,100.000,4.000\n100.000,100.000,5.000\n200.000,100.000,6.000\n")
    assert rows_[0]["lines"] == 7
    doc = ev.build_document(terrain_intake(), "a2", rows_, REVISION)
    assert compare.compare(doc, doc, "exports", capability="terrain-csv-export")["verdict"] == "pass"


def test_a_csv_over_the_comparator_string_bound_is_refused_by_name(rows):
    assert len(one(rows["a2"], "file")["text"]) > ev.COMPARATOR_MAX_STRING
    with pytest.raises(ev.EvidenceError, match="16384"):
        ev.build_document(terrain_intake(), "a2", rows["a2"], REVISION)
    docs = ev.run_documents(terrain_intake(), REVISION)
    assert isinstance(docs["a2"], ev.EvidenceError)
    assert all(isinstance(docs[s], dict) for s in ("a1", "a10", "a12"))


def test_build_document_refuses_a_bad_revision(rows):
    with pytest.raises(ev.EvidenceError):
        ev.build_document(terrain_intake(), "a1", rows["a1"], "HEAD")


# --------------------------------------------------------------------- CLI --

def test_cli_refuses_an_untracked_intake(tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(terrain_intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert ev.main(["--intake", str(intake), "--out-dir", str(out)]) == 2
    assert not out.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs a git executable to commit the intake")
def test_cli_writes_the_documents_it_can_and_names_the_refusal(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    intake = repo / "intake.json"
    intake.write_text(json.dumps(terrain_intake()), encoding="utf-8")

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True,
                       timeout=30)
    git("init", "-q")
    git("add", "intake.json")
    git("commit", "-q", "--no-verify", "-m", "intake")
    one_step = tmp_path / "one"
    assert ev.main(["--intake", str(intake), "--out-dir", str(one_step), "--step", "a1"]) == 0
    assert [p.name for p in one_step.iterdir()] == ["a1.json"]
    a1 = json.loads((one_step / "a1.json").read_text(encoding="utf-8"))
    assert len(a1["revision"]) == 40 and a1["fixture_sha256"] == compare.semantic_hash(terrain_intake())
    out = tmp_path / "out"
    assert ev.main(["--intake", str(intake), "--out-dir", str(out)]) == 2
    assert sorted(p.name for p in out.iterdir()) == ["a1.json", "a10.json", "a12.json"]
    assert "16384" in capsys.readouterr().err


# ---------------------------------------------------- the terrain fixture --

def test_fixture_a1_reproduces_the_captured_slope_counts(fixture_rows):
    row = one(fixture_rows["a1"], "slope-map")
    assert (row["rows"], row["cols"]) == (90, 150)
    colors = row["cell_colors"]
    assert (colors.count(3), colors.count(2), colors.count(1), len(colors)) == (13227, 34, 0, 13261)


def test_fixture_a2_reproduces_the_licensed_csv_byte_for_byte(fixture_rows):
    row = one(fixture_rows["a2"], "file")
    assert row["lines"] == 13501
    raw = analysis.UTF8_BOM + row["text"].replace("\n", "\r\n").encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == A2_CSV_SHA256


def test_fixture_a10_reproduces_the_balanced_grade(fixture_rows):
    pad = one(fixture_rows["a10"], "grade-pad")
    assert pad["elevation"]["value"] == pytest.approx(0.26064200685635874, abs=1e-9)
    assert pad["label"] == "GRADE PAD\\P0.26 m"
    assert pad["label_at"]["value"] == [250.0, 150.0]
    assert values(pad["boundary"]) == [[0.0, 0.0], [500.0, 0.0], [500.0, 300.0], [0.0, 300.0]]
    setting = one(fixture_rows["a10"], "setting")
    assert setting["name"] == "GradingElevationM" and setting["value"] == pad["elevation"]


def test_fixture_a12_colours_all_700_faces_green(fixture_rows):
    assert one(fixture_rows["a12"], "survey-colors")["colors"] == [GREEN] * 700


def test_fixture_documents_fit_the_comparator(fixture_rows):
    intake = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for step_id in ("a1", "a10", "a12"):
        doc = ev.build_document(intake, step_id, fixture_rows[step_id], REVISION)
        assert compare.compare(doc, doc, "exports", capability=doc["provenance"]["capability"])["verdict"] == "pass"
    with pytest.raises(ev.EvidenceError, match="16384"):
        ev.build_document(intake, "a2", fixture_rows["a2"], REVISION)
