"""Exact Branch2025 piece requests for Studio's four recorded rooftop split groups."""
import copy
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from solar_grid_split import (
    allocate_single_string, bisect_vertically, calculate_complexity,
    compute_sequences_for_panel_count, count_non_empty_columns,
    count_non_empty_rows, count_panels, ensure_pieces_within_column_limit,
    find_size_partitions, force_horizontal_split, force_vertical_split,
    generate_horizontal_cuts, get_max_pieces_for_panel_count, needs_splitting,
    should_use_carving, split_at_column, split_group, split_json_vertical,
)


GROUPS = json.loads(
    (SERVER / "tests/fixtures/w1_rooftop_split_groups.json").read_text()
)["groups"]


def grid_from_cells(cells, sequences):
    return {"Sequences": list(sequences), "Rows": [
        {"Panels": [{"Code": int(handle is not None), "Id": handle or "", "Seq": 0}
                    for handle in row]} for row in cells]}


def rectangle(rows, columns, sequences=None):
    return grid_from_cells([[f"{r}:{c}" for c in range(columns)] for r in range(rows)],
                           sequences or compute_sequences_for_panel_count(rows * columns, 14))


def recorded_shape(piece):
    return {
        "cells": [[cell["Id"] if cell["Code"] == 1 else None
                   for cell in row["Panels"]] for row in piece["Rows"]],
        "sequences": piece["Sequences"],
        "row_indices": piece.get("RowIndices"),
    }


@pytest.mark.parametrize("group", GROUPS, ids=lambda g: f"{g['group_block']}-{g['panel_count']}")
def test_recorded_rooftop_pieces_exactly(group):
    grid = grid_from_cells(group["group_grid"], group["group_sequences"])
    before = copy.deepcopy(grid)
    pieces = split_group(grid)
    assert len(pieces) == len(group["pieces"])
    assert [recorded_shape(piece) for piece in pieces] == group["pieces"]
    assert grid == before
    assert sum(count_panels(piece) for piece in pieces) == group["panel_count"]
    for piece in pieces:
        for row in piece["Rows"]:
            for cell in row["Panels"]:
                assert cell["Seq"] == 0
                if cell["Code"] == 0:
                    assert cell == {"Code": 0, "Id": "", "Seq": 0}


def test_recording_covers_all_sixteen_pieces():
    assert len(GROUPS) == 4
    assert sum(len(group["pieces"]) for group in GROUPS) == 16


@pytest.mark.parametrize("count,preferred,expected", [
    (48, 15, [12, 11, 4, 0]),
    (29, 15, [15, 14, 1, 1]),
    (31, 15, [11, 10, 1, 2]),
    (0, 14, [14, 13, 0, 0]),
    (-1, 0, [15, 14, 0, 0]),
    (1, 14, [1, 1, 1, 0]),
    (14, 0, [14, 13, 1, 0]),
    (3, 1, [1, 1, 3, 0]),
])
def test_compute_sequences_for_panel_count(count, preferred, expected):
    assert compute_sequences_for_panel_count(count, preferred) == expected


@pytest.mark.parametrize("count,expected", [
    (0, 1), (99, 1), (100, 2), (199, 2),
    (200, 4), (399, 4), (400, 8), (556, 8),
])
def test_get_max_pieces_for_panel_count(count, expected):
    assert get_max_pieces_for_panel_count(count) == expected


def test_complexity_counts_internal_edges_and_ragged_padding():
    solid = rectangle(3, 3)
    assert calculate_complexity(solid) == 0.0
    solid["Rows"][1]["Panels"][1] = {"Code": 0, "Id": "", "Seq": 0}
    assert calculate_complexity(solid) == 0.5
    ragged = grid_from_cells([["a", "b"], ["c"]], [3, 2, 1, 0])
    assert calculate_complexity(ragged) == pytest.approx(2 / 3)
    assert calculate_complexity({"Rows": [], "Sequences": []}) == 0.0


def test_carving_override_is_strictly_above_140_panels():
    assert should_use_carving(rectangle(10, 14)) is True
    assert should_use_carving(rectangle(10, 15)) is False


def test_carving_override_does_not_hide_high_complexity():
    grid = rectangle(15, 15)
    for r in range(1, 15, 2):
        for c in range(1, 15, 2):
            grid["Rows"][r]["Panels"][c] = {"Code": 0, "Id": "", "Seq": 0}
    assert count_panels(grid) > 140
    assert calculate_complexity(grid) > 0.35
    assert should_use_carving(grid) is True


def test_find_size_partitions_preserves_dfs_ties_after_balance_sort():
    assert find_size_partitions([6, 4, 3, 3, 2, 2], 10, 10) == [
        ([6, 2, 2], [4, 3, 3]),
        ([4, 3, 3], [6, 2, 2]),
        ([6, 4], [3, 3, 2, 2]),
        ([3, 3, 2, 2], [6, 4]),
    ]
    assert find_size_partitions([14, 14], 13, 15) == []


def test_unsplit_grid_is_returned_unchanged():
    grid = rectangle(7, 10)
    grid["RowIndices"] = list(range(20, 27))
    before = copy.deepcopy(grid)
    assert split_group(grid) == [before]
    assert split_group(grid)[0] is grid


def test_needs_splitting_uses_raw_dimensions_but_safety_uses_occupied():
    grid = grid_from_cells([["a"]] + [[None] for _ in range(30)], [1, 1, 1, 0])
    assert needs_splitting(grid)
    assert count_non_empty_rows(grid) == count_non_empty_columns(grid) == 1
    assert split_group(grid) == [grid]


def test_horizontal_cut_order_uses_float_midpoint_then_sorted_steps():
    positions = {(r, c): None for r in range(4) for c in range(4)}
    cuts = list(generate_horizontal_cuts(positions, 1))
    assert cuts[:6] == [
        (0, (1, 1, 1, 1)), (0, (2, 2, 2, 2)), (0, (3, 3, 3, 3)),
        (0, (1, 2, 2, 2)), (0, (1, 1, 2, 2)), (0, (1, 1, 1, 2)),
    ]


def test_vertical_gap_crops_at_gap_and_preserves_row_mapping():
    grid = grid_from_cells([["a", "b", None, "c", "d"]], [2, 1, 2, 0])
    grid["RowIndices"] = [17]
    a, b = split_json_vertical(grid)
    assert recorded_shape(a) == {"cells": [["a", "b"]], "sequences": [2, 1, 1, 0], "row_indices": [17]}
    assert recorded_shape(b) == {"cells": [[None, "c", "d"]], "sequences": [2, 1, 1, 0], "row_indices": [17]}


def test_force_vertical_and_bisect_use_distinct_midpoints_and_layouts():
    grid = rectangle(2, 4, [2, 1, 4, 0])
    forced = force_vertical_split(grid)
    bisected = bisect_vertically(grid)
    assert [count_panels(p) for p in forced] == [2, 6]
    assert [len(p["Rows"][0]["Panels"]) for p in forced] == [1, 3]
    assert [count_panels(p) for p in bisected] == [4, 4]
    assert [len(p["Rows"][0]["Panels"]) for p in bisected] == [4, 4]
    assert all(p["RowIndices"] == [0, 1] for p in forced + bisected)


def test_force_horizontal_crops_and_translates_row_indices():
    grid = rectangle(4, 2, [2, 1, 4, 0])
    grid["RowIndices"] = [10, 20, 30, 40]
    a, b = force_horizontal_split(grid)
    assert a["RowIndices"] == [10]
    assert b["RowIndices"] == [20, 30, 40]
    assert [count_panels(a), count_panels(b)] == [2, 6]


def test_dimension_safety_recomputes_sequences_without_an_input_plan():
    grid = rectangle(61, 1, [14, 13, 0, 0])
    pieces = ensure_pieces_within_column_limit([grid])
    assert sum(count_panels(p) for p in pieces) == 61
    assert all(count_non_empty_rows(p) <= 30 for p in pieces)
    assert [r for p in pieces for r in p["RowIndices"]] == list(range(61))
    for piece in pieces:
        a, b, qa, qb = piece["Sequences"]
        assert a * qa + b * qb == count_panels(piece)


def test_column_carving_keeps_literal_excluded_column_and_allocation_rejects_loss():
    region = {(0, c): None for c in range(5)}
    a, b = split_at_column(region, 2)
    assert list(a) == [(0, 0), (0, 1)]
    assert list(b) == [(0, 3), (0, 4)]
    assert allocate_single_string([3, 2, 1, 1], len(a), len(b)) is None
    assert allocate_single_string([14, 13, 2, 1], 14, 27) == ([14, 13, 1, 0], [14, 13, 1, 1])


def test_carving_allocation_rejects_greedy_remainder_without_backtracking():
    # Two 13s could fill 26, but the plugin first takes a 14 and rejects the 12 left.
    assert allocate_single_string([14, 13, 2, 2], 26, 28) is None
