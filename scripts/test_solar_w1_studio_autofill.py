"""Offline checks for the Studio auto-fill producer on the committed rooftop intake.

The licensed run this reproduces was captured on AutoCAD 2025 on 2026-09-23
(plans/ref/solar-parity-20260917/receipts/w2-autofill-20260923): REMOVEPANEL took
28 panels out of the 99-panel group, leaving 71, which is infeasible at a string
length of 14, and AUTOFILL then printed "1 panels moved" and moved exactly panel
8201 into the 137-panel group. Only the capture's INPUT (which 28 handles the
removal took) and its OUTCOME (8201, and which two groups) are written down here;
the group membership, the counts and the correction are all COMPUTED by running the
producer on data/rooftop_unsplit.intake.json, so a port that got the answer another
way would fail.

The producer is run twice: once with no removals, which is the control the
rebalanced run is diffed against (same producer, same intake, same four grouping
parameters, so every group is feasible and nothing moves), and once with the
captured 28. Entity ids are fresh per run, so the two runs are compared by SOURCE
HANDLE, never by id.
"""
import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_module("solar_w1_studio_autofill")
adapter = load_module("solar_studio_evidence")
# The producer puts server/ on sys.path, so the port's own predicate is the one
# this asserts feasibility with rather than a second copy of the rule.
from solar_autofill import is_feasible_count  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "rooftop_unsplit.dwg"
INTAKE = ROOT / "data" / "rooftop_unsplit.intake.json"
# The captured drawing's own four grouping parameters, and mSettings.NumPanelsInSequence
# at the captured AUTOFILL (bthost-autofill-after-reopen.jsonl).
PARAMETERS = {"branch-max-offset": 120, "alignment-tolerance": 12,
              "layer-contains": "Panels", "installation-design": "Roof",
              "max-string-length": 14}
STRING_LENGTH = 14
PANEL_COUNT = 882
GROUP_COUNT = 7
# removed-28-from-9CC7.json, in the capture's own order: the panels REMOVEPANEL took
# out of the 99-panel group, leaving 71.
REMOVED_28 = ["81E0", "81DF", "81DE", "81DD", "81DC", "81DB", "81DA", "81D9", "81D8",
              "81D7", "81D6", "81D5", "81D4", "81D3", "81D2", "81D1", "81D0", "81CF",
              "81CE", "81CD", "81CC", "81CB", "81CA", "81C9", "81C8", "81C7", "81C6",
              "81C5"]
MOVED_PANEL = "8201"
DONOR_SIZE = 71       # the cut group, after the 28 removals
RECEIVER_SIZE = 137   # the group the licensed run moved the panel into
# Rule G3 names a group by its lowest CURRENT member, so the receiver is renamed by
# the move it accepts: 8201 sorts below every handle it already held.
DONOR = "group:81E1"
RECEIVER_BEFORE = "group:8228"
RECEIVER_AFTER = "group:8201"
# "Correction: OptimalSolver: Group 6 -> Group 8 (1 panels, d=1027, direct)".
REPORTED_DISTANCE = 1027


def arguments(folder, remove=REMOVED_28, **overrides):
    values = {"fixture": FIXTURE, "intake": INTAKE, **PARAMETERS,
              "out-graph": folder / "graph.json", "out-metadata": folder / "metadata.json"}
    values.update(overrides)
    argv = [part for key, value in values.items() for part in ("--" + key, str(value))]
    return argv + [part for name in remove for part in ("--remove", str(name))]


def run(folder, **kwargs):
    assert producer.main(arguments(folder, **kwargs)) == 0
    return (producer.deserialize_graph((folder / "graph.json").read_bytes()),
            json.loads((folder / "metadata.json").read_text(encoding="utf-8")))


def handle_of(graph):
    """One pass: panel id -> normalized source handle, never a scan per lookup."""
    return {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
            for panel in graph["panels"]}


def membership(graph):
    """Group membership as handle sets, the only identity the two runs share."""
    handles = handle_of(graph)
    return [frozenset(handles[ref] for ref in frame["panel_refs"]) for frame in graph["frames"]]


@pytest.fixture(scope="module")
def control(tmp_path_factory):
    """No removals: every group the kernel builds is already feasible at 14."""
    return run(tmp_path_factory.mktemp("studio-autofill-control"), remove=())


@pytest.fixture(scope="module")
def rebalanced(tmp_path_factory):
    folder = tmp_path_factory.mktemp("studio-autofill")
    graph, metadata = run(folder)
    return graph, metadata, folder


def test_the_control_run_groups_the_drawing_and_moves_nothing(control):
    """The 99-panel group and the 137-panel group of the capture, before the removal."""
    graph, metadata = control
    assert producer.validate_graph(graph) == graph
    assert len(graph["frames"]) == GROUP_COUNT
    assert len(graph["panels"]) == PANEL_COUNT
    assert all(panel["frame_ref"] is not None for panel in graph["panels"])
    sizes = sorted(len(group) for group in membership(graph))
    assert sizes == [99, 104, 111, 123, 134, 137, 174]
    assert all(is_feasible_count(STRING_LENGTH, size) for size in sizes)
    cut = next(group for group in membership(graph) if len(group) == 99)
    assert set(REMOVED_28) < cut
    assert len(cut - set(REMOVED_28)) == DONOR_SIZE
    # Nothing to rebalance, so the run commits the grouping and stops there.
    assert metadata["provenance"]["corrections"] == []
    assert metadata["provenance"]["panels_moved"] == 0
    assert metadata["provenance"]["groups_modified"] == 0
    assert metadata["provenance"]["panels_removed"] == 0
    assert graph["rev"] == 1


def test_autofill_moves_exactly_panel_8201_from_the_71_group_to_the_137_group(rebalanced, control):
    """The captured outcome, computed: one panel, from the cut group to the 137-panel one."""
    graph, _, _ = rebalanced
    before = membership(control[0])
    after = membership(graph)
    donor_before = next(group for group in before if len(group) == 99) - set(REMOVED_28)
    receiver_before = next(group for group in before if len(group) == RECEIVER_SIZE)
    assert len(donor_before) == DONOR_SIZE and MOVED_PANEL in donor_before
    assert not is_feasible_count(STRING_LENGTH, DONOR_SIZE)

    assert donor_before - {MOVED_PANEL} in after
    assert receiver_before | {MOVED_PANEL} in after
    # Every group holds a count that splits into strings of 12 to 14 again.
    assert sorted(len(group) for group in after) == [70, 104, 111, 123, 134, 138, 174]
    assert all(is_feasible_count(STRING_LENGTH, len(group)) for group in after)


def test_every_other_group_keeps_exactly_the_members_it_had(rebalanced, control):
    graph, _, _ = rebalanced
    before = membership(control[0])
    donor_before = next(group for group in before if len(group) == 99)
    receiver_before = next(group for group in before if len(group) == RECEIVER_SIZE)
    untouched = [group for group in before if group not in (donor_before, receiver_before)]
    assert len(untouched) == GROUP_COUNT - 2
    for group in untouched:
        assert group in membership(graph)
    # The 28 the removal took are the only panels outside a group, and the moved
    # panel is in exactly one group.
    handles = handle_of(graph)
    free = sorted(handles[panel["id"]] for panel in graph["panels"] if panel["frame_ref"] is None)
    assert free == sorted(REMOVED_28)
    assert sum(1 for group in membership(graph) if MOVED_PANEL in group) == 1


def test_the_written_graph_survives_a_reopen_and_stays_valid(rebalanced):
    graph, metadata, folder = rebalanced
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    assert metadata["survived_reopen"] is True
    # rev 1 committed the seven groups, rev 2 removed the 28, rev 3 applied the correction.
    assert graph["rev"] == 3 and graph["parent_rev"] == 2
    assert len(graph["panels"]) == PANEL_COUNT and len(graph["frames"]) == GROUP_COUNT
    assert graph["strings"] == []
    reopened = producer.deserialize_graph((folder / "graph.json").read_bytes())
    assert reopened == graph


def test_the_two_groups_the_move_touched_keep_rectangular_matrices(rebalanced):
    graph, metadata, _ = rebalanced
    mapping = metadata["entity_mapping"]
    handles = handle_of(graph)
    for name, size in ((DONOR, DONOR_SIZE - 1), (RECEIVER_AFTER, RECEIVER_SIZE + 1)):
        frame = next(f for f in graph["frames"] if mapping[f["id"]] == name)
        assert len(frame["panel_refs"]) == size
        assert len(frame["matrix"]) == frame["module_rows"]
        assert all(len(row) == frame["module_columns"] for row in frame["matrix"])
        assert frame["module_slots"] == frame["module_rows"] * frame["module_columns"]
        cells = [cell["panel_ref"] for row in frame["matrix"] for cell in row]
        assert sorted(ref for ref in cells if ref is not None) == sorted(frame["panel_refs"])
    receiver = next(f for f in graph["frames"] if mapping[f["id"]] == RECEIVER_AFTER)
    moved = next(panel for panel in graph["panels"] if handles[panel["id"]] == MOVED_PANEL)
    assert moved["frame_ref"] == receiver["id"]
    assert moved["matrix_cell"] is not None
    cell = receiver["matrix"][moved["matrix_cell"]["row"]][moved["matrix_cell"]["col"]]
    assert cell["panel_ref"] == moved["id"]


def test_metadata_records_the_run_and_the_one_correction(rebalanced):
    graph, metadata, _ = rebalanced
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert metadata["parameters"] == {
        "family": "groups", "layer_filter": "*Panels*", "branch_max_offset": 120.0,
        "alignment_tolerance": 12.0, "installation_design": "Roof"}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_autofill.v1",
        # The LEDGER's capability_version for auto-fill, which the gate compares.
        "capability": "0", "engine": "server-builtin", "catalog": "none", "solver": "none"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["before"] == {"recorded": False}
    assert metadata["changes"] == {"created": [], "modified": [], "deleted": []}
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    assert metadata["execution_mode"] == "live" and metadata["state"] == "committed"
    assert metadata["synthetic_flagged"] is True
    assert "autofill/geometry-from-grouping-kernel" in metadata["fallback_fields"]
    provenance = metadata["provenance"]
    assert provenance["intake_sha256"] == hashlib.sha256(INTAKE.read_bytes()).hexdigest()
    assert provenance["panel_count"] == PANEL_COUNT
    assert provenance["group_count"] == provenance["groups_created"] == GROUP_COUNT
    assert provenance["panels_removed"] == 28
    assert provenance["removed"] == sorted(REMOVED_28)
    assert provenance["max_string_length"] == STRING_LENGTH
    assert provenance["solver"] == "OptimalPlanSolver"
    assert provenance["solver_feasible"] is provenance["solver_valid"] is True
    assert provenance["panels_moved"] == 1
    assert provenance["groups_modified"] == 2
    assert len(provenance["corrections"]) == 1
    correction = provenance["corrections"][0]
    assert correction["panels"] == [MOVED_PANEL]
    # A correction names the ids the two groups held BEFORE the move.
    assert (correction["from"], correction["to"]) == (DONOR, RECEIVER_BEFORE)
    assert correction["chain"] == "direct"
    assert REPORTED_DISTANCE - 1 <= correction["distance"] <= REPORTED_DISTANCE + 1


def test_the_mapping_names_every_group_by_its_members_after_the_move(rebalanced):
    """Rule G8: the evidence names the surviving groups and their members, nothing else."""
    graph, metadata, _ = rebalanced
    mapping = metadata["entity_mapping"]
    assert mapping == producer.evidence_mapping(graph)
    assert set(mapping) == {frame["id"] for frame in graph["frames"]} | {
        ref for frame in graph["frames"] for ref in frame["panel_refs"]}
    assert len(set(mapping.values())) == len(mapping)
    handles = handle_of(graph)
    for frame in graph["frames"]:
        members = [handles[ref] for ref in frame["panel_refs"]]
        assert mapping[frame["id"]] == producer.group_neutral_id(members)
        assert sorted(mapping[ref] for ref in frame["panel_refs"]) == sorted(members)
    names = {mapping[frame["id"]] for frame in graph["frames"]}
    assert DONOR in names and RECEIVER_AFTER in names and RECEIVER_BEFORE not in names
    # The 28 freed panels are still in the drawing and out of the evidence.
    assert not set(REMOVED_28) & set(mapping.values())


def test_groups_evidence_validates_the_joint_contract(rebalanced):
    graph, metadata, _ = rebalanced
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "groups", metadata)
    adapter.compare.validate_evidence(evidence, "groups")
    assert (graph, metadata) == originals
    assert evidence["input_sha256"] == adapter.compare.semantic_hash({
        "fixture_sha256": metadata["fixture_sha256"], "parameters": metadata["parameters"]})
    assert evidence["units"] == "in"
    groups = {group["name"]: [metadata["entity_mapping"][ref["entity_id"]]
                              for ref in group["membership"]] for group in evidence["after"]["groups"]}
    assert len(groups) == GROUP_COUNT
    assert len(groups[DONOR]) == DONOR_SIZE - 1 and MOVED_PANEL not in groups[DONOR]
    assert len(groups[RECEIVER_AFTER]) == RECEIVER_SIZE + 1
    assert MOVED_PANEL in groups[RECEIVER_AFTER]
    assert evidence["execution_mode"] == "live" and evidence["synthetic_flagged"] is True


def test_a_removal_the_drawing_cannot_take_is_refused_before_any_output(tmp_path, capsys):
    """Grounds the run: the producer reads the real drawing before it refuses."""
    assert producer.main(arguments(tmp_path, remove=["FFFFFF"])) == 2
    assert "absent from the grouped drawing" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


@pytest.mark.parametrize("override,message", [
    ({"max-string-length": 0}, "max string length must be a positive integer"),
    ({"branch-max-offset": 0}, "positive finite"),
    ({"alignment-tolerance": "nan"}, "positive finite"),
    ({"installation-design": "Ground"}, "installation design must be Roof"),
    ({"layer-contains": ""}, "layer contains"),
    ({"layer-contains": "Nope"}, "no panel polylines"),
])
def test_parameters_are_validated_before_output(tmp_path, capsys, override, message):
    assert producer.main(arguments(tmp_path, remove=(), **override)) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_fixture_hash_mismatch_fails_before_output(tmp_path, capsys):
    document = json.loads(INTAKE.read_text(encoding="utf-8"))
    document["source"]["dwg_sha256"] = "0" * 64
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(document), encoding="utf-8")
    assert producer.main(arguments(tmp_path, remove=(), intake=intake)) == 2
    assert "fixture hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()
