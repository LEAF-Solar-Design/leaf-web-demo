"""Studio's G35 evidence for the inverter device steps (i1, i4, i10, i18, i19).

A synthetic G35 state chain (state-i0, i3, i9, i17, i18) and a synthetic panel-group intake run the
producer end to end: the five documents in the plugin adapter's shape (parameters with the G22
answers and G30a form_values, one fixture hash, the delta rows and the report rows), the comparator
accepting every document against itself, one step alone equal to the same step of a full run, the
CLI writing all five files, and the refusals (a missing state, a foreign intake, a bad revision)
writing nothing.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = _load("solar_inverter_devices_evidence")
st = ev.st
compare = ev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"
OUTLINE = [[0.0, 0.0], [400.0, 0.0], [400.0, 200.0], [0.0, 200.0]]


def device(x, y, role="inverter", placement=None, box=0):
    return {"number": None, "role": role, "position": st.coordinate(x, y), "scale": 2.891214911191,
            "rotation": st.angle(0.0), "placement": placement, "hardware": None,
            "_detail": {"type_key": "A", "is_l2": role != "combiner", "box_input_count": box, "colour": 256}}


# A feeder vertex 40 units from i18's typed point 20300,3589.19: the running snap takes it.
FEEDER = {"cable_kind": "feeder", "from": 14, "to": 8,
          "vertices": [st.coordinate(20260.0, 3589.0), st.coordinate(20260.0, 3700.0)],
          "length": {"kind": "length", "value": 1.0, "unit": "ft"}}


def state(devices=(), cables=(), settings=None, n_strings=49):
    strings = [f"{0x2000 + i:X}" for i in range(n_strings)]
    base = {"InstallationDesign": "Roof", "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT),
            "L1ToL2Assignments": {}}
    base.update(settings or {})
    return {"format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
            "rows": {"device": list(devices), "cable": list(cables), "schedule": [], "lbd": [],
                     "string-assignment": [{"string": h, "device": 0, "input": 24, "label": "-", "colour": 254,
                                            "_detail": {"circuit": "-"}} for h in strings]},
            "setting": base,
            "geometry": {"strings": [{"string": h, "vertices": [[1.0 * i, 300.0], [1.0 * i, 310.0]]}
                                     for i, h in enumerate(strings)],
                         "panel_groups": [{"group": "A5", "position": [0.0, 0.0], "scale": 1.0, "rotation_deg": 0.0}]}}


def chain(tmp_path):
    folder = tmp_path / "states"
    folder.mkdir(parents=True)
    l2 = [device(50.0, -50.0, placement="AUTO_PLACED_NOT_OPTIMIZED"), device(200.0, -50.0), device(350.0, -50.0)]
    combiners = [device(20150.0, 3589.0, "combiner", box=20)]
    states = {
        0: state(),
        3: state(l2),
        9: state(l2, settings={"L1ToL2Assignments": {str(i): (i + 1) // 2 for i in range(1, 15)}}),
        17: state(l2 + combiners, [FEEDER]),
        18: state(l2 + combiners + [device(20260.0, 3589.0, "combiner", box=20)], [FEEDER]),
    }
    for number, value in states.items():
        (folder / f"state-i{number}.json").write_bytes(st.canonical(value))
    (folder / "state-c0.json").write_bytes(st.canonical(state()))
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps({"panel_groups": [{"handle": "A5", "name": "G", "outlines": [OUTLINE]}]}),
                      encoding="utf-8")
    return folder, intake


@pytest.fixture()
def docs(tmp_path):
    folder, intake = chain(tmp_path)
    return ev.run_steps(folder, ev.load_intake_groups(intake), revision=REVISION)


def rows_of(doc, kind=None):
    return [r for r in doc["after"]["rows"] if kind is None or r["type"] == kind]


def test_every_step_with_its_capability_answers_and_one_fixture(docs):
    assert list(docs) == ["i1", "i4", "i10", "i18", "i19", "c1"]
    assert [d["provenance"]["capability"] for d in docs.values()] == \
        ["inverter-add", "inverter-balance", "skid-reconcile", "inverter-add", "adopt-l2-inverters",
         "devices-cloud-place"]
    assert docs["i18"]["parameters"] == {"answers": ["15", "20300,3589.19,0", "AddLater"],
                                         "form_values": {"select_equipment_type": "Combiner box"}}
    assert docs["i10"]["parameters"] == {"answers": []}
    assert len({d["fixture_sha256"] for d in docs.values()}) == 1
    for doc in docs.values():
        assert doc["versions"]["engine"] == "server-builtin" and doc["versions"]["capability"] == "0"
        assert doc["after"]["format"] == "inverter-v1" and doc["units"] == "in"
        assert doc["entity_mapping"] == {r["id"]["entity_id"]: r["id"]["entity_id"] for r in doc["after"]["rows"]}


def test_i1_places_the_fleet_and_i18_the_snapped_combiner(docs):
    added = rows_of(docs["i1"], "device")
    assert len(added) == 3 and all(r["change"] == "added" and r["role"] == "inverter" and
                                   r["placement"] == "AUTO_PLACED_NOT_OPTIMIZED" and r["scale"] == 8.673644733572
                                   for r in added)
    assert [r["id"]["entity_id"] for r in added] == ["device-1", "device-2", "device-3"]
    (combiner,) = rows_of(docs["i18"])
    assert combiner["change"] == "added" and combiner["role"] == "combiner" and combiner["placement"] is None
    assert combiner["position"]["value"] == [20260.0, 3589.0] and combiner["scale"] == 2.891214911191


def test_i4_and_i10_report(docs):
    assert [(r["type"], r["name"], r["value"]) for r in rows_of(docs["i4"])] == [("report", "message", "no-imbalance")]
    assert [(r["type"], r["value"]) for r in rows_of(docs["i10"])] == [("report", "mismatch")]


def test_i19_adopts_every_device_and_records_the_save(docs):
    rows = rows_of(docs["i19"])
    devices = [r for r in rows if r["type"] == "device"]
    assert len(devices) == 5 and all(r["change"] == "changed" and r["role"] == "l2-inverter" and
                                     r["placement"] == "FIXED_L2" for r in devices)
    assert devices[0]["hardware"] == {"model": "TMEIC NINJA-5.05", "ac_kw": 5050.0, "match_score": 100}
    (setting,) = [r for r in rows if r["type"] == "setting"]
    assert setting["name"] == "HomerunRouting"
    assert json.loads(setting["value"])["CableCatalog"] == st.DEFAULT_CATALOG * 2


def test_comparator_accepts_every_document_against_itself(docs):
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["provenance"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_one_step_equals_the_same_step_of_a_full_run(tmp_path, docs):
    folder, intake = chain(tmp_path / "again")
    only = ev.run_steps(folder, ev.load_intake_groups(intake), revision=REVISION, only="i18")
    assert list(only) == ["i18"] and only["i18"] == docs["i18"]


def test_cli_writes_the_six_files(tmp_path):
    folder, intake = chain(tmp_path)
    out = tmp_path / "out"
    assert ev.main(["--states", str(folder), "--out", str(out), "--intake", str(intake)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["c1.json", "i1.json", "i10.json", "i18.json", "i19.json", "i4.json"]
    doc = json.loads((out / "i1.json").read_text(encoding="utf-8"))
    assert doc["revision"] is None and doc["fallback_fields"] == ["revision"]
    assert "\n" not in (out / "i1.json").read_text(encoding="utf-8").rstrip("\n")


def test_refusals_write_nothing(tmp_path):
    folder, intake = chain(tmp_path)
    (folder / "state-i17.json").unlink()
    out = tmp_path / "out"
    assert ev.main(["--states", str(folder), "--out", str(out), "--intake", str(intake)]) == 2
    assert not out.exists()
    folder, _ = chain(tmp_path / "foreign")
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"panel_groups": [{"handle": "B7", "outlines": [OUTLINE]}]}), encoding="utf-8")
    assert ev.main(["--states", str(folder), "--out", str(out), "--intake", str(foreign)]) == 2
    assert not out.exists()
    with pytest.raises(ev.EvidenceError, match="revision"):
        ev.run_steps(folder, ev.load_intake_groups(intake), revision="HEAD", only="i4")
    with pytest.raises(ev.EvidenceError):
        ev.run_steps(folder, ev.load_intake_groups(intake), only="i2")


def test_c1_places_string_inverters_and_assigns_every_string(docs):
    c1 = docs["c1"]
    devices = rows_of(c1, "device")
    assert devices and all(r["role"] == "combiner" and r["placement"] == "AUTO_PLACED_NOT_OPTIMIZED"
                           and r["scale"] == 8.673644733572 for r in devices)
    assigned = rows_of(c1, "string-assignment")
    assert assigned and all(r["change"] == "changed" and r["label"].startswith("+") for r in assigned)
    assert c1["parameters"]["form_values"]["low_utilization_on_last_inverter"] == "Keep current"
