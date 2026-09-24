"""Tests for server/solar_lite_cabling.py (G36: the plugin's lightweight cabling engine, adopt mode)."""
import importlib.util
import math
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "solar_lite_cabling.py"
_SPEC = importlib.util.spec_from_file_location("solar_lite_cabling", _PATH)
eng = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eng)


def tracker(x, y0, count, pitch=10.0, length=5.0):
    """A column of `count` strings at X `x`, each running `length` to the right."""
    return [((x, y0 + k * pitch), (x + length, y0 + k * pitch)) for k in range(count)]


def test_lanes_are_gap_midpoints_plus_two_field_edges():
    lanes = eng.derive_vertical_lane_xs([(0.0, 10.0), (50.0, 60.0), (62.0, 70.0)], 1.0, 25.0)
    # one gap wide enough (10..50: buckets 11..49), the 60..62 seam too narrow; edges at a quarter band
    assert lanes == [-2.75, 30.0, 72.75]


def test_lanes_need_extents_and_a_bucket():
    assert eng.derive_vertical_lane_xs([], 1.0, 25.0) == []
    assert eng.derive_vertical_lane_xs([(0.0, 5.0)], 0.0, 25.0) == []


def test_rows_join_x_aligned_y_adjacent_strings_only():
    pts = [(0.0, 0.0), (1.0, 20.0), (0.5, 100.0), (30.0, 0.0)]
    rows, s2row = eng.derive_rows(pts, 4.0, 36.0)
    assert [r["members"] for r in rows] == [[0, 1], [2], [3]]
    assert s2row == [0, 0, 1, 2]
    assert rows[0]["end0"] == (0.5, 0.0) and rows[0]["end1"] == (0.5, 20.0)


def test_same_row_first_packs_full_boxes_and_mounts_toward_the_nearest_lane():
    strings = tracker(0.0, 0.0, 20) + tracker(200.0, 0.0, 20)
    pts = [a for a, _ in strings]
    rows, s2row = eng.derive_rows(pts, 4.0, 36.0)
    lanes = [-500.0, 100.0, 300.0]          # x=200 ties 100 and 300: the first nearest (100) wins, left side
    boxes = eng.group_same_row_first(pts, rows, s2row, lanes, list(range(len(pts))), 16, 60.0, 8.0, 8.0)
    # each 20-string tracker: a full 16 box plus a 4 left over, merged into its own row's box (16 + 4 <= 20)
    assert [b["n"] for b in boxes] == [20, 20]
    assert boxes[0]["location"][0] == 8.0 and boxes[1]["location"][0] == 192.0
    assert all(b["pure"] for b in boxes)


def test_homerun_paths_run_along_the_row_then_jog_to_the_combiner():
    strings = tracker(0.0, 0.0, 3)
    pts = [a for a, _ in strings]
    rows, s2row = eng.derive_rows(pts, 4.0, 36.0)
    boxes = eng.group_same_row_first(pts, rows, s2row, [100.0], [0, 1, 2], 16, 60.0, 8.0, 8.0)
    paths = eng.build_homerun_paths(boxes, pts, rows, s2row)
    assert [p[2] for p in paths] == [[(0.0, 0.0), (0.0, 10.0), (8.0, 10.0)], [(0.0, 10.0), (8.0, 10.0)],
                                     [(0.0, 20.0), (0.0, 10.0), (8.0, 10.0)]]


def test_dedupe_drops_repeats_and_collinear_vertices():
    assert eng.dedupe_path([(0, 0), (0, 0), (0, 5), (0, 10), (3, 10)]) == [(0, 0), (0, 10), (3, 10)]


def test_feeders_comb_through_the_lane_nearest_the_combiner():
    paths = eng.build_feeder_paths([(1, 7)], {1: (10.0, 50.0)}, {7: (90.0, 0.0)}, [0.0, 60.0], False)
    assert paths == [(1, 7, [(10.0, 50.0), (0.0, 50.0), (0.0, 0.0), (90.0, 0.0)])]
    assert eng.build_feeder_paths([(1, 7)], {1: (10.0, 50.0)}, {7: (90.0, 0.0)}, [], True) == \
        [(1, 7, [(10.0, 50.0), (90.0, 0.0)])]


def test_phantoms_are_far_and_stacked():
    cbs = [(1, (0.0, 0.0)), (2, (10.0, 0.0))]
    invs = [(1, (5.0, 5.0)), (2, (6.0, 5.0)), (3, (7.0, 5.0)), (4, (5000.0, 0.0)), (5, (5050.0, 0.0))]
    kept, dropped = eng.filter_phantom_inverters(cbs, invs)
    assert [n for n, _ in dropped] == [4, 5] and [n for n, _ in kept] == [1, 2, 3]
    assert eng.filter_phantom_inverters(cbs, invs[:3]) == (invs[:3], [])


def test_assignment_respects_the_string_cap_then_falls_to_the_least_loaded():
    cbs = [(1, (0.0, 0.0), 10), (2, (1.0, 0.0), 10), (3, (100.0, 0.0), 10)]
    invs = [(7, (0.0, 1.0)), (8, (100.0, 1.0))]
    assign, total = eng.assign_feeders(cbs, invs, cap_override=36, weight_cap=15, tail_bias=True)
    # hardest first: 2 takes 7; 1 cannot fit 7 (20 > 15) and takes 8; 3 fits neither and falls to the first
    # least-loaded (7); the swap 1<->3 would put 20 strings on 7 again, so it is refused.
    assert assign == {2: 7, 1: 8, 3: 7}
    assert total > 0
    assert eng.assign_feeders([], invs) == ({}, 0.0)


def test_place_adopts_the_existing_inverters_and_reports_findings():
    strings = tracker(0.0, 0.0, 16) + tracker(300.0, 0.0, 2)
    result = eng.place(strings, [(9, (-20.0, 0.0)), (10, (320.0, 0.0))])
    assert result["success"] and [c["n"] for c in result["combiners"]] == [16, 2]
    assert result["assignments"] == [(1, 9), (2, 10)]
    assert [i["load"] for i in result["inverters"]] == [16, 2]
    assert result["findings"][0] == "strings/inverter 2-16 (DC:AC 0.01-0.08 at each unit's rating)"
    assert any(f.startswith("WARN: heavy load imbalance (2 vs 16 strings)") for f in result["findings"])
    assert eng.printed("WARN: x") == "! x" and eng.printed("y") == "- y"


def test_place_without_strings_or_inverters_warns():
    assert eng.place([], [])["warnings"] == ["No strings supplied."]
    result = eng.place(tracker(0.0, 0.0, 2), [])
    assert not result["success"] and result["warnings"] == ["No inverters available to assign feeders to."]


def test_free_placement_is_refused_by_name():
    options = dict(eng.default_options(), FreeInverters=True)
    with pytest.raises(eng.LiteCablingNotPortedError):
        eng.place(tracker(0.0, 0.0, 2), [(1, (0.0, 0.0))], None, options)


def test_the_euclidean_grouping_packs_within_reach():
    strings = tracker(0.0, 0.0, 6)
    options = dict(eng.default_options(), RowAwareRouting=False)
    result = eng.place(strings, [(1, (0.0, -30.0))], None, options)
    assert result["success"] and [c["n"] for c in result["combiners"]] == [6]
    assert result["homerun_paths"] is None


def test_default_weight_cap_and_sku():
    opt = eng.default_options()
    assert math.floor(eng.sku_ac_kw(opt) * opt["DcAcMax"] / opt["StringKw"]) == 263
    assert [eng.smallest_sku(n) for n in (1, 16, 17, 40)] == [8, 16, 20, 32]
