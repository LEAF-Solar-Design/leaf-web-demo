"""Synthetic evidence for X05's offline comparator, no transport or real input."""

from itertools import permutations
from copy import deepcopy
import csv
from hashlib import sha256
from io import StringIO
import json

import pytest

from benches.x05_reconcile import (METRICS, TABLE_COLUMNS, check_discrepancies,
    compare_csv_view, compare_recommendations, compare_snapshots, compare_strings, compare_table)


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


def test_every_grouping_recommendation_must_match_independent_report():
    assert compare_recommendations(16, [{"group_count": 16}] * 3)["verdict"] == "PASS"
    assert compare_recommendations(16, [{"group_count": 16}, {"group_count": 1}]) == {
        "verdict": "FAIL", "reason": "grouping_count_mismatch", "mismatched_recommendations": [1]}
    assert compare_recommendations(16, [])["verdict"] == "PASS"


@pytest.mark.parametrize("count", [None, True, "16", 0, -1, float("nan"), 10**100])
def test_grouping_missing_or_invalid_counts_are_unreadable(count):
    assert compare_recommendations(count, [{"group_count": 16}])["verdict"] == "UNREADABLE"
    assert compare_recommendations(16, [{"group_count": count}])["verdict"] == "UNREADABLE"
    assert compare_recommendations(16, [{}])["verdict"] == "UNREADABLE"


def table_fixture():
    values = dict(panels=24, optimizers=12, inverters=1, strings=2, kw_dc="9.6")
    rows = [dict(metric=key, studio=value, oracle=value, delta=0, verdict="PASS")
            for key, value in values.items()]
    return values, rows


def test_table_checks_all_five_rows_against_independent_saved_values():
    values, rows = table_fixture()
    assert compare_table(list(reversed(rows)), values, dict(values))["verdict"] == "PASS"
    changed_saved = dict(values, panels=25)
    evidence = compare_table(rows, changed_saved, values)
    assert evidence["verdict"] == "FAIL"
    assert {row["field"] for row in evidence["mismatches"]} == {"studio", "delta", "verdict"}
    rows[0].update(studio=25, delta=1, verdict="FAIL")
    # Table integrity can pass while policy still rejects its nonzero delta.
    assert compare_table(rows, changed_saved, values)["verdict"] == "PASS"


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("field", ["studio", "oracle", "delta", "verdict"])
def test_each_table_value_delta_and_verdict_is_independently_checked(metric, field):
    values, rows = table_fixture()
    row = next(row for row in rows if row["metric"] == metric)
    row[field] = "FAIL" if field == "verdict" else 999
    result = compare_table(rows, values, values)
    assert result["verdict"] == "FAIL"
    assert {"metric": metric, "field": field} in result["mismatches"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign", "no-value", "bool", "nan"])
def test_table_requires_exactly_five_valid_unique_rows(mutation):
    values, rows = table_fixture()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(dict(rows[0]))
    elif mutation == "foreign":
        rows[0]["metric"] = "other"
    elif mutation == "no-value":
        del rows[0]["studio"]
    else:
        rows[0]["studio"] = True if mutation == "bool" else float("nan")
    assert compare_table(rows, values, values)["verdict"] == "UNREADABLE"


def discrepancy_fixture():
    deltas = dict.fromkeys(METRICS, 0)
    deltas["panels"] = -1
    record = dict(fixture_id="synthetic", metric="panels", delta=-1,
                  name="documented panel revision", frozen_at="2026-11-15T12:00:00Z")
    return deltas, record


def test_discrepancy_requires_exact_named_fixture_row_delta_before_run():
    deltas, record = discrepancy_fixture()
    assert check_discrepancies("synthetic", deltas, [record], "2026-11-15T13:00:00Z")["verdict"] == "PASS"
    assert check_discrepancies("synthetic", dict.fromkeys(METRICS, 0), [],
                               "2026-11-15T13:00:00Z")["verdict"] == "PASS"


@pytest.mark.parametrize("change", [{"fixture_id": "other"}, {"metric": "strings"},
                                    {"delta": 1}, {"name": ""},
                                    {"frozen_at": "2026-11-15T13:00:00Z"},
                                    {"frozen_at": "2026-11-15T14:00:00Z"},
                                    {"frozen_at": "2026-11-15T12:00:00"}])
def test_absent_unrelated_unnamed_and_late_discrepancies_cannot_pass(change):
    deltas, record = discrepancy_fixture()
    assert check_discrepancies("synthetic", deltas, [], "2026-11-15T13:00:00Z")["verdict"] == "FAIL"
    result = check_discrepancies("synthetic", deltas, [dict(record, **change)], "2026-11-15T13:00:00Z")
    assert result["verdict"] in ("FAIL", "UNREADABLE")


def test_discrepancy_duplicate_policy_rows_are_ambiguous():
    deltas, record = discrepancy_fixture()
    assert check_discrepancies("synthetic", deltas, [record, record], "2026-11-15T13:00:00Z") == {
        "verdict": "UNREADABLE", "reason": "duplicate_discrepancy_record"}


def snapshot(strings, table):
    def digest(value):
        return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return {"strings_sha256": digest(strings), "table_sha256": digest(table)}


def test_reopen_detects_changed_assignments_with_equal_aggregate_totals():
    _, rows = table_fixture()
    before = snapshot(ORACLE, rows)
    swapped = [{"string_id": "A", "optimizers": 7}, {"string_id": "B", "optimizers": 5}]
    reopened = snapshot(swapped, rows)
    assert compare_snapshots(before, dict(before))["verdict"] == "PASS"
    assert compare_snapshots(before, reopened) == {
        "verdict": "FAIL", "reason": "snapshot_changed", "changed_hashes": ["strings_sha256"]}
    changed = deepcopy(rows)
    changed[0]["studio"] = 25
    assert compare_snapshots(before, snapshot(ORACLE, changed))["changed_hashes"] == ["table_sha256"]


@pytest.mark.parametrize("bad", [None, {}, {"strings_sha256": "a" * 64},
                                {"strings_sha256": "wrong", "table_sha256": "b" * 64}])
def test_snapshot_comparison_rejects_absent_or_invalid_hashes(bad):
    good = {"strings_sha256": "a" * 64, "table_sha256": "b" * 64}
    assert compare_snapshots(good, bad)["verdict"] == "UNREADABLE"
    assert compare_snapshots(bad, good)["verdict"] == "UNREADABLE"


def csv_fixture(rows, columns=TABLE_COLUMNS):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(columns)
    writer.writerows([row[key] for key in columns] for row in rows)
    return buffer.getvalue()


def test_csv_quoted_cells_bom_reordered_rows_and_columns_match_view():
    _, rows = table_fixture()
    text = "\ufeff" + csv_fixture(list(reversed(rows)), tuple(reversed(TABLE_COLUMNS)))
    assert compare_csv_view(text, rows)["verdict"] == "PASS"


@pytest.mark.parametrize("field", ["studio", "oracle", "delta", "verdict"])
def test_csv_and_view_disagreements_are_explicit(field):
    _, rows = table_fixture()
    view = deepcopy(rows)
    view[0][field] = "FAIL" if field == "verdict" else 99
    evidence = compare_csv_view(csv_fixture(rows), view)
    assert evidence["verdict"] == "FAIL"
    assert {"metric": "panels", "field": field} in evidence["mismatches"]


@pytest.mark.parametrize("mutation", ["duplicate-header", "duplicate-row", "missing-row", "bad-quote"])
def test_csv_malformed_or_incomplete_evidence_is_unreadable(mutation):
    _, rows = table_fixture()
    if mutation == "duplicate-header":
        text = csv_fixture(rows, ("metric", "studio", "oracle", "delta", "delta"))
    elif mutation == "duplicate-row":
        text = csv_fixture(rows + [rows[0]])
    elif mutation == "missing-row":
        text = csv_fixture(rows[:-1])
    else:
        text = '"metric,studio,oracle,delta,verdict\n'
    assert compare_csv_view(text, rows)["verdict"] == "UNREADABLE"


def test_large_string_indexing_is_linear_order_invariant_and_preserves_callers():
    class CountedDict(dict):
        reads = 0

        def get(self, *args):
            type(self).reads += 1
            return super().get(*args)

    for count in (500, 2000):
        oracle = [CountedDict(string_id=f"S{i}", optimizers=6) for i in range(count)]
        saved = [CountedDict(row) for row in oracle]
        saved[0]["optimizers"] = 7
        saved[-1]["string_id"] = "foreign"
        before = deepcopy((oracle, saved))
        CountedDict.reads = 0
        evidence = compare_strings(oracle, saved)
        assert CountedDict.reads <= count * 5
        assert evidence == compare_strings(list(reversed(oracle)), list(reversed(saved)))
        assert evidence["verdict"] == "FAIL" and evidence["foreign_ids"] == ["foreign"]
        assert evidence["missing_ids"] == [f"S{count - 1}"]
        assert evidence["mismatches"] == [{"string_id": "S0", "oracle": 6, "saved": 7}]
        assert (oracle, saved) == before
