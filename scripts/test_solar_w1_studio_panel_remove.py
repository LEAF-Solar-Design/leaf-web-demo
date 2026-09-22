"""Offline checks for the Studio panel-remove producer on a synthetic eleven-group intake.

Eleven is the group count the captured plugin run reported
(receipts/w2-panel-remove-20260922: still 11 groups after the removal), and the
run removed ONE panel from ONE of them, leaving the rest alone. The synthetic
intake here is built to group into exactly eleven groups of three, so a removal
leaves a group that still exists and still has members, exactly as the plugin's
"555 panels remain in the group" reports.
"""
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


producer = load_module("solar_w1_studio_panel_remove")
groups_producer = load_module("solar_w1_studio_groups")
ROOT = Path(__file__).resolve().parents[1]
# Any committed file binds the synthetic intake to a fixture; no rooftop capture is read here.
FIXTURE = ROOT / "contract" / "solar-design-graph.v1.schema.json"
WIDTH, HEIGHT = 77.0, 38.5
GROUP_COUNT = 11
GROUP_SIZE = 3
# Eleven islands of three panels, 5000 in apart: far beyond any branch offset, so the
# kernel answers with exactly one group per island.
ISLAND_PITCH = 5000.0
PANEL_PITCH = 79.0
PARAMETERS = {"branch-max-offset": 120, "alignment-tolerance": 12,
              "layer-contains": "Panel", "installation-design": "Roof"}


def handle(island, member):
    return format(GROUP_SIZE * island + member + 1, "X")


def rectangle(name, centre, layer="Panels", width=WIDTH, height=HEIGHT):
    cx, cy = centre
    hw, hh = width / 2, height / 2
    return {"handle": name, "layer": layer, "closed": True,
            "pts": [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh], [cx - hw, cy + hh]]}


def intake(fixture=FIXTURE):
    polylines = [rectangle(handle(island, member), (member * PANEL_PITCH, island * ISLAND_PITCH))
                 for island in range(GROUP_COUNT) for member in range(GROUP_SIZE)]
    return {"source": {"dwg_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()},
            "polylines": polylines}


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def arguments(folder, remove=("2",), **overrides):
    values = {"fixture": FIXTURE, "intake": None, **PARAMETERS,
              "out-graph": folder / "graph.json", "out-metadata": folder / "metadata.json"}
    values.update(overrides)
    if values["intake"] is None:
        values["intake"] = save(folder / "intake.json", intake())
    argv = [part for key, value in values.items() for part in ("--" + key, str(value))]
    return argv + [part for name in remove for part in ("--remove", str(name))]


def handles(graph, refs):
    by_id = {panel["id"]: panel for panel in graph["panels"]}
    return [producer.normalized_handle(by_id[ref]["provenance"]["source_handle"]) for ref in refs]


def run(folder, **kwargs):
    assert producer.main(arguments(folder, **kwargs)) == 0
    return (producer.deserialize_graph((folder / "graph.json").read_bytes()),
            json.loads((folder / "metadata.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def removed(tmp_path_factory):
    """Handle 2 out of island 0, a member that is NOT the group's lowest handle."""
    folder = tmp_path_factory.mktemp("studio-panel-remove")
    intake_path = save(folder / "intake.json", intake())
    graph, metadata = run(folder, remove=("2",), intake=intake_path)
    return graph, metadata, intake_path


@pytest.fixture(scope="module")
def grouped(removed, tmp_path_factory):
    """The SAME intake through the groups producer: the committed state the removal starts from."""
    _, _, intake_path = removed
    folder = tmp_path_factory.mktemp("studio-groups-for-remove")
    # The groups producer has no --remove flag, so the argument list carries none.
    assert groups_producer.main(arguments(folder, remove=(), intake=intake_path)) == 0
    return groups_producer.deserialize_graph((folder / "graph.json").read_bytes())


def test_the_named_panel_leaves_exactly_one_group_and_stays_in_the_graph(removed):
    graph, _, _ = removed
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    assert len(graph["frames"]) == GROUP_COUNT
    assert sorted(int(name, 16) for name in handles(graph, [p["id"] for p in graph["panels"]])) == list(
        range(1, GROUP_COUNT * GROUP_SIZE + 1))
    gone = [panel for panel in graph["panels"] if panel["frame_ref"] is None]
    assert [producer.normalized_handle(panel["provenance"]["source_handle"]) for panel in gone] == ["2"]
    assert gone[0]["matrix_cell"] is None
    sizes = sorted(len(frame["panel_refs"]) for frame in graph["frames"])
    assert sizes == [GROUP_SIZE - 1] + [GROUP_SIZE] * (GROUP_COUNT - 1)
    # rev 1 committed the eleven groups, rev 2 removed the panel.
    assert graph["rev"] == 2 and graph["parent_rev"] == 1


def test_the_group_that_lost_the_panel_keeps_its_matrix_rectangular(removed):
    graph, _, _ = removed
    shrunk = next(frame for frame in graph["frames"] if len(frame["panel_refs"]) == GROUP_SIZE - 1)
    assert len(shrunk["matrix"]) == shrunk["module_rows"]
    assert all(len(row) == shrunk["module_columns"] for row in shrunk["matrix"])
    assert shrunk["module_slots"] == shrunk["module_rows"] * shrunk["module_columns"]
    cells = [cell["panel_ref"] for row in shrunk["matrix"] for cell in row]
    assert sorted(ref for ref in cells if ref is not None) == sorted(shrunk["panel_refs"])
    assert sum(1 for ref in cells if ref is None) >= 1
    assert sorted(handles(graph, shrunk["panel_refs"])) == ["1", "3"]


def test_metadata_records_which_panel_left_which_group(removed):
    graph, metadata, intake_path = removed
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert metadata["parameters"] == {
        "family": "groups", "layer_filter": "*Panel*", "branch_max_offset": 120.0,
        "alignment_tolerance": 12.0, "installation_design": "Roof"}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_panel_remove.v1",
        # The LEDGER's capability_version for panel-remove, which the gate compares.
        "capability": "0", "engine": "server-builtin", "catalog": "none", "solver": "none"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["execution_mode"] == "live" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    provenance = metadata["provenance"]
    assert provenance["intake_sha256"] == hashlib.sha256(intake_path.read_bytes()).hexdigest()
    assert provenance["groups_created"] == GROUP_COUNT == provenance["group_count"]
    assert provenance["panel_count"] == GROUP_COUNT * GROUP_SIZE
    assert provenance["panels_removed"] == 1
    assert provenance["removed"] == [
        {"panel": "2", "group": "group:1", "group_before": "group:1", "remaining": 2}]


def test_the_mapping_covers_the_surviving_groups_and_their_members_only(removed):
    """Rule G8: the evidence names the surviving groups and their members, nothing else."""
    graph, metadata, _ = removed
    mapping = metadata["entity_mapping"]
    assert mapping == producer.evidence_mapping(graph)
    assert set(mapping) == {frame["id"] for frame in graph["frames"]} | {
        ref for frame in graph["frames"] for ref in frame["panel_refs"]}
    assert len(set(mapping.values())) == len(mapping)
    for frame in graph["frames"]:
        members = handles(graph, frame["panel_refs"])
        assert mapping[frame["id"]] == producer.group_neutral_id(members)
        assert sorted(mapping[ref] for ref in frame["panel_refs"]) == sorted(members)
        # Every remaining member is mapped, so the mapping loses only the removal.
        assert all(ref in mapping for ref in frame["panel_refs"])
    # The removed panel is still in the drawing, and the evidence no longer names it.
    gone = next(panel for panel in graph["panels"] if panel["frame_ref"] is None)
    assert producer.normalized_handle(gone["provenance"]["source_handle"]) == "2"
    assert gone["id"] not in mapping
    assert "2" not in set(mapping.values())


def test_the_removal_ran_on_the_same_committed_state_the_groups_producer_builds(removed, grouped):
    graph, _, _ = removed
    assert len(grouped["frames"]) == GROUP_COUNT
    assert all(panel["frame_ref"] is not None for panel in grouped["panels"])
    # Same intake, same entity identities: the removal started from THIS graph.
    assert {panel["id"] for panel in grouped["panels"]} == {panel["id"] for panel in graph["panels"]}
    assert {frame["id"] for frame in grouped["frames"]} == {frame["id"] for frame in graph["frames"]}
    untouched = {frame["id"]: frame for frame in grouped["frames"]
                 if len(frame["panel_refs"]) == GROUP_SIZE}
    for frame in graph["frames"]:
        before = untouched.get(frame["id"])
        if before is not None and len(frame["panel_refs"]) == GROUP_SIZE:
            assert frame["panel_refs"] == before["panel_refs"]
            assert frame["rev"] == before["rev"] == 1


def test_removing_a_groups_lowest_handle_renames_the_group(tmp_path):
    """Rule G3 derives the neutral id from CURRENT members, so both ids are recorded."""
    graph, metadata = run(tmp_path, remove=("4",))
    assert metadata["provenance"]["removed"] == [
        {"panel": "4", "group": "group:5", "group_before": "group:4", "remaining": 2}]
    renamed = next(frame for frame in graph["frames"] if len(frame["panel_refs"]) == GROUP_SIZE - 1)
    assert sorted(handles(graph, renamed["panel_refs"])) == ["5", "6"]
    assert metadata["entity_mapping"][renamed["id"]] == "group:5"


def test_several_panels_from_several_groups_go_in_one_run(tmp_path):
    graph, metadata = run(tmp_path, remove=("2", "3", "8"))
    assert metadata["provenance"]["panels_removed"] == 3
    assert [record["panel"] for record in metadata["provenance"]["removed"]] == ["2", "3", "8"]
    assert {record["group"] for record in metadata["provenance"]["removed"]} == {"group:1", "group:7"}
    sizes = sorted(len(frame["panel_refs"]) for frame in graph["frames"])
    assert sizes == [1, 2] + [GROUP_SIZE] * (GROUP_COUNT - 2)
    ungrouped = sorted(producer.normalized_handle(panel["provenance"]["source_handle"])
                       for panel in graph["panels"] if panel["frame_ref"] is None)
    assert ungrouped == ["2", "3", "8"]


@pytest.mark.parametrize("remove,message", [
    (("FF",), "absent from the grouped drawing"),
    (("2", "2"), "only once"),
    (("1", "2", "3"), "would empty a group"),
    (("zz",), "invalid source handle"),
])
def test_a_removal_the_group_cannot_take_is_refused(tmp_path, capsys, remove, message):
    assert producer.main(arguments(tmp_path, remove=remove)) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


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
