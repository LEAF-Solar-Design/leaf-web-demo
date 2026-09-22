"""Offline checks for the Studio single-string-add producer on the committed rooftop capture.

The solve half is the solve producer and the delete half is the string-delete
producer, so the circuits this run adds beside are the 66 the plugin itself
committed (server/tests/fixtures/w1_rooftop_unsplit_solve.json) and the panels it
wires are the ones a real delete freed. That capture is also the baseline: what the
run must leave untouched is the capture's own memberships, minus the circuits it
deleted, plus exactly the one circuit it added.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from unittest.mock import patch

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_module("solar_w1_studio_string_add")
adapter = load_module("solar_studio_evidence")
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/rooftop_unsplit.dwg"
INTAKE = ROOT / "data/rooftop_unsplit.intake.json"
PLACEMENT = ROOT / "data/rooftop_unsplit.licensed-placement.json"
CAPTURE = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_solve.json"
SIZING = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json"
STRING_COUNT = 66
PANEL_COUNT = 882
# Two circuits deleted, one added: the plugin's own capture added one circuit over
# some of the panels its delete had freed.
DELETED = 2


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def arguments(folder, delete=(), add=(), **overrides):
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    replay = save(folder / "replay.json", {"responses": [group["response"] for group in capture["groups"]]})
    values = {
        "fixture": FIXTURE, "intake": INTAKE, "placement": PLACEMENT,
        "sizing-response": SIZING, "max-string-length": 14,
        "dwgname": "rooftop_demo.dwg", "replay": replay,
        "out-graph": folder / "graph.json", "out-metadata": folder / "metadata.json",
    }
    values.update(overrides)
    argv = [part for key, value in values.items() for part in ("--" + key, str(value))]
    argv += [part for name in delete for part in ("--delete", str(name))]
    return argv + [part for handle in add for part in ("--add", str(handle))]


def run_offline(argv):
    with patch.object(producer.solve.cloud.requests.sessions.Session, "request",
                      side_effect=AssertionError("offline producer must not use network")), \
            patch.object(producer.solve.cloud, "resolve_grant",
                         side_effect=AssertionError("replay must not resolve a private grant")), \
            patch.object(producer.solve.sizing_cloud, "resolve_grant",
                         side_effect=AssertionError("recorded sizing must not resolve a private grant")):
        return producer.main(argv)


def plugin_strings():
    """The 66 memberships the licensed plugin committed, as normalized handles."""
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    return [[producer.normalized_handle(handle) for handle in members]
            for group in capture["groups"] for members in group["plugin_strings"]]


def plugin_neutral_ids():
    """Every circuit's neutral id, derived from the capture the same way the producer does."""
    return sorted("string:" + members[0] for members in plugin_strings())


def deleted_ids():
    return plugin_neutral_ids()[:DELETED]


def freed_handles():
    """Every panel the two deleted circuits leave unwired, by handle."""
    erased = set(deleted_ids())
    return sorted(handle for members in plugin_strings()
                  if "string:" + members[0] in erased for handle in members)


def added_handles():
    """Three freed panels named in DESCENDING handle order, which no solve produces.

    Three is shorter than every circuit the solver cut on this drawing, and the order
    is the reverse of the sorted one, so the receipt proves the add rather than
    re-proving the solve.
    """
    return freed_handles()[:3][::-1]


def memberships(graph):
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    return [[handles[ref] for ref in string["ordered_panel_refs"]] for string in graph["strings"]]


def read(folder):
    return (producer.deserialize_graph((folder / "graph.json").read_bytes()),
            json.loads((folder / "metadata.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def committed(tmp_path_factory):
    """Two of the plugin's own circuits deleted, then one added over three freed panels."""
    folder = tmp_path_factory.mktemp("studio-string-add")
    assert run_offline(arguments(folder, delete=deleted_ids(), add=added_handles())) == 0
    graph, metadata = read(folder)
    return graph, metadata, folder


def test_the_added_circuit_holds_the_named_panels_in_the_named_order(committed):
    graph, metadata, _ = committed
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    handles = added_handles()
    added = [members for members in memberships(graph) if members == handles]
    # Exactly one circuit carries that membership, in that order, and the order is
    # the selection's rather than the drawing's.
    assert added == [handles] and handles != sorted(handles)
    assert len(graph["strings"]) == STRING_COUNT - DELETED + 1
    index = next(i for i, members in enumerate(memberships(graph)) if members == handles)
    circuit = graph["strings"][index]
    assert circuit["module_count"] == len(handles)
    # Shorter than anything the solver cut on this drawing, so a re-solve cannot
    # produce this state: the receipt proves the ADD.
    assert len(handles) < min(len(members) for members in plugin_strings())
    # SINGLESTRING is local: no solver ran for this circuit and no inverter was chosen.
    assert circuit["inverter_ref"] is None
    assert "response_sha256" not in circuit["extra"]["polarity"]
    assert circuit["rev"] == graph["rev"]


def test_every_other_circuit_is_the_plugins_own(committed):
    graph, _, _ = committed
    erased = set(deleted_ids())
    expected = [members for members in plugin_strings() if "string:" + members[0] not in erased]
    assert len(expected) == STRING_COUNT - DELETED
    assert sorted(memberships(graph)) == sorted(expected + [added_handles()])
    # Every circuit the plugin solved still carries the revision its solve commit
    # gave it: only the added one is new at this revision.
    assert sum(1 for string in graph["strings"] if string["rev"] == graph["rev"]) == 1


def test_the_panels_the_add_left_over_stay_unassigned(committed):
    graph, _, _ = committed
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    assert len(handles) == PANEL_COUNT
    leftover = sorted(set(freed_handles()) - set(added_handles()))
    coverage = graph["extra"]["solve_coverage"]
    assert coverage["duplicate_panel_refs"] == []
    assert sorted(handles[ref] for ref in coverage["unassigned_panel_refs"]) == leftover
    # A leftover panel keeps its geometry, its group and its matrix cell; it is only unwired.
    for ref in coverage["unassigned_panel_refs"]:
        panel = next(p for p in graph["panels"] if p["id"] == ref)
        assert panel["assignment"] == {"string_ref": None, "seq": None}
        assert panel["frame_ref"] is not None and panel["matrix_cell"] is not None
    assert [frame["name"] for frame in graph["frames"]] == [f"Group {i}" for i in range(1, 8)]


def test_metadata_records_the_solve_the_delete_and_the_add(committed):
    graph, metadata, folder = committed
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
    assert metadata["parameters"] == {"family": "strings", "max_string_length": 14}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_string_add.v1",
        # The LEDGER's capability_version for string-single-add, which the gate compares.
        "capability": "0", "engine": "server-builtin", "catalog": "none",
        "solver": "leaf-stringer-service"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["before"] == {"recorded": False}
    assert metadata["changes"] == {"created": [], "modified": [], "deleted": []}
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    assert metadata["elapsed_ms"] >= 0
    assert metadata["execution_mode"] == "replay" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert set(metadata["synthetic_fields"]) == {"before", "changes", "versions.solver",
                                                 "settings/string_sizing"}
    provenance = metadata["provenance"]
    for field, path in (("intake_sha256", INTAKE), ("placement_sha256", PLACEMENT),
                        ("sizing_response_sha256", SIZING), ("replay_sha256", folder / "replay.json")):
        assert provenance[field] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(provenance["response_sha256s"]) == 7
    # The run solved first and freed the panels it used: these separate a real add
    # from a run that never solved or never had a free panel to wire.
    assert provenance["strings_solved"] == STRING_COUNT
    assert provenance["strings_deleted"] == DELETED
    assert provenance["deleted_strings"] == sorted(deleted_ids())
    assert provenance["added_panels"] == added_handles()
    assert provenance["added_string"] == "string:" + added_handles()[0]
    assert provenance["added_length"] == len(added_handles())
    assert provenance["strings_remaining"] == len(graph["strings"]) == STRING_COUNT - DELETED + 1
    assert provenance["panels_unassigned"] == len(graph["extra"]["solve_coverage"]["unassigned_panel_refs"])
    assert "grant_ref" not in json.dumps((graph, metadata))


def test_the_mapping_covers_every_panel_and_every_committed_circuit(committed):
    """Rule G8: the evidence names every panel, the survivors, and the added circuit."""
    graph, metadata, _ = committed
    mapping = metadata["entity_mapping"]
    assert mapping == producer.evidence_mapping(graph)
    assert set(mapping) == {entity["id"] for entity in graph["panels"] + graph["strings"]}
    assert len(set(mapping.values())) == len(mapping)
    for panel in graph["panels"]:
        assert mapping[panel["id"]] == producer.normalized_handle(panel["provenance"]["source_handle"])
    for string in graph["strings"]:
        assert mapping[string["id"]] == "string:" + mapping[string["ordered_panel_refs"][0]]
    # The added circuit is named by the evidence; the deleted ones are gone from it.
    assert "string:" + added_handles()[0] in set(mapping.values())
    assert set(deleted_ids()).isdisjoint(set(mapping.values()))


def test_produced_evidence_validates_joint_identity_contract(committed):
    graph, metadata, _ = committed
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "strings", metadata)
    adapter.compare.validate_evidence(evidence, "strings")
    assert evidence["revision"] == metadata["revision"]
    assert evidence["provenance"]["studio_graph_rev"] == graph["rev"]
    assert evidence["units"] == "in"
    ids = [metadata["entity_mapping"][record["id"]["entity_id"]] for record in evidence["after"]["strings"]]
    assert ids == sorted(ids) and len(ids) == STRING_COUNT - DELETED + 1
    assert set(deleted_ids()).isdisjoint(ids)
    record = next(item for item in evidence["after"]["strings"]
                  if metadata["entity_mapping"][item["id"]["entity_id"]]
                  == "string:" + added_handles()[0])
    # Rule G9: a circuit's ordered_membership is the capability's output, so it keeps
    # the order the selection gave it.
    assert [metadata["entity_mapping"][ref["entity_id"]] for ref in record["ordered_membership"]] \
        == added_handles()
    assert evidence["after"]["duplicate_panels"] == []
    assert len(evidence["after"]["unassigned_panels"]) == metadata["provenance"]["panels_unassigned"]
    assert evidence["after"]["length_distribution"] == sorted(
        string["module_count"] for string in graph["strings"])
    assert evidence["after"]["length_distribution"][0] == len(added_handles())
    assert all(item["polarity"] == "positive" for item in evidence["after"]["strings"])
    assert (graph, metadata) == originals


@pytest.mark.parametrize("add,message", [
    (("zz",), "invalid source handle"),
    (("",), "invalid source handle"),
    (("string:93A6",), "invalid source handle"),
])
def test_a_malformed_panel_handle_is_refused_before_the_solve_runs(tmp_path, capsys, add, message):
    with patch.object(producer.solve, "produce",
                      side_effect=AssertionError("a malformed handle must be refused before the solve")):
        assert run_offline(arguments(tmp_path, delete=deleted_ids(), add=add)) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_a_repeated_panel_is_refused_before_the_solve_runs(tmp_path, capsys):
    repeated = added_handles()[0]
    with patch.object(producer.solve, "produce",
                      side_effect=AssertionError("a repeated handle must be refused before the solve")):
        assert run_offline(arguments(tmp_path, delete=deleted_ids(),
                                     add=(repeated, repeated))) == 2
    assert "only once" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_a_panel_the_drawing_does_not_hold_is_refused(tmp_path, capsys, committed):
    graph, _, _ = committed
    absent = "FFFFFFFF"
    assert absent not in {producer.normalized_handle(panel["provenance"]["source_handle"])
                          for panel in graph["panels"]}
    assert run_offline(arguments(tmp_path, delete=deleted_ids(), add=(absent,))) == 2
    assert "absent from the drawing" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_a_panel_a_surviving_circuit_still_wires_is_refused(tmp_path, capsys):
    erased = set(deleted_ids())
    wired = next(members[0] for members in plugin_strings()
                 if "string:" + members[0] not in erased)
    # The plugin filters a wired panel out of its own selection, so the builtin
    # refuses one rather than silently dropping it, and nothing is written.
    assert run_offline(arguments(tmp_path, delete=deleted_ids(), add=(wired,))) == 2
    assert "PANEL_ALREADY_ASSIGNED" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()
