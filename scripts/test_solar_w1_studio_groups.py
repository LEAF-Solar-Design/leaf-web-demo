"""Offline checks for the Studio panel-group producer on a synthetic two-group intake."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_module("solar_w1_studio_groups")
adapter = load_module("solar_studio_evidence")
ROOT = Path(__file__).resolve().parents[1]
# Any committed file binds the synthetic intake to a fixture; no rooftop capture is read here.
FIXTURE = ROOT / "contract" / "solar-design-graph.v1.schema.json"
WIDTH, HEIGHT = 77.0, 38.5
# Two islands far apart: a 2 x 2 block at the origin and a row of three 5000 in east.
GROUP_A = {"1A": (0.0, 0.0), "1B": (79.0, 0.0), "1C": (0.0, 40.5), "1D": (79.0, 40.5)}
GROUP_B = {"2A": (5000.0, 0.0), "2B": (5079.0, 0.0), "2C": (5158.0, 0.0)}
PARAMETERS = {"branch-max-offset": 120, "alignment-tolerance": 12,
              "layer-contains": "Panel", "installation-design": "Roof"}


def rectangle(handle, centre, layer="Panels", width=WIDTH, height=HEIGHT):
    cx, cy = centre
    hw, hh = width / 2, height / 2
    return {"handle": handle, "layer": layer, "closed": True,
            "pts": [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh], [cx - hw, cy + hh]]}


def intake(fixture=FIXTURE, extra_polylines=()):
    polylines = [rectangle(handle, centre) for handle, centre in {**GROUP_A, **GROUP_B}.items()]
    # Not a panel layer, and not rectangular: the kernel selects neither.
    polylines.append(rectangle("3F", (2500.0, 0.0), layer="Outline", width=6000.0, height=400.0))
    polylines.append({"handle": "3E", "layer": "Panels", "closed": True,
                      "pts": [[0.0, 500.0], [77.0, 500.0], [38.5, 540.0]]})
    polylines.extend(extra_polylines)
    return {"source": {"dwg_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()},
            "polylines": polylines}


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def arguments(folder, **overrides):
    values = {"fixture": FIXTURE, "intake": None, **PARAMETERS,
              "out-graph": folder / "graph.json", "out-metadata": folder / "metadata.json"}
    values.update(overrides)
    if values["intake"] is None:
        values["intake"] = save(folder / "intake.json", intake())
    return [part for key, value in values.items() for part in ("--" + key, str(value))]


def handles(graph, refs):
    by_id = {panel["id"]: panel for panel in graph["panels"]}
    return [producer.normalized_handle(by_id[ref]["provenance"]["source_handle"]) for ref in refs]


@pytest.fixture(scope="module")
def grouped(tmp_path_factory):
    folder = tmp_path_factory.mktemp("studio-groups")
    assert producer.main(arguments(folder)) == 0
    graph = producer.deserialize_graph((folder / "graph.json").read_bytes())
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    return graph, metadata, folder


def test_reopened_graph_carries_the_two_kernel_groups(grouped):
    graph, _, _ = grouped
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    assert graph["rev"] == 1
    assert sorted(handles(graph, [p["id"] for p in graph["panels"]])) == sorted({**GROUP_A, **GROUP_B})
    assert [frame["name"] for frame in graph["frames"]] == ["group-1A", "group-2A"]
    assert [set(handles(graph, frame["panel_refs"])) for frame in graph["frames"]] == [set(GROUP_A), set(GROUP_B)]
    assert [(f["module_rows"], f["module_columns"]) for f in graph["frames"]] == [(2, 2), (1, 3)]
    for frame in graph["frames"]:
        cells = [cell["panel_ref"] for row in frame["matrix"] for cell in row if cell["panel_ref"] is not None]
        assert sorted(cells) == sorted(frame["panel_refs"])
        assert frame["provenance"]["licensed_matrix"] == "kernel"
        assert frame["provenance"]["angle_key"] == "0.0"
        assert (frame["module_width_along_row"], frame["module_height_across_row"]) == (WIDTH, HEIGHT)
        assert frame["installation_design"] == "Roof" and frame["electrical_zone_ref"] is None
    for panel in graph["panels"]:
        assert panel["frame_ref"] in {frame["id"] for frame in graph["frames"]}
        assert panel["matrix_cell"] is not None
        assert panel["assignment"] == {"string_ref": None, "seq": None}
    assert graph["strings"] == []
    assert graph["settings"]["extra"]["string_sizing"]["synthetic"] is True


def test_metadata_records_contract_v2_parameters_and_identities(grouped):
    graph, metadata, folder = grouped
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
    assert "input_sha256" not in metadata
    assert metadata["parameters"] == {
        "family": "groups", "layer_filter": "*Panel*", "branch_max_offset": 120.0,
        "alignment_tolerance": 12.0, "installation_design": "Roof"}
    assert type(metadata["parameters"]["branch_max_offset"]) is float
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_groups.v1",
        "capability": "panel-group-create", "engine": "server-builtin", "catalog": "none", "solver": "none"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["before"] == {"recorded": False}
    assert metadata["changes"] == {"created": [], "modified": [], "deleted": []}
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    assert metadata["elapsed_ms"] >= 0
    assert metadata["execution_mode"] == "live" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert set(metadata["synthetic_fields"]) == {"before", "changes", "settings/string_sizing"}
    assert "panels/producer-built-from-intake" in metadata["fallback_fields"]
    assert metadata["provenance"]["intake_sha256"] == hashlib.sha256((folder / "intake.json").read_bytes()).hexdigest()
    assert metadata["provenance"]["group_count"] == 2 and metadata["provenance"]["panel_count"] == 7
    mapping = metadata["entity_mapping"]
    assert set(mapping) == {entity["id"] for entity in graph["panels"] + graph["frames"]}
    for panel in graph["panels"]:
        assert mapping[panel["id"]] == producer.normalized_handle(panel["provenance"]["source_handle"])
    assert [mapping[frame["id"]] for frame in graph["frames"]] == ["group:1A", "group:2A"]
    assert len(set(mapping.values())) == len(mapping)


def test_groups_evidence_validates_the_joint_contract(grouped):
    graph, metadata, _ = grouped
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "groups", metadata)
    adapter.compare.validate_evidence(evidence, "groups")
    assert (graph, metadata) == originals
    assert evidence["input_sha256"] == adapter.compare.semantic_hash({
        "fixture_sha256": metadata["fixture_sha256"], "parameters": metadata["parameters"]})
    assert evidence["units"] == "in"
    assert evidence["frame"] == {
        "coordinate_system": "world", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "elevation_datum": "unrecorded", "crs": "none"}
    groups = evidence["after"]["groups"]
    assert [group["name"] for group in groups] == ["group:1A", "group:2A"]
    assert [[metadata["entity_mapping"][ref["entity_id"]] for ref in group["membership"]] for group in groups] == [
        ["1A", "1B", "1C", "1D"], ["2A", "2B", "2C"]]
    for group in groups:
        for field in ("boundaries", "elevation", "equipment_config", "sizing_provenance"):
            assert group[field] is None
            assert "groups/" + field + "/unrecorded" in evidence["fallback_fields"]
    assert evidence["provenance"]["group_names"] == {"group:1A": "group-1A", "group:2A": "group-2A"}
    assert evidence["execution_mode"] == "live" and evidence["synthetic_flagged"] is True
    assert evidence["versions"]["solver"] == "none"


def test_fixture_hash_mismatch_fails_before_output(tmp_path, capsys):
    document = intake()
    document["source"]["dwg_sha256"] = "0" * 64
    argv = arguments(tmp_path, intake=save(tmp_path / "intake.json", document))
    assert producer.main(argv) == 2
    assert "fixture hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


@pytest.mark.parametrize("override,message", [
    ({"branch-max-offset": 0}, "positive finite"),
    ({"alignment-tolerance": "nan"}, "positive finite"),
    ({"installation-design": "Ground"}, "installation design must be Roof"),
    ({"layer-contains": ""}, "layer contains"),
    ({"layer-contains": "Nope"}, "no panel polylines"),
])
def test_parameters_are_validated_before_output(tmp_path, capsys, override, message):
    assert producer.main(arguments(tmp_path, **override)) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_single_panel_group_is_refused(tmp_path, capsys):
    document = intake(extra_polylines=[rectangle("4A", (0.0, 5000.0))])
    argv = arguments(tmp_path, intake=save(tmp_path / "intake.json", document))
    assert producer.main(argv) == 2
    assert "fewer than two panels" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


def test_untracked_fixture_has_no_revision_and_is_refused(tmp_path, capsys):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True,
                   capture_output=True, timeout=15)
    fixture = tmp_path / "untracked.dwg"
    fixture.write_bytes(FIXTURE.read_bytes())
    argv = arguments(tmp_path, fixture=fixture, intake=save(tmp_path / "intake.json", intake(fixture)))
    assert producer.main(argv) == 2
    assert "fixture must be tracked in git" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
