"""The inverter output steps' Studio producer: each step from the previous synthetic state, the envelope,
the declared rows (the i9 workbook as a file row), and its refusals. States are synthetic and authored
here; nothing reads plugin evidence."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = _load("solar_inverter_outputs_evidence", HERE / "solar_inverter_outputs_evidence.py")
st = ev.st

SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]


def coord(x, y):
    return {"kind": "coordinate", "value": [x, y], "unit": "in"}


def synthetic_state():
    strings = [("1000", "+1/1a", 14), ("1001", "+2/1a", 13)]
    return {
        "format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
        "rows": {
            "device": [{"number": None, "role": "combiner", "position": coord(100.0, 100.0), "scale": 2.0,
                        "rotation": {"kind": "angle", "value": 0.0, "unit": "deg"}, "placement": None,
                        "hardware": None,
                        "_detail": {"type_key": "A", "is_l2": False, "box_input_count": 20, "colour": 1}}],
            "string-assignment": [{"string": h, "device": 1, "input": 1, "label": c, "colour": 1,
                                   "_detail": {"circuit": c, "panel_count": str(n)}} for h, c, n in strings],
            "cable": [{"cable_kind": "dc-homerun", "segment": "end", "from": "1000", "to": 1,
                       "vertices": [coord(0.0, 0.0), coord(0.0, 240.0)],
                       "length": {"kind": "length", "value": 20.0, "unit": "ft"},
                       "_detail": {"circuit": "+1/1a", "gauge": "NA", "closed": False}},
                      {"cable_kind": "feeder", "from": 1, "to": 1,
                       "vertices": [coord(0.0, 0.0), coord(100.0, 0.0)],
                       "length": {"kind": "length", "value": 8.3, "unit": "ft"},
                       "_detail": {"circuit": "F1/1", "gauge": "NA", "closed": False}}],
            "schedule": [], "lbd": []},
        "setting": {"InstallationDesign": "Roof", "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT),
                    "L1ToL2Assignments": {"1": 1, "2": 1}},
        "geometry": {"strings": [{"string": h, "vertices": [[0.0, 0.0], [120.0, 0.0]]} for h, _, _ in strings],
                     "panel_groups": [{"group": "A5", "position": [0.0, 0.0], "scale": 1.0, "rotation_deg": 0.0}]}}


@pytest.fixture()
def inputs(tmp_path):
    states = tmp_path / "states"
    states.mkdir()
    text = json.dumps(synthetic_state())
    for n in range(0, 21):
        (states / f"state-i{n}.json").write_text(text, encoding="utf-8")
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps({"panel_groups": [{"handle": "A5", "outlines": [SQUARE]}]}), encoding="utf-8")
    return states, intake, tmp_path / "out"


def run(inputs):
    states, intake, out_dir = inputs
    code = ev.main(["--states", str(states), "--intake", str(intake), "--out", str(out_dir)])
    docs = {step: json.loads((out_dir / f"{step}.json").read_text(encoding="utf-8"))
            for step in ev.STEP_IDS if (out_dir / f"{step}.json").exists()}
    return code, docs


def report(doc):
    return [r["value"] for r in doc["after"]["rows"] if r["type"] == "report"]


def test_the_producer_writes_the_nine_steps(inputs):
    code, docs = run(inputs)
    assert code == 0 and sorted(docs) == sorted(["i6", "i7", "i8", "i9", "i14", "i15", "i16", "i20", "i21"])
    assert (inputs[2] / ev.WORKBOOK_NAME).read_bytes()[:2] == b"PK"
    assert report(docs["i6"]) == ["no-change"]
    assert report(docs["i7"]) == ["none-snapped"]
    assert report(docs["i16"]) == ["no-homerun-trunk"]
    assert report(docs["i21"]) == ["no-tracker-rows"]
    assert [(r["type"], r.get("value", r.get("name"))) for r in docs["i20"]["after"]["rows"]][0] == \
        ("report", "no-trench")
    assert [r["name"] for r in docs["i20"]["after"]["rows"] if r["type"] == "setting"] == ["HomerunRouting"]


def test_the_step_rows_carry_the_changed_objects(inputs):
    _, docs = run(inputs)
    schedules = [r for r in docs["i8"]["after"]["rows"] if r["type"] == "schedule"]
    assert [(r["id"]["entity_id"], r["index"], r["cells"][0][0]) for r in schedules] == \
        [("schedule-1", 1, "STRING SCHEDULE"), ("schedule-2", 2, "COMBINER / INVERTER SCHEDULE"),
         ("schedule-3", 3, "EQUIPMENT SCHEDULE")]
    assert schedules[2]["position"]["value"] == [22000.0, 5500.0]
    (marker,) = docs["i14"]["after"]["rows"]
    assert (marker["type"], marker["lbd_kind"], marker["change"], marker["feeder"]) == \
        ("lbd", "marker", "added", {"id": None, "ref": "device"})
    (block,) = docs["i15"]["after"]["rows"]
    assert (block["lbd_kind"], block["feeder"]["ref"]) == ("block", "cable")
    rows = docs["i9"]["after"]["rows"]
    assert [r["type"] for r in rows] == ["file"] and rows[0]["role"] == "string-export-xlsx"
    assert "# String Schedule\n" in "".join(rows[0]["chunks"])


def test_the_i9_workbook_is_the_export_form_s_on_the_i9_host(inputs):
    _, docs = run(inputs)
    (row,) = docs["i9"]["after"]["rows"]
    lines = "".join(row["chunks"]).split("\n")
    assert [line for line in lines if line.startswith("# ")] == \
        ["# Homeruns", "# Equipment Schedule", "# Inverter Schedule", "# String Schedule"]   # no feeders: L2 off
    assert lines[2:6] == ["1 - a\t1\tEnd Homerun\tNA\t20.00\t30.00\t15.00",     # SelectAll: newest first
                          "1 - a\t1\tString\t14\t10.00\t30.00\t15.00",
                          "1 - a\t2\tString\t13\t10.00\t10.00\t5.00",
                          "-\t-\tFeeder\tNA\t8.33\tN/A\tN/A"]
    assert "INV-1..5\tString Inverter\tSungrow\tSG250HX\t5\t250.0kW AC, 800V, 180.5A\tUL 1741\t690.4" in lines
    assert "INV-1\tA\t2\t2/2\t14, 13\t27\t-\t-\t1500\t-\t" in lines
    assert ev.host_for("i9")["UseL2Collectors"] is False and ev.host_for("i8")["UseL2Collectors"] is True


def test_the_envelope_follows_the_adapter(inputs):
    _, docs = run(inputs)
    assert docs["i8"]["parameters"] == {"answers": ["22000,5500"]}
    assert docs["i15"]["parameters"] == {"answers": ["BA99"]} == docs["i20"]["parameters"]
    assert docs["i9"]["parameters"] == {"answers": [], "form_values": {"branch_string_export": "Export All"}}
    assert docs["i6"]["provenance"]["declared_divergence"] is True
    assert docs["i9"]["provenance"]["declared_divergence"] is True
    assert "declared_divergence" not in docs["i8"]["provenance"]
    for step, doc in docs.items():
        assert doc["versions"]["engine"] == "server-builtin" and doc["versions"]["capability"] == "0"
        assert doc["after"]["source_revision"] == step and doc["after"]["format"] == "inverter-v1"
        assert doc["fixture_sha256"] == docs["i6"]["fixture_sha256"]
        assert set(doc["entity_mapping"]) == {r["id"]["entity_id"] for r in doc["after"]["rows"]}


def test_a_missing_state_writes_nothing(inputs):
    states, intake, out_dir = inputs
    (states / "state-i13.json").unlink()
    code, docs = run(inputs)
    assert code == 2 and docs == {}


def test_an_intake_of_another_drawing_is_refused(inputs):
    states, intake, out_dir = inputs
    intake.write_text(json.dumps({"panel_groups": [{"handle": "B7", "outlines": [SQUARE]}]}), encoding="utf-8")
    code, docs = run(inputs)
    assert code == 2 and docs == {}


def test_one_step_only(inputs):
    states, intake, out_dir = inputs
    assert ev.main(["--states", str(states), "--intake", str(intake), "--out", str(out_dir), "--step", "i16"]) == 0
    assert sorted(p.name for p in out_dir.iterdir()) == ["i16.json"]
