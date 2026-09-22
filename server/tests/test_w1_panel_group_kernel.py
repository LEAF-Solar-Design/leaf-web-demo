"""The pure PanelGroupCreate kernel against the plugin's recorded groups and matrices."""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
REPO = SERVER.parent
sys.path.insert(0, str(SERVER))

import solar_panel_group_kernel as kernel

ORACLE = json.loads((SERVER / "tests" / "fixtures" / "w1_rooftop_panel_groups.json").read_text(encoding="utf-8"))
SETTINGS = ORACLE["settings"]
FIXTURES = {f["drawing"]: f for f in ORACLE["fixtures"]}


def _intake(fixture):
    return json.loads((REPO / fixture["intake"]).read_text(encoding="utf-8"))


def _kernel_groups(fixture):
    panels = kernel.panels_from_intake(_intake(fixture), installation_design=SETTINGS["installation_design"])
    return kernel.group_panels(
        panels,
        branch_max_offset=SETTINGS["branch_max_offset"],
        alignment_tolerance=SETTINGS["alignment_tolerance"],
        installation_design=SETTINGS["installation_design"],
    )


def _norm(handle):
    return None if handle in (None, "") else handle.upper()


@pytest.fixture(scope="module")
def computed():
    return {name: _kernel_groups(f) for name, f in FIXTURES.items()}


# ------------------------------------------------------------------ the oracle


def test_oracle_has_both_rooftop_fixtures():
    assert set(FIXTURES) == {"rooftop_demo", "rooftop_unsplit"}
    assert len(FIXTURES["rooftop_demo"]["groups"]) == 11
    assert len(FIXTURES["rooftop_unsplit"]["groups"]) == 7


@pytest.mark.parametrize("drawing", sorted(FIXTURES))
def test_group_membership_matches_plugin(drawing, computed):
    plugin = {frozenset(_norm(h) for h in g["members"]) for g in FIXTURES[drawing]["groups"]}
    ours = {frozenset(_norm(h) for h in g["members"]) for g in computed[drawing]}
    assert len(computed[drawing]) == len(FIXTURES[drawing]["groups"])
    assert ours == plugin


@pytest.mark.parametrize("drawing", sorted(FIXTURES))
def test_every_group_matrix_matches_plugin_cell_for_cell(drawing, computed):
    ours = {frozenset(_norm(h) for h in g["members"]): g["matrix"] for g in computed[drawing]}
    for group in FIXTURES[drawing]["groups"]:
        key = frozenset(_norm(h) for h in group["members"])
        assert key in ours, group["name"]
        mine = [[_norm(c) for c in row] for row in ours[key]]
        theirs = [[_norm(c) for c in row] for row in group["matrix"]]
        assert len(mine) == len(theirs), (group["name"], "rows")
        for r, (a, b) in enumerate(zip(mine, theirs)):
            assert len(a) == len(b), (group["name"], "columns", r)
            assert a == b, (group["name"], "row", r)


@pytest.mark.parametrize("drawing", sorted(FIXTURES))
def test_matrix_cells_are_distinct_group_members(drawing, computed):
    for group in computed[drawing]:
        cells = [c for row in group["matrix"] for c in row if c is not None]
        assert len(cells) == len(set(cells))
        assert set(cells) <= set(group["members"])


@pytest.mark.parametrize("drawing", sorted(FIXTURES))
def test_fixture_groups_come_back_in_documented_order(drawing, computed):
    firsts = [min(int(h, 16) for h in g["members"]) for g in computed[drawing]]
    assert firsts == sorted(firsts)


# ---------------------------------------------------------------- unit checks


def _panel(handle, cx, cy, angle=0.0, col=77.0, row=38.5):
    return {"handle": handle, "centre": (cx, cy), "angle": angle, "column_dim": col, "row_dim": row}


def _group(panels, **kw):
    kw.setdefault("branch_max_offset", 120.0)
    kw.setdefault("alignment_tolerance", 12.0)
    return kernel.group_panels(panels, **kw)


def test_v_close_is_an_open_interval():
    assert kernel.v_close(1.0, 1.0005)
    assert not kernel.v_close(0.0, 12.0, 12.0)
    assert kernel.v_close(0.0, 11.999, 12.0)


def test_angle_key_folds_opposite_directions():
    assert kernel.angle_key(math.radians(90)) == kernel.angle_key(math.radians(270)) == "90.0"
    assert kernel.angle_key(0.0) == kernel.angle_key(math.pi) == "0.0"
    assert kernel.angle_key(math.radians(200)) == "20.0"


def test_angle_key_rounds_half_away_from_zero_on_the_exact_value():
    assert kernel.angle_key(math.radians(12.25)) in ("12.3", "12.2")
    assert kernel._csharp_format_0_0(0.25) == "0.3"
    assert kernel._csharp_format_0_0(0.35) == "0.3"  # 0.35 is 0.34999... in binary
    assert kernel._csharp_format_0_0(179.96) == "180.0"


def test_angle_key_snaps_near_right_angles_like_rtod():
    assert kernel.angle_key(math.radians(179.9995)) == "0.0"
    assert kernel.angle_key(math.radians(359.9995)) == "0.0"
    assert kernel.angle_key(math.radians(89.9995)) == "90.0"


def test_group_row_angle_collapses_to_half_turn():
    assert kernel.group_row_angle(math.radians(270)) == pytest.approx(math.radians(90))
    assert kernel.group_row_angle(math.pi) == 0.0
    assert kernel.group_row_angle(-math.radians(10)) == pytest.approx(math.radians(170))


def test_bucketing_is_first_match_and_order_dependent():
    buckets, keys = kernel.bucket_by_distance(["a", "b", "c"], [0.0, 10.0, 20.0], 12.0)
    assert buckets == {0.0: ["a", "b"], 20.0: ["c"]}
    assert keys == [0.0, 20.0]
    buckets, keys = kernel.bucket_by_distance(["b", "a", "c"], [10.0, 0.0, 20.0], 12.0)
    assert buckets == {10.0: ["b", "a", "c"]}
    assert keys == [10.0]


def test_bucket_keys_sort_ascending_and_keep_raw_first_distance():
    buckets, keys = kernel.bucket_by_distance([1, 2, 3], [50.3, -40.1, 51.0], 12.0)
    assert keys == [-40.1, 50.3]
    assert buckets[50.3] == [1, 3]


def test_matrix_rows_ascend_and_columns_are_reversed():
    # Row angle 0: rows ascend in Y (row 0 lowest); the column distances carry a
    # sign flip and are then reversed, so column 0 is the rightmost panel.
    panels = [_panel("A", 0, 0), _panel("B", 100, 0), _panel("C", 0, 50), _panel("D", 100, 50)]
    assert kernel.group_matrix(panels, 0.0, 12.0) == [["B", "A"], ["D", "C"]]


def test_matrix_leaves_blank_cells_for_missing_panels():
    panels = [_panel("A", 0, 0), _panel("B", 100, 0), _panel("D", 100, 50)]
    assert kernel.group_matrix(panels, 0.0, 12.0) == [["B", "A"], ["D", None]]


def test_rectangle_test_tolerates_intake_quantization_only():
    # Opposite edges 38.613 vs 38.614: what 3-decimal rounding leaves of a
    # slightly rotated panel (handles 863D, 8630, 862B on rooftop_demo).
    quantized = {"handle": "863D", "closed": True, "pts": [[0, 0], [79.329, 0], [79.329, 38.613], [0, 38.614]]}
    panel = kernel.panel_from_polyline(quantized)
    assert panel is not None
    assert panel["column_dim"] == pytest.approx(79.329)
    # A 0.017 edge mismatch is far beyond quantization and stays rejected.
    skewed = {"handle": "1", "closed": True, "pts": [[0, 0], [79.329, 0], [79.329, 38.613], [0, 38.63]]}
    assert kernel.panel_from_polyline(skewed) is None


def test_grow_offset_is_half_the_gap_plus_five_percent():
    assert kernel.grow_offset(120.0) == pytest.approx(63.0)


def test_grown_rectangles_join_islands_at_the_grow_boundary():
    # Grown width 77 + 2*63 = 203, so centres 202 apart unite and 204 apart do not.
    near = _group([_panel("1", 0, 0), _panel("2", 202, 0)])
    far = _group([_panel("1", 0, 0), _panel("2", 204, 0)])
    assert [g["members"] for g in near] == [["1", "2"]]
    assert [g["members"] for g in far] == [["1"], ["2"]]


def test_island_test_respects_rotation():
    theta = math.radians(30)
    along = (math.cos(theta), math.sin(theta))
    across = (-math.sin(theta), math.cos(theta))
    joined = _group([_panel("1", 0, 0, theta), _panel("2", 202 * along[0], 202 * along[1], theta)])
    # Grown row depth 38.5 + 126 = 164.5; 165 across is apart even though the
    # axis-aligned boxes of the two rotated rectangles overlap.
    apart = _group([_panel("1", 0, 0, theta), _panel("2", 165 * across[0], 165 * across[1], theta)])
    assert len(joined) == 1
    assert len(apart) == 2


def test_islands_chain_transitively():
    groups = _group([_panel("1", 0, 0), _panel("2", 200, 0), _panel("3", 400, 0)])
    assert [g["members"] for g in groups] == [["1", "2", "3"]]


def test_one_island_splits_by_angle_key():
    groups = _group([_panel("1", 0, 0, 0.0), _panel("2", 100, 0, math.pi), _panel("3", 0, 60, math.pi / 2)])
    assert [g["members"] for g in groups] == [["1", "2"], ["3"]]
    assert [g["angle_key"] for g in groups] == ["0.0", "90.0"]


def test_groups_come_back_in_ascending_smallest_handle_regardless_of_input_order():
    panels = [_panel("A0", 0, 0), _panel("1F", 1000, 0), _panel("2", 1200, 0), _panel("B", 5000, 0)]
    forward = _group(panels)
    backward = _group(list(reversed(panels)))
    assert forward == backward
    assert [g["members"] for g in forward] == [["2", "1F"], ["B"], ["A0"]]


def test_inputs_are_not_mutated():
    panels = [_panel("1", 0, 0), _panel("2", 100, 0)]
    before = copy.deepcopy(panels)
    _group(panels)
    assert panels == before
    intake = {"polylines": [{"layer": "Panels", "closed": True, "handle": "A",
                             "pts": [[0, 0, 0], [77, 0, 0], [77, 38.5, 0], [0, 38.5, 0]]}]}
    snapshot = copy.deepcopy(intake)
    kernel.panels_from_intake(intake)
    assert intake == snapshot


def test_panel_from_polyline_long_side_first():
    panel = kernel.panel_from_polyline(
        {"layer": "Panels", "closed": True, "handle": "A", "pts": [[0, 0], [77, 0], [77, 38.5], [0, 38.5]]})
    assert panel["centre"] == (38.5, 19.25)
    assert panel["angle"] == 0.0
    assert (panel["column_dim"], panel["row_dim"]) == (77.0, 38.5)


def test_panel_from_polyline_short_side_first_uses_second_segment():
    panel = kernel.panel_from_polyline(
        {"layer": "Panels", "closed": True, "handle": "A", "pts": [[0, 0], [0, 38.5], [-77, 38.5], [-77, 0]]})
    assert panel["angle"] == pytest.approx(math.pi)
    assert panel["centre"] == (-38.5, 19.25)
    assert (panel["column_dim"], panel["row_dim"]) == (77.0, 38.5)


def test_ground_panels_turn_a_quarter_and_swap_dimensions():
    panel = kernel.panel_from_polyline(
        {"layer": "Panels", "closed": True, "handle": "A", "pts": [[0, 0], [77, 0], [77, 38.5], [0, 38.5]]},
        installation_design="Ground")
    assert panel["angle"] == pytest.approx(math.pi / 2)
    assert (panel["column_dim"], panel["row_dim"]) == (38.5, 77.0)


def test_panels_from_intake_filters_layer_closure_and_shape():
    rect = [[0, 0], [77, 0], [77, 38.5], [0, 38.5]]
    intake = {"polylines": [
        {"layer": "Panels", "closed": True, "handle": "1", "pts": rect},
        {"layer": "roof PANEL layout", "closed": True, "handle": "2", "pts": rect},
        {"layer": "Roof", "closed": True, "handle": "3", "pts": rect},
        {"layer": "Panels", "closed": False, "handle": "4", "pts": rect},
        {"layer": "Panels", "closed": True, "handle": "5", "pts": [[0, 0], [77, 0], [70, 38.5], [0, 38.5]]},
    ]}
    assert [p["handle"] for p in kernel.panels_from_intake(intake)] == ["1", "2"]


def test_duplicate_handles_and_bad_settings_fail_closed():
    rect = [[0, 0], [77, 0], [77, 38.5], [0, 38.5]]
    with pytest.raises(kernel.PanelGroupKernelError):
        kernel.panels_from_intake({"polylines": [
            {"layer": "Panels", "closed": True, "handle": "1", "pts": rect},
            {"layer": "Panels", "closed": True, "handle": "1", "pts": rect}]})
    with pytest.raises(kernel.PanelGroupKernelError):
        kernel.group_panels([_panel("1", 0, 0)], branch_max_offset=0, alignment_tolerance=12)
    with pytest.raises(kernel.PanelGroupKernelError):
        kernel.group_panels([_panel("1", 0, 0)], branch_max_offset=120, alignment_tolerance=12,
                            installation_design="Carport")
