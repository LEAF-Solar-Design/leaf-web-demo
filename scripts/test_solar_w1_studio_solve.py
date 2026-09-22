"""Offline W1 producer checks against the committed rooftop capture."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
from unittest.mock import patch

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_module("solar_w1_studio_solve")
adapter = load_module("solar_studio_evidence")
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/rooftop_unsplit.dwg"
INTAKE = ROOT / "data/rooftop_unsplit.intake.json"
PLACEMENT = ROOT / "data/rooftop_unsplit.licensed-placement.json"
CAPTURE = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_solve.json"
SIZING = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json"


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def arguments(folder, **overrides):
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    replay = save(folder / "replay.json", {"responses": [group["response"] for group in capture["groups"]]})
    values = {
        "fixture": FIXTURE, "intake": INTAKE, "placement": PLACEMENT,
        "sizing-response": SIZING, "max-string-length": 14,
        "dwgname": "rooftop_demo.dwg", "replay": replay,
        "out-graph": folder / "graph.json", "out-metadata": folder / "metadata.json",
        **overrides,
    }
    return [part for key, value in values.items() for part in ("--" + key, str(value))]


def run_offline(argv):
    with patch.object(producer.cloud.requests.sessions.Session, "request",
                      side_effect=AssertionError("offline producer must not use network")), \
            patch.object(producer.cloud, "resolve_grant",
                         side_effect=AssertionError("replay must not resolve a private grant")), \
            patch.object(producer.sizing_cloud, "resolve_grant",
                         side_effect=AssertionError("recorded sizing must not resolve a private grant")):
        return producer.main(argv)


@pytest.fixture(scope="module")
def solved(tmp_path_factory):
    folder = tmp_path_factory.mktemp("studio-solve")
    assert run_offline(arguments(folder)) == 0
    graph = producer.deserialize_graph((folder / "graph.json").read_bytes())
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    return graph, metadata, folder


def test_reopened_studio_solve_reproduces_all_66_plugin_strings(solved):
    graph, _, _ = solved
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    actual = [[handles[ref] for ref in string["ordered_panel_refs"]] for string in graph["strings"]]
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    expected = [members for group in capture["groups"] for members in group["plugin_strings"]]
    assert len(actual) == len(expected) == 66
    assert sorted(actual) == sorted(expected)
    assert len(handles) == 882
    assert len([ref for string in graph["strings"] for ref in string["ordered_panel_refs"]]) == 882
    assert graph["extra"]["solve_coverage"] == {"unassigned_panel_refs": [], "duplicate_panel_refs": []}
    assert [frame["name"] for frame in graph["frames"]] == [f"Group {i}" for i in range(1, 8)]
    for frame in graph["frames"]:
        assert frame["provenance"]["licensed_placement"] == "recorded"
        assert frame["provenance"]["placement_sha256"] == hashlib.sha256(PLACEMENT.read_bytes()).hexdigest()
    for string in graph["strings"]:
        assert string["extra"]["polarity"]["negative_panel_ref"] == string["ordered_panel_refs"][0]
        assert string["extra"]["polarity"]["positive_panel_ref"] == string["ordered_panel_refs"][-1]


def test_metadata_binds_shared_fixture_and_records_fallbacks(solved):
    graph, metadata, folder = solved
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
    assert "input_sha256" not in metadata
    assert metadata["parameters"] == {"family": "strings", "max_string_length": 14}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_solve.v1",
        "capability": "0", "engine": "server-builtin", "catalog": "none", "solver": "leaf-stringer-service"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["before"] == {"recorded": False}
    assert metadata["changes"] == {"created": [], "modified": [], "deleted": []}
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    assert metadata["elapsed_ms"] >= 0
    assert metadata["execution_mode"] == "replay" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert set(metadata["synthetic_fields"]) == {"before", "changes", "versions.solver", "settings/string_sizing"}
    assert "panels/producer-built-from-intake" in metadata["fallback_fields"]
    assert "frames/recorded-licensed-placement" in metadata["fallback_fields"]
    provenance = metadata["provenance"]
    for field, path in (("intake_sha256", INTAKE), ("placement_sha256", PLACEMENT),
                        ("sizing_response_sha256", SIZING), ("replay_sha256", folder / "replay.json")):
        assert provenance[field] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(provenance["response_sha256s"]) == 7
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in provenance["response_sha256s"])
    mapping = metadata["entity_mapping"]
    assert set(mapping) == {entity["id"] for entity in graph["panels"] + graph["strings"]}
    for panel in graph["panels"]:
        assert mapping[panel["id"]] == producer.normalized_handle(panel["provenance"]["source_handle"])
    for string in graph["strings"]:
        assert mapping[string["id"]] == "string:" + mapping[string["ordered_panel_refs"][0]]
    assert "grant_ref" not in json.dumps((graph, metadata))
    assert "access_token" not in json.dumps((graph, metadata))


def test_produced_evidence_validates_joint_identity_contract(solved):
    graph, metadata, _ = solved
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "strings", metadata)
    adapter.compare.validate_evidence(evidence, "strings")
    assert evidence["revision"] == metadata["revision"]
    assert evidence["input_sha256"] == adapter.compare.semantic_hash({
        "fixture_sha256": metadata["fixture_sha256"], "parameters": metadata["parameters"]})
    assert evidence["provenance"]["studio_graph_rev"] == graph["rev"]
    assert evidence["provenance"]["studio_graph_sha256"] == adapter.compare.semantic_hash(graph)
    assert evidence["units"] == "in"
    assert evidence["frame"] == {
        "coordinate_system": "world", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "elevation_datum": "unrecorded", "crs": "none"}
    ids = [metadata["entity_mapping"][record["id"]["entity_id"]] for record in evidence["after"]["strings"]]
    assert ids == sorted(ids)
    assert evidence["after"]["unassigned_panels"] == evidence["after"]["duplicate_panels"] == []
    assert all(record["polarity"] == "positive" for record in evidence["after"]["strings"])
    assert (graph, metadata) == originals


@pytest.mark.parametrize("mismatch", ["fixture", "intake", "placement"])
def test_fixture_hash_mismatch_fails_before_output(tmp_path, capsys, mismatch):
    if mismatch == "fixture":
        replacement = tmp_path / "changed.dwg"
        replacement.write_bytes(FIXTURE.read_bytes() + b"changed")
    else:
        original = INTAKE if mismatch == "intake" else PLACEMENT
        document = json.loads(original.read_text(encoding="utf-8"))
        if mismatch == "intake":
            document["source"]["dwg_sha256"] = "0" * 64
        else:
            document["fixture_sha256"] = "0" * 64
        replacement = save(tmp_path / (mismatch + ".json"), document)
    assert run_offline(arguments(tmp_path, **{mismatch: replacement})) == 2
    assert "fixture hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


@pytest.mark.parametrize("copies", [0, 2])
def test_replay_requires_exactly_one_matching_response(tmp_path, capsys, copies):
    argv = arguments(tmp_path)
    path = tmp_path / "replay.json"
    responses = json.loads(path.read_text(encoding="utf-8"))["responses"]
    save(path, {"responses": responses * copies})
    assert run_offline(argv) == 2
    assert f"exactly one matching response (found {copies})" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_untracked_fixture_has_no_revision_and_is_refused(tmp_path, capsys):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True,
                   capture_output=True, timeout=15)
    fixture = tmp_path / "untracked.dwg"
    fixture.write_bytes(FIXTURE.read_bytes())
    assert run_offline(arguments(tmp_path, fixture=fixture)) == 2
    assert "fixture must be tracked in git" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()
