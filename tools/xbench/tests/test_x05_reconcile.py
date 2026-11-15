"""Synthetic evidence for X05's offline comparator, no transport or real input."""

from itertools import permutations

import pytest

from benches.x05_reconcile import compare_strings


ORACLE = [{"string_id": "A", "optimizers": 5}, {"string_id": "B", "optimizers": 7}]


def test_equal_totals_do_not_prove_per_string_counts():
    saved = [{"string_id": "A", "optimizers": 6}, {"string_id": "B", "optimizers": 6}]
    assert sum(row["optimizers"] for row in ORACLE) == sum(row["optimizers"] for row in saved)
    for oracle_order in permutations(ORACLE):
        for saved_order in permutations(saved):
            evidence = compare_strings(list(oracle_order), list(saved_order))
            assert evidence["verdict"] == "FAIL"
            assert evidence["mismatches"] == [
                {"string_id": "A", "oracle": 5, "saved": 6},
                {"string_id": "B", "oracle": 7, "saved": 6}]


def test_exact_ids_and_counts_pass_in_any_order():
    assert compare_strings(ORACLE, list(reversed(ORACLE)))["verdict"] == "PASS"


def test_missing_and_foreign_ids_are_explicit():
    evidence = compare_strings(ORACLE, [ORACLE[0], {"string_id": "C", "optimizers": 7}])
    assert evidence["verdict"] == "FAIL"
    assert evidence["missing_ids"] == ["B"] and evidence["foreign_ids"] == ["C"]
    assert compare_strings(ORACLE, ORACLE[:1])["missing_ids"] == ["B"]


@pytest.mark.parametrize("source", ["oracle", "saved"])
def test_duplicate_ids_never_compare_as_valid(source):
    duplicate = ORACLE + [ORACLE[0]]
    evidence = compare_strings(duplicate if source == "oracle" else ORACLE,
                               duplicate if source == "saved" else ORACLE)
    assert evidence == {"verdict": "UNREADABLE", "reason": source + "_duplicate_id"}


@pytest.mark.parametrize("rows", [None, {}, [], [None], [{"string_id": []}],
                                  [{"string_id": "A", "optimizers": True}],
                                  [{"string_id": "A", "optimizers": -1}],
                                  [{"string_id": "A", "optimizers": float("nan")}],
                                  [{"string_id": "A", "optimizers": 10**100}]])
def test_malformed_rows_are_unreadable_without_exceptions(rows):
    assert compare_strings(rows, ORACLE)["verdict"] == "UNREADABLE"
    assert compare_strings(ORACLE, rows)["verdict"] == "UNREADABLE"
