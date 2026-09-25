"""Studio's G28 d-step evidence (d3, d4, d6, d7, d8).

A small synthetic terrain intake runs the whole chain (t1 to t7, the a-steps, the b-steps the
d-steps read, then d3 to d8) so every row kind is built on every runner: d3's report and
setting rows, the d4 trench, the d6 preview and d7 removal, the d8 file
rows in zip entry order with the manifest's host fields blanked, the parameters' answers, the
comparator accepting every document against itself, and refusals. The committed terrain
intake (inputs only) runs the chain once more: the trench, the preview of the array d5
defines, and the yield manifest carrying the licensed b1 totals (1434 frames, 27871.2 kWp,
19008 piles) the same tracker rows gave.
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


dev = _load("solar_ground_dsteps_evidence")
compare = dev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"
INTAKE = Path(__file__).resolve().parent.parent / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"

PRESET = {"Name": "Small", "ModuleLengthM": 2.0, "ModuleWidthM": 1.0, "Rows": 1, "Columns": 2,
          "HorizontalGapM": 0.0, "VerticalGapM": 0.0, "ColorIndex": 5, "PileTemplateName": "Two",
          "MaxSlopePercent": 80.0, "MaxCrossAxisSlopePct": 5.0,
          "Piling": {"MinPileLengthM": 3.0, "MaxPileLengthM": 6.0}}
TEMPLATE = {"Name": "Two", "HorizontalPoleCount": 1, "VerticalPoleCount": 2, "PileDiameterM": 0.2,
            "PileRevealM": 1.0, "PileEmbedmentM": 1.5}
BOUNDARY = [[0.0, 0.0], [3.0, 0.0], [3.0, 8.0], [0.0, 8.0]]
ZIP_ROLES = ["yield-layout-csv", "yield-bom_project_overview-csv", "yield-dc_homerun_bom-csv",
             "yield-dc_homerun_segments-csv", "yield-ac_feeder_bom-csv", "yield-ac_feeder_segments-csv",
             "yield-shading-csv", "yield-vegetation_masses-csv", "yield-readme-txt"]


def terrain_intake():
    """A 30 x 9 m plane rising 0.3 m per metre east, as 3 x 3 m faces."""
    faces = []
    for i in range(10):
        for j in range(3):
            x0, x1, y0, y1 = 3.0 * i, 3.0 * i + 3.0, 3.0 * j, 3.0 * j + 3.0
            faces.append([[x0, y0, 0.3 * x0], [x1, y0, 0.3 * x1], [x1, y1, 0.3 * x1], [x0, y1, 0.3 * x0]])
    return {"units": "m", "boundary": deepcopy(BOUNDARY), "active_preset": deepcopy(PRESET),
            "pile_template": deepcopy(TEMPLATE), "terrain_faces": faces}


@pytest.fixture(scope="module")
def run():
    return dev.run_steps(terrain_intake(), REVISION)


@pytest.fixture(scope="module")
def licensed():
    return dev.run_steps(json.loads(INTAKE.read_text(encoding="utf-8")), REVISION)


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def xy(point):
    return point["value"]


def file_text(row):
    return "".join(row["chunks"])


# ------------------------------------------------------------------ steps --

def test_every_step_in_order_with_its_capability_and_answers(run):
    docs, _, _ = run
    assert list(docs) == ["d3", "d4", "d6", "d7", "d8"]
    assert [d["provenance"]["capability"] for d in docs.values()] == \
        ["trackers-to-panelgroups", "trench-routing", "show-export-preview", "show-export-preview", "yield-export"]
    assert [d["parameters"]["answers"] for d in docs.values()] == [[], ["20,20", "120,70"], [], [], []]
    for doc in docs.values():
        assert doc["versions"]["engine"] == "server-builtin" and doc["versions"]["capability"] == "0"
        assert set(doc["parameters"]) == {"units_keyword", "grid_cells_long_axis", "active_preset", "answers"}


def test_d3_reports_and_commits_counters(run):
    docs, state, _ = run
    rows = rows_of(docs["d3"])
    assert [r["name"] for r in rows] == ["panel-group-slots", "panel-groups-created",
                                        "PanelGroupColour", "PanelGroupNumber"]
    assert all(type(r["value"]) is int for r in rows)
    state = deepcopy(state)
    before = deepcopy(state)
    repeated = dev.step_rows("d3", state, terrain_intake())
    count = next(r["value"] for r in repeated if r["name"] == "panel-groups-created")
    for name in ("PanelGroupNumber", "PanelGroupColour"):
        assert state["settings"][name] == before["settings"][name] + count
        before["settings"][name] += count
    assert state == before


def test_d3_without_trackers_leaves_settings_unchanged():
    state = dev.with_dstep_store({"grid": None, "frames": [], "settings": {
        "PanelGroupNumber": 238, "PanelGroupColour": 237}})
    before = deepcopy(state)
    rows = dev.step_rows("d3", state, terrain_intake())
    assert all(r["type"] == "report" and r["value"] == 0 for r in rows)
    assert state == before


def test_d4_trench_row(run):
    docs, state, _ = run
    (row,) = rows_of(docs["d4"])
    assert row["id"]["entity_id"] == "trench-1" and row["type"] == "trench"
    assert xy(row["vertices"][0]) == [20.0, 20.0] and xy(row["vertices"][-1]) == [120.0, 70.0]
    assert len(row["vertices"]) == 101 and row["closed"] is False
    assert row["depth"] == {"kind": "length", "value": 1.0, "unit": "m"}
    assert row["width"] == {"kind": "length", "value": 0.6, "unit": "m"}
    assert row["voltage_class"] == "MIXED"
    assert len(state["trenches"]) == 1


def test_d6_preview_and_d7_removal(run):
    docs, state, _ = run
    rows = rows_of(docs["d6"])
    assert [(r["id"]["entity_id"], r["role"]) for r in rows] == \
        [("export-preview-1", "array-outline"), ("export-preview-2", "clearance")]
    outline, clearance = ([xy(p) for p in r["vertices"]] for r in rows)
    # The clearance is the outline grown by the 2 m maintenance margin on every side.
    assert clearance[0][0] == pytest.approx(outline[0][0] - 2.0)
    assert clearance[0][1] == pytest.approx(outline[0][1] - 2.0)
    assert [(r["type"], r["of"], r["count"]) for r in rows_of(docs["d7"])] == [("removed", "export-preview", 2)]
    assert state["export_preview"] == []


def test_d8_file_rows_in_zip_order(run):
    docs, _, entries = run
    rows = rows_of(docs["d8"])
    roles = [r["role"] for r in rows]
    assert roles[:9] == ZIP_ROLES and roles[-1] == "yield-manifest-json"
    assert roles[9:-1] in ([], ["yield-pile_grouping-csv"])
    assert [r["id"]["entity_id"] for r in rows] == [f"file-{n}" for n in range(1, len(rows) + 1)]
    assert [dev.yield_role(name) for name, _ in entries] == roles
    for row in rows:
        assert "\r" not in file_text(row) and row["lines"] == file_text(row).count("\n")
    manifest = json.loads(file_text(rows[-1]))
    assert [manifest[k] for k in ("exported_at_utc", "project_name", "drawing_path", "plugin_version")] == [""] * 4
    assert manifest["exported_units"] == "Meters" and manifest["schema"] == "yield-export/1"
    assert [f["name"] for f in manifest["files"]] == [name for name, _ in entries[:-1]]
    assert set(dev.SYNTHETIC_MANIFEST_FIELDS) <= set(docs["d8"]["synthetic_fields"])
    assert not rows_of(docs["d8"], "unexpected-change")
    overview = file_text(rows[1])
    assert overview.startswith("Project name: Current drawing,Project name: Current drawing\n")


def test_d8_drawing_file_name_reaches_the_overview(run):
    _, state, _ = run
    ctx = {"drawing_file_name": "run.dwg"}
    rows = dev.step_rows("d8", state, terrain_intake(), ctx)
    overview = next(r for r in rows if r["role"] == "yield-bom_project_overview-csv")
    assert file_text(overview).startswith("Project name: run.dwg,Project name: run.dwg\n")


def test_manifest_host_fields_are_blanked():
    text = ('{\n  "exported_at_utc": "2026-09-23T10:00:00Z",\n  "frames": 2,\n  "project_name": "a \\"b\\"",\n'
            '  "drawing_path": "C:\\\\x\\\\y.dwg",\n  "plugin_version": "1.2.3.4",\n  "exported_units": "Meters"\n}\n')
    assert dev.blank_host_fields(text) == ('{\n  "exported_at_utc": "",\n  "frames": 2,\n  "project_name": "",\n'
                                           '  "drawing_path": "",\n  "plugin_version": "",\n'
                                           '  "exported_units": "Meters"\n}\n')
    assert dev.yield_role("bom_project_overview.csv") == "yield-bom_project_overview-csv"
    assert dev.yield_role("README.txt") == "yield-readme-txt"


def test_comparator_accepts_every_document_against_itself(run):
    docs, _, _ = run
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["versions"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_one_step_equals_the_same_step_of_a_full_run(run):
    docs, _, _ = run
    only, _, _ = dev.run_steps(terrain_intake(), REVISION, only="d6")
    assert list(only) == ["d6"] and only["d6"] == docs["d6"]


def test_refusals():
    with pytest.raises(dev.EvidenceError):
        dev.run_steps(terrain_intake(), REVISION, only="d5")
    with pytest.raises(dev.EvidenceError):
        dev.step_rows("d1", {"grid": None, "frames": []}, terrain_intake())
    with pytest.raises(dev.EvidenceError):
        dev.step_rows("d4", {"frames": []}, terrain_intake())
    with pytest.raises(dev.EvidenceError, match="revision"):
        dev.build_document(terrain_intake(), "d4", "trench-routing", "trench", [], "HEAD")
    with pytest.raises(dev.EvidenceError):
        dev._answer_point("20;20")


def test_cli_refuses_an_untracked_intake(tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(terrain_intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert dev.main(["--intake", str(intake), "--out-dir", str(out)]) == 2
    assert not out.exists()


# ------------------------------------------- the committed terrain intake --

def test_licensed_trench_preview_and_yield_totals(licensed):
    docs, _, entries = licensed
    (trench,) = rows_of(docs["d4"])
    assert len(trench["vertices"]) == 101
    outline = [xy(p) for p in rows_of(docs["d6"])[0]["vertices"]]
    # d5's array_0: 100 x 85 landscape modules of 1.640 x 0.992 m at 0.02 m spacing, centred on 250,250.
    assert outline[0] == pytest.approx([250.0 - 82.99, 250.0 - 43.0])
    assert outline[2] == pytest.approx([250.0 + 82.99, 250.0 + 43.0])
    manifest = json.loads(dict(entries)["manifest.json"].decode("utf-8"))
    assert (manifest["frames"], manifest["kwp_total"], manifest["piles"]) == (1434, 27871.2, 19008)
    overview = dict(entries)["bom_project_overview.csv"].decode("utf-8")
    assert '"Total capacity, kWp",27871.2\r\n' in overview and "Module quantity,69678\r\n" in overview
    d3 = {r["name"]: r["value"] for r in rows_of(docs["d3"])}
    assert d3 == {"panel-group-slots": 69678, "panel-groups-created": 237,
                  "PanelGroupNumber": 238, "PanelGroupColour": 237}
    assert rows_of(docs["d3"], "setting") == [
        {"id": {"entity_id": f"setting-{name}"}, "type": "setting", "quantity": 1,
         "unit": "each", "name": name, "value": value}
        for name, value in (("PanelGroupColour", 237), ("PanelGroupNumber", 238))]
