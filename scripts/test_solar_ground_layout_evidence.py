"""Studio ground layout evidence (contract G20: a3, a4, a8, a9, a11) from a small synthetic intake.

Every intake is authored in this file: no capture, no network, so the collected count is
the same on every runner and nothing skips except the one CLI case that needs a git
executable to commit its intake. The licensed fixture numbers (119 rows, 34986 slots,
the shade limit angle, the minimum pitch) are computed in
server/tests/test_solar_ground_layout.py from the committed terrain intake.

Covered: every row kind this producer emits (setting with the absent-is-default rule,
tracker-row, setback-ring), their G17 shapes, G9 row order across types, G13 chaining
(each step reads Studio's own prior state, the layout reads the t7 grid), the G20
parameters (answers), the frozen comparator accepting every document against itself,
refusals, and the CLI.
"""
from __future__ import annotations

from copy import deepcopy
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


ev = _load("solar_ground_layout_evidence")
compare = ev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"

# The terrain producer's own small fixture shape: 1 x 4 m frames, a 3 x 8 m boundary and a
# 30 x 9 m plane rising 0.3 m per metre east as 3 x 3 m faces.
PRESET = {"Name": "Small", "ModuleLengthM": 2.0, "ModuleWidthM": 1.0, "Rows": 1, "Columns": 2,
          "HorizontalGapM": 0.0, "VerticalGapM": 0.0, "ColorIndex": 5, "PileTemplateName": "Two",
          "MaxSlopePercent": 80.0, "MaxCrossAxisSlopePct": 5.0,
          "Piling": {"MinPileLengthM": 3.0, "MaxPileLengthM": 6.0}}
TEMPLATE = {"Name": "Two", "HorizontalPoleCount": 1, "VerticalPoleCount": 2, "PileDiameterM": 0.2,
            "PileRevealM": 1.0, "PileEmbedmentM": 1.5}
BOUNDARY = [[0.0, 0.0], [3.0, 0.0], [3.0, 8.0], [0.0, 8.0]]
SITE = [[0.0, 0.0], [500.0, 0.0], [500.0, 300.0], [0.0, 300.0]]
MODULE_SETTINGS = ["TorqueTubeHeightM", "TrackerModuleAlongAxisM", "TrackerModuleCrossAxisM", "TrackerModuleGapM",
                   "TrackerModulePmaxW", "TrackerRailOverhangM", "TrackerTorqueTubeRadiusM"]


def terrain_intake():
    faces = []
    for i in range(10):
        for j in range(3):
            x0, x1, y0, y1 = 3.0 * i, 3.0 * i + 3.0, 3.0 * j, 3.0 * j + 3.0
            faces.append([[x0, y0, 0.3 * x0], [x1, y0, 0.3 * x1], [x1, y1, 0.3 * x1], [x0, y1, 0.3 * x0]])
    return {"units": "m", "boundary": deepcopy(BOUNDARY), "active_preset": deepcopy(PRESET),
            "pile_template": deepcopy(TEMPLATE), "terrain_faces": faces}


@pytest.fixture(scope="module")
def docs():
    return ev.run_layout(terrain_intake(), REVISION)


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def ids(doc):
    return [r["id"]["entity_id"] for r in rows_of(doc)]


def settings_of(doc):
    return {r["name"]: r["value"] for r in rows_of(doc, "setting")}


# ------------------------------------------------------------- the steps --

def test_every_layout_step_in_scenario_order(docs):
    assert list(docs) == ["a3", "a4", "a8", "a9", "a11"]
    assert [d["provenance"]["capability"] for d in docs.values()] == [
        "tracker-module-spec", "row-spacing-calculator", "tracker-layout", "sat-layout-from-gcr", "setback-boundary"]
    assert all(d["after"]["source_revision"] == step and d["after"]["format"] == "ground-v1"
               for step, d in docs.items())


def test_a3_setting_rows_are_the_module_leafmodule_saves(docs):
    doc = docs["a3"]
    assert ids(doc) == [f"setting-{name}" for name in MODULE_SETTINGS]
    values = settings_of(doc)
    assert values["TorqueTubeHeightM"] == {"kind": "length", "value": 1.5, "unit": "m"}
    assert values["TrackerModuleCrossAxisM"] == {"kind": "length", "value": 2.1, "unit": "m"}
    assert values["TrackerModulePmaxW"] == 400.0      # not M or Deg: the exact scalar
    # Written at their declared defaults and absent before: not changes.
    assert "TrackerCorridorGapM" not in values and "HomerunRouting" not in values


def test_a4_saves_the_minimum_pitch_and_the_routing_catalog_grows(docs):
    values = settings_of(docs["a4"])
    assert sorted(values) == ["HomerunRouting", "LeafSpacingMinPitchM"]
    assert values["LeafSpacingMinPitchM"]["value"] == pytest.approx(4.2279994425599625, abs=1e-12)
    routing = json.loads(values["HomerunRouting"])
    assert len(routing["CableCatalog"]) == 4
    assert values["HomerunRouting"] == json.dumps(routing, sort_keys=True, separators=(",", ":"))


def test_a8_tracker_row_fields_and_shapes(docs):
    rows = rows_of(docs["a8"])
    assert [r["type"] for r in rows] == ["tracker-row"]
    row = rows[0]
    assert row["id"] == {"entity_id": "tracker-row-1"}
    assert (row["block"], row["source_command"], row["tracker_model"]) == ("LEAFSAT", "LEAFTRACK", "single_axis_tracker")
    assert (row["row_index"], row["slots"]) == (0, 7)
    assert row["insert"] == {"kind": "coordinate", "value": [2.1, pytest.approx(4.0, abs=1e-12)], "unit": "m"}
    assert row["rotation"] == {"kind": "angle", "value": pytest.approx(90.0, abs=1e-12), "unit": "deg"}
    assert row["scale_x"]["value"] == pytest.approx(7 * 1.02 + 0.1, abs=1e-9)
    assert row["scale_y"] == {"kind": "length", "value": 2.1, "unit": "m"}
    assert row["cross_axis_width"] == {"kind": "length", "value": 2.1, "unit": "m"}
    assert row["axis_start"]["value"][1] == pytest.approx(4.0 - 3.57 - 0.05, abs=1e-9)
    assert row["axis_end"]["value"][1] == pytest.approx(4.0 + 3.57 + 0.05, abs=1e-9)


def test_a9_rows_are_sorted_by_type_then_id(docs):
    doc = docs["a9"]
    assert ids(doc) == ["setting-HomerunRouting", "setting-ShadeLimitAngleDeg", "tracker-row-1"]
    values = settings_of(doc)
    assert values["ShadeLimitAngleDeg"]["unit"] == "deg"
    assert values["ShadeLimitAngleDeg"]["value"] == pytest.approx(29.780934315714596, abs=1e-12)
    assert len(json.loads(values["HomerunRouting"])["CableCatalog"]) == 6
    row = rows_of(doc, "tracker-row")[0]
    assert row["source_command"] == "LEAFSAT"
    assert row["insert"]["value"][0] == pytest.approx(4.2279994425599625 / 2, abs=1e-12)


def test_a11_on_a_boundary_too_small_for_5_m_commits_nothing(docs):
    assert rows_of(docs["a11"]) == []


def test_a11_setback_ring_on_the_site_boundary():
    rows = ev.step_rows("a11", {}, {"boundary": deepcopy(SITE)})
    assert len(rows) == 1
    ring = rows[0]
    assert (ring["id"], ring["type"], ring["kind"]) == ("setback-ring-1", "setback-ring", "array")
    assert [v["value"] for v in ring["vertices"]] == [[5.0, 5.0], [495.0, 5.0], [495.0, 295.0], [5.0, 295.0]]
    assert ring["distance"] == {"kind": "length", "value": 5.0, "unit": "m"}


def test_tracker_rows_order_by_row_index_then_slots():
    placements = [{"block": "LEAFSAT", "insert": (x, 1.0, 0.0), "rotation_rad": 0.0, "scale": (1.0, 2.1, 1.0),
                   "row_index": ri, "slots": s, "axis_start": (x, 0.0), "axis_end": (x, 2.0),
                   "cross_axis_width_du": 2.1, "source_command": "LEAFTRACK", "tracker_model": "single_axis_tracker"}
                  for x, ri, s in ((3.0, 1, 5), (1.0, 0, 9), (2.0, 0, 4))]
    rows = ev.tracker_rows(placements)
    assert [(r["id"], r["row_index"], r["slots"]) for r in rows] == [
        ("tracker-row-1", 0, 4), ("tracker-row-2", 0, 9), ("tracker-row-3", 1, 5)]


# ------------------------------------------------------------- chaining --

def test_the_layout_reads_studios_own_prior_state():
    intake = terrain_intake()
    state = {"grid": None}
    ev.step_rows("a3", state, intake)
    # Without a4's saved minimum pitch LEAFTRACK offers 6 m: the first row centre (3 m)
    # is not inside the 3 m wide boundary, so nothing is drawn.
    assert ev.step_rows("a8", state, intake) == []
    ev.step_rows("a4", state, intake)
    assert len(ev.step_rows("a8", state, intake)) == 1
    assert len(state["tracker_rows"]) == 1


def test_one_step_equals_the_same_step_of_a_full_run(docs):
    only = ev.run_layout(terrain_intake(), REVISION, only="a8")
    assert list(only) == ["a8"] and only["a8"] == docs["a8"]


# ------------------------------------------------------ document contract --

def test_document_fields_parameters_and_hashes(docs):
    intake = terrain_intake()
    for step, doc in docs.items():
        assert set(doc) == compare.EVIDENCE_KEYS
        assert doc["fixture_sha256"] == compare.semantic_hash(intake)
        assert doc["parameters"] == {"units_keyword": "Meters", "grid_cells_long_axis": 150,
                                     "active_preset": "Small", "answers": ev.STEP_ANSWERS[step]}
        assert doc["input_sha256"] == compare.semantic_hash(
            {"fixture_sha256": doc["fixture_sha256"], "parameters": doc["parameters"]})
        assert doc["output_sha256"] == compare.semantic_hash(doc["after"])
        assert doc["entity_mapping"] == {i: i for i in ids(doc)}
        assert (doc["revision"], doc["state"], doc["survived_reopen"]) == (REVISION, "committed", True)
        assert doc["versions"]["capability"] == "0"
    assert docs["a11"]["parameters"]["answers"] == ["Array", "LEAF-BOUNDARY", 5]


def test_comparator_accepts_every_document_against_itself(docs):
    for step, doc in docs.items():
        result = compare.compare(doc, doc, "exports", capability=ev.LAYOUT_STEPS[step][0])
        assert result["verdict"] == "pass", result["diffs"]


def test_comparator_reports_a_moved_tracker_row(docs):
    moved = deepcopy(docs["a8"])
    moved["after"]["rows"][0]["insert"]["value"][0] += 0.01
    moved["output_sha256"] = compare.semantic_hash(moved["after"])
    result = compare.compare(docs["a8"], moved, "exports", capability="tracker-layout")
    assert result["verdict"] == "fail"
    assert result["diffs"] == ["after/rows/0/insert: quantity differs"]


# ------------------------------------------------------------ refusals --

def test_unknown_step_bad_revision_and_bad_intake_are_refused():
    with pytest.raises(ev.EvidenceError, match="not a layout step"):
        ev.step_rows("a5", {}, terrain_intake())
    with pytest.raises(ev.EvidenceError, match="not a layout step"):
        ev.run_layout(terrain_intake(), REVISION, only="t1")
    with pytest.raises(ev.EvidenceError, match="revision"):
        ev.build_document(terrain_intake(), "a3", [], "HEAD")
    with pytest.raises(ev.EvidenceError):
        ev.run_layout(dict(terrain_intake(), boundary=BOUNDARY[:2]), REVISION)
    with pytest.raises(ev.EvidenceError, match="refused"):
        ev.step_rows("a8", {}, {"boundary": [[0, 0], [1, 0], [float("nan"), 1]]})


# ------------------------------------------------------------------ CLI --

def test_cli_refuses_an_untracked_intake(tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(terrain_intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert ev.main(["--intake", str(intake), "--out-dir", str(out)]) == 2
    assert not out.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs a git executable to commit the intake")
def test_cli_writes_one_document_per_layout_step_from_a_committed_intake(tmp_path):
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
    out = tmp_path / "out"
    assert ev.main(["--intake", str(intake), "--out-dir", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{s}.json" for s in ("a3", "a4", "a8", "a9", "a11"))
    a8 = json.loads((out / "a8.json").read_text(encoding="utf-8"))
    assert len(a8["revision"]) == 40 and a8["fixture_sha256"] == compare.semantic_hash(terrain_intake())
    assert ev.main(["--intake", str(intake), "--out-dir", str(tmp_path / "one"), "--step", "a9"]) == 0
    assert [p.name for p in (tmp_path / "one").iterdir()] == ["a9.json"]
