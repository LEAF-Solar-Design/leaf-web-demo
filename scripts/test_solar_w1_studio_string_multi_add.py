"""Offline checks for the Studio MULTISTRING producer on the committed rooftop capture.

The solve half is the solve producer and the delete half is the string-delete
producer, so the circuits this run re-strings beside are the 66 the plugin itself
committed (server/tests/fixtures/w1_rooftop_unsplit_solve.json) and the panels it
feeds the stringer are the ones a real delete freed. Two circuits of the FIRST
group are deleted and all of their panels are then selected, which is the shape of
the licensed capture: two circuits out, one sub-matrix solve, new circuits back.

The sub-matrix answer is synthesized here, never fetched: `synthesize` builds the
one response the wire contract accepts for the exact grid the producer sent, with a
serpentine Seq order and the string lengths StringComboFunc asked for. That is what
makes these tests hermetic AND makes the cut differ from the plugin's own circuits,
which is the point of MULTISTRING: the solver decides the membership, the selection
only decides which panels take part.
"""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_module("solar_w1_studio_string_multi_add")
adapter = load_module("solar_studio_evidence")
cloud = producer.cloud
combo = producer.combo
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/rooftop_unsplit.dwg"
INTAKE = ROOT / "data/rooftop_unsplit.intake.json"
PLACEMENT = ROOT / "data/rooftop_unsplit.licensed-placement.json"
CAPTURE = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_solve.json"
SIZING = ROOT / "server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json"
STRING_COUNT = 66
PANEL_COUNT = 882
MAX_STRING_LENGTH = 14
DWGNAME = "rooftop_demo.dwg"
# Two circuits of one group: their panels share a frame, so the sub-matrix is one
# block of that group's matrix rather than a stack, exactly as the plugin's
# temporary PanelGroup is one group.
DELETED = 2


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def capture():
    return json.loads(CAPTURE.read_text(encoding="utf-8"))


def plugin_strings():
    """The 66 memberships the licensed plugin committed, as normalized handles."""
    return [[producer.normalized_handle(handle) for handle in members]
            for group in capture()["groups"] for members in group["plugin_strings"]]


def first_group_strings():
    return [[producer.normalized_handle(handle) for handle in members]
            for members in capture()["groups"][0]["plugin_strings"]]


def deleted_ids():
    """The two lowest neutral ids of the first group's circuits."""
    return sorted("string:" + members[0] for members in first_group_strings())[:DELETED]


def selected_handles():
    """Every panel the two deleted circuits free, as a SET in sorted order."""
    erased = set(deleted_ids())
    return sorted(handle for members in first_group_strings()
                  if "string:" + members[0] in erased for handle in members)


def expected_sequences():
    return combo.sequences(MAX_STRING_LENGTH, len(selected_handles()))


def expected_lengths():
    first_length, second_length, first_quantity, second_quantity = expected_sequences()
    return sorted([first_length] * first_quantity + [second_length] * second_quantity)


def solve_replay(folder):
    return save(folder / "solve-replay.json",
                {"responses": [group["response"] for group in capture()["groups"]]})


def solve_fields(folder, **overrides):
    values = {"fixture": FIXTURE, "intake": INTAKE, "placement": PLACEMENT,
              "sizing_response": SIZING, "max_string_length": MAX_STRING_LENGTH,
              "dwgname": DWGNAME, "replay": solve_replay(folder), "grant_ref": None,
              "tenant": None, "record_responses": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def arguments(folder, delete=(), add=(), **overrides):
    values = {
        "fixture": FIXTURE, "intake": INTAKE, "placement": PLACEMENT,
        "sizing-response": SIZING, "max-string-length": MAX_STRING_LENGTH,
        "dwgname": DWGNAME, "replay": solve_replay(folder),
        "out-graph": folder / "graph.json", "out-metadata": folder / "metadata.json",
    }
    values.update(overrides)
    argv = [part for key, value in values.items() if value is not None
            for part in ("--" + key, str(value))]
    argv += [part for name in delete for part in ("--delete", str(name))]
    return argv + [part for handle in add for part in ("--add", str(handle))]


def offline():
    """No network, and no private grant resolved on any replayed path."""
    return (patch.object(cloud.requests.sessions.Session, "request",
                         side_effect=AssertionError("offline producer must not use network")),
            patch.object(cloud, "resolve_grant",
                         side_effect=AssertionError("replay must not resolve a private grant")),
            patch.object(producer.solve.sizing_cloud, "resolve_grant",
                         side_effect=AssertionError("recorded sizing must not resolve a grant")))


def run_offline(argv, solved=None):
    """Run the CLI with no network. `solved` replaces the solve half with its cached result.

    The solve producer is exercised once per module by the `prepared` fixture; every
    run after that reuses that committed graph instead of replaying 7 group solves
    again, which is the same state by construction (panel ids are derived from the
    intake hash and the circuits are the capture's own).
    """
    network, grant, sizing = offline()
    cached = (patch.object(producer.solve, "produce",
                           lambda args: (deepcopy(solved[0]), deepcopy(solved[1])))
              if solved is not None else nullcontext())
    with network, grant, sizing, cached:
        return producer.main(argv)


def synthesize(request, job_id):
    """The one stringer answer the wire contract accepts for this exact grid.

    Serpentine over the sub-matrix's own cells, Seq 1..N in visiting order, and the
    string lengths StringComboFunc asked for, so `cloud.proposal` can check the echo,
    the path coverage and the lengths against the request before anything commits.
    """
    parsed = cloud.StringerRequest.model_validate(request)
    grid = parsed.wire_payload()["grid"]
    order = []
    for index, row in enumerate(grid["Rows"]):
        columns = range(len(row["Panels"]))
        for column in (columns if index % 2 == 0 else reversed(columns)):
            if row["Panels"][column]["Code"] == 1:
                order.append((index, column))
    for sequence, (index, column) in enumerate(order, 1):
        grid["Rows"][index]["Panels"][column]["Seq"] = sequence
    lengths, counts = grid["Sequences"]
    sequence_length = [length for length, count in zip(lengths, counts) for _ in range(count)]
    path = [[index + 1, column + 1] for index, column in order]
    info = {
        "steps_taken": len(path), "steps_taken_in_string": len(path),
        "horizontal_movements": 0, "vertical_movements": 0, "invalid_vertical_movements": 0,
        "distance_total": 0.0, "vertical_sequential": 0, "diagonal_movements": 0,
        "allow_one_gap_hops": False, "max_gap_hop": 0, "gap_hop_movements": 0,
        "vertical_temp": -1, "sequence_length": sequence_length,
        "remaining_string_length": 0, "sequence_length_index": 0, "num_panels": float(len(path)),
        "visited_path": path, "str_length_state_index": 0, "terminated": True,
        "last_agent_x": path[-1][0], "last_agent_y": path[-1][1],
    }
    return {"status": "completed", "job_id": job_id, "data": {
        "status": "completed",
        "best_result": {"last_action": 0, "terminated": True, "info": info, "beam_idx": 0,
                        "grid_id": job_id, "model_id": "synthetic-multistring",
                        "gumbel_scale": 1.0, "string_start_split_count": 1,
                        "model": "synthetic-multistring"},
        "final_grid": grid, "model_used": "synthetic-multistring", "gumbel_scale_used": 1.0,
        "distance_total": 0.0, "total_valid_solutions": 1, "gumbel_summary": {"1.0": 0},
        "first_pass_best_distance": 0.0, "improvement": None, "second_pass_triggered": False}}


def memberships(graph):
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    return [[handles[ref] for ref in string["ordered_panel_refs"]] for string in graph["strings"]]


def read(folder):
    return (producer.deserialize_graph((folder / "graph.json").read_bytes()),
            json.loads((folder / "metadata.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    """One real solve, the delete applied, and the sub-matrix answer built from it."""
    folder = tmp_path_factory.mktemp("studio-multi-string-prepare")
    network, grant, sizing = offline()
    with network, grant, sizing:
        solved, metadata = producer.commit_solve(solve_fields(folder), folder)
    refs = producer.deletion_plan(solved, deleted_ids())
    deletion = producer.builtin("solar_string_delete").delete_strings(
        deepcopy(solved), {"expected_rev": solved["rev"], "string_refs": refs})
    freed = deletion["graph"]
    selected = set(producer.selection_plan(freed, selected_handles()))
    request, sequences = producer.multi_request(
        freed, selected, max_string_length=MAX_STRING_LENGTH, dwgname=DWGNAME)
    replay = save(folder / "multi-replay.json",
                  {"responses": [synthesize(request, producer.MULTI_JOB)]})
    return SimpleNamespace(solved=(solved, metadata), freed=freed, selected=selected,
                           request=request, sequences=sequences, replay=replay, folder=folder)


@pytest.fixture(scope="module")
def committed(prepared, tmp_path_factory):
    folder = tmp_path_factory.mktemp("studio-multi-string")
    argv = arguments(folder, delete=deleted_ids(), add=selected_handles(),
                     **{"multi-replay": prepared.replay})
    assert run_offline(argv, solved=prepared.solved) == 0
    graph, metadata = read(folder)
    return graph, metadata, folder


def added_memberships(graph):
    """The circuits this run committed: exactly the ones inside the selection.

    A surviving circuit cannot qualify, because the selection is the set of panels
    the delete freed and no survivor wires any of them.
    """
    selection = set(selected_handles())
    return [members for members in memberships(graph) if set(members) <= selection]


def test_the_committed_cut_is_the_one_the_stringer_returned(prepared, committed):
    graph, metadata, _ = committed
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in prepared.freed["panels"]}
    by_upper = {ref.upper(): ref for ref in prepared.selected}
    # The service's own cut of its final grid, read the way StringPlacement reads it.
    cut = producer.ordered_strings(synthesize(prepared.request, producer.MULTI_JOB))
    expected = [[handles[by_upper[identity]] for identity in members] for members in cut]
    added = added_memberships(graph)
    # MULTISTRING discards the pick order: the membership and the order of every
    # committed circuit come from the solver, not from `--add`.
    assert added == expected
    # The combination decides how many circuits and how long each one is.
    assert sorted(len(members) for members in added) == expected_lengths()
    assert len(graph["strings"]) == STRING_COUNT - DELETED + len(added)
    assert all(len(members) <= MAX_STRING_LENGTH for members in added)


def test_every_selected_panel_is_wired_exactly_once(committed):
    graph, metadata, _ = committed
    selection = selected_handles()
    chosen = set(selection)
    wired = [handle for members in memberships(graph) for handle in members
             if handle in chosen]
    assert sorted(wired) == selection
    coverage = graph["extra"]["solve_coverage"]
    assert coverage["duplicate_panel_refs"] == []
    # The delete freed exactly these panels and the multi-add re-wired all of them,
    # so nothing is left unassigned.
    assert coverage["unassigned_panel_refs"] == []
    assert metadata["provenance"]["panels_unassigned"] == 0
    handles = {panel["id"]: producer.normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    assert len(handles) == PANEL_COUNT
    for panel in graph["panels"]:
        assert panel["assignment"]["string_ref"] is not None
        assert panel["frame_ref"] is not None and panel["matrix_cell"] is not None


def test_the_sequences_come_from_the_combo_port(prepared, committed):
    graph, metadata, _ = committed
    count = len(selected_handles())
    assert prepared.sequences == combo.sequences(MAX_STRING_LENGTH, count)
    assert metadata["provenance"]["multi_sequences"] == prepared.sequences
    # The flat list the plugin writes at BranchCmd.cs:17446, and the request carries it.
    assert prepared.request["grid"]["Sequences"] == prepared.sequences
    first_length, second_length, first_quantity, second_quantity = prepared.sequences
    assert first_length * first_quantity + second_length * second_quantity == count
    assert metadata["provenance"]["added_lengths"] == expected_lengths()


def test_the_sub_matrix_holds_exactly_the_selected_panels(prepared):
    cells = producer.submatrix(prepared.freed, prepared.selected)
    present = [cell["panel_ref"] for row in cells for cell in row if cell is not None]
    assert sorted(present) == sorted(prepared.selected)
    assert len(cells) <= producer.MAX_GRID and len(cells[0]) <= producer.MAX_GRID
    assert all(len(row) == len(cells[0]) for row in cells)
    # Every row and column of the sub-matrix carries at least one selected panel:
    # the rest of the group is not in the grid the solver sees.
    assert all(any(cell is not None for cell in row) for row in cells)
    for column in range(len(cells[0])):
        assert any(row[column] is not None for row in cells)
    wire = [cell for row in prepared.request["grid"]["Rows"] for cell in row["Panels"]]
    assert sorted(cell["Id"] for cell in wire if cell["Code"] == 1) == sorted(prepared.selected)


def test_no_other_circuit_changed(committed):
    graph, _, _ = committed
    erased = set(deleted_ids())
    survivors = [members for members in plugin_strings() if "string:" + members[0] not in erased]
    assert len(survivors) == STRING_COUNT - DELETED
    committed_now = memberships(graph)
    selection = set(selected_handles())
    assert sorted(members for members in committed_now
                  if not set(members) <= selection) == sorted(survivors)
    # One revision per committed circuit, ending at the graph's own: the string-add
    # builtin commits them one at a time, and nothing the plugin solved moved.
    added_revs = sorted(string["rev"] for string, members in zip(graph["strings"], committed_now)
                        if set(members) <= selection)
    survivor_revs = [string["rev"] for string, members in zip(graph["strings"], committed_now)
                     if not set(members) <= selection]
    assert added_revs == list(range(graph["rev"] - len(added_revs) + 1, graph["rev"] + 1))
    assert max(survivor_revs) < min(added_revs)
    assert [frame["name"] for frame in graph["frames"]] == [f"Group {i}" for i in range(1, 8)]


def test_metadata_records_the_solve_the_delete_and_the_multi_add(prepared, committed):
    graph, metadata, folder = committed
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
    assert metadata["parameters"] == {"family": "strings", "max_string_length": MAX_STRING_LENGTH}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1",
        "producer": "solar_w1_studio_string_multi_add.v1",
        # The LEDGER's capability_version for string-multi-add, which the gate compares.
        "capability": "0", "engine": "server-builtin", "catalog": "none",
        "solver": "leaf-stringer-service"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["execution_mode"] == "replay" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    assert metadata["elapsed_ms"] >= 0
    provenance = metadata["provenance"]
    for field, path in (("intake_sha256", INTAKE), ("placement_sha256", PLACEMENT),
                        ("sizing_response_sha256", SIZING),
                        ("multi_replay_sha256", prepared.replay)):
        assert provenance[field] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert provenance["strings_solved"] == STRING_COUNT
    assert provenance["strings_deleted"] == DELETED
    assert provenance["deleted_strings"] == deleted_ids()
    assert provenance["selected_panels"] == selected_handles()
    assert provenance["selected_count"] == len(selected_handles())
    # One sub-matrix call, one response, hashed.
    assert len(provenance["multi_response_sha256s"]) == 1
    assert all(re.fullmatch(r"[0-9a-f]{64}", value)
               for value in provenance["multi_response_sha256s"])
    assert provenance["added_strings"] == ["string:" + members[0]
                                           for members in added_memberships(graph)]
    assert provenance["strings_remaining"] == len(graph["strings"])
    assert "grant_ref" not in json.dumps((graph, metadata))


def test_the_mapping_covers_every_panel_and_every_committed_circuit(committed):
    """Rule G8: the evidence names every panel, the survivors, and the added circuits."""
    graph, metadata, _ = committed
    mapping = metadata["entity_mapping"]
    assert mapping == producer.evidence_mapping(graph)
    assert set(mapping) == {entity["id"] for entity in graph["panels"] + graph["strings"]}
    assert len(set(mapping.values())) == len(mapping)
    for string in graph["strings"]:
        assert mapping[string["id"]] == "string:" + mapping[string["ordered_panel_refs"][0]]
    named = set(mapping.values())
    assert set(metadata["provenance"]["added_strings"]) <= named
    # The two deleted circuits are gone from the evidence; their panels are not.
    assert set(deleted_ids()).isdisjoint(named)
    assert set(selected_handles()) <= named


def test_produced_evidence_validates_joint_identity_contract(committed):
    graph, metadata, _ = committed
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "strings", metadata)
    adapter.compare.validate_evidence(evidence, "strings")
    assert evidence["revision"] == metadata["revision"]
    assert evidence["provenance"]["studio_graph_rev"] == graph["rev"]
    assert evidence["units"] == "in"
    ids = [metadata["entity_mapping"][record["id"]["entity_id"]]
           for record in evidence["after"]["strings"]]
    assert ids == sorted(ids) and len(ids) == len(graph["strings"])
    assert set(deleted_ids()).isdisjoint(ids)
    assert evidence["after"]["duplicate_panels"] == []
    assert evidence["after"]["unassigned_panels"] == []
    assert evidence["after"]["length_distribution"] == sorted(
        string["module_count"] for string in graph["strings"])
    assert (graph, metadata) == originals


def test_a_live_sub_matrix_call_records_a_replayable_answer(prepared, tmp_path):
    """Live mode writes what it received; replaying that file commits the same circuits."""
    record = tmp_path / "recorded-multi.json"
    answers = {}

    def transport(request, grant):
        answers["raw"] = cloud.canonical_bytes(synthesize(request.model_dump(),
                                                          producer.MULTI_JOB))
        return answers["raw"]

    live = tmp_path / "live"
    live.mkdir()
    argv = arguments(live, delete=deleted_ids(), add=selected_handles(), replay=None,
                     **{"grant-ref": "live-grant", "tenant": "studio-live",
                        "record-multi": record})
    with patch.object(cloud, "post_stringer", transport), \
            patch.object(cloud, "resolve_grant",
                         lambda *args: producer.CloudGrant("studio-live", "")), \
            patch.object(producer.solve, "produce",
                         lambda args: (deepcopy(prepared.solved[0]),
                                       deepcopy(prepared.solved[1]))):
        assert producer.main(argv) == 0
    recorded = json.loads(record.read_text(encoding="utf-8"))
    assert recorded == {"responses": [json.loads(answers["raw"])]}
    live_graph, live_metadata = read(live)
    assert live_metadata["provenance"]["multi_response_sha256s"] == [
        hashlib.sha256(answers["raw"]).hexdigest()]
    assert "multi_replay_sha256" not in live_metadata["provenance"]

    replayed = tmp_path / "replayed"
    replayed.mkdir()
    assert run_offline(arguments(replayed, delete=deleted_ids(), add=selected_handles(),
                                 **{"multi-replay": record}),
                       solved=prepared.solved) == 0
    graph, metadata = read(replayed)
    # The recorded answer replays to the same circuits: same memberships, same
    # response hash, only the mode differs.
    assert sorted(memberships(graph)) == sorted(memberships(live_graph))
    assert (metadata["provenance"]["multi_response_sha256s"]
            == live_metadata["provenance"]["multi_response_sha256s"])
    assert metadata["provenance"]["multi_sequences"] == prepared.sequences


def test_an_unknown_panel_is_refused_before_any_mutation(prepared, tmp_path, capsys):
    absent = "FFFFFFFF"
    assert absent not in {producer.normalized_handle(panel["provenance"]["source_handle"])
                          for panel in prepared.freed["panels"]}
    with patch.object(cloud, "post_stringer",
                      side_effect=AssertionError("an unknown panel must be refused first")):
        assert run_offline(arguments(tmp_path, delete=deleted_ids(), add=(absent,),
                                     **{"multi-replay": prepared.replay}),
                           solved=prepared.solved) == 2
    assert "absent from the drawing" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_a_wired_panel_is_refused_before_the_stringer_is_called(prepared, tmp_path, capsys):
    erased = set(deleted_ids())
    wired = next(members[0] for members in plugin_strings()
                 if "string:" + members[0] not in erased)
    with patch.object(cloud, "post_stringer",
                      side_effect=AssertionError("a wired panel must be refused first")):
        assert run_offline(arguments(tmp_path, delete=deleted_ids(),
                                     add=selected_handles() + [wired],
                                     **{"multi-replay": prepared.replay}),
                           solved=prepared.solved) == 2
    assert "a circuit already wires" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


@pytest.mark.parametrize("add,message", [
    (("zz",), "invalid source handle"),
    (("",), "invalid source handle"),
    (("string:93A6",), "invalid source handle"),
])
def test_a_malformed_panel_handle_is_refused_before_the_solve_runs(tmp_path, capsys, add, message):
    with patch.object(producer.solve, "produce",
                      side_effect=AssertionError("a malformed handle must be refused first")):
        assert run_offline(arguments(tmp_path, delete=deleted_ids(), add=add,
                                     **{"multi-replay": tmp_path / "absent.json"})) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


def test_a_repeated_panel_is_refused_before_the_solve_runs(tmp_path, capsys):
    repeated = selected_handles()[0]
    with patch.object(producer.solve, "produce",
                      side_effect=AssertionError("a repeated handle must be refused first")):
        assert run_offline(arguments(tmp_path, delete=deleted_ids(),
                                     add=(repeated, repeated),
                                     **{"multi-replay": tmp_path / "absent.json"})) == 2
    assert "only once" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


def test_a_replayed_solve_may_not_reach_the_network_for_its_sub_matrix(tmp_path):
    """No half-offline mode: a replayed solve replays the sub-matrix call too."""
    with pytest.raises(SystemExit) as refusal:
        producer.main(arguments(tmp_path, delete=deleted_ids(), add=selected_handles()))
    assert refusal.value.code == 2


def test_record_multi_refuses_a_replayed_sub_matrix_call(tmp_path):
    with pytest.raises(SystemExit) as refusal:
        producer.main(arguments(tmp_path, delete=deleted_ids(), add=selected_handles(),
                                **{"multi-replay": tmp_path / "absent.json",
                                   "record-multi": tmp_path / "out.json"}))
    assert refusal.value.code == 2
