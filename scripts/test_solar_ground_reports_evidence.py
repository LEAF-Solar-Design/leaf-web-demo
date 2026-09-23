"""Studio's G23 report evidence (b3, b4, b12, b13, b14).

The committed terrain intake runs the whole chain once (t1 to t7, the a-steps these steps read,
then the five b-steps), and every reported value must equal what the plugin printed on that
site: the fence audit's zero counts, 1,197 sampled points from the LEAF-TRACKERS polylines, the
empty vegetation import and its status record, the empty fence mesh, and the optimal spacing
sweep (pitch 6.300 m, GCR 0.333, capture 39.79 %). Small hand-made states cover the rows the
site does not reach: the fence mesh wipe, refusals of committed geometry G23 has no row for,
the read-only rule, and the no-terrain paths. Every document must pass the comparator against
itself.
"""
from __future__ import annotations

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


rev = _load("solar_ground_reports_evidence")
ev = rev.ev
compare = rev.compare
reports = rev.reports
REVISION = "0123456789abcdef0123456789abcdef01234567"
INTAKE = Path(__file__).resolve().parent.parent / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"


def intake():
    return json.loads(INTAKE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run():
    return rev.run_steps(intake(), REVISION)


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def reported(doc):
    """{name: value} of a document's report rows, a quantity read as its number."""
    out = {}
    for r in rows_of(doc, "report"):
        v = r["value"]
        out[r["name"]] = v["value"] if isinstance(v, dict) else v
    return out


def bare_state():
    """A state with no terrain grid and nothing drawn, for the hand-made cases."""
    return rev.with_report_store({"grid": None, "mesh_faces": 0, "frames": [], "piles": []})


# ------------------------------------------------------ the licensed site --

def test_every_step_in_order_with_its_capability_and_answers(run):
    docs, _ = run
    assert list(docs) == ["b3", "b4", "b12", "b13", "b14"]
    caps = {d["provenance"]["step"]: d["provenance"]["capability"] for d in docs.values()}
    assert caps == {"b3": "fence-3d-audit", "b4": "mesh-diff-view", "b12": "vegetation-from-civil",
                    "b13": "fence-mesh-generate", "b14": "optimal-row-spacing"}
    assert docs["b14"]["parameters"]["answers"] == ["select:LEAF-BOUNDARY"]
    assert all(docs[s]["parameters"]["answers"] == [] for s in ("b3", "b4", "b12", "b13"))
    for doc in docs.values():
        assert not rows_of(doc, "unexpected-change")


def test_b3_fence_audit_matches_the_plugin(run):
    docs, _ = run
    assert reported(docs["b3"]) == {"source-3d": 0, "flat": 0, "flat-drapeable": 0, "mesh-faces": 0}


def test_b4_mesh_diff_matches_the_plugin(run):
    docs, _ = run
    assert reported(docs["b4"]) == {"sampled-points": 1197, "block-references": 0, "blocks-skipped": 0}


def test_b12_vegetation_import_matches_the_plugin(run):
    docs, state = run
    assert reported(docs["b12"]) == {"imported-regions": 0, "source-regions": 0, "height-labels": 0,
                                     "mesh-faces": 0}
    [record] = rows_of(docs["b12"], "status-record")
    assert record["name"] == "vegetation-import-last" and record["id"] == {"entity_id": "status-record-1"}
    fields = {k: v for k, v in record.items() if k not in ("id", "type", "quantity", "unit", "name")}
    assert fields == {"schema": 1, "source-regions": 0, "height-labels": 0, "imported-regions": 0,
                      "mesh-faces": 0, "restriction-regions": 0, "skipped-regions": 0, "snapped-labels": 0,
                      "meters-per-unit": 1}  # G26: hyphenated names; integral values are ints
    assert state["status_records"]["vegetation-import-last"]["schema"] == 1


def test_b13_fence_mesh_matches_the_plugin(run):
    docs, _ = run
    assert reported(docs["b13"]) == {"fence-items": 0, "source-3d": 0, "flat": 0, "draped": 0, "mesh-faces": 0}
    assert not rows_of(docs["b13"], "removed")


B14 = {"optimal-pitch": 6.3, "optimal-gcr": 0.333, "capture-percent": 39.79, "shading-loss-percent": 60.21,
       "target-achieved": "no"}
B14_SWEEP = [(3.15, 0.667, 20.34, 79.66, 159), (3.316, 0.633, 21.42, 78.58, 151),
             (3.482, 0.603, 22.46, 77.54, 144), (3.647, 0.576, 23.51, 76.49, 137),
             (3.813, 0.551, 24.55, 75.45, 131)]


def test_b14_optimal_spacing_matches_the_plugin(run):
    docs, _ = run
    got = reported(docs["b14"])
    expected = dict(B14)
    for k, (pitch, gcr, capture, loss, rows) in enumerate(B14_SWEEP, 1):
        expected.update({f"sweep-{k}-pitch": pitch, f"sweep-{k}-gcr": gcr, f"sweep-{k}-capture-percent": capture,
                         f"sweep-{k}-shading-loss-percent": loss, f"sweep-{k}-rows": rows})
    assert got == expected
    units = {r["name"]: r["value"]["unit"] for r in rows_of(docs["b14"], "report") if isinstance(r["value"], dict)}
    assert units["optimal-pitch"] == "m"


def _m(value):
    return {"kind": "length", "value": value, "unit": "m"}


def _b14_table():
    table = {"capture-percent": 39.79, "optimal-gcr": 0.333, "optimal-pitch": _m(6.3),
             "shading-loss-percent": 60.21, "target-achieved": "no"}
    for k, (pitch, gcr, capture, loss, rows) in enumerate(B14_SWEEP, 1):
        table.update({f"sweep-{k}-capture-percent": capture, f"sweep-{k}-gcr": gcr, f"sweep-{k}-pitch": _m(pitch),
                      f"sweep-{k}-rows": rows, f"sweep-{k}-shading-loss-percent": loss})
    return table


# Contract G25's report table: per step, every report name the plugin printed on the terrain capture and
# the value it printed. Test data only; the producer computes every value.
G25 = {"b3": {"flat": 0, "flat-drapeable": 0, "mesh-faces": 0, "source-3d": 0},
       "b4": {"block-references": 0, "blocks-skipped": 0, "sampled-points": 1197},
       "b12": {"height-labels": 0, "imported-regions": 0, "mesh-faces": 0, "source-regions": 0},
       "b13": {"draped": 0, "fence-items": 0, "flat": 0, "mesh-faces": 0, "source-3d": 0},
       "b14": _b14_table()}


def _shape(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return f"quantity:{value['kind']}:{value['unit']}"
    return type(value).__name__


def test_report_rows_are_the_g25_table(run):
    docs, _ = run
    assert set(G25) == set(rev.STEP_IDS)
    for step in rev.STEP_IDS:
        rows = rows_of(docs[step], "report")
        assert [r["id"]["entity_id"] for r in rows] == [f"report-{name}" for name in sorted(G25[step])], step
        for row in rows:
            want, got = G25[step][row["name"]], row["value"]
            assert _shape(got) == _shape(want), (step, row["name"])
            if isinstance(want, dict):
                assert got["value"] == pytest.approx(want["value"], abs=1e-3), (step, row["name"])
            else:
                assert got == want, (step, row["name"])


def test_documents_validate_and_hash(run):
    docs, _ = run
    fixture = compare.semantic_hash(intake())
    for step_id, doc in docs.items():
        compare.validate_evidence(doc, "exports")
        assert doc["fixture_sha256"] == fixture
        assert doc["input_sha256"] == compare.semantic_hash({"fixture_sha256": fixture,
                                                             "parameters": doc["parameters"]})
        assert doc["output_sha256"] == compare.semantic_hash(doc["after"])
        assert doc["after"]["source_revision"] == step_id
        ids = [r["id"]["entity_id"] for r in rows_of(doc)]
        assert sorted(doc["entity_mapping"]) == sorted(ids)


def test_comparator_accepts_every_document_against_itself(run):
    docs, _ = run
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["versions"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_rows_are_sorted_by_type_then_id(run):
    docs, _ = run
    for doc in docs.values():
        keys = [rev._row_order({"id": r["id"]["entity_id"], "type": r["type"]}) for r in rows_of(doc)]
        assert keys == sorted(keys)


def test_status_record_is_named_by_purpose_only(run):
    docs, _ = run
    [record] = rows_of(docs["b12"], "status-record")
    assert set(record) == {"id", "type", "quantity", "unit", "name", "schema", "source-regions", "height-labels",
                           "imported-regions", "mesh-faces", "restriction-regions", "skipped-regions",
                           "snapped-labels", "meters-per-unit"}  # G26: hyphenated field names


# ------------------------------------------------------- hand-made states --

def test_b13_wipes_the_previous_fence_mesh():
    state = bare_state()
    state["fence_mesh_faces"] = [None, None]
    rows = rev.step_rows("b13", state, intake())
    assert [(r["of"], r["count"]) for r in rows if r["type"] == "removed"] == [("fence-mesh-face", 2)]
    assert state["fence_mesh_faces"] == []
    assert {r["name"] for r in rows if r["type"] == "report"} == {"fence-items", "source-3d", "flat", "draped",
                                                                   "mesh-faces"}


def test_committed_geometry_without_a_g23_row_is_refused(monkeypatch):
    fence = {"type": "polyline", "layer": "C-FENCE", "vertices": [[0, 0, 1.0], [5, 0, 1.0]], "closed": False}
    wood = {"type": "polyline", "layer": "V-VEGE-WDLN", "vertices": [[0, 0], [9, 0], [9, 9], [0, 9]], "closed": True}
    tag = {"type": "text", "layer": "V-VEGE-WDLN-TXT", "text": "30'", "position": [4, 4]}
    monkeypatch.setattr(rev, "drawing_entities", lambda state, intake: [fence, wood, tag])
    with pytest.raises(rev.EvidenceError, match="fence mesh faces"):
        rev.step_rows("b13", bare_state(), intake())
    with pytest.raises(rev.EvidenceError, match="vegetation outlines"):
        rev.step_rows("b12", bare_state(), intake())
    rows = rev.step_rows("b3", bare_state(), intake())
    assert {r["name"]: r["value"] for r in rows}["source-3d"] == 1


def test_a_read_only_step_that_moved_the_state_is_flagged(monkeypatch):
    def mutating(state, intake):
        state["status_records"]["moved"] = {}
        return []
    monkeypatch.setitem(rev.STEPS, "b3", mutating)
    rows = rev.step_rows("b3", bare_state(), intake())
    assert [r["type"] for r in rows] == ["unexpected-change"]


def test_b4_without_a_terrain_grid_reports_nothing():
    # The plugin prints no sampled-points line without a terrain grid, so G25 has no row for it.
    assert rev.step_rows("b4", bare_state(), intake()) == []


def test_unknown_steps_and_malformed_state_are_refused():
    with pytest.raises(rev.EvidenceError):
        rev.step_rows("b5", bare_state(), intake())
    with pytest.raises(rev.EvidenceError):
        rev.step_rows("b3", {"frames": []}, intake())
    with pytest.raises(rev.EvidenceError):
        rev.run_steps(intake(), REVISION, only="b1")
    with pytest.raises(rev.EvidenceError):
        rev.report_row("Not Neutral", 1)
    with pytest.raises(rev.EvidenceError):
        rev.build_document(intake(), "b3", "fence-3d-audit", "fence-audit", [], "not-a-revision")


def test_cli_refuses_an_untracked_intake(tmp_path):
    copy = tmp_path / "intake.json"
    shutil.copyfile(INTAKE, copy)
    out = tmp_path / "out"
    assert rev.main(["--intake", str(copy), "--out-dir", str(out), "--step", "b3"]) == 2
    assert not out.exists()
