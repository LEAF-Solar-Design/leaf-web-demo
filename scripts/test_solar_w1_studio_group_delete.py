"""Offline checks for the Studio group-delete producer on a synthetic eleven-group intake.

Eleven is the count the plugin printed on the captured run
(receipts/w2-group-delete-20260922, "Deleted 11 panel group(s)"), so the synthetic
intake here is built to group into exactly eleven and the producer must record
eleven created and eleven deleted.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_module("solar_w1_studio_group_delete")
groups_producer = load_module("solar_w1_studio_groups")
ROOT = Path(__file__).resolve().parents[1]
# Any committed file binds the synthetic intake to a fixture; no rooftop capture is read here.
FIXTURE = ROOT / "contract" / "solar-design-graph.v1.schema.json"
WIDTH, HEIGHT = 77.0, 38.5
GROUP_COUNT = 11
# Eleven islands of two panels, 5000 in apart: far beyond any branch offset, so the
# kernel answers with exactly one group per island.
ISLAND_PITCH = 5000.0
PANEL_PITCH = 79.0
PARAMETERS = {"branch-max-offset": 120, "alignment-tolerance": 12,
              "layer-contains": "Panel", "installation-design": "Roof"}


def handle(island, member):
    return format(2 * island + member + 1, "X")


def rectangle(name, centre, layer="Panels", width=WIDTH, height=HEIGHT):
    cx, cy = centre
    hw, hh = width / 2, height / 2
    return {"handle": name, "layer": layer, "closed": True,
            "pts": [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh], [cx - hw, cy + hh]]}


def intake(fixture=FIXTURE):
    polylines = [rectangle(handle(island, member), (member * PANEL_PITCH, island * ISLAND_PITCH))
                 for island in range(GROUP_COUNT) for member in (0, 1)]
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
def deleted(tmp_path_factory):
    folder = tmp_path_factory.mktemp("studio-group-delete")
    intake_path = save(folder / "intake.json", intake())
    assert producer.main(arguments(folder, intake=intake_path)) == 0
    graph = producer.deserialize_graph((folder / "graph.json").read_bytes())
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    return graph, metadata, folder, intake_path


@pytest.fixture(scope="module")
def grouped(deleted, tmp_path_factory):
    """The SAME intake through the groups producer: the committed state the delete starts from."""
    _, _, _, intake_path = deleted
    folder = tmp_path_factory.mktemp("studio-groups-for-delete")
    assert groups_producer.main(arguments(folder, intake=intake_path)) == 0
    return groups_producer.deserialize_graph((folder / "graph.json").read_bytes())


def test_the_run_ends_with_no_groups_and_the_panels_it_started_with(deleted):
    graph, _, _, _ = deleted
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    assert graph["frames"] == []
    assert sorted(int(name, 16) for name in handles(graph, [p["id"] for p in graph["panels"]])) == list(
        range(1, 2 * GROUP_COUNT + 1))
    for panel in graph["panels"]:
        assert panel["frame_ref"] is None and panel["matrix_cell"] is None
        assert panel["assignment"] == {"string_ref": None, "seq": None}
    assert graph["strings"] == [] and graph["electrical_zones"] == []
    # rev 1 committed the eleven groups, rev 2 deleted them.
    assert graph["rev"] == 2 and graph["parent_rev"] == 1


def test_metadata_records_eleven_groups_created_and_eleven_deleted(deleted):
    graph, metadata, folder, intake_path = deleted
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert metadata["parameters"] == {
        "family": "groups", "layer_filter": "*Panel*", "branch_max_offset": 120.0,
        "alignment_tolerance": 12.0, "installation_design": "Roof"}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_group_delete.v1",
        # The LEDGER's capability_version for panel-group-delete-all, which the gate compares.
        "capability": "0", "engine": "server-builtin", "catalog": "none", "solver": "none"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["execution_mode"] == "live" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    provenance = metadata["provenance"]
    assert provenance["intake_sha256"] == hashlib.sha256(intake_path.read_bytes()).hexdigest()
    assert provenance["groups_created"] == provenance["groups_deleted"] == GROUP_COUNT
    assert provenance["group_count"] == 0 and provenance["panel_count"] == 2 * GROUP_COUNT
    assert provenance["deleted_groups"] == sorted(
        "group:" + handle(island, 0) for island in range(GROUP_COUNT))


def test_the_mapping_is_empty_when_every_group_has_been_deleted(deleted):
    """Rule G8: the evidence references no entity, so neither does the mapping.

    The panels survive the delete and are NOT mapped: the plugin's own evidence
    after PanelGroupDeleteAll references nothing either.
    """
    graph, metadata, _, _ = deleted
    assert metadata["entity_mapping"] == {}
    assert producer.evidence_mapping(graph) == {}
    assert graph["panels"] and graph["frames"] == []


def test_a_run_that_creates_groups_without_deleting_maps_the_groups_and_their_panels(grouped):
    """The same rule from the other side: with groups in `after`, they and their members map."""
    mapping = producer.evidence_mapping(grouped)
    assert set(mapping) == {frame["id"] for frame in grouped["frames"]} | {
        panel["id"] for panel in grouped["panels"]}
    assert len(set(mapping.values())) == len(mapping) == GROUP_COUNT + 2 * GROUP_COUNT
    for frame in grouped["frames"]:
        members = handles(grouped, frame["panel_refs"])
        assert mapping[frame["id"]] == producer.group_neutral_id(members)
        assert sorted(mapping[ref] for ref in frame["panel_refs"]) == sorted(members)


def test_the_delete_ran_on_the_same_committed_state_the_groups_producer_builds(deleted, grouped):
    graph, _, _, _ = deleted
    assert len(grouped["frames"]) == GROUP_COUNT
    assert all(panel["frame_ref"] is not None for panel in grouped["panels"])
    # Same intake, same entity identities: the delete started from THIS graph.
    assert {panel["id"] for panel in grouped["panels"]} == {panel["id"] for panel in graph["panels"]}
    serialized = producer.serialize_graph(graph)
    for frame in grouped["frames"]:
        assert frame["id"] not in serialized, "a deleted group is still named somewhere"


def test_deleting_twice_is_a_no_op(deleted):
    graph, _, _, _ = deleted
    module = producer.builtin("solar_panel_group_delete")
    again = module.delete_all_groups(graph, {"expected_rev": graph["rev"]})
    assert again["no_op"] is True and again["deleted"] == 0
    assert again["graph"] == graph


def test_no_reference_to_a_deleted_group_can_survive(grouped):
    module = producer.builtin("solar_panel_group_delete")
    frame_ids = [frame["id"] for frame in grouped["frames"]]
    after = module.delete_all_groups(grouped, {"expected_rev": grouped["rev"]})["graph"]
    assert after["frames"] == [] and all(panel["frame_ref"] is None for panel in after["panels"])
    assert not any(frame_id in producer.serialize_graph(after) for frame_id in frame_ids)
    # A reference the builtin cannot reach refuses the whole delete instead.
    stale = deepcopy(grouped)
    stale["settings"]["extra"]["group_notes"] = {frame_ids[0]: "kept"}
    with pytest.raises(module.GraphValidationError, match="GROUP_REFERENCE_NOT_CLEARED"):
        module.delete_all_groups(stale, {"expected_rev": stale["rev"]})


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
