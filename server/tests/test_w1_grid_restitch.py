"""Response-side parity with Branch2025's committed split rooftop groups."""

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from solar_grid_restitch import (
    combine_flat_sequences, escalate, failed_group_indices, is_response_failed,
    ordered_strings, restitch,
)


GROUPS = json.loads(
    (SERVER / "tests/fixtures/w1_rooftop_split_groups.json").read_text(encoding="utf-8")
)["groups"]


def panel(identity, sequence, code=1):
    return {"Code": code, "Id": identity, "Seq": sequence}


def response(rows, lengths=(), **data):
    return {"data": {
        "final_grid": {"Rows": [{"Panels": row} for row in rows]},
        "best_result": {"info": {"sequence_length": list(lengths)}},
        **data,
    }}


@pytest.mark.parametrize("group", GROUPS, ids=lambda g: g["group_block"])
def test_committed_split_group_strings(group):
    merged = restitch(group["responses"], [p["row_indices"] for p in group["pieces"]])
    actual = ordered_strings(merged)
    expected = group["plugin_strings"]
    assert sorted(actual) == sorted(expected)
    assert len(actual) == len(expected)
    for committed, recorded in zip(sorted(actual), sorted(expected)):
        assert committed == recorded
    assert sum(map(len, actual)) == group["panel_count"]


def test_partial_beam_keeps_previous_length_and_commits_remainder():
    group = GROUPS[1]
    partial = group["responses"][-1]
    assert group["panel_count"] == 487
    assert set(partial["data"]["best_result"]["info"]) == {"distance_total"}
    assert not is_response_failed(partial)
    merged = restitch(group["responses"], [p["row_indices"] for p in group["pieces"]])
    supplied = merged["data"]["best_result"]["info"]["sequence_length"]
    assert sum(supplied) == 319
    assert supplied[-1] == 13
    strings = ordered_strings(merged)[len(supplied):]
    assert [len(string) for string in strings] == [13] * 12 + [12]
    ordered = sorted((p for row in partial["data"]["final_grid"]["Rows"]
                      for p in row["Panels"] if p["Code"] == 1), key=lambda p: p["Seq"])
    # StringPlacement.cs:975 commits upper-cased handles.
    assert [identity for string in strings for identity in string] == [
        p["Id"].upper() for p in ordered]


def test_failure_predicates_from_plugin():
    good = response([[panel("A", 1)]], [1])
    failures = [None, "", "not json", "[]", [], {}, {"data": None},
                {"data": {}}, {"data": {"final_grid": {}, "best_result": None}},
                {"data": {"final_grid": None, "best_result": {}}}]
    for error in ("failed", "", False, 0, {}):
        failures.append({**good, "error": error})
    for count in (0, "0", " +0 ", 0.0):
        failures.append(response([], total_valid_solutions=count))
    failures.append(response([], message="Stopped: No valid solutions found"))
    for failed in failures:
        assert is_response_failed(failed), repr(failed)
        if isinstance(failed, dict):
            assert is_response_failed(json.dumps(failed))


def test_success_predicates_do_not_invent_failures():
    # C# tests presence, not truthiness, solution count positivity or status.
    successes = [{"data": {"final_grid": {}, "best_result": {}}}]
    for count in (None, 1, -1, "unknown", "0.0", 0.5, 2**40):
        successes.append(response([], total_valid_solutions=count))
    successes.append({**response([], message="no valid solutions"), "error": None,
                      "status": "error"})
    for success in successes:
        assert not is_response_failed(success)
        assert not is_response_failed(json.dumps(success))


def test_failed_group_indices_use_any_piece_and_missing_response():
    good = response([], [1])
    assert failed_group_indices([good, "", good], [[0], [0, 1], [2, 4], [], [2]]) == [1, 2]


def test_escalation_sequence():
    state = {"jogs": 1, "depth": 10, "exhausted": False, "original_index": 3}
    initial = deepcopy(state)
    for jogs, depth, exhausted in ((2, 10, False), (2, 1, False),
                                    (2, 0, False), (2, 0, True)):
        state = escalate(state)
        assert state == {"jogs": jogs, "depth": depth, "exhausted": exhausted,
                         "original_index": 3}
    assert initial == {"jogs": 1, "depth": 10, "exhausted": False, "original_index": 3}
    assert escalate(state) == state


def test_flat_sequence_shape_discards_quantities():
    assert combine_flat_sequences([14, 13, 5, 2], [14, 12, 3, 4]) == [[14, 13], [14, 13]]
    assert combine_flat_sequences([14, 13, 0, 2], None) == [[13, 0], [13, 0]]
    assert combine_flat_sequences([[14, 13], [14, 13]], []) == [[0, 0], [0, 0]]
    assert combine_flat_sequences(None, None) == [[0, 0], [0, 0]]


def test_horizontal_merge_restores_row_gaps_and_metadata():
    a = response([[panel("A", 1)]], [1], distance_total="2.5")
    a["data"]["final_grid"].update(Dwgname="roof.dwg", Modify=[1, -1], Sequences=[1, 0, 1, 0])
    b = response([[panel("B", 1), panel("C", 2)]], [2], distance_total=4)
    b["data"]["final_grid"]["Sequences"] = [2, 0, 1, 0]
    empty = panel("", 0, 0)
    assert restitch([a, b], [[1], [3]]) == {"data": {
        "final_grid": {"Dwgname": "roof.dwg", "Modify": [1, -1],
                       "Sequences": [[2, 1], [2, 1]],
                       "Rows": [{"Panels": [empty, empty]},
                                {"Panels": [panel("A", 1), empty]},
                                {"Panels": [empty, empty]},
                                {"Panels": [panel("B", 2), panel("C", 3)]}]},
        "distance_total": 6.5,
        "best_result": {"info": {"sequence_length": [1, 2]}, "model": "combined"},
    }}


def test_shared_rows_concatenate_but_single_piece_rows_overlay():
    a = response([[panel("A", 1)], [panel("B", 2)]], [2])
    b = response([[panel("C", 1)], [panel("D", 2)]], [2])
    rows = restitch([a, b], [[0, 1], [1, 2]])["data"]["final_grid"]["Rows"]
    assert rows == [
        {"Panels": [panel("A", 1), panel("", 0, 0)]},
        {"Panels": [panel("B", 2), panel("C", 3)]},
        {"Panels": [panel("D", 4), panel("", 0, 0)]},
    ]


def test_three_piece_fold_resets_accumulated_row_indices():
    pieces = [response([[panel(identity, 1)]], [1]) for identity in ("A", "B", "C")]
    merged = restitch(pieces, [[2], [4], [2]])
    rows = merged["data"]["final_grid"]["Rows"]
    assert len(rows) == 5
    assert rows[2]["Panels"] == [panel("A", 1), panel("C", 3)]
    assert rows[4]["Panels"] == [panel("B", 2), panel("", 0, 0)]
    assert ordered_strings(merged) == [["A"], ["B"], ["C"]]


def test_offsets_use_all_codes_and_duplicate_ids_keep_first_sequence():
    a = response([[panel("A", 1), panel("", 7, 0)]], [1])
    b = response([[panel("A", 1), panel("B", 2), panel("ZERO", 0)]], [2])
    merged = restitch([a, b], [[0], [0]])
    panels = merged["data"]["final_grid"]["Rows"][0]["Panels"]
    assert [p["Seq"] for p in panels] == [1, 0, 1, 9, 0]
    assert ordered_strings(merged) == [["A"], ["B"]]


def test_draw_cut_defaults_retains_zero_and_ignores_seq_gaps():
    panels = [panel(f"P{i}", i) for i in range(1, 9)]
    assert [len(s) for s in ordered_strings(response([panels]))] == [6, 2]
    assert [len(s) for s in ordered_strings(response([panels], [2, 0, 3]))] == [2, 2, 3, 1]
    assert ordered_strings(response([[panel("b", 8), panel("a", 2),
                                      panel("duplicate", 2), panel("zero", 0)]], [1])) == [["A"], ["B"]]


def test_restitch_failure_filter_singletons_and_merge_fallback():
    good = response([[panel("A", 1)]], [1])
    assert restitch([], []) == ""
    assert restitch(["", "invalid"], [[], []]) == ""
    assert restitch(["invalid"], [[]]) == "invalid"
    assert restitch(["", good], [[], [0]]) == good
    malformed = {"data": {"final_grid": {}, "best_result": {}}}
    assert not is_response_failed(malformed)
    assert restitch([good, malformed], [[0], [1]]) == good


def test_restitch_and_string_cut_do_not_mutate_inputs():
    group = deepcopy(GROUPS[1])
    before = deepcopy(group)
    rows = [p["row_indices"] for p in group["pieces"]]
    merged = restitch([json.dumps(r) for r in group["responses"]], rows)
    snapshot = deepcopy(merged)
    ordered_strings(merged)
    assert merged == snapshot
    assert group == before


def test_legacy_overload_reads_response_row_indices():
    a = response([[panel("A", 1)]], [1])
    b = response([[panel("B", 1)]], [1])
    a["data"]["final_grid"]["RowIndices"] = [2]
    b["data"]["final_grid"]["RowIndices"] = [4]
    assert restitch([a, b]) == restitch([a, b], [[2], [4]])
