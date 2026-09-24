"""Studio's G35 evidence for the inverter cabling steps (i5, i11, i12, i13, i17, inverter-position).

A synthetic G35 state chain (state-i0, i10, i11, i12, i16), the committed state-i4 with its committed
combiner intake (LEAFCOMBINERAUTO reads the drawing its intake was dumped from) and a synthetic
panel-group intake run the producer end to end: the documents in the plugin adapter's shape (parameters
with the G22 answers and G30a form values, one fixture hash, the delta rows and the report rows), every
step written with exit status 0, the declared steps marked, one step alone equal to the same step of a
full run, and the refusals (a missing state, a bad revision, i5 without its intake) writing nothing.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
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


ev = _load("solar_inverter_cabling_evidence")
st = ev.st
REVISION = "0123456789abcdef0123456789abcdef01234567"
GROUPS = [{"handle": "A5", "outlines": [[[0.0, 300.0], [400.0, 300.0], [400.0, 500.0], [0.0, 500.0]]]},
          {"handle": "A6", "outlines": [[[600.0, 300.0], [1000.0, 300.0], [1000.0, 500.0], [600.0, 500.0]]]}]
L2_1, L2_8, CB_3, CB_14 = (0.0, 0.0), (1000.0, 0.0), (100.0, 200.0), (900.0, 200.0)
STRINGS = {"A01": ((50.0, 400.0), (150.0, 400.0), "+1/1a", CB_3),
           "A02": ((850.0, 400.0), (950.0, 400.0), "+2/8a", CB_14)}


def device(xy, role):
    return {"number": None, "role": role, "position": st.coordinate(*xy), "scale": 2.891214911191,
            "rotation": st.angle(0.0), "placement": None, "hardware": None,
            "_detail": {"type_key": "A", "is_l2": role != "combiner", "box_input_count": 0, "colour": 256}}


def feeder(l1, l2, points):
    return {"cable_kind": "feeder", "from": l1, "to": l2, "vertices": [st.coordinate(*p) for p in points],
            "length": {"kind": "length", "value": 40.0, "unit": "ft"},
            "_detail": {"circuit": f"F{l1}/{l2}", "gauge": "NA", "closed": False}}


def homerun(string, segment, leg, target, circuit):
    return {"cable_kind": "dc-homerun", "segment": segment, "from": string,
            "to": int(circuit.split("/")[1][:-1]),
            "vertices": [st.coordinate(*leg), st.coordinate(*target)],
            "length": {"kind": "length", "value": math.dist(leg, target) / 12.0, "unit": "ft"},
            "_detail": {"circuit": circuit, "gauge": "14 AWG", "closed": False}}


def state():
    cables = [feeder(3, 1, [CB_3, (-100.0, 200.0), (-100.0, 0.0), L2_1]),
              feeder(14, 8, [CB_14, (1100.0, 200.0), (1100.0, 0.0), L2_8])]
    for handle, (start, end, circuit, target) in STRINGS.items():
        cables += [homerun(handle, "start", start, target, circuit), homerun(handle, "end", end, target, circuit)]
    return {"format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
            "rows": {"device": [device(L2_1, "inverter"), device(L2_8, "inverter"),
                                device(CB_3, "combiner"), device(CB_14, "combiner")],
                     "string-assignment": [{"string": h, "device": int(c.split("/")[1][:-1]), "input": 1,
                                            "label": c, "colour": 1, "_detail": {"circuit": c}}
                                           for h, (_, _, c, _) in STRINGS.items()],
                     "cable": cables, "schedule": [], "lbd": []},
            "setting": {"InstallationDesign": "Roof", "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT),
                        "L1ToL2Assignments": {"3": 1, "14": 8}},
            "geometry": {"strings": [{"string": h, "vertices": [list(s), list(e)], "midpoint": None,
                                      "start": list(s), "end": list(e), "panels": []}
                                     for h, (s, e, _, _) in STRINGS.items()],
                         "panel_groups": [{"group": g["handle"], "position": [0.0, 0.0], "scale": 1.0,
                                           "rotation_deg": 0.0} for g in GROUPS]}}


COMMITTED = ev.DEFAULT_STATES


@pytest.fixture()
def chain(tmp_path):
    states = tmp_path / "states"
    states.mkdir()
    for number in (0, 10, 11, 12, 16):
        (states / f"state-i{number}.json").write_text(json.dumps(state()), encoding="utf-8")
    (states / "state-i4.json").write_bytes((COMMITTED / "state-i4.json").read_bytes())
    (states / "state-i19.json").write_bytes((COMMITTED / "state-i19.json").read_bytes())
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps({"panel_groups": GROUPS}), encoding="utf-8")
    return states, intake


def rows_of(doc, kind):
    return [r for r in doc["after"]["rows"] if r["type"] == kind]


def test_the_producer_writes_every_step(chain, tmp_path, capsys):
    states, intake = chain
    out = tmp_path / "out"
    code = ev.main(["--states", str(states), "--intake", str(intake), "--out", str(out),
                    "--combiner-intake", str(COMMITTED / "combiner-intake.json")])
    assert code == 0
    written = sorted(p.name for p in out.iterdir())
    assert written == ["i11.json", "i12.json", "i13.json", "i17.json", "i5.json", "l1.json", "l2.json",
                       "position.json"]
    assert "not written" not in capsys.readouterr().err
    docs = {name[:-5]: json.loads((out / name).read_text(encoding="utf-8")) for name in written}
    assert docs["i5"]["parameters"] == {"answers": [], "form_values": {"combiner_input_plan": "Apply"}}
    counts = {}
    for row in docs["i5"]["after"]["rows"]:
        key = (row["type"], row.get("cable_kind"))
        counts[key] = counts.get(key, 0) + 1
    assert counts == {("cable", "dc-homerun"): 346, ("cable", "feeder"): 14, ("device", None): 14,
                      ("setting", None): 4}
    assert all(r["role"] == "combiner" and r["change"] == "added" for r in rows_of(docs["i5"], "device"))
    fixture = st.digest(st.publish(st.load_state(states / "state-i0.json")))
    assert {doc["fixture_sha256"] for doc in docs.values()} == {fixture}
    assert rows_of(docs["i11"], "report")[0]["value"] == "no-feeders"
    assert rows_of(docs["i12"], "report")[0]["value"] == "no-change"
    assert [r["name"] for r in rows_of(docs["i11"], "setting")] == ["HomerunRouting"]
    lite = rows_of(docs["i13"], "cable")
    assert len(lite) == 2 and all(r["change"] == "changed" and r["length"]["value"] == 0.0 for r in lite)
    assert docs["i17"]["parameters"] == {"answers": ["A9D5", "20153.4,3589.19,0"]}
    assert docs["i17"]["provenance"]["declared_divergence"] is True
    assert docs["position"]["parameters"] == {"answers": ["A9D5"]}
    assert docs["position"]["provenance"]["declared_divergence"] is True
    assert docs["position"]["versions"]["engine"] == "server-builtin"
    moved = rows_of(docs["i17"], "device")
    # G35b: the acquired point, as is.
    assert len(moved) == 1 and moved[0]["position"]["value"] == [20150.90569654952, 3589.187227900471]
    assert len(rows_of(docs["i17"], "cable")) == 2
    # G36: the cabling studio on the committed i19 state. l1 opens it (the save's catalog pair, the report);
    # l2 opens it, Simulates and Commits: 173 combiners, 173 routed homeruns, 173 comb feeders, the 14 earlier
    # feeders removed, the 322 earlier homeruns kept.
    assert rows_of(docs["l1"], "report")[0]["value"] == "studio-opened"
    assert [r["name"] for r in rows_of(docs["l1"], "setting")] == ["HomerunRouting"]
    assert docs["l2"]["parameters"] == {"answers": [], "form_values": {"cabling_redesign_simulate": "Simulate",
                                                                       "cabling_redesign_commit": "Commit"}}
    kinds = {}
    for row in docs["l2"]["after"]["rows"]:
        key = (row["type"], row.get("cable_kind"), row.get("change"))
        kinds[key] = kinds.get(key, 0) + 1
    assert kinds == {("device", None, "added"): 173, ("cable", "dc-homerun", "added"): 173,
                     ("cable", "feeder", "added"): 173, ("cable", "feeder", "removed"): 14}
    assert {r["scale"] for r in rows_of(docs["l2"], "device")} == {8.673644733572}


def test_one_step_alone_equals_the_full_run(chain):
    states, intake = chain
    groups = ev.load_intake_groups(intake)
    combiner = ev.load_combiner_intake(COMMITTED / "combiner-intake.json")
    full, refused = ev.run_steps(states, groups, revision=REVISION, combiner_intake=combiner)
    alone, _ = ev.run_steps(states, groups, revision=REVISION, only="i13")
    assert alone == {"i13": full["i13"]} and refused == {}
    assert full["i13"]["revision"] == REVISION and full["i13"]["fallback_fields"] == []
    five, _ = ev.run_steps(states, groups, revision=REVISION, only="i5", combiner_intake=combiner)
    assert five == {"i5": full["i5"]}


def test_refusals_write_nothing(chain, tmp_path):
    states, intake = chain
    (states / "state-i12.json").unlink()
    out = tmp_path / "refused"
    assert ev.main(["--states", str(states), "--intake", str(intake), "--out", str(out)]) == 2
    assert not out.exists()
    with pytest.raises(ev.EvidenceError):
        ev.run_steps(states, ev.load_intake_groups(intake), revision="HEAD", only="i11")
    with pytest.raises(ev.EvidenceError, match="combiner intake"):
        ev.run_steps(states, ev.load_intake_groups(intake), only="i5")
    bad = tmp_path / "bad-intake.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert ev.main(["--states", str(states), "--intake", str(intake), "--out", str(out),
                    "--combiner-intake", str(bad), "--step", "i5"]) == 2
    assert not out.exists()
