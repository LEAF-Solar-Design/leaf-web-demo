"""Offline checks for the Studio panel-add producer on a synthetic eleven-group intake.

Eleven is the group count the captured plugin run reported
(receipts/w2-panel-add-20260922: still 11 groups after the claim), and the run
freed ONE panel from ONE group and added it to a DIFFERENT one, leaving the rest
alone. The synthetic intake here is built to group into exactly eleven islands of
three, 5000 in apart, so the group that claims a freed panel is one the grouping
kernel would never have put it in, exactly as the capture's Group 6 claimed a
panel that belongs geometrically to the 555-panel group.
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


producer = load_module("solar_w1_studio_panel_add")
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


def arguments(folder, remove=("2",), to="group:4", **overrides):
    values = {"fixture": FIXTURE, "intake": None, **PARAMETERS, "to": to,
              "out-graph": folder / "graph.json", "out-metadata": folder / "metadata.json"}
    values.update(overrides)
    if values["intake"] is None:
        values["intake"] = save(folder / "intake.json", intake())
    if values["to"] is None:
        # The groups producer has no --to flag, so the argument list carries none.
        values.pop("to")
    argv = [part for key, value in values.items() for part in ("--" + key, str(value))]
    return argv + [part for name in remove for part in ("--remove", str(name))]


def handles(graph, refs):
    by_id = {panel["id"]: panel for panel in graph["panels"]}
    return [producer.normalized_handle(by_id[ref]["provenance"]["source_handle"]) for ref in refs]


def frame_holding(graph, name):
    """The frame whose members carry `name`, read back by handle."""
    return next(frame for frame in graph["frames"] if name in handles(graph, frame["panel_refs"]))


def run(folder, **kwargs):
    assert producer.main(arguments(folder, **kwargs)) == 0
    return (producer.deserialize_graph((folder / "graph.json").read_bytes()),
            json.loads((folder / "metadata.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def claimed(tmp_path_factory):
    """Handle 2 leaves island 0 and is claimed by island 1's group, 5000 in away."""
    folder = tmp_path_factory.mktemp("studio-panel-add")
    intake_path = save(folder / "intake.json", intake())
    graph, metadata = run(folder, remove=("2",), to="group:4", intake=intake_path)
    return graph, metadata, intake_path


@pytest.fixture(scope="module")
def grouped(claimed, tmp_path_factory):
    """The SAME intake through the groups producer: the committed state the claim starts from."""
    _, _, intake_path = claimed
    folder = tmp_path_factory.mktemp("studio-groups-for-add")
    assert groups_producer.main(arguments(folder, remove=(), to=None, intake=intake_path)) == 0
    return groups_producer.deserialize_graph((folder / "graph.json").read_bytes())


def test_the_freed_panel_joins_the_named_group_and_every_panel_survives(claimed):
    graph, _, _ = claimed
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    assert len(graph["frames"]) == GROUP_COUNT
    assert sorted(int(name, 16) for name in handles(graph, [p["id"] for p in graph["panels"]])) == list(
        range(1, GROUP_COUNT * GROUP_SIZE + 1))
    # Every panel is grouped again: the claim put the freed one back, elsewhere.
    assert all(panel["frame_ref"] is not None for panel in graph["panels"])
    receiver = frame_holding(graph, "4")
    assert sorted(handles(graph, receiver["panel_refs"]), key=lambda h: int(h, 16)) == ["2", "4", "5", "6"]
    # The plugin appended the claimed panel at the END of the membership it printed.
    assert handles(graph, receiver["panel_refs"])[-1] == "2"
    assert sorted(handles(graph, frame_holding(graph, "1")["panel_refs"])) == ["1", "3"]
    sizes = sorted(len(frame["panel_refs"]) for frame in graph["frames"])
    assert sizes == [GROUP_SIZE - 1] + [GROUP_SIZE] * (GROUP_COUNT - 2) + [GROUP_SIZE + 1]
    # rev 1 committed the eleven groups, rev 2 freed the panel, rev 3 claimed it.
    assert graph["rev"] == 3 and graph["parent_rev"] == 2


def test_the_receiving_group_keeps_its_matrix_rectangular(claimed):
    graph, _, _ = claimed
    receiver = frame_holding(graph, "4")
    assert len(receiver["matrix"]) == receiver["module_rows"]
    assert all(len(row) == receiver["module_columns"] for row in receiver["matrix"])
    assert receiver["module_slots"] == receiver["module_rows"] * receiver["module_columns"]
    assert receiver["module_slots"] >= GROUP_SIZE + 1
    cells = [cell["panel_ref"] for row in receiver["matrix"] for cell in row]
    assert sorted(ref for ref in cells if ref is not None) == sorted(receiver["panel_refs"])
    # The claimed panel's own cell says where it landed, and the cell says so back.
    joined = next(panel for panel in graph["panels"]
                  if producer.normalized_handle(panel["provenance"]["source_handle"]) == "2")
    location = joined["matrix_cell"]
    assert receiver["matrix"][location["row"]][location["col"]]["panel_ref"] == joined["id"]
    assert joined["frame_ref"] == receiver["id"]


def test_metadata_records_which_panel_moved_into_which_group(claimed):
    graph, metadata, intake_path = claimed
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert metadata["parameters"] == {
        "family": "groups", "layer_filter": "*Panel*", "branch_max_offset": 120.0,
        "alignment_tolerance": 12.0, "installation_design": "Roof"}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_panel_add.v1",
        # The LEDGER's capability_version for panel-add, which the gate compares.
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
    assert provenance["panels_freed"] == provenance["panels_added"] == 1
    assert provenance["freed"] == ["2"]
    # Rule G3 names a group by its lowest member, so claiming handle 2 renames the
    # receiving group even though nothing about the group itself moved.
    assert provenance["added"] == {"requested": "group:4", "group_before": "group:4",
                                   "group": "group:2", "panels": ["2"],
                                   "size_before": GROUP_SIZE, "size_after": GROUP_SIZE + 1}
    assert provenance["group_sizes"] == sorted(
        len(frame["panel_refs"]) for frame in graph["frames"])


def test_the_mapping_covers_every_group_and_its_members(claimed):
    """Rule G8: the evidence names the groups and their members, nothing else."""
    graph, metadata, _ = claimed
    mapping = metadata["entity_mapping"]
    assert mapping == producer.evidence_mapping(graph)
    assert set(mapping) == {frame["id"] for frame in graph["frames"]} | {
        ref for frame in graph["frames"] for ref in frame["panel_refs"]}
    assert len(set(mapping.values())) == len(mapping)
    for frame in graph["frames"]:
        members = handles(graph, frame["panel_refs"])
        assert mapping[frame["id"]] == producer.group_neutral_id(members)
        assert sorted(mapping[ref] for ref in frame["panel_refs"]) == sorted(members)
    # The claimed panel is mapped from its NEW group, and the group it left is renamed.
    joined = next(panel for panel in graph["panels"]
                  if producer.normalized_handle(panel["provenance"]["source_handle"]) == "2")
    assert mapping[joined["id"]] == "2"
    assert mapping[frame_holding(graph, "4")["id"]] == "group:2"
    assert mapping[frame_holding(graph, "1")["id"]] == "group:1"


def test_the_claim_ran_on_the_same_committed_state_the_groups_producer_builds(claimed, grouped):
    graph, _, _ = claimed
    assert len(grouped["frames"]) == GROUP_COUNT
    assert all(panel["frame_ref"] is not None for panel in grouped["panels"])
    # Same intake, same entity identities: the claim started from THIS graph.
    assert {panel["id"] for panel in grouped["panels"]} == {panel["id"] for panel in graph["panels"]}
    assert {frame["id"] for frame in grouped["frames"]} == {frame["id"] for frame in graph["frames"]}
    untouched = {frame["id"]: frame for frame in grouped["frames"]
                 if len(frame["panel_refs"]) == GROUP_SIZE}
    for frame in graph["frames"]:
        before = untouched.get(frame["id"])
        if before is not None and len(frame["panel_refs"]) == GROUP_SIZE:
            assert frame["panel_refs"] == before["panel_refs"]
            assert frame["matrix"] == before["matrix"]
            assert frame["rev"] == before["rev"] == 1


def test_a_claim_that_is_not_the_lowest_handle_leaves_the_group_named_as_it_was(tmp_path):
    graph, metadata = run(tmp_path, remove=("8",), to="group:4")
    assert metadata["provenance"]["added"] == {
        "requested": "group:4", "group_before": "group:4", "group": "group:4",
        "panels": ["8"], "size_before": GROUP_SIZE, "size_after": GROUP_SIZE + 1}
    assert sorted(handles(graph, frame_holding(graph, "4")["panel_refs"]),
                  key=lambda h: int(h, 16)) == ["4", "5", "6", "8"]
    assert sorted(handles(graph, frame_holding(graph, "7")["panel_refs"])) == ["7", "9"]
    assert metadata["entity_mapping"][frame_holding(graph, "7")["id"]] == "group:7"


def test_several_panels_from_several_groups_go_into_one_group(tmp_path):
    graph, metadata = run(tmp_path, remove=("2", "3", "8"), to="group:4")
    provenance = metadata["provenance"]
    assert provenance["panels_freed"] == provenance["panels_added"] == 3
    assert provenance["freed"] == ["2", "3", "8"]
    # The claim is one call in the order the flags named, which is the selection order.
    assert provenance["added"]["panels"] == ["2", "3", "8"]
    assert provenance["added"]["size_after"] == 6
    receiver = frame_holding(graph, "4")
    assert handles(graph, receiver["panel_refs"])[-3:] == ["2", "3", "8"]
    assert provenance["group_sizes"] == [1, 2] + [GROUP_SIZE] * (GROUP_COUNT - 3) + [6]
    assert all(panel["frame_ref"] is not None for panel in graph["panels"])


def test_a_group_reclaims_the_panel_it_just_lost_into_the_slot_it_emptied(tmp_path, grouped):
    """The freed slot is reused, so the matrix the kernel built does not grow."""
    graph, metadata = run(tmp_path, remove=("2",), to="group:1")
    before = next(frame for frame in grouped["frames"]
                  if "2" in handles(grouped, frame["panel_refs"]))
    after = frame_holding(graph, "2")
    assert after["id"] == before["id"]
    assert sorted(handles(graph, after["panel_refs"]), key=lambda h: int(h, 16)) == ["1", "2", "3"]
    assert (after["module_rows"], after["module_columns"], after["module_slots"]) == (
        before["module_rows"], before["module_columns"], before["module_slots"])
    assert metadata["provenance"]["added"] == {
        "requested": "group:1", "group_before": "group:1", "group": "group:1",
        "panels": ["2"], "size_before": GROUP_SIZE - 1, "size_after": GROUP_SIZE}
    assert metadata["provenance"]["group_sizes"] == [GROUP_SIZE] * GROUP_COUNT


@pytest.mark.parametrize("remove,to,message", [
    (("2",), "group:FF", "absent from the grouped drawing"),
    (("2",), "group:5", "absent from the grouped drawing"),
    (("2",), "zz", "invalid source handle"),
    (("FF",), "group:4", "absent from the grouped drawing"),
    (("2", "2"), "group:4", "only once"),
    (("1", "2", "3"), "group:4", "would empty a group"),
])
def test_a_claim_the_drawing_cannot_take_is_refused(tmp_path, capsys, remove, to, message):
    assert producer.main(arguments(tmp_path, remove=remove, to=to)) == 2
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
