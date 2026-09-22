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
SPLIT_RECORD = ROOT / "server/tests/fixtures/w1_rooftop_split_groups.json"
REAL_RESPONSE = ROOT / "server/tests/fixtures/w1_stringer_response_real.json"
DEMO_FIXTURE = ROOT / "data/rooftop_demo.dwg"
DEMO_INTAKE = ROOT / "data/rooftop_demo.v2.intake.json"
DEMO_PLACEMENT = ROOT / "data/rooftop_demo.licensed-placement.json"


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


def test_sizing_commits_the_plugin_recommendation_through_its_guard(solved):
    graph, _, _ = solved
    sizing = json.loads(SIZING.read_text(encoding="utf-8"))
    evidence = producer.sizing_cloud.require_sizing(graph)
    record = evidence["records"][graph["settings"]["id"]]
    assert record["adapter_version"] == "2.0.0"
    assert record["request"] == sizing["request"] and record["response"] == sizing["response"]
    settings = graph["settings"]
    assert settings["panels_in_sequence"] == 14 and settings["global_string_sizing_confirmed"] is True
    per_module = 52.58 * (1.0 + -0.13145 / 100.0 * (-2.700000047683716 - 25.0))
    assert settings["voc_cold"] == {
        "passes": True, "override_accepted": False, "suggested_string_length": 0,
        "per_module": per_module, "string_voltage": per_module * 14, "max_dc_voltage": 1500.0}


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


def split_scenario(folder):
    """One recorded split group of the full rooftop as a one-frame run.

    The intake and placement keep only that group's panels (still bound to the
    rooftop fixture). Each replayed answer echoes the piece wire grid Studio
    sends, carrying the plugin's recorded Seq values and string lengths.
    """
    record = json.loads(SPLIT_RECORD.read_text(encoding="utf-8"))
    group = min(record["groups"], key=lambda g: g["panel_count"])
    norm = lambda h: None if h is None else producer.normalized_handle(h)  # noqa: E731
    wanted = [[norm(h) for h in row] for row in group["group_grid"]]
    placement = json.loads(DEMO_PLACEMENT.read_text(encoding="utf-8"))
    source = next(g for g in placement["groups"]
                  if [[norm(h) for h in row] for row in g["matrix"]] == wanted)
    placement["groups"] = [dict(source, name="Group 1")]
    members = {h for row in wanted for h in row if h}
    intake = json.loads(DEMO_INTAKE.read_text(encoding="utf-8"))
    intake["polylines"] = [p for p in intake["polylines"] if norm(p["handle"]) in members]
    by_handle, _ = producer.import_panels({"source_hash": "0" * 64, "rev": 0, "panels": []},
                                          intake, "geometry-only")
    template = json.loads(REAL_RESPONSE.read_text(encoding="utf-8"))
    bodies = []
    for piece, recorded in zip(group["pieces"], group["responses"]):
        seqs = {norm(cell["Id"]): cell["Seq"] for row in recorded["data"]["final_grid"]["Rows"]
                for cell in row["Panels"] if cell["Code"] == 1}
        cells = piece["cells"]
        kept_rows = [r for r, row in enumerate(cells) if any(row)]
        kept_cols = [c for c in range(len(cells[0])) if any(cells[r][c] for r in kept_rows)]
        rows = []
        for r in kept_rows:
            wires = []
            for c in kept_cols:
                handle = cells[r][c]
                wire = {"Code": 0, "Id": "", "Seq": 0, "InverterId": -1,
                        "StringInputNumber": 0, "X": 0.0, "Y": 0.0, "Angle": 0.0}
                if handle:
                    panel = by_handle[norm(handle)]
                    wire.update(Code=1, Id=handle, Seq=seqs[norm(handle)], X=panel["centre"][0],
                                Y=panel["centre"][1], Angle=panel["angle"])
                wires.append(wire)
            rows.append({"Panels": wires})
        body = deepcopy(template)
        data = body["data"]
        sequences = piece["sequences"]
        data["final_grid"] = {"Dwgname": "rooftop_demo.dwg", "Rows": rows, "Modify": [],
                              "Sequences": [sequences[:2], sequences[2:]]}
        data["total_valid_solutions"] = recorded["data"]["total_valid_solutions"]
        data["message"] = recorded["data"].get("message")
        info = recorded["data"]["best_result"]["info"]
        if "sequence_length" in info:
            order = sorted((wire["Seq"], r + 1, c + 1) for r, row in enumerate(rows)
                           for c, wire in enumerate(row["Panels"]) if wire["Code"] == 1)
            data["best_result"]["info"].update(
                sequence_length=list(info["sequence_length"]),
                visited_path=[[r, c] for _, r, c in order],
                num_panels=float(len(order)), steps_taken=len(order))
        else:
            data["best_result"]["info"] = {"distance_total": 0.0}
        bodies.append(body)
    overrides = {"fixture": DEMO_FIXTURE, "intake": save(folder / "split-intake.json", intake),
                 "placement": save(folder / "split-placement.json", placement)}
    return group, bodies, overrides


def test_split_frame_replays_piece_by_piece_and_commits_the_plugin_strings(tmp_path):
    group, bodies, overrides = split_scenario(tmp_path)
    replay = save(tmp_path / "split-replay.json", {"responses": bodies})
    assert run_offline(arguments(tmp_path, replay=replay, **overrides)) == 0
    graph = producer.deserialize_graph((tmp_path / "graph.json").read_bytes())
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    actual = [[handles[ref] for ref in string["ordered_panel_refs"]] for string in graph["strings"]]
    expected = [[producer.normalized_handle(h) for h in members] for members in group["plugin_strings"]]
    assert len(actual) == len(expected)
    assert sorted(actual) == sorted(expected)
    assert len(handles) == group["panel_count"]
    assert sum(map(len, actual)) == group["panel_count"]
    assert graph["extra"]["solve_coverage"] == {"unassigned_panel_refs": [], "duplicate_panel_refs": []}
    [frame] = graph["frames"]
    assert frame["name"] == "Group 1"
    split = frame["extra"]["solve"]["split"]
    assert len(split["pieces"]) == len(group["pieces"])
    assert [p["row_indices"] for p in split["pieces"]] == [p["row_indices"] for p in group["pieces"]]
    provenance = metadata["provenance"]
    assert provenance["split_frames"] == [{"frame": "Group 1", "pieces": len(group["pieces"]),
                                           "split_state": {"jogs": 1, "depth": 10}}]
    assert len(provenance["response_sha256s"]) == len(group["pieces"])
    assert metadata["parameters"] == {"family": "strings", "max_string_length": 14}
    assert metadata["fixture_sha256"] == hashlib.sha256(DEMO_FIXTURE.read_bytes()).hexdigest()
    assert "grant_ref" not in json.dumps((graph, metadata))


def test_split_replay_refuses_a_piece_echo_that_does_not_match(tmp_path, capsys):
    _, bodies, overrides = split_scenario(tmp_path)
    wire = next(w for row in bodies[-1]["data"]["final_grid"]["Rows"]
                for w in row["Panels"] if w["Code"] == 1)
    wire["X"] += 1.0
    replay = save(tmp_path / "split-replay.json", {"responses": bodies})
    assert run_offline(arguments(tmp_path, replay=replay, **overrides)) == 2
    assert "exactly one matching response (found 0)" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()
