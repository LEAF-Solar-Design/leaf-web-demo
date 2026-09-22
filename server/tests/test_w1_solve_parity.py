"""Offline parity with the plugin's seven committed rooftop solve groups."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import leaf_cloud_client as cloud
from leaf_cloud_grants import CloudGrant
from solar_design_graph import (
    GraphValidationError, deserialize_graph, serialize_graph, validate_graph,
)
from solar_solve_request import build_stringer_request, plugin_sequences
from test_w1_design_graph import entity, graph  # noqa: F401
from test_w1_solve_commit import candidate, commit, no_network  # noqa: F401


GROUPS = json.loads(
    (SERVER / "tests/fixtures/w1_rooftop_unsplit_solve.json").read_text()
)["groups"]
SIZING_RECORDINGS = [
    (14, [14, 13, 1, 0]), (40, [14, 13, 1, 2]), (70, [14, 13, 5, 0]),
    (82, [14, 13, 4, 2]), (83, [14, 13, 5, 1]), (86, [13, 12, 2, 5]),
    (92, [14, 13, 1, 6]), (94, [14, 13, 3, 4]), (96, [14, 13, 5, 2]),
    (99, [13, 12, 3, 5]), (104, [14, 13, 0, 8]), (108, [14, 13, 4, 4]),
    (111, [14, 13, 7, 1]), (114, [13, 12, 6, 3]), (115, [13, 12, 7, 2]),
    (119, [14, 13, 2, 7]), (122, [14, 13, 5, 4]), (123, [14, 13, 6, 3]),
    (124, [14, 13, 7, 2]), (126, [14, 13, 9, 0]), (129, [13, 12, 9, 1]),
    (134, [14, 13, 4, 6]), (137, [14, 13, 7, 3]), (140, [14, 13, 10, 0]),
    (168, [14, 13, 12, 0]), (171, [14, 13, 2, 11]),
    (174, [14, 13, 5, 8]), (177, [14, 13, 8, 5]),
]


def test_plugin_sizing_rule_matches_recordings():
    for count, expected in SIZING_RECORDINGS:
        assert plugin_sequences(count, 14) == expected
    for group in GROUPS:
        assert plugin_sequences(len(group["panels"]), 14) == group["sequences"]
    for invalid in (True, False, 0, 901, 14.0):
        for count, maximum in ((invalid, 14), (14, invalid)):
            with pytest.raises(GraphValidationError, match="^INVALID_STRING_SIZING: "):
                plugin_sequences(count, maximum)


def rooftop_case(graph, group, monkeypatch):
    graph = copy.deepcopy(graph)
    frame = graph["frames"][0]
    positions = {handle: (r, c) for r, row in enumerate(group["matrix"])
                 for c, handle in enumerate(row) if handle is not None}
    panels, by_handle = [], {}
    for source in group["panels"]:
        handle = source["handle"]
        r, c = positions[handle]
        # The graph contract requires application IDs, not raw CAD handles.
        panel = entity("panel", int(handle, 16), frame_ref=frame["id"],
                       matrix_cell={"row": r, "col": c},
                       centre=copy.deepcopy(source["centre"]), angle=source["angle"],
                       assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = handle
        panels.append(panel)
        by_handle[handle] = panel
    matrix = []
    for row in group["matrix"]:
        cells = []
        for handle in row:
            panel = by_handle.get(handle)
            cells.append({
                "code": "panel" if panel else "empty",
                "panel_ref": panel["id"] if panel else None, "seq": None,
                "inverter_id": None, "string_input_number": None,
                "x": panel["centre"][0] if panel else 0.0,
                "y": panel["centre"][1] if panel else 0.0,
                "angle": panel["angle"] if panel else 0.0,
            })
        matrix.append(cells)
    frame.update(panel_refs=[p["id"] for p in panels], module_rows=len(matrix),
                 module_columns=len(matrix[0]), module_slots=len(matrix) * len(matrix[0]),
                 matrix=matrix, sequences=[], panel_assignments=[
                     {"panel_ref": p["id"], "string_ref": None, "seq": None,
                      "inverter_id": None, "string_input_number": None} for p in panels])
    graph.update(panels=panels, strings=[], inverters=[], routes=[], schedules=[])
    graph["electrical_zones"][0]["panel_refs"] = frame["panel_refs"][:]
    validate_graph(graph)
    request = build_stringer_request(
        graph, frame["id"], max_string_length=14, dwgname="rooftop_demo.dwg")
    assert request["grid"]["Sequences"] == group["sequences"]
    response = copy.deepcopy(group["response"])
    for row in response["data"]["final_grid"]["Rows"]:
        for cell in row["Panels"]:
            if cell["Code"] == 1:
                cell["Id"] = by_handle[cell["Id"]]["id"]
    # Remap only identity, retaining the recorded geometry, order and lengths.
    expected_grid = copy.deepcopy(response["data"]["final_grid"])
    for row in expected_grid["Rows"]:
        for cell in row["Panels"]:
            cell["Seq"] = 0
    assert cloud.StringerRequest.model_validate(request).wire_payload()["grid"] == expected_grid
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant("fixture-tenant", ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *args: cloud.canonical_bytes(response))
    return graph, request, response


@pytest.mark.parametrize("group", GROUPS, ids=lambda group: f"piece-{group['piece']}")
def test_studio_solve_reproduces_plugin_strings(graph, monkeypatch, group):
    case = rooftop_case(graph, group, monkeypatch)
    graph, _, _ = case
    result = commit.commit_solve(graph, {"expected_rev": graph["rev"]},
                                 candidate=candidate(case))
    reopened = deserialize_graph(serialize_graph(result))
    handles = {p["id"]: p["provenance"]["source_handle"] for p in reopened["panels"]}
    actual = [[handles[ref] for ref in string["ordered_panel_refs"]]
              for string in reopened["strings"]]
    expected = group["plugin_strings"]
    assert sorted(actual) == sorted(expected)
    assert len(actual) == len(expected)
    for committed, recorded in zip(sorted(actual), sorted(expected)):
        assert committed == recorded
    for string in reopened["strings"]:
        polarity = string["extra"]["polarity"]
        assert polarity["rule"] == "ordered-final-grid-first-negative-last-positive"
        assert polarity["negative_panel_ref"] == string["ordered_panel_refs"][0]
        assert polarity["positive_panel_ref"] == string["ordered_panel_refs"][-1]
    assert reopened["extra"]["solve_coverage"] == {
        "duplicate_panel_refs": [], "unassigned_panel_refs": []}


def cut_strings(refs, lengths):
    strings, offset = [], 0
    for length in lengths:
        strings.append(refs[offset:offset + length])
        offset += length
    assert offset == len(refs)
    return strings


def test_fixture_discriminates_path_from_final_grid():
    group = next(group for group in GROUPS if group["piece"] == 21)
    data = group["response"]["data"]
    info = data["best_result"]["info"]
    rows = data["final_grid"]["Rows"]
    path = [rows[r - 1]["Panels"][c - 1]["Id"] for r, c in info["visited_path"]]
    final = [ref for _, ref in sorted(
        (cell["Seq"], cell["Id"]) for row in rows for cell in row["Panels"]
        if cell["Code"] == 1)]
    assert sorted(cut_strings(path, info["sequence_length"])) != sorted(group["plugin_strings"])
    assert sorted(cut_strings(final, info["sequence_length"])) == sorted(group["plugin_strings"])
    assert sum(len(group["plugin_strings"]) for group in GROUPS) == 66


@pytest.mark.parametrize("defect", ["repeated", "gap"])
def test_final_grid_order_fails_closed(graph, monkeypatch, defect):
    case = rooftop_case(graph, GROUPS[0], monkeypatch)
    graph, _, response = case
    cells = sorted((cell for row in response["data"]["final_grid"]["Rows"]
                    for cell in row["Panels"] if cell["Code"] == 1),
                   key=lambda cell: cell["Seq"])
    if defect == "repeated":
        cells[1]["Seq"] = cells[0]["Seq"]
    else:
        cells[-1]["Seq"] += 1
    before = copy.deepcopy(graph)
    proposal = candidate(case)
    with pytest.raises(GraphValidationError, match="^INVALID_FINAL_GRID_ORDER: "):
        commit.commit_solve(graph, {"expected_rev": graph["rev"]}, candidate=proposal)
    assert graph == before
