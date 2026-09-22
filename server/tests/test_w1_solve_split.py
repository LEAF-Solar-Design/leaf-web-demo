"""Studio solves the plugin's split rooftop groups: pieces, one call each, restitch, cut, commit."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import leaf_cloud_client as cloud
import solar_solve_request as solve_request
import solar_solve_results as solve
from leaf_cloud_grants import CloudError, CloudGrant
from solar_design_graph import (
    GraphValidationError, deserialize_graph, serialize_graph, validate_graph,
)
from test_w1_design_graph import entity, graph  # noqa: F401


FIXTURES = SERVER / "tests/fixtures"
RECORD = json.loads((FIXTURES / "w1_rooftop_split_groups.json").read_text(encoding="utf-8"))
GROUPS = RECORD["groups"]
MAX_STRING_LENGTH = RECORD["max_string_length"]
DWG = "rooftop-demo"
TENANT = "fixture-tenant"
JOB = "split-job"
_BUILT = {}


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


commit = builtin("solar_commit_solve")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline network forbidden")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", refuse)


def group_graph(base, group):
    """A graph whose one frame is the recorded group, cell for cell."""
    result = copy.deepcopy(base)
    frame = result["frames"][0]
    panels, handles, matrix = [], {}, []
    for r, row in enumerate(group["group_grid"]):
        cells = []
        for c, handle in enumerate(row):
            ref = None
            x, y = 1.1 * c, -2.1 * r
            if handle is not None:
                panel = entity("panel", len(panels) + 1, frame_ref=frame["id"],
                               matrix_cell={"row": r, "col": c}, centre=[x, y], angle=0.0,
                               assignment={"string_ref": None, "seq": None})
                panels.append(panel)
                ref = panel["id"]
                handles[ref] = handle
            cells.append({"code": "panel" if ref else "empty", "panel_ref": ref, "seq": None,
                          "inverter_id": None, "string_input_number": None,
                          "x": x, "y": y, "angle": 0.0})
        matrix.append(cells)
    width = len(matrix[0])
    frame.update(panel_refs=[p["id"] for p in panels], module_rows=len(matrix),
                 module_columns=width, module_slots=len(matrix) * width, matrix=matrix,
                 sequences=[], panel_assignments=[
                     {"panel_ref": p["id"], "string_ref": None, "seq": None,
                      "inverter_id": None, "string_input_number": None} for p in panels])
    result.update(panels=panels, strings=[], inverters=[], routes=[], schedules=[])
    result["electrical_zones"][0]["panel_refs"] = frame["panel_refs"][:]
    return validate_graph(result), handles


def built(base, group):
    """Graph, handle map and split piece requests, computed once per group."""
    key = group["group_block"]
    if key not in _BUILT:
        solved, handles = group_graph(base, group)
        pieces = solve_request.build_split_requests(
            solved, solved["frames"][0]["id"], max_string_length=MAX_STRING_LENGTH,
            dwgname=DWG)
        _BUILT[key] = (solved, handles, pieces)
    return copy.deepcopy(_BUILT[key])


def bind(solved, pieces, split_state=None):
    return solve.bind_split_request(
        solved, pieces, expected_rev=solved["rev"], frame_ref=solved["frames"][0]["id"],
        tenant_id=TENANT, job_id=JOB, split_state=split_state or {"jogs": 1, "depth": 10})


def layout(piece, handles):
    grid = piece["request"]["grid"]
    return {"cells": [[handles[wire["Id"]] if wire["Code"] == 1 else None
                       for wire in row["Panels"]] for row in grid["Rows"]],
            "sequences": grid["Sequences"], "row_indices": piece["row_indices"]}


def replay_recorded_piece_without_echo_check(result, params, tenant_id, job_id):
    """TEST ONLY stand-in for leaf_cloud_client.piece_proposal_provenance.

    The recorded answers were made for the plugin's own requests (drawing
    handles, no geometry, reduced fields), so the strict response model and the
    final_grid echo of Studio's request cannot hold for them. Everything else
    about the envelope (job, tenant, request hash) is still checked here.
    """
    request = cloud.validate_piece_params(params).request
    request_hash = hashlib.sha256(cloud.canonical_bytes(request.wire_payload())).hexdigest()
    if (result["job_id"] != job_id or result["tenant_id"] != tenant_id
            or result["request_sha256"] != request_hash):
        raise ValueError("replayed envelope does not bind")
    info = result["proposal"]["data"]["best_result"]["info"]
    return {"execution_mode": "leaf_cloud_service", "solver": result["solver"],
            "request_sha256": request_hash, "response_sha256": result["response_sha256"],
            "partial_beam": "sequence_length" not in info}


def recorded_envelopes(binding, group, handles):
    """The recorded piece answers, replayed by piece order, with handles as panel ids."""
    to_ref = {handle.upper(): ref for ref, handle in handles.items()}
    envelopes = []
    for index, (piece, response) in enumerate(zip(binding["pieces"], group["responses"])):
        answer = copy.deepcopy(response)
        for row in answer["data"]["final_grid"]["Rows"]:
            for cell in row["Panels"]:
                if cell["Code"] == 1:
                    cell["Id"] = to_ref[cell["Id"].upper()]
        request = cloud.PieceStringerRequest.model_validate(piece["request"])
        envelopes.append({
            "schema_version": "leaf.solar-proposal.v1",
            "job_id": solve.piece_job_id(JOB, index), "tenant_id": TENANT,
            "drawing_changed": False,
            "request_sha256": hashlib.sha256(
                cloud.canonical_bytes(request.wire_payload())).hexdigest(),
            "response_sha256": hashlib.sha256(cloud.canonical_bytes(answer)).hexdigest(),
            "solver": {"endpoint": cloud.SOLVER_URL, "adapter_version": "1.0.0"},
            "proposal": answer, "visited_path": [],
        })
    return envelopes


@pytest.mark.parametrize("group", GROUPS, ids=lambda g: f"{g['group_block']}-{g['panel_count']}")
def test_split_requests_reproduce_recorded_pieces(graph, group):
    solved, handles, pieces = built(graph, group)
    frame_ref = solved["frames"][0]["id"]
    assert solve_request.needs_split_solve(solved, frame_ref, max_string_length=MAX_STRING_LENGTH)
    assert [layout(piece, handles) for piece in pieces] == group["pieces"]
    panels = {p["id"]: p for p in solved["panels"]}
    for piece in pieces:
        grid = piece["request"]["grid"]
        assert grid["Modify"] == [] and grid["Dwgname"] == DWG
        for row in grid["Rows"]:
            for wire in row["Panels"]:
                if wire["Code"] == 1:
                    panel = panels[wire["Id"]]
                    assert [wire["X"], wire["Y"], wire["Angle"]] == panel["centre"] + [panel["angle"]]
                else:
                    assert wire["Id"] == ""
    binding = bind(solved, pieces)
    assert [p["row_indices"] for p in binding["pieces"]] == [p["row_indices"] for p in group["pieces"]]


@pytest.mark.parametrize("group", GROUPS, ids=lambda g: f"{g['group_block']}-{g['panel_count']}")
def test_replayed_pieces_commit_the_plugin_strings(graph, group, monkeypatch):
    monkeypatch.setattr(solve, "piece_proposal_provenance", replay_recorded_piece_without_echo_check)
    solved, handles, pieces = built(graph, group)
    before = copy.deepcopy(solved)
    binding = bind(solved, pieces)
    candidate = solve.complete_split_search(solved, binding, recorded_envelopes(binding, group, handles))
    assert candidate["accepted"] is False
    result = commit.commit_solve(solved, {"expected_rev": solved["rev"]}, candidate=candidate)
    assert solved == before and result["rev"] == solved["rev"] + 1
    reopened = deserialize_graph(serialize_graph(result))
    strings = [[handles[ref].upper() for ref in s["ordered_panel_refs"]] for s in reopened["strings"]]
    assert len(strings) == len(group["plugin_strings"])
    assert sorted(strings) == sorted(group["plugin_strings"])
    assert sum(map(len, strings)) == group["panel_count"]
    assert reopened["extra"]["solve_coverage"] == {
        "duplicate_panel_refs": [], "unassigned_panel_refs": []}
    by_id = {p["id"]: p for p in reopened["panels"]}
    for string in reopened["strings"]:
        refs = string["ordered_panel_refs"]
        assert string["module_count"] == len(refs)
        assert string["extra"]["polarity"]["negative_panel_ref"] == refs[0]
        assert string["extra"]["polarity"]["positive_panel_ref"] == refs[-1]
        for seq, ref in enumerate(refs):
            assert by_id[ref]["assignment"] == {"string_ref": string["id"], "seq": seq}
    record = reopened["frames"][0]["extra"]["solve"]
    assert record["initial_complete"] is True and record["job_id"] == JOB
    assert [p["partial_beam"] for p in record["split"]["pieces"]] == [
        "sequence_length" not in r["data"]["best_result"]["info"] for r in group["responses"]]
    assert [p["row_indices"] for p in record["split"]["pieces"]] == [
        p["row_indices"] for p in group["pieces"]]


def test_partial_beam_group_is_recorded():
    assert sum("sequence_length" not in r["data"]["best_result"]["info"]
               for g in GROUPS for r in g["responses"]) == 1


def real_piece():
    request = json.loads((FIXTURES / "w1_stringer_request_4x6.json").read_text())
    response = json.loads((FIXTURES / "w1_stringer_response_real.json").read_text())
    return {"grant_ref": "fixture-grant", "request": request}, response


def as_partial_beam(response):
    """Reduce a full answer to the fallback shape the service sends (measured live 2026-09-22):
    best_result carries only info, and data drops its second-pass fields for a message."""
    partial = copy.deepcopy(response)
    data = partial["data"]
    for field in ("gumbel_summary", "first_pass_best_distance", "improvement", "second_pass_triggered"):
        data.pop(field, None)
    data["best_result"] = {"info": {"distance_total": 0.0}}
    data["message"] = "Used engine finalizer result (partial beam)"
    return partial


def test_client_accepts_the_partial_beam_only_for_pieces(monkeypatch):
    params, response = real_piece()
    partial = as_partial_beam(response)
    answers = {"raw": cloud.canonical_bytes(partial)}
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant(TENANT, ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *args: answers["raw"])
    with pytest.raises(CloudError):
        cloud.proposal(params, TENANT, "job")
    result = cloud.piece_proposal(params, TENANT, "job")
    assert result["visited_path"] == []
    proof = cloud.piece_proposal_provenance(result, params, TENANT, "job")
    assert proof["partial_beam"] is True
    with pytest.raises(ValueError):
        cloud.piece_proposal_provenance(result, params, TENANT, "other-job")
    answers["raw"] = cloud.canonical_bytes(response)
    full = cloud.piece_proposal(params, TENANT, "job")
    assert full["visited_path"] == cloud.proposal(params, TENANT, "job")["visited_path"]
    assert cloud.piece_proposal_provenance(full, params, TENANT, "job")["partial_beam"] is False


def test_client_keeps_echo_and_refuses_plugin_failed_pieces(monkeypatch):
    params, response = real_piece()
    partial = as_partial_beam(response)
    answers = {}
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant(TENANT, ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *args: answers["raw"])
    moved = copy.deepcopy(partial)
    moved["data"]["final_grid"]["Rows"][0]["Panels"][0]["X"] = 99.0
    answers["raw"] = cloud.canonical_bytes(moved)
    with pytest.raises(CloudError) as refused:
        cloud.piece_proposal(params, TENANT, "job")
    assert refused.value.classification == "cloud_response_invalid"
    for change in ({"total_valid_solutions": 0}, {"message": "No valid solutions found"}):
        failed = copy.deepcopy(partial)
        failed["data"].update(change)
        answers["raw"] = cloud.canonical_bytes(failed)
        with pytest.raises(CloudError) as refused:
            cloud.piece_proposal(params, TENANT, "job")
        assert refused.value.classification == "cloud_piece_failed"


LIVE = json.loads((FIXTURES / "w1_piece_live_responses.json").read_text(encoding="utf-8"))
PARTIAL_BEAM_MESSAGE = "Used engine finalizer result (partial beam)"


def live_piece(kind, monkeypatch, response=None):
    """One live-run piece call (request and answer) served from the fixture, no network."""
    raw = cloud.canonical_bytes(LIVE[kind]["response"] if response is None else response)
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant(TENANT, ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *args: raw)
    return {"grant_ref": "fixture-grant", "request": LIVE[kind]["request"]}


def test_live_fixture_is_the_measured_pair():
    assert LIVE["source"].startswith("live stringer, 2026-09-22")
    assert list(LIVE["partial"]["response"]["data"]["best_result"]) == ["info"]
    assert LIVE["partial"]["response"]["data"]["best_result"]["info"] == {"distance_total": 0}
    assert LIVE["partial"]["response"]["data"]["message"] == PARTIAL_BEAM_MESSAGE
    assert len(LIVE["partial"]["request"]["grid"]["Rows"]) == 9
    assert set(cloud.BestResult.model_fields) <= set(LIVE["full"]["response"]["data"]["best_result"])
    assert "message" not in LIVE["full"]["response"]["data"]


def test_live_partial_beam_piece_is_accepted_with_no_path(monkeypatch):
    params = live_piece("partial", monkeypatch)
    result = cloud.piece_proposal(params, TENANT, "job")
    assert result["visited_path"] == []
    assert result["proposal"]["data"]["best_result"] == {"info": {"distance_total": 0.0}}
    assert result["proposal"]["data"]["message"] == PARTIAL_BEAM_MESSAGE
    assert result["response_sha256"] == hashlib.sha256(
        cloud.canonical_bytes(LIVE["partial"]["response"])).hexdigest()
    proof = cloud.piece_proposal_provenance(result, params, TENANT, "job")
    assert proof["partial_beam"] is True
    with pytest.raises(ValueError):
        cloud.piece_proposal_provenance(result, params, TENANT, "other-job")


def test_live_full_piece_keeps_every_whole_frame_check(monkeypatch):
    params = live_piece("full", monkeypatch)
    result = cloud.piece_proposal(params, TENANT, "job")
    panels = sum(p["Code"] == 1 for row in LIVE["full"]["request"]["grid"]["Rows"] for p in row["Panels"])
    assert len(result["visited_path"]) == panels == 111
    assert len({tuple(cell) for cell in result["visited_path"]}) == panels
    assert result["proposal"]["data"]["message"] is None
    proof = cloud.piece_proposal_provenance(result, params, TENANT, "job")
    assert proof["partial_beam"] is False
    assert result["visited_path"] == cloud.proposal(params, TENANT, "job")["visited_path"]


def test_live_partial_beam_is_refused_for_a_whole_frame(monkeypatch):
    params = live_piece("partial", monkeypatch)
    with pytest.raises(CloudError) as refused:
        cloud.proposal(params, TENANT, "job")
    assert refused.value.classification == "cloud_response_invalid"
    piece = cloud.piece_proposal(params, TENANT, "job")
    with pytest.raises(ValueError):
        cloud.proposal_provenance(piece, params, TENANT, "job")


@pytest.mark.parametrize("best_result", [
    {}, {"distance_total": 0}, {"info": {}}, {"info": {"distance_total": 0, "steps_taken": 1}},
], ids=["empty", "no-info", "empty-info", "hybrid-info"])
def test_best_result_with_neither_shape_is_refused(monkeypatch, best_result):
    body = copy.deepcopy(LIVE["partial"]["response"])
    body["data"]["best_result"] = best_result
    params = live_piece("partial", monkeypatch, body)
    with pytest.raises(CloudError) as refused:
        cloud.piece_proposal(params, TENANT, "job")
    assert refused.value.classification == "cloud_response_invalid"


@pytest.mark.parametrize("field", sorted(cloud.BestResult.model_fields))
def test_full_answer_missing_a_required_field_is_refused(monkeypatch, field):
    body = copy.deepcopy(LIVE["full"]["response"])
    del body["data"]["best_result"][field]
    params = live_piece("full", monkeypatch, body)
    with pytest.raises(CloudError) as refused:
        cloud.piece_proposal(params, TENANT, "job")
    assert refused.value.classification == "cloud_response_invalid"


def test_piece_request_keeps_raw_rows_but_the_wire_grid_is_capped(graph):
    _, _, pieces = built(graph, GROUPS[0])
    request = pieces[0]["request"]
    assert len(request["grid"]["Rows"]) == 31
    cloud.PieceStringerRequest.model_validate(request)
    with pytest.raises(ValueError):
        cloud.StringerRequest.model_validate(request)
    column = {"grid": {"Dwgname": DWG, "Sequences": [11, 10, 1, 2], "Modify": [], "Rows": [
        {"Panels": [{"Code": 1, "Id": f"p{r}", "Seq": 0, "InverterId": -1,
                     "StringInputNumber": 0, "X": 0.0, "Y": float(r), "Angle": 0.0}]}
        for r in range(31)]}}
    with pytest.raises(ValueError):
        cloud.PieceStringerRequest.model_validate(column)


def test_binding_refuses_misplaced_duplicate_or_missing_piece_cells(graph):
    solved, _, pieces = built(graph, GROUPS[3])
    bind(solved, pieces)
    moved = copy.deepcopy(pieces)
    wires = [w for row in moved[0]["request"]["grid"]["Rows"] for w in row["Panels"] if w["Code"] == 1]
    wires[0]["Id"], wires[1]["Id"] = wires[1]["Id"], wires[0]["Id"]
    shifted = copy.deepcopy(pieces)
    shifted[0]["row_indices"] = shifted[0]["row_indices"][1:] + shifted[0]["row_indices"][:1]
    for defect in (moved, shifted, pieces[:-1], pieces + pieces[:1], []):
        with pytest.raises(GraphValidationError, match="SOLVE_GRID_MISMATCH"):
            bind(solved, defect)
    with pytest.raises(GraphValidationError, match="INVALID_SOLVE_BINDING"):
        bind(solved, pieces, {"jogs": 3, "depth": 10})


def test_retry_loop_escalates_jogs_then_depth_then_gives_up(graph, monkeypatch):
    solved, _, _ = built(graph, GROUPS[3])
    seen, calls = [], []
    real = solve_request.split_group

    def spy(grid, *, depth=10, jogs=1):
        seen.append((jogs, depth))
        return real(grid, depth=depth, jogs=jogs)

    def fail(index, piece):
        calls.append(index)
        return None

    monkeypatch.setattr(solve_request, "split_group", spy)
    with pytest.raises(GraphValidationError, match="SPLIT_SOLVE_EXHAUSTED"):
        solve_request.solve_split_frame(solved, solved["frames"][0]["id"],
                                        max_string_length=MAX_STRING_LENGTH, dwgname=DWG,
                                        solve_piece=fail)
    assert seen == [(1, 10), (2, 10), (2, 1), (2, 0)]
    assert calls.count(0) == 4


def test_retry_loop_returns_the_first_round_where_every_piece_succeeds(graph):
    solved, _, pieces = built(graph, GROUPS[3])
    rounds = []
    good = {"data": {"final_grid": {}, "best_result": {}}}

    def flaky(index, piece):
        if index == 0:
            rounds.append(piece)
        if len(rounds) == 1 and index == 1:
            return {"proposal": {"data": dict(good["data"], total_valid_solutions=0)}}
        return {"proposal": good}

    out = solve_request.solve_split_frame(solved, solved["frames"][0]["id"],
                                          max_string_length=MAX_STRING_LENGTH, dwgname=DWG,
                                          solve_piece=flaky)
    assert len(rounds) == 2
    assert out["split_state"] == {"jogs": 2, "depth": 10}
    assert len(out["proposals"]) == len(out["pieces"])
    binding = bind(solved, out["pieces"], out["split_state"])
    assert binding["split_state"] == {"jogs": 2, "depth": 10}


def test_commit_refuses_a_piece_the_plugin_would_retry(graph, monkeypatch):
    monkeypatch.setattr(solve, "piece_proposal_provenance", replay_recorded_piece_without_echo_check)
    group = GROUPS[3]
    solved, handles, pieces = built(graph, group)
    binding = bind(solved, pieces)
    envelopes = recorded_envelopes(binding, group, handles)
    envelopes[1]["proposal"]["data"]["total_valid_solutions"] = 0
    candidate = solve.complete_split_search(solved, binding, envelopes)
    with pytest.raises(GraphValidationError, match="SPLIT_PIECE_FAILED"):
        commit.commit_solve(solved, {"expected_rev": solved["rev"]}, candidate=candidate)


def test_split_candidate_is_refused_after_the_graph_moves(graph, monkeypatch):
    monkeypatch.setattr(solve, "piece_proposal_provenance", replay_recorded_piece_without_echo_check)
    group = GROUPS[3]
    solved, handles, pieces = built(graph, group)
    binding = bind(solved, pieces)
    candidate = solve.complete_split_search(solved, binding, recorded_envelopes(binding, group, handles))
    moved = copy.deepcopy(solved)
    moved["source_hash"] = "b" * 64
    with pytest.raises(GraphValidationError):
        commit.commit_solve(moved, {"expected_rev": moved["rev"]}, candidate=candidate)


def test_unsplit_frame_keeps_the_single_request_path(graph):
    assert not solve_request.needs_split_solve(
        graph, graph["frames"][0]["id"], max_string_length=MAX_STRING_LENGTH)
