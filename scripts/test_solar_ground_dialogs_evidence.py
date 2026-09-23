"""Studio's G30 dialog-batch evidence (e1, e4, e6).

The committed terrain intake and the committed BEFORE pile-template store run the three steps
once, and each must equal what the plugin committed in the capture: e1's tree (canopy radius 2,
restriction ring 3, the label "Tree h=8.0 trunk=3.0" at 250,147.5), e4's one project area bound
to the intake's active preset, and e6's store file, whose normalized text must hash to the
captured AFTER store's (and whose bytes, written by the CLI, to its raw bytes). Hand-made cases
cover the read-only rule, a missing store file, the G24 digest path, and every refusal. Every
document must pass the comparator against itself.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rev = _load("solar_ground_dialogs_evidence")
ev = rev.ev
compare = rev.compare
dialogs = rev.dialogs
REVISION = "0123456789abcdef0123456789abcdef01234567"
INTAKE = rev.DEFAULT_INTAKE
# The e6 capture's AFTER store: raw bytes (CRLF, no BOM) and its G20-normalized text (LF).
AFTER_RAW_SHA256 = "0b8ba49ec4feac4598c8adf6cf612884a5eafcbe68f50e56ced5c5d1f4431a11"
AFTER_NORMALIZED_SHA256 = "877c5e2ce8c3b3882e700cfcc5ac2afefc5ceb95a68d4f18b7adb21c5e2d11c3"
AFTER_LINES = 403
CAPTURED_AREA_RECORD = ('{"Version":1,"Areas":[{"Name":"Area 1","SubAreaId":"","BoundaryHandle":null,'
                        '"FramePresetName":"TinyTest","PitchOverrideM":0.0,"ExclusionZoneHandles":[],'
                        '"CachedBounds":null}]}')


def intake():
    return json.loads(INTAKE.read_text(encoding="utf-8"))


def before_store():
    return rev.read_store_file(rev.DEFAULT_PILE_STORE)


@pytest.fixture(scope="module")
def run():
    return rev.run_steps(intake(), REVISION, before_store())


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def reported(doc):
    return {r["name"]: r["value"] for r in rows_of(doc, "report")}


# ------------------------------------------------------- the licensed site --

def test_every_step_in_order_with_its_capability_and_answers(run):
    docs, _, _ = run
    assert list(docs) == ["e1", "e4", "e6"]
    caps = {d["provenance"]["step"]: d["provenance"]["capability"] for d in docs.values()}
    assert caps == {"e1": "shading-object-placement", "e4": "project-areas", "e6": "pile-templates"}
    assert docs["e1"]["parameters"]["answers"] == ["form:Tree:defaults", "250,150"]
    assert docs["e4"]["parameters"]["answers"] == ["Add Area", "OK"]
    assert docs["e6"]["parameters"]["answers"] == ["+", "OK"]
    for doc in docs.values():
        assert doc["parameters"]["active_preset"] == "TinyTest"
        assert doc["fixture_sha256"] == compare.semantic_hash(intake())
    assert "form_values" not in docs["e4"]["parameters"] and "form_values" not in docs["e6"]["parameters"]


def test_e1_is_the_captured_tree_with_the_form_defaults(run):
    docs, state, _ = run
    [row] = rows_of(docs["e1"])
    assert row["id"] == {"entity_id": "shading-object-1"} and row["object_kind"] == "tree"
    assert row["center"] == {"kind": "coordinate", "value": [250.0, 150.0], "unit": "m"}
    assert row["radius"] == {"kind": "length", "value": 2.0, "unit": "m"}
    assert row["restriction"] == {"kind": "length", "value": 3.0, "unit": "m"}
    assert row["label"] == "Tree h=8.0 trunk=3.0"
    assert row["label_at"] == {"kind": "coordinate", "value": [250.0, 147.5], "unit": "m"}
    form_values = docs["e1"]["parameters"]["form_values"]
    assert form_values == {"tree_top_diameter": 4.0, "tree_trunk_height": 3.0, "tree_total_height": 8.0,
                           "tree_restriction_offset": 1.0, "station_length": 5.0, "station_width": 3.0,
                           "station_height": 3.0, "station_restriction_offset": 1.0, "fence_height": 2.0,
                           "fence_width": 0.2, "vegetation_height": 12.0}
    assert list(form_values) == [key for _, key in dialogs.SHADING_FORM_KEYS]
    assert len(state["shading"]) == 3


def test_e4_is_the_captured_area_record(run):
    docs, state, _ = run
    [row] = rows_of(docs["e4"])
    assert (row["name"], row["preset"], row["sub_area_id"], row["boundary"]) == ("Area 1", "TinyTest", "", None)
    assert row["pitch_override"] == {"kind": "length", "value": 0.0, "unit": "m"}
    assert state["area_record"] == CAPTURED_AREA_RECORD


def test_e6_writes_the_captured_after_store(run):
    docs, _, store_after = run
    [file_row] = rows_of(docs["e6"], "file")
    assert file_row["role"] == "pile-templates-json" and file_row["lines"] == AFTER_LINES
    assert hashlib.sha256("".join(file_row["chunks"]).encode("utf-8")).hexdigest() == AFTER_NORMALIZED_SHA256
    assert hashlib.sha256(store_after.encode("utf-8")).hexdigest() == AFTER_RAW_SHA256
    assert reported(docs["e6"]) == {"templates": 3, "active-template": "New"}
    assert not rows_of(docs["e6"], "unexpected-change")


def test_comparator_accepts_every_document_against_itself(run):
    docs, _, _ = run
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["versions"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_documents_name_no_storage_internals(run):
    docs, _, _ = run
    text = json.dumps(docs).lower()
    for needle in ("leaf-pv-areas", "pile_templates.json", "appdata"):
        assert needle not in text


# ------------------------------------------------------------ hand-made --

def test_one_step_runs_its_predecessors():
    docs, state, store_after = rev.run_steps(intake(), REVISION, before_store(), only="e4")
    assert list(docs) == ["e4"] and len(state["shading"]) == 3 and store_after is None


def test_a_read_only_step_whose_drawing_moved_is_flagged(monkeypatch):
    def mutating(state, intake, ctx):
        state["shading"] = state["shading"] + [{"type": "circle"}]
        return []
    monkeypatch.setitem(rev.STEPS, "e6", mutating)
    rows = rev.step_rows("e6", rev.new_state(None), intake())
    assert [r["type"] for r in rows] == ["unexpected-change"]


def test_a_missing_store_file_starts_from_the_default_full_template():
    docs, _, _ = rev.run_steps(intake(), REVISION, None, only="e6")
    assert reported(docs["e6"]) == {"templates": 2, "active-template": "New"}


def test_an_unknown_step_is_refused():
    with pytest.raises(rev.EvidenceError, match="not one of"):
        rev.run_steps(intake(), REVISION, before_store(), only="e2")


def test_a_bad_revision_is_refused():
    with pytest.raises(rev.EvidenceError, match="revision"):
        rev.build_document(intake(), "e6", "pile-templates", "pile-templates", [], "not-a-revision")


def test_a_foreign_state_is_refused():
    with pytest.raises(rev.EvidenceError, match="dialog state"):
        rev.step_rows("e1", {"grid": None}, intake())


def test_an_engine_refusal_is_named(monkeypatch):
    monkeypatch.setitem(rev.ANSWERS, "e1", ("form:Station:defaults", "250,150"))
    with pytest.raises(rev.EvidenceError, match="not the answered Station"):
        rev.step_rows("e1", rev.new_state(None), intake())


def test_cli_writes_the_three_documents_and_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(rev.ev, "fixture_revision", lambda path: REVISION)
    out, store = tmp_path / "out", tmp_path / "store.json"
    assert rev.main(["--intake", str(INTAKE), "--out-dir", str(out), "--store-out", str(store)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["e1.json", "e4.json", "e6.json"]
    for p in out.iterdir():
        compare.validate_evidence(compare.load_evidence(p), "exports")
    assert hashlib.sha256(store.read_bytes()).hexdigest() == AFTER_RAW_SHA256


def test_cli_refuses_an_untracked_intake(tmp_path):
    copy = tmp_path / "intake.json"
    shutil.copyfile(INTAKE, copy)
    out = tmp_path / "out"
    assert rev.main(["--intake", str(copy), "--out-dir", str(out), "--step", "e6"]) == 2
    assert not out.exists()


def test_a_store_past_the_g24_threshold_is_digested():
    text = ("x" * 99 + "\n") * 10_500
    row = rev.store_file_row(text)
    assert "chunks" not in row and row["chars"] == len(text) and row["lines"] == 10_500
    assert row["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert "".join(row["head"]) == ("x" * 99 + "\n") * 40


def test_file_text_is_normalized_before_it_is_compared():
    assert rev.normalize_text("﻿a\r\nb\rc") == "a\nb\nc"
