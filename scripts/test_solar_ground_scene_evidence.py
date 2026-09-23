"""Studio's G20 array and scene evidence (a5, a6, a7, a13).

A small synthetic terrain intake authored here runs the whole chain (t1 to t7, then the four
a-steps) so every row kind is covered on every runner: the a5 `array` row with every default,
a6's read-only rule (the arrays it reports, and `unexpected-change` when the state moves), the
a7 `file` rows with the G20 text normalization, the a13 `removed` row, the parameters' answers,
the comparator accepting every document against itself, and refusals. One case computes a7
from the committed terrain intake (Studio's own t1 grid, G13) and pins the plugin's files by
their normalized SHA-256 only.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
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


sev = _load("solar_ground_scene_evidence")
ev = sev.ev
compare = sev.compare
scene = sev.scene
REVISION = "0123456789abcdef0123456789abcdef01234567"
INTAKE = Path(__file__).resolve().parent.parent / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"
# SHA-256 of the plugin's a7 files after the G20 normalization (the files stay private).
A7_SHA256 = {"scene-dae": "e1d9e2b506c0a5d601b8028ce42d7ebbb1df99eee73b5eb7f6a2eeda1719715f",
             "scene-pvc": "7f25d1f405032a92b1a168ca4aa3e7ce854388caa4514cca1d8ac801030d293f"}
A7_LINES = {"scene-dae": 42, "scene-pvc": 159}

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
    return sev.run_steps(terrain_intake(), REVISION)


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def assert_g21_chunks(chunks):
    """G21: a non-empty list of strings, each at most 16,000 characters, every one but the
    last ending in a line feed."""
    assert isinstance(chunks, list) and chunks and all(isinstance(c, str) for c in chunks)
    assert all(len(c) <= 16000 for c in chunks)
    assert all(c.endswith("\n") for c in chunks[:-1])


def a5_state():
    """Studio's t1 state of the synthetic intake with the a5 array defined."""
    intake = terrain_intake()
    state = sev.terrain_state(intake, through="t1")
    sev.step_rows("a5", state, intake)
    return state, intake


# ------------------------------------------------------------------ steps --

def test_every_step_in_order_with_its_capability_and_answers(run):
    docs, _ = run
    assert list(docs) == ["a5", "a6", "a7", "a13"]
    assert [d["provenance"]["capability"] for d in docs.values()] == \
        ["define-array", "list-arrays", "export-pvsyst-scene", "delete-array"]
    assert [d["parameters"]["answers"] for d in docs.values()] == [["250", "250"], [], [], ["array_0"]]
    for doc in docs.values():
        assert set(doc["parameters"]) == {"units_keyword", "grid_cells_long_axis", "active_preset", "answers"}


def test_a5_array_row_carries_every_default(run):
    docs, _ = run
    (row,) = rows_of(docs["a5"])
    assert row["id"] == {"entity_id": "array-1"} and row["type"] == "array" and row["key"] == "array_0"
    assert row["centre"] == {"kind": "coordinate", "value": [250.0, 250.0], "unit": "m"}
    assert (row["modules_x"], row["modules_y"], row["orientation"]) == (100, 85, 0)
    assert row["module_w"]["value"] == 0.992 and row["module_h"]["value"] == 1.64
    assert row["spacing_x"]["value"] == 0.02 and row["spacing_y"]["value"] == 0.02
    assert row["tilt"] == {"kind": "angle", "value": 15.0, "unit": "deg"}
    assert row["azimuth"] == {"kind": "angle", "value": 0.0, "unit": "deg"}
    assert row["size_x"]["value"] == pytest.approx(165.98) and row["size_y"]["value"] == pytest.approx(86.0)
    outline = [v for p in row["outline"] for v in p["value"]]
    assert outline == pytest.approx([167.01, 207.0, 332.99, 207.0, 332.99, 293.0, 167.01, 293.0])


def test_a6_reports_the_array_and_changes_nothing(run):
    docs, _ = run
    assert rows_of(docs["a6"]) == rows_of(docs["a5"])
    assert not rows_of(docs["a6"], "unexpected-change")


def test_a6_flags_a_read_only_step_that_moved_the_state(monkeypatch):
    state, intake = a5_state()
    real = scene.list_arrays

    def mutating(records):
        records.append(dict(records[0], key="array_7"))
        return real(records[:1])
    monkeypatch.setattr(scene, "list_arrays", mutating)
    rows = sev.step_rows("a6", state, intake)
    assert [r["type"] for r in rows] == ["array", "unexpected-change"]


def test_a7_writes_one_file_row_per_role_normalized(run):
    docs, _ = run
    dae, pvc = rows_of(docs["a7"], "file")
    assert (dae["id"], dae["role"], pvc["id"], pvc["role"]) == \
        ({"entity_id": "file-1"}, "scene-dae", {"entity_id": "file-2"}, "scene-pvc")
    for row in (dae, pvc):
        assert "text" not in row
        assert_g21_chunks(row["chunks"])
        text = "".join(row["chunks"])
        assert "\r" not in text and not text.startswith("﻿")
        assert row["lines"] == text.count("\n") + 1
    dae_text, pvc_text = "".join(dae["chunks"]), "".join(pvc["chunks"])
    assert "<created></created>" in dae_text and "<modified></modified>" in dae_text
    assert '<geometry id="Frame0">' in pvc_text and '<geometry id="ground">' in dae_text


def test_a13_removes_the_array_and_its_outline(run):
    docs, state = run
    assert [(r["type"], r["of"], r["count"]) for r in rows_of(docs["a13"])] == [("removed", "array", 1)]
    assert state["arrays"] == [] and state["array_outlines"] == []


def test_deleting_an_unknown_key_changes_nothing():
    state, _ = a5_state()
    before = deepcopy(state["arrays"])
    assert sev.step_delete(state, ("array_9",)) == []
    assert state["arrays"] == before


def test_a7_without_a_terrain_grid_writes_no_file():
    state = sev.with_array_store(ev.new_state())
    assert sev.step_rows("a7", state, terrain_intake()) == []


# --------------------------------------------------------- file rows --

def test_normalize_file_text_is_the_g20_rule():
    raw = "﻿<a>\r\n<created>2026-09-23T10:18:06.7121837Z</created>\r<modified />\n</a>"
    assert sev.normalize_file_text(raw, "scene-dae") == "<a>\n<created></created>\n<modified></modified>\n</a>"
    assert sev.normalize_file_text("<created>x</created>\r\n", "scene-pvc") == "<created>x</created>\n"
    with pytest.raises(sev.EvidenceError):
        sev.normalize_file_text("x", "terrain-mesh")


def test_line_count_counts_a_last_line_without_a_newline():
    assert (sev.line_count(""), sev.line_count("a"), sev.line_count("a\n"), sev.line_count("a\nb")) == (0, 1, 1, 2)


def test_a_file_over_the_comparator_string_bound_is_carried_in_chunks():
    line = "x" * 99 + "\n"
    text = line * 400                      # 40,000 characters, over the comparator's 16384
    (row,) = sev.file_rows({"scene-pvc": text})
    assert "text" not in row and row["lines"] == 400
    assert_g21_chunks(row["chunks"])
    assert "".join(row["chunks"]) == text and len(row["chunks"]) == 3
    assert [len(c) for c in row["chunks"]] == [16000, 16000, 8000]


def test_a_single_line_over_the_chunk_bound_is_refused():
    (row,) = sev.file_rows({"scene-pvc": "x" * sev.MAX_CHUNK})
    assert row["chunks"] == ["x" * sev.MAX_CHUNK] and row["lines"] == 1
    with pytest.raises(sev.EvidenceError, match="16000"):
        sev.file_rows({"scene-pvc": "x" * (sev.MAX_CHUNK + 1)})
    with pytest.raises(sev.EvidenceError, match="16000"):
        sev.file_rows({"scene-pvc": "a\n" + "x" * (sev.MAX_CHUNK + 1) + "\nb\n"})


def test_g21_chunks_split_only_after_line_feeds():
    assert sev.g21_chunks("") == [""]
    assert sev.g21_chunks("a\nb") == ["a\nb"]
    assert sev.g21_chunks("ab\ncd\nef", limit=5) == ["ab\n", "cd\nef"]
    # Form feeds and U+2028 are not line feeds: a line holding them is still one line.
    with pytest.raises(ValueError):
        sev.g21_chunks("x" * 9000 + " " + "y" * 9000 + "\n")
    first, second = "a" * 5000 + "\n", "p" * 5000 + "\x0c" + "q" * 7000 + "\n"
    chunks = sev.g21_chunks(first + second)
    assert chunks == [first, second]
    assert_g21_chunks(chunks)


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
        assert doc["after"]["format"] == "ground-v1"


def test_comparator_accepts_every_document_against_itself(run):
    docs, _ = run
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["versions"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_one_step_equals_the_same_step_of_a_full_run(run):
    docs, _ = run
    only, _ = sev.run_steps(terrain_intake(), REVISION, only="a6")
    assert list(only) == ["a6"] and only["a6"] == docs["a6"]


def test_unknown_steps_and_malformed_state_are_refused():
    with pytest.raises(sev.EvidenceError):
        sev.step_rows("a8", sev.with_array_store(ev.new_state()), terrain_intake())
    with pytest.raises(sev.EvidenceError):
        sev.run_steps(terrain_intake(), REVISION, only="a9")
    with pytest.raises(sev.EvidenceError):
        sev.step_rows("a5", {"frames": []}, terrain_intake())
    with pytest.raises(sev.EvidenceError):
        sev.terrain_state(terrain_intake(), through="g1")


def test_a_malformed_store_is_a_named_refusal():
    state, intake = a5_state()
    state["arrays"][0]["half_x"] = float("nan")
    with pytest.raises(sev.EvidenceError):
        sev.step_rows("a7", state, intake)


def test_cli_refuses_an_untracked_intake(tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(terrain_intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert sev.main(["--intake", str(intake), "--out-dir", str(out)]) == 2
    assert not out.exists()


# -------------------------------------------------- the licensed a7 --

def test_a7_file_rows_from_the_committed_intake_match_the_plugin():
    """a7 from Studio's own state (t1 grid; t2 to t7 and a1 to a4 leave the grid and store
    alone) with the a5 array, against the plugin's normalized files."""
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    state = sev.terrain_state(intake, through="t1")
    sev.step_rows("a5", state, intake)
    rows = sev.step_rows("a7", state, intake)
    assert [r["role"] for r in rows] == ["scene-dae", "scene-pvc"]
    for row in rows:
        assert row["lines"] == A7_LINES[row["role"]]
        assert_g21_chunks(row["chunks"])
        text = "".join(row["chunks"])
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == A7_SHA256[row["role"]], row["role"]
