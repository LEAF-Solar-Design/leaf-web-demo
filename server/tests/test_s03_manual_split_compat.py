"""Synthetic S03 manual geometry compatibility with the split admission path."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import leaf_cloud_client as cloud
import solar_project_solve as project_solve
import solar_sizing_client as sizing
import solar_solve_request as requests
from product_capability_availability import w1_graph_readiness
from solar_design_graph import validate_graph
from test_w1_design_graph import entity, graph  # noqa: F401


def manual_large_graph(base):
    value = deepcopy(base)
    frame = value["frames"][0]
    panels, matrix = [], []
    for row in range(22):
        cells = []
        for col in range(12):
            panel = entity("panel", len(panels) + 1, frame_ref=frame["id"],
                matrix_cell={"row": row, "col": col}, centre=[2.0 * col, -3.0 * row],
                angle=0.0, assignment={"string_ref": None, "seq": None})
            panels.append(panel)
            cells.append({"code": "panel", "panel_ref": panel["id"], "seq": None,
                "inverter_id": None, "string_input_number": None,
                "x": panel["centre"][0], "y": panel["centre"][1], "angle": 0.0})
        matrix.append(cells)
    frame.update(panel_refs=[p["id"] for p in panels], matrix=matrix,
        module_rows=22, module_columns=12, module_slots=264, module_power_watts=0,
        extra={"grouping_basis": "geometry-only", "module_power_status": "unknown",
               "sizing_evidence_mode": "manual-global"}, sequences=[],
        panel_assignments=[{"panel_ref": p["id"], "string_ref": None, "seq": None,
            "inverter_id": None, "string_input_number": None} for p in panels])
    value.update(panels=panels, strings=[], inverters=[], routes=[], schedules=[])
    value["electrical_zones"][0]["panel_refs"] = frame["panel_refs"][:]
    settings = value["settings"]
    settings.update(panels_in_sequence=14, voc_cold=sizing.manual_voltage(),
                    global_string_sizing_confirmed=True)
    settings["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return validate_graph(value)


def test_manual_unknown_power_split_admission(graph, monkeypatch):
    def no_io(*args, **kwargs):
        pytest.fail("split planning must stay offline")
    monkeypatch.setattr(cloud, "resolve_grant", no_io)
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", no_io)
    value = manual_large_graph(graph)
    before = deepcopy(value)
    frame = value["frames"][0]
    assert w1_graph_readiness(value)["solar-solve-proposal"]["input_ready"] is True
    assert requests.needs_split_solve(value, frame["id"], max_string_length=14)
    request = requests.build_stringer_request(value, frame["id"], max_string_length=14,
        dwgname="synthetic-manual-split", full_frame=True)
    plan = project_solve.plan_request(SimpleNamespace(graph=value,
        organization_id="synthetic-tenant"), {"grant_ref": "synthetic-no-access", "request": request},
        {"frame_ref": frame["id"], "expected_rev": value["rev"], "phase": "initial"})
    binding = plan["split_binding"]
    assert len(binding["pieces"]) > 1
    refs = [cell["Id"] for piece in binding["pieces"]
        for row in piece["request"]["grid"]["Rows"] for cell in row["Panels"] if cell["Code"] == 1]
    assert len(refs) == len(set(refs)) == 264
    assert set(refs) == set(frame["panel_refs"])
    assert plan["max_string_length"] == 14
    for piece in binding["pieces"]:
        parsed = cloud.PieceStringerRequest.model_validate(piece["request"])
        lengths, counts = parsed.grid.Sequences
        assert max(lengths) <= 14
        occupied = sum(cell.Code == 1 for row in parsed.grid.Rows for cell in row.Panels)
        assert sum(length * count for length, count in zip(lengths, counts)) == occupied
    assert value == before
    assert value["frames"][0]["module_power_watts"] == 0
    assert value["settings"]["extra"]["string_sizing"]["records"] == {}


def extra_panel_graph(base):
    value = manual_large_graph(base)
    frame = value["frames"][0]
    row = frame["module_rows"]
    panel = entity("panel", 265, frame_ref=frame["id"], matrix_cell={"row": row, "col": 0},
        centre=[0.0, -3.0 * row], angle=0.0, assignment={"string_ref": None, "seq": None})
    value["panels"].append(panel)
    frame["panel_refs"].append(panel["id"])
    frame["panel_assignments"].append({"panel_ref": panel["id"], "string_ref": None,
        "seq": None, "inverter_id": None, "string_input_number": None})
    frame["matrix"].append([{"code": "panel" if col == 0 else "empty",
        "panel_ref": panel["id"] if col == 0 else None, "seq": None,
        "inverter_id": None, "string_input_number": None,
        "x": 0.0, "y": panel["centre"][1] if col == 0 else 0.0, "angle": 0.0}
        for col in range(frame["module_columns"])])
    frame["module_rows"] += 1
    frame["module_slots"] += frame["module_columns"]
    value["electrical_zones"][0]["panel_refs"].append(panel["id"])
    value["settings"]["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return validate_graph(value)


def synthetic_piece(piece, tenant, child_job, monkeypatch, *, partial):
    from leaf_cloud_grants import CloudGrant

    request = cloud.PieceStringerRequest.model_validate(piece["request"])
    final = request.wire_payload()["grid"]
    path = []
    for row, cells in enumerate(final["Rows"]):
        for col, cell in enumerate(cells["Panels"]):
            if cell["Code"] == 1:
                path.append([row + 1, col + 1])
                cell["Seq"] = len(path)
    data = {"status": "completed", "best_result": {"info": {"distance_total": 0.0}},
        "final_grid": final, "model_used": "synthetic-offline", "gumbel_scale_used": 0.0,
        "distance_total": 0.0, "total_valid_solutions": 1}
    if partial:
        data["message"] = "synthetic partial beam"
    else:
        lengths, counts = request.grid.Sequences
        sizes = [length for length, count in zip(lengths, counts) for _ in range(count)]
        info = dict(steps_taken=len(path), sequence_length=sizes, num_panels=len(path),
            visited_path=path, steps_taken_in_string=0, horizontal_movements=0,
            vertical_movements=0, invalid_vertical_movements=0, distance_total=0.0,
            vertical_sequential=0, diagonal_movements=0, allow_one_gap_hops=False,
            max_gap_hop=0, gap_hop_movements=0, vertical_temp=-1, remaining_string_length=0,
            sequence_length_index=0, str_length_state_index=0, terminated=True,
            last_agent_x=path[-1][0], last_agent_y=path[-1][1])
        data["best_result"] = dict(last_action=0, terminated=True, info=info, beam_idx=0,
            grid_id=child_job, model_id="synthetic-offline", gumbel_scale=0.0,
            string_start_split_count=0, model="synthetic-offline")
        data.update(gumbel_summary={}, first_pass_best_distance=0.0, improvement=None,
                    second_pass_triggered=False)
    raw = cloud.canonical_bytes({"status": "completed", "job_id": child_job, "data": data})
    # Exercise the real receipt producer with a synthetic transport, never a provider.
    monkeypatch.setattr(cloud, "resolve_grant", lambda ref, owner:
        CloudGrant(owner, "synthetic-not-a-credential"))
    monkeypatch.setattr(cloud, "post_stringer", lambda sent, grant: raw)
    return cloud.piece_proposal({"grant_ref": "synthetic-offline", "request": piece["request"]},
        tenant, child_job)


@pytest.mark.parametrize("all_partial", [False, True])
def test_manual_unknown_power_native_restitch_and_cut(graph, monkeypatch, all_partial):
    import solar_solve_results as solve
    from solar_design_graph import deserialize_graph, serialize_graph

    def no_network(*args, **kwargs):
        pytest.fail("offline synthetic fixture attempted a network call")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", no_network)
    value = extra_panel_graph(graph)
    before = deepcopy(value)
    frame = value["frames"][0]
    pieces = requests.build_split_requests(value, frame["id"], max_string_length=14,
        dwgname="synthetic-offline-restitch")
    assert len(pieces) > 1
    binding = solve.bind_split_request(value, pieces, expected_rev=value["rev"],
        frame_ref=frame["id"], tenant_id="synthetic-tenant", job_id="synthetic-parent",
        split_state={"jogs": 1, "depth": 10})
    proposals = [synthetic_piece(piece, binding["tenant_id"],
        solve.piece_job_id(binding["job_id"], index), monkeypatch,
        partial=all_partial or index > 0) for index, piece in enumerate(pieces)]
    candidate = solve.complete_split_search(value, binding, proposals)
    assert candidate["accepted"] is False
    solved = solve.accept_split_candidate(value, candidate, expected_rev=value["rev"])
    if all_partial:
        prefix, carried = [], 6  # Native fallback when no piece reports lengths.
    else:
        lengths, counts = cloud.PieceStringerRequest.model_validate(pieces[0]["request"]).grid.Sequences
        prefix = [length for length, count in zip(lengths, counts) for _ in range(count)]
        carried = prefix[-1]
    count, remainder = divmod(265 - sum(prefix), carried)
    assert remainder > 0  # This synthetic shape exercises the final partial cut.
    expected_sizes = prefix + [carried] * count + [remainder]
    assert [string["module_count"] for string in solved["strings"]] == expected_sizes
    refs = [ref for string in solved["strings"] for ref in string["ordered_panel_refs"]]
    assert len(refs) == len(set(refs)) == 265
    assert set(refs) == set(frame["panel_refs"])
    assert all(1 <= string["module_count"] <= 14 for string in solved["strings"])
    for string in solved["strings"]:
        assert string["extra"]["polarity"]["negative_panel_ref"] == string["ordered_panel_refs"][0]
        assert string["extra"]["polarity"]["positive_panel_ref"] == string["ordered_panel_refs"][-1]
        points = {panel["id"]: panel["centre"] for panel in solved["panels"]}
        assert string["route"] == [points[ref] for ref in string["ordered_panel_refs"]]
    assert solved["frames"][0]["module_power_watts"] == 0
    assert solved["frames"][0]["extra"]["module_power_status"] == "unknown"
    assert solved["settings"]["extra"]["string_sizing"]["records"] == {}
    assert len(solved["frames"][0]["extra"]["solve"]["split"]["pieces"]) == len(pieces)
    assert not any(solve.coverage(solved).values())
    assert deserialize_graph(serialize_graph(solved)) == solved
    assert value == before
    stale = deepcopy(value)
    stale["rev"] += 1
    stale["parent_rev"] = value["rev"]
    with pytest.raises(solve.GraphValidationError, match="STALE_SOLVE_RESULT"):
        solve.accept_split_candidate(stale, candidate, expected_rev=stale["rev"])


def test_native_membership_correction_refreshes_derived_geometry(graph, monkeypatch):
    import math
    import solar_solve_results as solve

    value = extra_panel_graph(graph)
    frame = value["frames"][0]
    pieces = requests.build_split_requests(value, frame["id"], max_string_length=14,
        dwgname="synthetic-correction")
    binding = solve.bind_split_request(value, pieces, expected_rev=value["rev"],
        frame_ref=frame["id"], tenant_id="synthetic-tenant", job_id="synthetic-correction",
        split_state={"jogs": 1, "depth": 10})
    proposals = [synthetic_piece(piece, binding["tenant_id"],
        solve.piece_job_id(binding["job_id"], index), monkeypatch, partial=True)
        for index, piece in enumerate(pieces)]
    before = solve.accept_split_candidate(value, solve.complete_split_search(value, binding, proposals),
        expected_rev=value["rev"])
    saved = deepcopy(before)
    first, second = before["strings"][:2]
    updates = [first["ordered_panel_refs"][:-1],
        first["ordered_panel_refs"][-1:] + second["ordered_panel_refs"]]
    params = {"expected_rev": before["rev"], "memberships": [
        {"string_ref": string["id"], "ordered_panel_refs": refs}
        for string, refs in zip((first, second), updates)]}
    corrected = solve.correct_graph(before, params)
    points = {panel["id"]: panel["centre"] for panel in corrected["panels"]}
    for string in corrected["strings"][:2]:
        refs = string["ordered_panel_refs"]
        route = [points[ref] for ref in refs]
        assert string["route"] == route
        assert string["length_ft"] == pytest.approx(sum(math.dist(a, b)
            for a, b in zip(route, route[1:])) / .3048)
        polarity = string["extra"]["polarity"]
        assert polarity["negative_panel_ref"] == refs[0]
        assert polarity["positive_panel_ref"] == refs[-1]
        assert polarity["source_rev"] == before["rev"]
        assert "response_sha256" not in polarity
        assert string["validity"]["state"] == "stale"
    for old, current in zip(before["strings"][2:], corrected["strings"][2:]):
        assert current["ordered_panel_refs"] == old["ordered_panel_refs"]
        assert current["route"] == old["route"]
        assert current["extra"]["polarity"] == old["extra"]["polarity"]
    assert before == saved
    assert corrected["frames"][0]["module_power_watts"] == 0
    assert not any(solve.coverage(corrected).values())
    with pytest.raises(solve.GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        solve.require_current_export(corrected)
    restore = {"expected_rev": corrected["rev"], "memberships": [
        {"string_ref": string["id"], "ordered_panel_refs": string["ordered_panel_refs"]}
        for string in before["strings"][:2]]}
    restored = solve.correct_graph(corrected, restore)
    for old, current in zip(before["strings"], restored["strings"]):
        assert current["ordered_panel_refs"] == old["ordered_panel_refs"]
        assert current["route"] == old["route"]
        assert current["length_ft"] == old["length_ft"]
        assert current["extra"]["polarity"]["negative_panel_ref"] == old["ordered_panel_refs"][0]
        assert current["extra"]["polarity"]["positive_panel_ref"] == old["ordered_panel_refs"][-1]
    assert not any(solve.coverage(restored).values())


def test_legacy_manual_route_stays_stale_after_correction(graph):
    import solar_solve_results as solve

    before = deepcopy(graph)
    string = graph["strings"][0]
    corrected = solve.correct_graph(graph, {"expected_rev": graph["rev"], "memberships": [
        {"string_ref": string["id"], "ordered_panel_refs": string["ordered_panel_refs"][::-1]}]})
    assert corrected["strings"][0]["route"] == string["route"]
    assert corrected["strings"][0]["length_ft"] == string["length_ft"]
    assert corrected["strings"][0]["validity"]["state"] == "stale"
    assert graph == before
