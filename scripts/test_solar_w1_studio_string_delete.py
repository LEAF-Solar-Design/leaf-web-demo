"""Offline checks for the Studio string-delete producer on the committed rooftop capture.

The solve half is the solve producer, so the circuits this run deletes from are the
66 the plugin itself committed (server/tests/fixtures/w1_rooftop_unsplit_solve.json).
That capture is also the baseline: the memberships left after a delete are the
capture's, minus exactly the circuits the run named, so the check does not need a
second solve to say what "unchanged" means.
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


producer = load_module("solar_w1_studio_string_delete")
adapter = load_module("solar_studio_evidence")
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/rooftop_unsplit.dwg"
INTAKE = ROOT / "data/rooftop_unsplit.intake.json"
PLACEMENT = ROOT / "data/rooftop_unsplit.licensed-placement.json"
CAPTURE = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_solve.json"
SIZING = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json"
STRING_COUNT = 66
PANEL_COUNT = 882


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def arguments(folder, delete=(), **overrides):
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
    return argv + [part for name in delete for part in ("--delete", str(name))]


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


def memberships(graph):
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    return [[handles[ref] for ref in string["ordered_panel_refs"]] for string in graph["strings"]]


def read(folder):
    return (producer.deserialize_graph((folder / "graph.json").read_bytes()),
            json.loads((folder / "metadata.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def deleted(tmp_path_factory):
    """Two of the plugin's own circuits deleted from the committed solved drawing."""
    folder = tmp_path_factory.mktemp("studio-string-delete")
    chosen = plugin_neutral_ids()[:2]
    assert run_offline(arguments(folder, delete=chosen)) == 0
    graph, metadata = read(folder)
    return graph, metadata, chosen, folder


@pytest.fixture(scope="module")
def emptied(tmp_path_factory):
    """Every circuit selected at once, which the plugin's own selection allows."""
    folder = tmp_path_factory.mktemp("studio-string-delete-all")
    assert run_offline(arguments(folder, delete=plugin_neutral_ids())) == 0
    return read(folder)


def test_the_named_circuits_leave_and_the_rest_are_the_plugins_own(deleted):
    graph, _, chosen, _ = deleted
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    erased = set(chosen)
    expected = [members for members in plugin_strings() if "string:" + members[0] not in erased]
    assert len(expected) == STRING_COUNT - 2
    assert sorted(memberships(graph)) == sorted(expected)
    # The circuits the run named are the only ones missing.
    assert set(plugin_neutral_ids()) - {"string:" + members[0] for members in memberships(graph)} == set(chosen)
    # The delete is the graph's last revision and it rewrote no surviving circuit:
    # every one of them still carries the revision its solve commit gave it.
    assert max(string["rev"] for string in graph["strings"]) < graph["rev"]


def test_every_panel_survives_and_only_the_freed_ones_are_unassigned(deleted):
    graph, _, chosen, _ = deleted
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    assert len(handles) == PANEL_COUNT
    erased = set(chosen)
    freed = sorted(handle for members in plugin_strings()
                   if "string:" + members[0] in erased for handle in members)
    coverage = graph["extra"]["solve_coverage"]
    assert coverage["duplicate_panel_refs"] == []
    assert sorted(handles[ref] for ref in coverage["unassigned_panel_refs"]) == freed
    # A freed panel keeps its geometry, its group and its matrix cell; it is only unwired.
    for ref in coverage["unassigned_panel_refs"]:
        panel = next(p for p in graph["panels"] if p["id"] == ref)
        assert panel["assignment"] == {"string_ref": None, "seq": None}
        assert panel["frame_ref"] is not None and panel["matrix_cell"] is not None
    assert [frame["name"] for frame in graph["frames"]] == [f"Group {i}" for i in range(1, 8)]


def test_metadata_records_the_solve_it_started_from_and_the_delete(deleted):
    graph, metadata, chosen, folder = deleted
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
    assert metadata["parameters"] == {"family": "strings", "max_string_length": 14}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_string_delete.v1",
        # The LEDGER's capability_version for string-delete, which the gate compares.
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
    # The run solved first: these separate a real delete from a run that never solved.
    assert provenance["strings_solved"] == STRING_COUNT
    assert provenance["strings_deleted"] == 2 and provenance["deleted_strings"] == sorted(chosen)
    assert provenance["strings_remaining"] == len(graph["strings"]) == STRING_COUNT - 2
    assert provenance["panels_unassigned"] == len(graph["extra"]["solve_coverage"]["unassigned_panel_refs"])
    assert "grant_ref" not in json.dumps((graph, metadata))


def test_the_mapping_covers_every_panel_and_the_surviving_circuits_only(deleted):
    """Rule G8: the evidence names every panel and the circuits that are still there."""
    graph, metadata, chosen, _ = deleted
    mapping = metadata["entity_mapping"]
    assert mapping == producer.evidence_mapping(graph)
    assert set(mapping) == {entity["id"] for entity in graph["panels"] + graph["strings"]}
    assert len(set(mapping.values())) == len(mapping)
    for panel in graph["panels"]:
        assert mapping[panel["id"]] == producer.normalized_handle(panel["provenance"]["source_handle"])
    for string in graph["strings"]:
        assert mapping[string["id"]] == "string:" + mapping[string["ordered_panel_refs"][0]]
    # A deleted circuit is gone from the evidence, and its panels are still named.
    assert set(chosen).isdisjoint(set(mapping.values()))


def test_produced_evidence_validates_joint_identity_contract(deleted):
    graph, metadata, chosen, _ = deleted
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "strings", metadata)
    adapter.compare.validate_evidence(evidence, "strings")
    assert evidence["revision"] == metadata["revision"]
    assert evidence["provenance"]["studio_graph_rev"] == graph["rev"]
    assert evidence["units"] == "in"
    ids = [metadata["entity_mapping"][record["id"]["entity_id"]] for record in evidence["after"]["strings"]]
    assert ids == sorted(ids) and len(ids) == STRING_COUNT - 2
    assert set(chosen).isdisjoint(ids)
    assert evidence["after"]["duplicate_panels"] == []
    assert len(evidence["after"]["unassigned_panels"]) == metadata["provenance"]["panels_unassigned"]
    assert evidence["after"]["length_distribution"] == sorted(
        string["module_count"] for string in graph["strings"])
    assert all(record["polarity"] == "positive" for record in evidence["after"]["strings"])
    assert (graph, metadata) == originals


def test_deleting_every_circuit_leaves_a_valid_graph_and_comparable_evidence(emptied):
    graph, metadata = emptied
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    assert graph["strings"] == []
    assert len(graph["panels"]) == PANEL_COUNT
    assert [frame["name"] for frame in graph["frames"]] == [f"Group {i}" for i in range(1, 8)]
    coverage = graph["extra"]["solve_coverage"]
    assert coverage["duplicate_panel_refs"] == []
    assert sorted(coverage["unassigned_panel_refs"]) == sorted(p["id"] for p in graph["panels"])
    provenance = metadata["provenance"]
    assert provenance["strings_deleted"] == STRING_COUNT and provenance["strings_remaining"] == 0
    assert provenance["panels_unassigned"] == PANEL_COUNT
    # Every panel is still named by the evidence; no circuit is.
    assert set(metadata["entity_mapping"]) == {panel["id"] for panel in graph["panels"]}
    evidence = adapter.build_evidence(graph, "strings", metadata)
    adapter.compare.validate_evidence(evidence, "strings")
    assert evidence["after"]["strings"] == [] and evidence["after"]["length_distribution"] == []
    assert len(evidence["after"]["unassigned_panels"]) == PANEL_COUNT


@pytest.mark.parametrize("delete,message", [
    (("string:zz",), "invalid source handle"),
    (("string:",), "invalid source handle"),
    (("S1",), "named string:<handle>"),
])
def test_a_malformed_string_id_is_refused_before_the_solve_runs(tmp_path, capsys, delete, message):
    with patch.object(producer.solve, "produce",
                      side_effect=AssertionError("a malformed id must be refused before the solve")):
        assert run_offline(arguments(tmp_path, delete=delete)) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_a_circuit_the_solved_drawing_does_not_hold_is_refused(tmp_path, capsys):
    # A real panel handle that is NOT a circuit's first panel: the drawing knows the
    # handle, so only the string lookup can refuse it.
    absent = "string:" + plugin_strings()[0][1]
    assert absent not in plugin_neutral_ids()
    assert run_offline(arguments(tmp_path, delete=(absent,))) == 2
    assert "absent from the solved drawing" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_a_repeated_string_is_refused_before_the_solve_runs(tmp_path, capsys):
    repeated = plugin_neutral_ids()[0]
    with patch.object(producer.solve, "produce",
                      side_effect=AssertionError("a repeated id must be refused before the solve")):
        assert run_offline(arguments(tmp_path, delete=(repeated, repeated))) == 2
    assert "only once" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_fixture_hash_mismatch_fails_before_output(tmp_path, capsys):
    document = json.loads(INTAKE.read_text(encoding="utf-8"))
    document["source"]["dwg_sha256"] = "0" * 64
    argv = arguments(tmp_path, delete=plugin_neutral_ids()[:1],
                     intake=save(tmp_path / "intake.json", document))
    assert run_offline(argv) == 2
    assert "fixture hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()
