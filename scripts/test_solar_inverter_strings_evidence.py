"""Studio's G35 evidence for the string steps (i2, i3).

A synthetic G35 state chain (state-i0, state-i1 with two central inverters at the capture's first two
device positions and 40 unassigned strings, state-i2 the Studio engine's own i2 result) runs the
producer end to end under the capture host inputs: both documents in the plugin adapter's shape
(parameters with the G30a form_values, one fixture hash, the changed strings and the saved settings),
the comparator accepting every document against itself, one step alone equal to the same step of a
full run, the CLI writing both files, and the refusals (a missing state, a bad revision, an unknown
step) writing nothing.
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


ev = _load("solar_inverter_strings_evidence")
st = ev.st
compare = ev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"
N_STRINGS = 40


def device(x, y):
    return {"number": None, "role": "inverter", "position": st.coordinate(x, y), "scale": 8.673644733572,
            "rotation": st.angle(0.0), "placement": "AUTO_PLACED_NOT_OPTIMIZED", "hardware": None,
            "_detail": {"type_key": "A", "is_l2": True, "box_input_count": 0, "colour": 5}}


def state(devices=()):
    handles = [f"{0x2000 + i:X}" for i in range(N_STRINGS)]
    return {"format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
            "rows": {"device": list(devices), "cable": [], "schedule": [], "lbd": [],
                     "string-assignment": [{"string": h, "device": 0, "input": 24, "label": "-", "colour": 254,
                                            "_detail": {"circuit": "-", "string_number": 0}} for h in handles]},
            "setting": {"InstallationDesign": "Roof", "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT),
                        "NumMppt": 3, "StringPerMppt": 3},
            "geometry": {"strings": [{"string": h, "vertices": [[15000.0 + 60.0 * i, 1300.0], [15050.0 + 60.0 * i, 1300.0]],
                                      "start": [15000.0 + 60.0 * i, 1300.0], "end": [15050.0 + 60.0 * i, 1300.0],
                                      "panels": ["93A6"]} for i, h in enumerate(handles)],
                         "panel_groups": []}}


def chain(tmp_path):
    folder = tmp_path / "states"
    folder.mkdir(parents=True)
    fleet = [device(14298.6, 1644.1), device(16758.4, 1576.1)]
    states = {0: state(), 1: state(fleet)}
    after, _ = ev.strings.assign_strings(st.validate_state(states[1]), ev.CAPTURE_HOST, ev.STEP_FORM_VALUES["i2"])
    states[2] = st.publish(after)
    for number, value in states.items():
        (folder / f"state-i{number}.json").write_bytes(st.canonical(value))
    return folder


@pytest.fixture()
def docs(tmp_path):
    return ev.run_steps(chain(tmp_path), revision=REVISION)


def rows_of(doc, kind=None):
    return [r for r in doc["after"]["rows"] if kind is None or r["type"] == kind]


def test_both_steps_with_their_capability_answers_and_one_fixture(docs):
    assert list(docs) == ["i2", "i3"]
    assert [d["provenance"]["capability"] for d in docs.values()] == ["assign-strings", "color-strings"]
    assert docs["i2"]["parameters"] == {"answers": [], "form_values": {
        "assign_strings_to_central_inverters": "OK", "assign_strings_to_central_inverters_mode": "Auto",
        "excess_capacity": "Yes"}}
    assert docs["i3"]["parameters"] == {"answers": []}
    assert len({d["fixture_sha256"] for d in docs.values()}) == 1
    for doc in docs.values():
        assert doc["versions"]["engine"] == "server-builtin" and doc["versions"]["capability"] == "0"
        assert doc["after"]["format"] == "inverter-v1" and doc["units"] == "in"
        assert doc["entity_mapping"] == {r["id"]["entity_id"]: r["id"]["entity_id"] for r in doc["after"]["rows"]}


def test_i2_assigns_every_string_and_records_the_save(docs):
    strings = rows_of(docs["i2"], "string-assignment")
    assert len(strings) == N_STRINGS and all(r["change"] == "changed" for r in strings)
    assert {r["device"] for r in strings} == {1, 2}
    assert sum(1 for r in strings if r["device"] == 1) == 36     # 6 MPPTs x 6 strings
    assert {r["colour"] for r in strings if r["device"] == 1} == {2}
    assert {r["colour"] for r in strings if r["device"] == 2} == {5}
    assert all(r["label"].startswith("+") and r["label"].split("/")[1][:-1] == str(r["device"]) and
               r["input"] == ord(r["label"][-1]) - ord("a") + 1 for r in strings)
    settings = {r["name"]: r["value"] for r in rows_of(docs["i2"], "setting")}
    assert set(settings) == {"HomerunRouting", "TagHeight"}
    assert json.loads(settings["HomerunRouting"])["CableCatalog"] == st.DEFAULT_CATALOG * 2
    assert rows_of(docs["i2"], "device") == [] and rows_of(docs["i2"], "report") == []


def test_i3_recolours_by_inverter_number(docs):
    strings = rows_of(docs["i3"])
    assert len(strings) == N_STRINGS and {r["type"] for r in strings} == {"string-assignment"}
    assert {(r["device"], r["colour"]) for r in strings} == {(1, 1), (2, 2)}


def test_comparator_accepts_every_document_against_itself(docs):
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["provenance"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_one_step_equals_the_same_step_of_a_full_run(tmp_path, docs):
    only = ev.run_steps(chain(tmp_path / "again"), revision=REVISION, only="i3")
    assert list(only) == ["i3"] and only["i3"] == docs["i3"]


def test_cli_writes_both_files(tmp_path):
    folder = chain(tmp_path)
    out = tmp_path / "out"
    assert ev.main(["--states", str(folder), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["i2.json", "i3.json"]
    doc = json.loads((out / "i2.json").read_text(encoding="utf-8"))
    assert doc["revision"] is None and doc["fallback_fields"] == ["revision"]
    assert "\n" not in (out / "i2.json").read_text(encoding="utf-8").rstrip("\n")


def test_refusals_write_nothing(tmp_path):
    folder = chain(tmp_path)
    (folder / "state-i2.json").unlink()
    out = tmp_path / "out"
    assert ev.main(["--states", str(folder), "--out", str(out)]) == 2
    assert not out.exists()
    with pytest.raises(ev.EvidenceError, match="revision"):
        ev.run_steps(chain(tmp_path / "bad"), revision="HEAD", only="i2")
    with pytest.raises(ev.EvidenceError):
        ev.run_steps(chain(tmp_path / "unknown"), only="i4")
