"""Studio's G34/G34a/G34b Rooftop workflow flow evidence (flow-step and flow-event rows).

The committed drawings of the three G34a fixture states (the unsplit fixture, the zones fixture,
the solve fixture) carrying G34b's host input, and hand-made intakes, run through the producer:
every document must pass the comparator against itself, carry engine "server-builtin" and
capability version "0", carry the G34b envelope exactly (parameters, flow-v1, flow-<fixture>, the
fixture's own intake hash, input_sha256 per G17), list the eight Rooftop steps with flow, index,
step and can_advance only, read the host's L1/L2 setting and never the drawing's copy, and (on the
zones fixture only) carry the G34a event script rows exactly as the engine derives them, in G9
order, as compact JSON. The CLI writes a document that reads back as written and refuses an
untracked intake, a malformed intake or reopen intake, a missing or misplaced reopen intake, a bad
revision and an unknown fixture.
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


rev = _load("solar_workflow_flow_evidence")
compare = rev.compare
flow = rev.flow
REVISION = "0123456789abcdef0123456789abcdef01234567"

ROOT = Path(__file__).resolve().parents[1]
INTAKES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "flow"

READY = {
    "flow": "rooftop",
    "electrical_zones": [{"name": "Zone A", "panels": ["1A2", "1A3"], "panels_in_sequence": 12}],
    "elevation_zones": [],
    "missing_zone_panels": {"electrical": 0, "elevation": 0},
    "panels_in_sequence": 12,
    "global_string_sizing_confirmed": True,
    "panel_groups": 3,
    "strings": {"polylines": 5, "paths": 5, "persisted": 5},
    "inverters": 2,
    "l2_collectors": 0,
    "combiners": 2,
    "host_use_l2_collectors": False,
    "use_l2_collectors": False,
    "homeruns": 4,
}

# G34b: the capture host's per-user UseL2Collectors was true for every G34a capture.
CAPTURE_HOST_USE_L2 = True


def facts(base=READY, **over):
    raw = copy.deepcopy(base)
    raw.update(copy.deepcopy(over))
    return raw


def intake(name):
    """A committed drawing-facts intake carrying G34b's host input: the committed file's own value
    once the adapter has regenerated it, else the capture host's."""
    raw = json.loads((INTAKES / f"{name}.json").read_text(encoding="utf-8"))
    raw.setdefault("host_use_l2_collectors", CAPTURE_HOST_USE_L2)
    return raw


# G34a's fixture states on the committed drawings: the unsplit fixture (no zones, groups or
# strings), the zones fixture (unsized zones with panels, groups), the solve fixture (groups and
# strings, no zones, no confirmed drawing length); none carries equipment or homeruns.
UNSPLIT, ZONES, SOLVE = intake("unsplit"), intake("zsplit"), intake("full_run")
T, F = True, False
N, C, S = "not-started", "complete", "stale"


def build(raw, fixture, reopen=None):
    return rev.build_document(copy.deepcopy(raw), REVISION, fixture, copy.deepcopy(reopen))


def rows_of(doc, row_type):
    return [r for r in doc["after"]["rows"] if r["type"] == row_type]


def bare(row):
    return {k: v for k, v in row.items() if k not in ("id", "type", "quantity", "unit")}


@pytest.mark.parametrize("raw, fixture, reopen", [(UNSPLIT, "unsplit", None), (ZONES, "zones", UNSPLIT),
                                                  (SOLVE, "solve", None)])
def test_documents_pass_the_comparator_against_themselves(raw, fixture, reopen):
    doc = build(raw, fixture, reopen)
    assert compare.compare(doc, doc, "exports", capability=rev.CAPABILITY)["verdict"] == "pass"


def test_document_carries_the_engine_and_capability_version():
    doc = build(facts(), "solve")
    assert doc["versions"]["engine"] == "server-builtin"
    assert doc["versions"]["capability"] == "0"
    assert doc["versions"]["producer"] == "studio"
    assert doc["provenance"]["capability"] == "leaf-workflow-palette"


@pytest.mark.parametrize("raw, fixture, reopen, parameters", [
    (UNSPLIT, "unsplit", None, {"flow": "rooftop", "fixture": "unsplit"}),
    (ZONES, "zones", UNSPLIT, {"flow": "rooftop", "fixture": "zones", "reopen_fixture": "unsplit"}),
    (SOLVE, "solve", None, {"flow": "rooftop", "fixture": "solve"}),
])
def test_the_envelope_is_g34b(raw, fixture, reopen, parameters):
    doc = build(raw, fixture, reopen)
    assert doc["parameters"] == parameters
    assert doc["after"]["format"] == "flow-v1"
    assert doc["after"]["source_revision"] == f"flow-{fixture}"
    assert doc["fixture_sha256"] == compare.semantic_hash(raw)
    assert doc["input_sha256"] == compare.semantic_hash({"fixture_sha256": doc["fixture_sha256"],
                                                         "parameters": parameters})
    assert doc["output_sha256"] == compare.semantic_hash(doc["after"])
    assert (doc["state"], doc["survived_reopen"]) == ("committed", True)


def test_flow_step_rows_are_the_eight_rooftop_steps_with_g34a_fields_only():
    doc = build(facts(homeruns=0), "solve")
    steps = rows_of(doc, "flow-step")
    assert [r["id"]["entity_id"] for r in steps] == [f"flow-step-{n}" for n in range(1, 9)]
    assert [bare(r) for r in steps] == flow.flow_steps(facts(homeruns=0))
    assert {frozenset(r) for r in steps} == {frozenset({"id", "type", "quantity", "unit", "flow", "index", "step",
                                                        "can_advance"})}


# The literal port on the committed drawings with the host's setting off (the Combiners step passes
# through) and on, which gives G34a's observed values exactly.
@pytest.mark.parametrize("raw, fixture, reopen, host, expected", [
    (UNSPLIT, "unsplit", None, False, [F, F, F, F, T, F, T, T]),
    (ZONES, "zones", UNSPLIT, False, [F, T, F, F, T, F, T, T]),
    (SOLVE, "solve", None, False, [F, F, T, F, T, F, T, T]),
    (UNSPLIT, "unsplit", None, True, [F, F, F, F, F, F, T, T]),
    (ZONES, "zones", UNSPLIT, True, [F, T, F, F, F, F, T, T]),
    (SOLVE, "solve", None, True, [F, F, T, F, F, F, T, T]),
])
def test_flow_step_rows_on_the_g34a_fixture_states(raw, fixture, reopen, host, expected):
    doc = build(facts(raw, host_use_l2_collectors=host), fixture, reopen)
    assert [r["can_advance"] for r in rows_of(doc, "flow-step")] == expected


def test_the_drawing_copy_of_the_setting_is_not_read():
    on = build(facts(SOLVE, use_l2_collectors=True), "solve")
    off = build(facts(SOLVE, use_l2_collectors=False), "solve")
    assert on["after"] == off["after"]
    assert on["fixture_sha256"] != off["fixture_sha256"]


def test_only_the_zones_fixture_carries_event_rows():
    assert rows_of(build(UNSPLIT, "unsplit"), "flow-event") == []
    assert rows_of(build(SOLVE, "solve"), "flow-event") == []
    assert len(rows_of(build(ZONES, "zones", UNSPLIT), "flow-event")) == 6


def test_flow_event_rows_are_the_g34a_script():
    doc = build(ZONES, "zones", UNSPLIT)
    rows = rows_of(doc, "flow-event")
    assert [r["id"]["entity_id"] for r in rows] == [f"flow-event-{n}" for n in range(1, 7)]
    assert [bare(r) for r in rows] == [
        {"event": "jump", "current_index": 1, "statuses": [C] + [N] * 7},
        {"event": "advance", "advanced": True, "current_index": 2, "statuses": [C, C] + [N] * 6},
        {"event": "advance", "advanced": False, "current_index": 2, "statuses": [C, C] + [N] * 6},
        {"event": "back", "current_index": 1, "statuses": [C, C, S, S, S, S, S, N]},
        {"event": "reopen", "current_index": 1, "statuses": [C, C, S, S, S, S, S, N]},
        {"event": "reopen", "current_index": 0, "statuses": [N] * 8},
    ]


def test_rows_are_sorted_by_type_then_numeric_id():
    doc = build(facts(), "zones", facts())
    types = [r["type"] for r in doc["after"]["rows"]]
    assert types == ["flow-event"] * 12 + ["flow-step"] * 8
    assert doc["after"]["rows"][9]["id"] == {"entity_id": "flow-event-10"}
    assert doc["after"]["rows"][11]["id"] == {"entity_id": "flow-event-12"}


def test_the_document_serializes_as_compact_json():
    doc = build(facts(), "zones", facts())
    text = rev.serialize(doc)
    assert "\n" not in text and ", " not in text and '": ' not in text
    assert json.loads(text) == doc


def test_hashes_bind_the_own_intake_and_the_rows():
    doc = build(ZONES, "zones", UNSPLIT)
    assert doc["fixture_sha256"] == compare.semantic_hash(ZONES)
    other_reopen = build(ZONES, "zones", ZONES)
    assert (other_reopen["fixture_sha256"], other_reopen["input_sha256"]) == \
        (doc["fixture_sha256"], doc["input_sha256"])
    assert other_reopen["output_sha256"] != doc["output_sha256"]
    solve = build(SOLVE, "solve")
    assert solve["fixture_sha256"] != doc["fixture_sha256"]
    assert solve["output_sha256"] != doc["output_sha256"]


@pytest.mark.parametrize("fixture, reopen", [("zones", None), ("unsplit", UNSPLIT), ("solve", UNSPLIT)])
def test_the_reopen_intake_belongs_to_the_zones_fixture_only(fixture, reopen):
    with pytest.raises(rev.EvidenceError):
        build(ZONES, fixture, reopen)


@pytest.mark.parametrize("revision", ["abc", "0123456789ABCDEF0123456789ABCDEF01234567", None])
def test_a_bad_revision_is_refused(revision):
    with pytest.raises(rev.EvidenceError):
        rev.build_document(facts(), revision, "solve")


@pytest.mark.parametrize("fixture", ["rooftop-unsplit", None, "zsplit"])
def test_an_unknown_fixture_is_refused(fixture):
    with pytest.raises(rev.EvidenceError):
        rev.build_document(facts(), REVISION, fixture)


def _without(key):
    raw = facts()
    del raw[key]
    return raw


@pytest.mark.parametrize("raw", [[], {"panel_groups": 3}, facts(panel_groups=-1), facts(flow="ground"),
                                 facts(strings={"polylines": 0}), _without("host_use_l2_collectors")])
def test_a_malformed_intake_is_refused(raw):
    with pytest.raises(rev.EvidenceError):
        rev.build_document(raw, REVISION, "solve")


def test_a_malformed_reopen_intake_is_refused():
    with pytest.raises(rev.EvidenceError):
        build(ZONES, "zones", {"panel_groups": 3})


def test_cli_writes_a_document_that_reads_back(tmp_path):
    source, out = tmp_path / "intake.json", tmp_path / "out" / "flow.json"
    source.write_text(json.dumps(facts()), encoding="utf-8")
    assert rev.main(["--intake", str(source), "--out", str(out), "--revision", REVISION, "--fixture", "solve"]) == 0
    doc = compare.load_evidence(out)
    compare.validate_evidence(doc, "exports")
    assert out.read_text(encoding="utf-8") == rev.serialize(build(facts(), "solve"))


def test_cli_with_a_reopen_intake_writes_the_event_rows(tmp_path):
    source, reopen, out = tmp_path / "zones.json", tmp_path / "unsplit.json", tmp_path / "flow.json"
    source.write_text(json.dumps(ZONES), encoding="utf-8")
    reopen.write_text(json.dumps(UNSPLIT), encoding="utf-8")
    assert rev.main(["--intake", str(source), "--reopen-intake", str(reopen), "--out", str(out),
                     "--revision", REVISION, "--fixture", "zones"]) == 0
    assert out.read_text(encoding="utf-8") == rev.serialize(build(ZONES, "zones", UNSPLIT))
    assert len(rows_of(compare.load_evidence(out), "flow-event")) == 6


def test_cli_on_the_committed_drawings(tmp_path):
    # The committed drawings as the host-input intakes the adapter regenerates them into.
    source, reopen, out = tmp_path / "zsplit.json", tmp_path / "unsplit.json", tmp_path / "zones.json"
    source.write_text(json.dumps(intake("zsplit")), encoding="utf-8")
    reopen.write_text(json.dumps(intake("unsplit")), encoding="utf-8")
    assert rev.main(["--intake", str(source), "--reopen-intake", str(reopen), "--out", str(out),
                     "--revision", REVISION, "--fixture", "zones"]) == 0
    doc = compare.load_evidence(out)
    assert doc["parameters"] == {"flow": "rooftop", "fixture": "zones", "reopen_fixture": "unsplit"}
    assert [r["event"] for r in rows_of(doc, "flow-event")] == ["jump", "advance", "advance", "back", "reopen",
                                                               "reopen"]
    assert rows_of(doc, "flow-event")[-1]["statuses"] == [N] * 8


def test_cli_refuses_an_untracked_intake(tmp_path):
    source, out = tmp_path / "intake.json", tmp_path / "out" / "flow.json"
    source.write_text(json.dumps(facts()), encoding="utf-8")
    assert rev.main(["--intake", str(source), "--out", str(out), "--fixture", "solve"]) == 2
    assert not out.exists()


def test_cli_refuses_the_zones_fixture_without_its_reopen_intake(tmp_path):
    source, out = tmp_path / "zones.json", tmp_path / "flow.json"
    source.write_text(json.dumps(ZONES), encoding="utf-8")
    assert rev.main(["--intake", str(source), "--out", str(out), "--revision", REVISION, "--fixture", "zones"]) == 2
    assert not out.exists()


def test_cli_refuses_an_intake_that_is_not_json(tmp_path):
    source, out = tmp_path / "intake.json", tmp_path / "flow.json"
    source.write_text("not json", encoding="utf-8")
    assert rev.main(["--intake", str(source), "--out", str(out), "--revision", REVISION, "--fixture", "solve"]) == 2
    assert not out.exists()


def test_cli_refuses_a_reopen_intake_that_is_not_json(tmp_path):
    source, reopen, out = tmp_path / "zones.json", tmp_path / "unsplit.json", tmp_path / "flow.json"
    source.write_text(json.dumps(ZONES), encoding="utf-8")
    reopen.write_text("not json", encoding="utf-8")
    assert rev.main(["--intake", str(source), "--reopen-intake", str(reopen), "--out", str(out),
                     "--revision", REVISION, "--fixture", "zones"]) == 2
    assert not out.exists()
