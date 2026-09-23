"""Studio's G33 PVcase-solve evidence (step v1, capability pvcase-solve).

A synthetic intake of the committed pvcase intake's shape (panel groups in the plugin's read order
with their rows, the panels-per-string setting, no L2 snapshot), authored in this file, is solved and
turned into the v1 document: the panel-assignment rows by value and in handle order, the envelope,
the hashes, the comparator accepting it against itself, the CLI writing it, and the refusals (a
malformed intake, a solve that refuses, a bad revision) as named errors. The committed intake is
solved too, its hashes pinned to the values the plugin's recorded v1 carries. No git and no network:
every revision is pinned.
"""
from __future__ import annotations

import copy
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


pe = _load("solar_pvcase_solve_evidence")
compare = pe.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"
# The commit that added the committed intake (G33), the revision its evidence pins.
INTAKE_REVISION = "b3ae54bd4c58cee334f15b0acbe7169c6128af06"


def cell(ident, code=1, x=0.0, y=0.0):
    return {"code": code, "id": ident, "seq": 0, "inverter_id": -1, "string_input_number": 0, "x": x, "y": y}


def group(handle, rows):
    return {"handle": handle, "installation": "Roof",
            "panel_size": {"height_across_row": 38.46875, "width_along_row": 77.0},
            "row_angle_rad": 0.0, "sequences": [], "rows": rows}


def intake():
    # Read order is descending handle value, as the plugin walks the rooftop fixture.
    return {
        "units": "in",
        "panels_per_string": 3,
        "panel_groups": [
            group("A646", [[cell("8891", x=10.0), cell("8890", x=20.0), None],
                           [cell("888F", x=10.0, y=5.0), cell("888E", x=20.0, y=5.0)]]),
            group("A63B", [[cell("84ED"), cell("84EC")]]),
            group("A5DE", [[cell("93E8"), cell("93FC"), cell("93FB"), cell("93FA")]]),
        ],
    }


EXPECTED_ROWS = [
    {"id": "panel-assignment-1", "type": "panel-assignment", "quantity": 1, "unit": "each", "group": "A5DE",
     "assignments": [["93E8", 1, 4], ["93FC", 1, 4], ["93FB", 1, 4], ["93FA", 1, 5]]},
    {"id": "panel-assignment-2", "type": "panel-assignment", "quantity": 1, "unit": "each", "group": "A63B",
     "assignments": [["84ED", 1, 3], ["84EC", 1, 3]]},
    {"id": "panel-assignment-3", "type": "panel-assignment", "quantity": 1, "unit": "each", "group": "A646",
     "assignments": [["8891", 1, 1], ["8890", 1, 1], ["888F", 1, 1], ["888E", 1, 2]]},
]


def test_rows_are_the_solved_assignments_in_handle_order():
    rows, outcome = pe.solve_rows(intake())
    assert rows == EXPECTED_ROWS
    assert (outcome["panels_assigned"], outcome["strings_created"], outcome["written"]) == (10, 5, 10)


def test_document_envelope():
    doc = pe.run(intake(), REVISION)
    assert doc["after"]["rows"] == [dict(r, id={"entity_id": r["id"]}) for r in EXPECTED_ROWS]
    assert doc["after"]["source_revision"] == "v1" and doc["after"]["format"] == "ground-v1"
    assert doc["parameters"] == {"answers": []}
    assert doc["versions"] == {"schema": compare.SCHEMA, "producer": "studio", "capability": "0",
                               "engine": "server-builtin", "catalog": "none", "solver": "none"}
    assert doc["entity_mapping"] == {f"panel-assignment-{n}": f"panel-assignment-{n}" for n in (1, 2, 3)}
    assert doc["provenance"] == {"side": "studio", "fixture_kind": "rooftop", "step": "v1",
                                 "capability": "pvcase-solve", "operation": "pvcase-solve"}
    assert doc["units"] == "in" and doc["revision"] == REVISION
    assert (doc["state"], doc["survived_reopen"], doc["execution_mode"]) == ("committed", True, "live")


def test_hashes_follow_rules_one_and_four():
    doc = pe.run(intake(), REVISION)
    assert doc["fixture_sha256"] == compare.semantic_hash(intake())
    assert doc["input_sha256"] == compare.semantic_hash({"fixture_sha256": doc["fixture_sha256"],
                                                        "parameters": {"answers": []}})
    assert doc["output_sha256"] == compare.semantic_hash(doc["after"])


def test_document_passes_the_comparator_against_itself():
    doc = pe.run(intake(), REVISION)
    result = compare.compare(doc, copy.deepcopy(doc), "exports", capability="pvcase-solve")
    assert result["verdict"] == "pass" and result["diffs"] == []


def test_a_changed_assignment_is_a_diff():
    doc = pe.run(intake(), REVISION)
    other = copy.deepcopy(doc)
    other["after"]["rows"][0]["assignments"][3][2] = 6
    other["output_sha256"] = compare.semantic_hash(other["after"])
    result = compare.compare(doc, other, "exports", capability="pvcase-solve")
    assert result["diffs"] == ["after/rows/0/assignments/3/2: value differs"]


def test_the_intake_is_not_mutated_and_the_run_is_deterministic():
    source = intake()
    first = pe.run(source, REVISION)
    assert source == intake()
    assert pe.run(source, REVISION) == first


def test_group_without_panels_still_emits_its_row():
    source = intake()
    source["panel_groups"].append(group("A5D0", [[None], []]))
    rows, outcome = pe.solve_rows(source)
    assert [(r["group"], r["assignments"]) for r in rows][0] == ("A5D0", [])
    assert [r["id"] for r in rows] == [f"panel-assignment-{n}" for n in (1, 2, 3, 4)]
    assert outcome["rejection_reasons"] == ["[A5D0] zero panels survived (raw=0, code0=0)"]


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(l2_inverters=[]),
    lambda s: s["panel_groups"][0].update(Dwgname="x.dwg"),
    lambda s: s["panel_groups"][0].update(matrix={"Rows": []}),
])
def test_a_foreign_intake_shape_is_refused(mutate):
    source = intake()
    mutate(source)
    with pytest.raises(pe.EvidenceError, match="intake refused"):
        pe.run(source, REVISION)


@pytest.mark.parametrize("groups,reason", [
    ([], "no panel groups found"),
    ([group("B1", [[cell("1", code=0)]])], "no usable panels"),
])
def test_a_refused_solve_is_a_named_error(groups, reason):
    source = intake()
    source["panel_groups"] = groups
    with pytest.raises(pe.EvidenceError, match=reason):
        pe.run(source, REVISION)


@pytest.mark.parametrize("revision", ["HEAD", "0123", REVISION.upper(), None])
def test_revision_must_be_a_full_commit(revision):
    with pytest.raises(pe.EvidenceError, match="revision"):
        pe.run(intake(), revision)


def test_committed_intake_evidence():
    source = json.loads(pe.DEFAULT_INTAKE.read_text(encoding="utf-8"))
    doc = pe.run(source, INTAKE_REVISION)
    rows = doc["after"]["rows"]
    assert [r["group"] for r in rows] == ["A5DE", "A5EA", "A5F4", "A5FE", "A608", "A612", "A61D", "A627",
                                          "A631", "A63B", "A646"]
    assert sum(len(r["assignments"]) for r in rows) == 2345
    assert rows[0]["assignments"][0] == ["93E8", 1, 69] and rows[0]["assignments"][-1] == ["945C", 1, 88]
    assert rows[-1]["assignments"][:2] == [["8891", 1, 1], ["8890", 1, 1]]
    # The same three hashes the plugin's recorded v1 carries for this intake (G13: computed here).
    assert doc["fixture_sha256"] == "364eeaca982cc3eaed58628c1d6c81ac6640ff47ba70e2fb2142991f46f27a41"
    assert doc["input_sha256"] == "c49b4b16989d53ca35f4e1a4b0a6627b167918b6a9f819f4e9d173025c64366e"
    assert doc["output_sha256"] == "8906d68e02b723105bf53f88793f8b9e9b532f07a47349b99dcdbe8e0ef03b42"


def test_cli_writes_v1_json(tmp_path):
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert pe.main(["--intake", str(path), "--out-dir", str(out), "--revision", REVISION]) == 0
    written = (out / "v1.json").read_text(encoding="utf-8")
    assert json.loads(written) == pe.run(intake(), REVISION)
    assert written.endswith("\n") and "\n" not in written[:-1]  # compact JSON


def test_cli_refuses_a_malformed_intake(tmp_path, capsys):
    path = tmp_path / "intake.json"
    bad = intake()
    bad["panels_per_string"] = "28"
    path.write_text(json.dumps(bad), encoding="utf-8")
    assert pe.main(["--intake", str(path), "--out-dir", str(tmp_path / "out"), "--revision", REVISION]) == 2
    assert "solar-pvcase-solve-evidence:" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
