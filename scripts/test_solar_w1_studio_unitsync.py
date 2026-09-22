"""Offline W1 unit-sync checks: the builtin, the producer and the settings evidence family."""
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


producer = load_module("solar_w1_studio_unitsync")
adapter = load_module("solar_studio_evidence")
solve = producer.solve
unit_sync = solve.builtin("solar_unit_sync")
GraphValidationError = unit_sync.GraphValidationError
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/rooftop_unsplit.dwg"
FIXTURE_HASH = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def rectangle(x, y, width=77.0, height=38.5):
    return [[x, y, 0.0], [x + width, y, 0.0], [x + width, y + height, 0.0], [x, y + height, 0.0]]


def synthetic_intake(fixture_hash=FIXTURE_HASH):
    """Two panels bound to the committed fixture: small, and built here, not read from disk."""
    return {"dwg": "data/rooftop_unsplit.dwg", "source": {"dwg_sha256": fixture_hash}, "layers": ["Panels"],
            "polylines": [{"layer": "Panels", "closed": True, "handle": "1A", "xdata": None, "pts": rectangle(0.0, 0.0)},
                          {"layer": "Panels", "closed": True, "handle": "1B", "xdata": None, "pts": rectangle(100.0, 0.0)}]}


def seeded_graph(drawing_units="in"):
    intake = synthetic_intake()
    graph = solve.new_empty_graph(
        tenant_id="studio-replay", drawing_id="w1-unit-sync", source_hash=hashlib.sha256(b"intake").hexdigest(),
        units={"drawing_units": drawing_units, "wcs_to_ucs": IDENTITY, "elevation_datum": "unrecorded", "crs": ""},
        created_at="2026-09-22T00:00:00+00:00")
    solve.import_panels(graph, intake, "2026-09-22T00:00:00+00:00")
    return solve.validate_graph(graph)


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def arguments(folder, distance_unit="Meters", **overrides):
    values = {"fixture": FIXTURE, "intake": save(folder / "intake.json", synthetic_intake()),
              "distance-unit": distance_unit, "out-graph": folder / "graph.json",
              "out-metadata": folder / "metadata.json", **overrides}
    return [part for key, value in values.items() for part in ("--" + key, str(value))]


def run_offline(argv):
    with patch.object(solve.cloud.requests.sessions.Session, "request",
                      side_effect=AssertionError("offline producer must not use network")):
        return producer.main(argv)


# --- the builtin --- #

@pytest.mark.parametrize("distance_unit, name, scale, feet", [("Meters", "m", 1.0, False), ("Feet", "ft", 0.3048, True)])
def test_sync_changes_the_declaration_only(distance_unit, name, scale, feet):
    graph = seeded_graph()
    original = deepcopy(graph)
    result = unit_sync.sync_units(graph, {"expected_rev": graph["rev"], "distance_unit": distance_unit})
    assert graph == original, "the caller's graph is never mutated in place"
    assert result["changed"] is True and result["drawing_units"] == name
    synced = result["graph"]
    units = synced["project"]["units"]
    assert units["drawing_units"] == name and units["meters_per_unit"] == scale
    assert units["drawing_unit_is_feet"] is feet and units["source"] == "explicit"
    assert units["wcs_to_ucs"] == IDENTITY and units["compute_units"] == "m"
    assert solve.sizing_cloud.units_resolved(synced)
    assert synced["rev"] == original["rev"] + 1 and synced["parent_rev"] == original["rev"]
    assert synced["project"]["rev"] == synced["rev"]
    assert synced["project"]["provenance"]["last_writer"] == "solar-unit-sync"
    assert synced["project"]["provenance"]["tool_id"] == "solar-unit-sync"
    assert [(p["centre"], p["angle"]) for p in synced["panels"]] == [(p["centre"], p["angle"]) for p in original["panels"]]
    assert [p["rev"] for p in synced["panels"]] == [p["rev"] for p in original["panels"]]
    assert synced["settings"] == original["settings"]
    assert solve.validate_graph(synced) == synced
    assert solve.deserialize_graph(solve.serialize_graph(synced)) == synced


@pytest.mark.parametrize("distance_unit, name", [("Meters", "m"), ("Feet", "ft")])
def test_already_in_sync_writes_nothing_and_says_so(distance_unit, name):
    graph = seeded_graph(name)
    result = unit_sync.sync_units(graph, {"expected_rev": graph["rev"], "distance_unit": distance_unit})
    assert result["changed"] is False and result["drawing_units"] == name
    assert result["graph"] == graph
    assert result["graph"]["rev"] == graph["rev"]


@pytest.mark.parametrize("bad", ["Inches", "meters", "", 6, None, True, ["Meters"]])
def test_bad_distance_unit_is_refused(bad):
    graph = seeded_graph()
    with pytest.raises(GraphValidationError) as refusal:
        unit_sync.sync_units(graph, {"expected_rev": graph["rev"], "distance_unit": bad})
    assert refusal.value.code == "INVALID_UNIT_SYNC_REQUEST"


@pytest.mark.parametrize("params", [{"expected_rev": 0}, {"distance_unit": "Meters"},
                                    {"expected_rev": 0, "distance_unit": "Meters", "cancel": False}, [], "Meters"])
def test_request_shape_is_exact(params):
    graph = seeded_graph()
    with pytest.raises(GraphValidationError) as refusal:
        unit_sync.sync_units(graph, params)
    assert refusal.value.code == "INVALID_UNIT_SYNC_REQUEST"


@pytest.mark.parametrize("stale", [1, -1, "0", None, 0.0])
def test_stale_expected_rev_is_refused(stale):
    graph = seeded_graph()
    assert graph["rev"] == 0
    with pytest.raises(GraphValidationError) as refusal:
        unit_sync.sync_units(graph, {"expected_rev": stale, "distance_unit": "Feet"})
    assert refusal.value.code == "STALE_GRAPH_REVISION"


def test_unresolved_units_are_refused_before_any_write():
    graph = seeded_graph()
    graph["project"]["units"]["meters_per_unit"] = 1.0  # says inches, scaled as metres
    with pytest.raises(GraphValidationError) as refusal:
        unit_sync.sync_units(graph, {"expected_rev": graph["rev"], "distance_unit": "Meters"})
    assert refusal.value.code == "UNRESOLVED_UNITS"


def test_run_entry_returns_the_synced_graph():
    graph = seeded_graph()
    synced = unit_sync.run(graph, {"expected_rev": graph["rev"], "distance_unit": "Feet"})
    assert synced["project"]["units"]["drawing_units"] == "ft" and synced["rev"] == 1


# --- the producer --- #

@pytest.fixture(scope="module")
def synced(tmp_path_factory):
    folder = tmp_path_factory.mktemp("studio-unitsync")
    assert run_offline(arguments(folder)) == 0
    graph = solve.deserialize_graph((folder / "graph.json").read_bytes())
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    return graph, metadata, folder


def test_producer_syncs_the_reopened_graph_to_metres_without_moving_panels(synced):
    graph, _, _ = synced
    assert solve.validate_graph(graph) == graph
    units = graph["project"]["units"]
    assert units["drawing_units"] == "m" and units["meters_per_unit"] == 1.0
    assert units["drawing_unit_is_feet"] is False and units["source"] == "explicit"
    assert graph["rev"] == 1 and graph["parent_rev"] == 0
    by_handle = {solve.normalized_handle(p["provenance"]["source_handle"]): p for p in graph["panels"]}
    assert set(by_handle) == {"1A", "1B"}
    assert by_handle["1A"]["centre"] == [38.5, 19.25] and by_handle["1B"]["centre"] == [138.5, 19.25]
    assert all(p["angle"] == 0.0 for p in graph["panels"])
    assert graph["frames"] == graph["strings"] == []


def test_producer_metadata_carries_the_settings_identity(synced):
    graph, metadata, folder = synced
    assert metadata["fixture_sha256"] == FIXTURE_HASH
    assert metadata["revision"] == solve.fixture_revision(FIXTURE.resolve())
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
    assert "input_sha256" not in metadata
    assert metadata["parameters"] == {"family": "settings", "distance_unit": "Meters"}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_unitsync.v1",
        # The LEDGER's capability_version for unit-sync, which the gate compares; the
        # capability itself is named by the receipt directory, not by this field.
        "capability": "0", "engine": "server-builtin", "catalog": "none", "solver": "none"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "m" and metadata["angle_units"] == "deg"
    assert metadata["before"] == {"recorded": False}
    assert metadata["changes"] == {"created": [], "modified": [], "deleted": []}
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    assert metadata["elapsed_ms"] >= 0
    assert metadata["execution_mode"] == "replay" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert set(metadata["synthetic_fields"]) == {"before", "changes"}
    assert "panels/producer-built-from-intake" in metadata["fallback_fields"]
    assert "units/before-sync/producer-seeded-in" in metadata["fallback_fields"]
    assert metadata["provenance"]["intake_sha256"] == hashlib.sha256((folder / "intake.json").read_bytes()).hexdigest()
    assert metadata["provenance"]["unit_sync"] == {"changed": True, "drawing_units": "m", "seed_units": "in"}
    # A settings receipt compares a drawing-wide declaration: no entity appears in `after`, and
    # the plugin's own settings evidence maps nothing, so this run maps nothing either.
    assert metadata["entity_mapping"] == {}


def test_producer_feet_records_the_feet_declaration(tmp_path):
    assert run_offline(arguments(tmp_path, "Feet")) == 0
    graph = solve.deserialize_graph((tmp_path / "graph.json").read_bytes())
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    units = graph["project"]["units"]
    assert units["drawing_units"] == "ft" and units["meters_per_unit"] == 0.3048 and units["drawing_unit_is_feet"] is True
    assert metadata["parameters"] == {"family": "settings", "distance_unit": "Feet"}
    assert metadata["geometry_units"] == "ft"
    assert metadata["provenance"]["unit_sync"]["drawing_units"] == "ft"


def test_producer_input_hash_binds_fixture_and_parameters(synced):
    graph, metadata, _ = synced
    evidence_hash = adapter.compare.semantic_hash({
        "fixture_sha256": FIXTURE_HASH, "parameters": {"family": "settings", "distance_unit": "Meters"}})
    assert adapter.build_evidence(graph, "settings", metadata)["input_sha256"] == evidence_hash


@pytest.mark.parametrize("mismatch", ["fixture", "intake"])
def test_fixture_hash_mismatch_fails_before_output(tmp_path, capsys, mismatch):
    if mismatch == "fixture":
        replacement = tmp_path / "changed.dwg"
        replacement.write_bytes(FIXTURE.read_bytes() + b"changed")
    else:
        replacement = save(tmp_path / "mismatch.json", synthetic_intake("0" * 64))
    assert run_offline(arguments(tmp_path, **{mismatch: replacement})) == 2
    assert "fixture hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_producer_refuses_a_distance_unit_outside_the_plugin_preference(tmp_path):
    with pytest.raises(SystemExit):
        producer.main(arguments(tmp_path, "Inches"))
    assert not (tmp_path / "graph.json").exists()


def test_producer_refuses_an_intake_without_panels(tmp_path, capsys):
    intake = synthetic_intake()
    intake["polylines"] = []
    assert run_offline(arguments(tmp_path, intake=save(tmp_path / "empty.json", intake))) == 2
    assert "intake requires panel polylines" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


# --- the settings evidence family --- #

def test_settings_evidence_emits_the_insunits_code(synced):
    graph, metadata, _ = synced
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "settings", metadata)
    adapter.compare.validate_evidence(evidence, "settings")
    assert evidence["after"] == {"settings": {"insunits": 6}}
    assert evidence["output_sha256"] == adapter.compare.semantic_hash(evidence["after"])
    assert evidence["units"] == "m"
    assert evidence["parameters"] == {"family": "settings", "distance_unit": "Meters"}
    assert evidence["versions"]["solver"] == "none" and evidence["versions"]["capability"] == "0"
    assert evidence["frame"] == {"coordinate_system": "world", "transform": IDENTITY,
                                 "elevation_datum": "unrecorded", "crs": "none"}
    assert "after/settings/insunits/derived-from-drawing-units" in evidence["fallback_fields"]
    assert evidence["synthetic_flagged"] is True
    assert evidence["execution_mode"] == "recorded"
    assert evidence["provenance"]["studio_graph_rev"] == 1
    assert evidence["provenance"]["studio_graph_sha256"] == adapter.compare.semantic_hash(graph)
    assert (graph, metadata) == originals


@pytest.mark.parametrize("name, code", [("in", 1), ("ft", 2), ("mm", 4), ("cm", 5), ("m", 6)])
def test_settings_evidence_maps_every_comparable_unit(synced, name, code):
    graph, metadata, _ = synced
    graph = deepcopy(graph)
    graph["project"]["units"]["drawing_units"] = name
    metadata = dict(metadata, geometry_units=name)
    evidence = adapter.build_evidence(graph, "settings", metadata)
    assert evidence["after"] == {"settings": {"insunits": code}} and evidence["units"] == name


@pytest.mark.parametrize("name", ["furlong", "", None, 6])
def test_settings_evidence_refuses_a_unit_without_an_insunits_code(synced, name):
    graph, metadata, _ = synced
    graph = deepcopy(graph)
    graph["project"]["units"]["drawing_units"] = name
    with pytest.raises(adapter.compare.InputError, match="no INSUNITS code"):
        adapter.build_evidence(graph, "settings", metadata)


@pytest.mark.parametrize("name", ["km", "yd"])
def test_settings_evidence_refuses_units_the_comparator_cannot_scale(synced, name):
    graph, metadata, _ = synced
    graph = deepcopy(graph)
    graph["project"]["units"]["drawing_units"] = name
    with pytest.raises(adapter.compare.InputError):
        adapter.build_evidence(graph, "settings", metadata)


def test_other_families_still_refuse_and_unknown_family_is_named(synced):
    graph, metadata, _ = synced
    # "devices" is a comparator family Studio has no adapter for yet; zones joined the adapter
    # in the zones slice, so it is no longer the unknown-family example.
    with pytest.raises(adapter.compare.InputError, match="groups, panels, settings, strings and zones"):
        adapter.build_evidence(graph, "devices", metadata)
    # A settings run's metadata describes no entities, so a family that needs them refuses
    # rather than inventing a mapping. The panels family is exercised by its own producer.
    with pytest.raises(adapter.compare.InputError, match="explicit neutral mapping"):
        adapter.build_evidence(graph, "panels", metadata)
    settings = adapter.build_evidence(graph, "settings", metadata)
    assert "after/settings/insunits/derived-from-drawing-units" in settings["fallback_fields"]


def test_evidence_cli_writes_the_settings_family(synced, tmp_path):
    _, _, folder = synced
    output = tmp_path / "evidence.json"
    assert adapter.main(["--graph", str(folder / "graph.json"), "--metadata", str(folder / "metadata.json"),
                         "--family", "settings", "--output", str(output)]) == 0
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["after"] == {"settings": {"insunits": 6}}
    adapter.compare.validate_evidence(evidence, "settings")
