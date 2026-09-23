"""Studio's G27 rooftop-chain evidence (c1 to c7, c9, c10, and c11 for string-rebuild).

A synthetic intake of the rooftop intake's shape, authored in this file with the scenario's own
G22 handles, runs the whole chain once: the flip, the swap, the frame-group create, rename, list,
select and delete, the ten export settings, the string-data file, and the rebuild. Every row is
checked by value, every document passes the frozen comparator against itself, a single step
equals the same step of the whole chain, and the refusals (malformed intakes and answers, a
read-only step that moves state, an unknown string) are named errors. No git and no network.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rev = _load("solar_rooftop_chain_evidence")
compare = rev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"


def marker(handle, x, y):
    return {"handle": handle, "at": [x, y]}


def intake():
    return {
        "units": "in",
        "strings": [
            {"handle": "A67A", "panels": ["B01", "B02", "B03"],
             "label": {"mppt_count": 2, "strings_on_inverter": 1, "inverter": 1},
             "start": marker("A67B", 100, 10), "end": marker("A67C", 200, 10)},
            {"handle": "A912", "panels": ["C1", "C2"],
             "label": {"mppt_count": 2, "strings_on_inverter": 3, "inverter": 1}, "circuit": "1/1a",
             "start": marker("A913", 10.004, 5), "end": marker("A914", 20, 5)},
            {"handle": "A902", "panels": ["C3", "C4"],
             "label": {"mppt_count": 2, "strings_on_inverter": 7, "inverter": 2}, "circuit": "2/1b",
             "start": marker("A903", 30, 5), "end": marker("A904", 40, 5)},
            {"handle": "A90E", "panels": ["C5", "C6"],
             "label": {"mppt_count": 2, "strings_on_inverter": 4, "inverter": 1}, "circuit": "1/2a",
             "start": marker("A90F", 10, 15), "end": marker("A910", 20, 15.555)},
        ],
        "panel_groups": [
            {"handle": "A646", "name": "Group 11", "panels": ["C1", "C2", "C5", "C6"]},
            {"handle": "A63B", "name": "Group 10", "panels": ["C3", "C4"]},
            {"handle": "A631", "name": "Group 9", "panels": ["B01", "B02", "B03"]},
            {"handle": "A5DE", "name": "Group 1", "panels": []},
        ],
        "settings": {"HomerunRouting": {"CableCatalog": [{"Name": "cable-a"}]}},
    }


@pytest.fixture(scope="module")
def run():
    return rev.run_steps(intake(), REVISION)


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def plain(row):
    out = dict(row)
    out["id"] = row["id"]["entity_id"]
    return out


# ------------------------------------------------------------------ the chain --

def test_every_step_in_order_with_its_capability_and_answers(run):
    docs, _ = run
    assert list(docs) == ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c9", "c10", "c11"]
    caps = {s: d["provenance"]["capability"] for s, d in docs.items()}
    assert caps == {"c1": "string-flip", "c2": "string-swap", "c3": "frame-group-create",
                    "c4": "frame-group-manage", "c5": "frame-group-manage", "c6": "select-by-frame-group",
                    "c7": "frame-group-manage", "c9": "export-settings", "c10": "string-data",
                    "c11": "string-rebuild"}
    assert docs["c3"]["parameters"] == {"answers": ["FGA", "groups:A646,A63B,A631"]}
    assert docs["c9"]["parameters"]["answers"] == ["1.134", "2.278", "0.025", "0.03", "1", "12.5", "185",
                                                   "0.6", "ACME", "P440"]
    for step, doc in docs.items():
        assert doc["versions"]["engine"] == "server-builtin" and doc["versions"]["capability"] == "0"
        assert doc["after"]["source_revision"] == step
        assert not rows_of(doc, "unexpected-change")


def test_c1_flip_reverses_the_picked_string(run):
    docs, _ = run
    assert [plain(r) for r in rows_of(docs["c1"])] == [
        {"id": "string-1", "type": "string", "quantity": 1, "unit": "each", "string": "A67A",
         "panels": ["B03", "B02", "B01"], "count": 3}]


def test_c2_swap_exchanges_labels_and_circuits(run):
    docs, state = run
    labels = [plain(r) for r in rows_of(docs["c2"], "string-label")]
    assert labels == [
        {"id": "string-label-1", "type": "string-label", "quantity": 1, "unit": "each", "string": "A902",
         "mppt_count": 2, "strings_on_inverter": 3, "inverter": 1},
        {"id": "string-label-2", "type": "string-label", "quantity": 1, "unit": "each", "string": "A912",
         "mppt_count": 2, "strings_on_inverter": 7, "inverter": 2}]
    assert [(r["string"], r["panels"]) for r in rows_of(docs["c2"], "string")] == [
        ("A902", ["C3", "C4"]), ("A912", ["C1", "C2"])]
    assert (state["strings"]["A912"]["circuit"], state["strings"]["A902"]["circuit"]) == ("2/1b", "1/1a")
    assert docs["c2"]["parameters"] == {"answers": ["string:A912", "string:A902"]}


def test_c3_c4_c7_write_the_frame_groups_setting_with_the_clock_zeroed(run):
    docs, _ = run
    values = {s: [plain(r) for r in rows_of(docs[s])] for s in ("c3", "c4", "c7")}
    group = '[{"ColorIndex":0,"FrameHandles":["A646","A63B","A631"],"LastModifiedTicks":0,"Name":"%s"}]'
    for step, text in (("c3", group % "FGA"), ("c4", group % "FGB"), ("c7", "[]")):
        assert values[step] == [{"id": "setting-FrameGroups", "type": "setting", "quantity": 1, "unit": "each",
                                 "name": "FrameGroups", "value": text}]
        assert "setting/FrameGroups/LastModifiedTicks" in docs[step]["synthetic_fields"]


def test_c5_lists_the_group_count_and_its_frames(run):
    docs, _ = run
    assert {r["name"]: r["value"] for r in rows_of(docs["c5"], "report")} == {
        "frame-groups": 1, "frame-group-1-frames": 3}
    assert [r["id"]["entity_id"] for r in rows_of(docs["c5"])] == ["report-frame-group-1-frames", "report-frame-groups"]


def test_c6_selects_the_group_frames_sorted(run):
    docs, _ = run
    assert [plain(r) for r in rows_of(docs["c6"])] == [
        {"id": "selection-1", "type": "selection", "quantity": 1, "unit": "each",
         "handles": ["A631", "A63B", "A646"]}]


def test_c9_records_the_ten_answered_settings(run):
    docs, _ = run
    (row,) = rows_of(docs["c9"])
    assert row["module_width"] == {"kind": "length", "value": 1.134, "unit": "m"}
    assert row["module_y_spacing"] == {"kind": "length", "value": 0.03, "unit": "m"}
    assert row["maintenance_margin"] == {"kind": "length", "value": 0.6, "unit": "m"}
    assert row["tilt"] == {"kind": "angle", "value": 12.5, "unit": "deg"}
    assert row["azimuth"] == {"kind": "angle", "value": 185.0, "unit": "deg"}
    assert (row["orientation"], row["manufacturer"], row["product"]) == (1, "ACME", "P440")


def test_c10_writes_the_string_data_file(run):
    docs, _ = run
    (row,) = rows_of(docs["c10"])
    assert (row["id"]["entity_id"], row["role"]) == ("file-1", "string-data")
    text = "".join(row["chunks"])
    data = json.loads(text)
    assert [g["name"] for g in data["groups"]] == ["Group 1", "Group 10", "Group 11", "Group 9"]
    (group,) = [g for g in data["groups"] if g["strings"]]
    assert group["handle"] == "A646"
    assert group["strings"] == [
        {"handle": "A912", "startPoint": {"handle": "A913", "coordinate": "10.00,5.00"},
         "endPoint": {"handle": "A914", "coordinate": "20.00,5.00"}},
        {"handle": "A90E", "startPoint": {"handle": "A90F", "coordinate": "10.00,15.00"},
         "endPoint": {"handle": "A910", "coordinate": "20.00,15.56"}}]
    assert "\r" not in text and not text.endswith("\n")
    assert row["lines"] == text.count("\n") + 1


def test_c11_reports_every_string_rebuilt(run):
    docs, _ = run
    assert [plain(r) for r in rows_of(docs["c11"])] == [
        {"id": "report-rebuilt-strings", "type": "report", "quantity": 1, "unit": "each",
         "name": "rebuilt-strings", "value": 4}]


def test_every_document_passes_the_comparator_against_itself(run):
    docs, _ = run
    for step, doc in docs.items():
        result = compare.compare(doc, copy.deepcopy(doc), "exports", capability=doc["provenance"]["capability"])
        assert result["verdict"] == "pass" and result["diffs"] == [], step


def test_a_single_step_equals_that_step_of_the_chain(run):
    docs, _ = run
    for step in ("c4", "c10"):
        only, _ = rev.run_steps(intake(), REVISION, only=step)
        assert list(only) == [step] and only[step] == docs[step]


def test_the_chain_is_deterministic(run):
    docs, _ = run
    again, _ = rev.run_steps(intake(), REVISION)
    assert again == docs


def test_no_storage_names_in_any_document(run):
    docs, _ = run
    text = "".join(rev.ev._serialize(d) for d in docs.values())
    assert not re.search(r"xrecord|regapp|leaf-properties|\"LEAF\"", text, re.IGNORECASE)


# ----------------------------------------------------------- the committed intake --

COMMITTED_INTAKE = Path(__file__).resolve().parent.parent / "docs" / "parity" / "evidence" / "rooftop" / "chain" / \
    "intake.json"


def committed_intake():
    return json.loads(COMMITTED_INTAKE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def committed():
    data = committed_intake()
    docs, state = rev.run_steps(data, REVISION)
    return data, docs, state


def test_the_committed_intake_runs_every_step(committed):
    data, docs, state = committed
    assert list(docs) == list(rev.STEP_IDS)
    for step, doc in docs.items():
        assert doc["units"] == "in" and not rows_of(doc, "unexpected-change"), step
        result = compare.compare(doc, copy.deepcopy(doc), "exports", capability=doc["provenance"]["capability"])
        assert result["verdict"] == "pass", step
    by_handle = {s["handle"]: s for s in data["strings"]}

    (flip,) = [plain(r) for r in rows_of(docs["c1"])]
    assert flip["string"] == "A67A" and flip["panels"] == by_handle["A67A"]["panels"][::-1] and flip["count"] == 14

    labels = {r["string"]: {k: v for k, v in plain(r).items() if k not in ("id", "type", "quantity", "unit", "string")}
              for r in rows_of(docs["c2"], "string-label")}
    assert labels == {"A902": by_handle["A912"]["label"], "A912": by_handle["A902"]["label"]}
    assert labels["A902"]["strings_on_inverter"] == 13 and labels["A912"]["strings_on_inverter"] == 9
    assert not rows_of(docs["c2"], "string")

    group = '[{"ColorIndex":0,"FrameHandles":["A646","A63B","A631"],"LastModifiedTicks":0,"Name":"%s"}]'
    for step, text in (("c3", group % "FGA"), ("c4", group % "FGB"), ("c7", "[]")):
        assert [(r["name"], r["value"]) for r in rows_of(docs[step])] == [("FrameGroups", text)], step
    assert {r["name"]: r["value"] for r in rows_of(docs["c5"])} == {"frame-groups": 1, "frame-group-1-frames": 3}
    assert rows_of(docs["c6"])[0]["handles"] == ["A631", "A63B", "A646"]
    (settings_row,) = rows_of(docs["c9"])
    assert settings_row["module_width"]["value"] == 1.134 and settings_row["product"] == "P440"

    (file_row,) = rows_of(docs["c10"])
    string_data = json.loads("".join(file_row["chunks"]))
    assert [g["name"] for g in string_data["groups"]] == [
        "Group 1", "Group 10", "Group 11", "Group 2", "Group 3", "Group 4", "Group 5", "Group 6", "Group 7",
        "Group 8", "Group 9"]
    assert all(g["strings"] == [] for g in string_data["groups"])   # the intake records no group outline

    assert [(r["name"], r["value"]) for r in rows_of(docs["c11"])] == [("rebuilt-strings", 173)]
    assert len(state["strings"]) == len(data["strings"]) == 173


def test_cli_writes_every_step_from_the_committed_intake(tmp_path, monkeypatch):
    monkeypatch.setattr(rev.ev, "fixture_revision", lambda p: REVISION)
    assert rev.main(["--intake", str(COMMITTED_INTAKE), "--out-dir", str(tmp_path / "out")]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(f"{s}.json" for s in rev.STEP_IDS)


# ------------------------------------------------------------- hand-made cases --

def test_a_swap_without_circuits_emits_labels_only():
    data = intake()
    for s in data["strings"]:
        s.pop("circuit", None)
    state = rev.initial_state(data)
    rows = rev.step_rows("c2", state)
    assert {r["type"] for r in rows} == {"string-label"}


def test_equal_labels_swap_to_no_rows():
    data = intake()
    data["strings"][2]["label"] = dict(data["strings"][1]["label"])
    data["strings"][2]["circuit"] = data["strings"][1]["circuit"]
    assert rev.step_rows("c2", rev.initial_state(data)) == []


def test_a_duplicate_create_changes_nothing():
    data = intake()
    data["settings"]["FrameGroups"] = [{"Name": "fga", "FrameHandles": ["A5DE"], "ColorIndex": 0,
                                        "LastModifiedTicks": 1}]
    state = rev.initial_state(data)
    assert rev.step_rows("c3", state) == []
    assert state["settings"]["FrameGroups"][0]["Name"] == "fga"


def test_list_with_no_groups_reports_zero():
    rows = rev.step_rows("c5", rev.initial_state(intake()))
    assert [(r["name"], r["value"]) for r in rows] == [("frame-groups", 0)]


def test_unchanged_export_settings_emit_no_row():
    state = rev.initial_state(intake())
    assert rev.step_rows("c9", state, [""] * 10) == []


def test_a_read_only_step_that_moves_state_is_flagged(monkeypatch):
    def mutating(state, answers):
        state["selection"].append("A1")
        return []
    monkeypatch.setitem(rev.STEPS, "c5", mutating)
    rows = rev.step_rows("c5", rev.initial_state(intake()))
    assert [r["type"] for r in rows] == ["unexpected-change"]


def test_an_unknown_string_is_refused():
    with pytest.raises(rev.EvidenceError, match="FFFF"):
        rev.step_rows("c1", rev.initial_state(intake()), ["string:FFFF"])


@pytest.mark.parametrize("step, answers", [("c1", ["A67A"]), ("c1", []), ("c3", ["FGA", "strings:A646"]),
                                           ("c10", ["strings:"])])
def test_malformed_answers_are_refused(step, answers):
    with pytest.raises(rev.EvidenceError):
        rev.step_rows(step, rev.initial_state(intake()), answers)


def _bad(mutate):
    data = intake()
    mutate(data)
    return data


@pytest.mark.parametrize("data", [
    [],
    _bad(lambda d: d.pop("settings")),
    _bad(lambda d: d.update(extra=1)),
    _bad(lambda d: d.update(units="furlong")),
    _bad(lambda d: d["strings"].append(copy.deepcopy(d["strings"][0]))),
    _bad(lambda d: d["strings"][0].update(handle="XYZ")),
    _bad(lambda d: d["strings"][0]["label"].update(kind=1)),
    _bad(lambda d: d["settings"].update(Other=1)),
    _bad(lambda d: d["settings"].update(FrameGroups=[{"Name": "x"}])),
    _bad(lambda d: d.update(panel_points={"C1": [0]})),
    _bad(lambda d: d["panel_groups"].append(dict(d["panel_groups"][0]))),
])
def test_malformed_intakes_are_refused(data):
    with pytest.raises(rev.EvidenceError):
        rev.run_steps(data, REVISION)


def test_a_bad_revision_is_refused():
    with pytest.raises(rev.EvidenceError):
        rev.run_steps(intake(), "HEAD")


def test_an_unknown_step_is_refused():
    with pytest.raises(rev.EvidenceError):
        rev.run_steps(intake(), REVISION, only="c8")


def test_a_large_file_is_compared_by_digest():
    text = ("x" * 99 + "\n") * 11_000
    (row,) = rev.file_rows({"string-data": text})
    assert "chunks" not in row and row["chars"] == len(text) and row["lines"] == 11_000
    assert "".join(row["head"]) == ("x" * 99 + "\n") * 40
    assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])


def test_file_text_is_normalized_and_chunked():
    (row,) = rev.file_rows({"string-data": "﻿a\r\nb\r\n" + ("y" * 10 + "\r\n") * 3000})
    assert "".join(row["chunks"]).startswith("a\nb\n") and all(len(c) <= rev.MAX_CHUNK for c in row["chunks"])
    with pytest.raises(rev.EvidenceError):
        rev.file_rows({"string-data": "z" * (rev.MAX_CHUNK + 1)})
    with pytest.raises(rev.EvidenceError):
        rev.file_rows({"not-a-role": "a"})


def test_cli_writes_every_step(tmp_path, monkeypatch):
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(intake()), encoding="utf-8")
    monkeypatch.setattr(rev.ev, "fixture_revision", lambda p: REVISION)
    assert rev.main(["--intake", str(path), "--out-dir", str(tmp_path / "out")]) == 0
    written = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert written == sorted(f"{s}.json" for s in rev.STEP_IDS)
    doc = json.loads((tmp_path / "out" / "c6.json").read_text(encoding="utf-8"))
    assert doc["after"]["rows"][0]["handles"] == ["A631", "A63B", "A646"]


def test_cli_refusal_exits_2(tmp_path, monkeypatch):
    path = tmp_path / "intake.json"
    path.write_text(json.dumps({"units": "in"}), encoding="utf-8")
    monkeypatch.setattr(rev.ev, "fixture_revision", lambda p: REVISION)
    assert rev.main(["--intake", str(path), "--out-dir", str(tmp_path / "out")]) == 2
