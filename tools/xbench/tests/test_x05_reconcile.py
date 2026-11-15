"""Synthetic evidence for X05's offline comparator, no transport or real input."""

from itertools import permutations
from copy import deepcopy
import csv
from hashlib import sha256
from io import StringIO
import json

import pytest

from benches.x05_reconcile import (METRICS, TABLE_COLUMNS, check_discrepancies,
    MAX_CSV_CELL, MAX_CSV_CHARS, canonical_snapshot, reconcile_offline,
    MAX_STRING_EXAMPLES, compare_bound_snapshots, compare_saved_string_totals,
    MAX_RECOMMENDATION_EXAMPLES, compare_oracle_string_totals,
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
        "verdict": "FAIL", "reason": "grouping_count_mismatch", "mismatched_recommendations": [1],
        "mismatch_count": 1}
    assert compare_recommendations(16, [], no_recommendations_shown=True)["verdict"] == "PASS"


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


def complete_evidence():
    values, rows = table_fixture()
    hashes = canonical_snapshot(ORACLE, rows)
    return dict(oracle_strings=deepcopy(ORACLE), saved_strings=deepcopy(ORACLE),
                report_grid_count=2, recommendations=[{"group_count": 2}],
                view_rows=rows, saved_values=values, oracle_values=dict(values),
                fixture_id="synthetic", discrepancy_records=[], run_started_at="2026-11-15T13:00:00Z",
                before=hashes, reopened=dict(hashes), csv_text=csv_fixture(rows),
                before_rows={"strings": deepcopy(ORACLE), "table_rows": deepcopy(rows)},
                reopened_rows={"strings": deepcopy(ORACLE), "table_rows": deepcopy(rows)})


def test_offline_orchestrator_composes_all_checks_with_unreadable_precedence():
    evidence = complete_evidence()
    result = reconcile_offline(evidence)
    assert result["verdict"] == "PASS" and len(result["stages"]) == 6
    evidence["recommendations"][0]["group_count"] = 1
    assert reconcile_offline(evidence)["verdict"] == "FAIL"
    del evidence["reopened"]
    result = reconcile_offline(evidence)
    assert result["verdict"] == "UNREADABLE"
    assert result["stages"]["grouping"]["verdict"] == "FAIL"
    assert result["stages"]["reopen"]["reason"] == "stage_evidence_missing"


@pytest.mark.parametrize("key", list(complete_evidence()))
def test_offline_orchestrator_requires_every_stage_input(key):
    evidence = complete_evidence()
    del evidence[key]
    assert reconcile_offline(evidence)["verdict"] == "UNREADABLE"


def test_orchestrator_derives_nonzero_policy_deltas_from_independent_values():
    evidence = complete_evidence()
    evidence["saved_values"]["panels"] += 1
    evidence["deltas"] = dict.fromkeys(METRICS, 0)  # An extra claim cannot hide the change.
    result = reconcile_offline(evidence)
    assert result["stages"]["table"]["verdict"] == "FAIL"
    assert result["stages"]["policy"]["verdict"] == "FAIL"


def test_canonical_hashes_ignore_order_but_preserve_assignments_and_table_values():
    _, rows = table_fixture()
    before = canonical_snapshot(ORACLE, rows)
    assert before == canonical_snapshot(list(reversed(ORACLE)), list(reversed(rows)))
    normalized = deepcopy(rows)
    normalized[-1].update(studio="9.600", oracle="9.600", delta="-0.000")
    assert before == canonical_snapshot(ORACLE, normalized)
    changed = deepcopy(ORACLE)
    changed[0]["optimizers"], changed[1]["optimizers"] = 7, 5
    after = canonical_snapshot(changed, rows)
    assert before["strings_sha256"] != after["strings_sha256"]
    assert before["table_sha256"] == after["table_sha256"]
    rows[0]["studio"] += 1
    assert before["table_sha256"] != canonical_snapshot(ORACLE, rows)["table_sha256"]


@pytest.mark.parametrize("side", ["strings", "table"])
@pytest.mark.parametrize("bad", [None, [], [None], [{"unknown": "value"}]])
def test_canonical_hashing_rejects_invalid_rows_before_producing_hashes(side, bad):
    _, rows = table_fixture()
    result = canonical_snapshot(bad if side == "strings" else ORACLE,
                                bad if side == "table" else rows)
    assert result == {"verdict": "UNREADABLE", "reason": "snapshot_rows_unreadable"}


def test_csv_ingestion_bounds_large_inputs_cells_and_rows():
    _, rows = table_fixture()
    assert compare_csv_view("x" * (MAX_CSV_CHARS + 1), rows)["reason"] == "csv_input_too_large"
    for count in (100, 1000):
        assert compare_csv_view(csv_fixture(rows * count), rows)["verdict"] == "UNREADABLE"
    oversized = deepcopy(rows)
    oversized[0]["studio"] = "1" * (MAX_CSV_CELL + 1)
    assert compare_csv_view(csv_fixture(oversized), rows)["reason"] == "csv_cell_too_large"
    assert compare_csv_view(csv_fixture(rows + [rows[0]]), rows)["reason"] == "csv_excess_rows"
    text = csv_fixture(rows).replace('"metric"', '"' + "x" * (MAX_CSV_CELL + 1) + '"', 1)
    assert compare_csv_view(text, rows)["reason"] == "csv_cell_too_large"


@pytest.mark.parametrize("frozen,run,verdict", [
    ("2026-11-15T12:00:00+01:00", "2026-11-15T11:00:00Z", "FAIL"),
    ("2026-11-15T12:00:00+01:00", "2026-11-15T11:00:01Z", "PASS"),
    ("2026-11-15T06:00:00-05:00", "2026-11-15T12:00:00+01:00", "FAIL"),
    ("0001-01-01T00:00:00+00:01", "2026-11-15T11:00:00Z", "UNREADABLE"),
    ("9999-12-31T23:59:59-00:01", "2026-11-15T11:00:00Z", "UNREADABLE"),
    ("2026-11-15T11:00:00Z", "0001-01-01T00:00:00+00:01", "UNREADABLE"),
    ("2026-11-15T11:00:00Z", "9999-12-31T23:59:59-00:01", "UNREADABLE"),
])
def test_discrepancy_timestamp_offsets_ties_and_datetime_boundaries(frozen, run, verdict):
    deltas, record = discrepancy_fixture()
    record["frozen_at"] = frozen
    result = check_discrepancies("synthetic", deltas, [record], run)
    assert result["verdict"] == verdict
    if verdict == "UNREADABLE":
        assert result["reason"] in ("discrepancy_record_unreadable", "discrepancy_evidence_unreadable")


def test_numeric_validation_preserves_caller_decimal_context_on_all_return_paths():
    from decimal import Inexact, Rounded, localcontext

    values, rows = table_fixture()
    with localcontext() as caller:
        caller.prec = 2
        caller.Emin, caller.Emax = -1, 1
        caller.traps[Inexact] = caller.traps[Rounded] = True
        caller.clear_flags()
        original = (caller.prec, caller.Emin, caller.Emax, caller.rounding,
                    dict(caller.flags), dict(caller.traps))
        assert compare_table(rows, values, values)["verdict"] == "PASS"
        for malformed in ("1.5", "NaN", True, "not a number"):
            changed = deepcopy(rows)
            changed[0]["studio"] = malformed
            assert compare_table(changed, values, values)["verdict"] == "UNREADABLE"
            assert canonical_snapshot(ORACLE, changed)["verdict"] == "UNREADABLE"
            assert compare_csv_view(csv_fixture(changed), rows)["verdict"] == "UNREADABLE"
        changed = deepcopy(rows)
        changed[0]["studio"] = 25
        assert compare_table(changed, values, values)["verdict"] == "FAIL"
        assert (caller.prec, caller.Emin, caller.Emax, caller.rounding,
                dict(caller.flags), dict(caller.traps)) == original


def test_csv_view_agreement_cannot_prove_independent_saved_values():
    values, rows = table_fixture()
    saved = dict(values, panels=25)
    assert compare_csv_view(csv_fixture(rows), rows)["verdict"] == "PASS"
    assert compare_table(rows, saved, values)["verdict"] == "FAIL"


@pytest.mark.parametrize("metric,value", [("strings", 3), ("optimizers", 13)])
def test_cross_stage_saved_string_totals_reject_mutually_consistent_export_claims(metric, value):
    evidence = complete_evidence()
    evidence["saved_values"][metric] = evidence["oracle_values"][metric] = value
    for row in evidence["view_rows"]:
        if row["metric"] == metric:
            row.update(studio=value, oracle=value)
    evidence["csv_text"] = csv_fixture(evidence["view_rows"])
    rows = {"strings": deepcopy(ORACLE), "table_rows": deepcopy(evidence["view_rows"])}
    evidence.update(before_rows=deepcopy(rows), reopened_rows=deepcopy(rows),
                    before=canonical_snapshot(ORACLE, rows["table_rows"]),
                    reopened=canonical_snapshot(ORACLE, rows["table_rows"]))
    result = reconcile_offline(evidence)
    assert result["verdict"] == "FAIL"
    assert all(stage["verdict"] == "PASS" for name, stage in result["stages"].items() if name != "strings")
    strings = result["stages"]["strings"]
    assert strings["identity"]["verdict"] == "PASS"
    assert strings["totals"]["mismatched_metrics"] == [metric]


def test_saved_totals_missing_evidence_is_unreadable():
    assert compare_saved_string_totals(ORACLE, {})["verdict"] == "UNREADABLE"
    assert compare_saved_string_totals(None, table_fixture()[0])["verdict"] == "UNREADABLE"


def test_equal_fabricated_hashes_cannot_hide_changed_canonical_snapshot_rows():
    evidence = complete_evidence()
    forged = {"strings_sha256": "a" * 64, "table_sha256": "b" * 64}
    evidence["before"] = evidence["reopened"] = forged
    evidence["reopened_rows"]["strings"][0]["optimizers"] = 7
    evidence["reopened_rows"]["strings"][1]["optimizers"] = 5
    assert compare_snapshots(forged, forged)["verdict"] == "PASS"
    result = reconcile_offline(evidence)
    assert result["verdict"] == "FAIL"
    assert result["stages"]["reopen"]["reason"] == "snapshot_hash_binding_mismatch"
    assert result["stages"]["reopen"]["unbound_phases"] == ["before", "reopened"]


@pytest.mark.parametrize("bad", [None, {}, {"strings": ORACLE}, {"strings": [], "table_rows": []}])
def test_bound_snapshots_require_both_valid_row_sets(bad):
    evidence = complete_evidence()
    assert compare_bound_snapshots(evidence["before"], evidence["reopened"],
                                   evidence["before_rows"], bad)["verdict"] == "UNREADABLE"


def test_empty_recommendations_require_explicit_absence_evidence():
    assert compare_recommendations(2, [])["reason"] == "recommendation_absence_unverified"
    for flag in (None, False, "true", 1):
        assert compare_recommendations(2, [], no_recommendations_shown=flag)["verdict"] == "UNREADABLE"
    assert compare_recommendations(2, None, no_recommendations_shown=True)["verdict"] == "UNREADABLE"
    assert compare_recommendations(2, [{"group_count": 2}], no_recommendations_shown=True)["reason"] == "recommendation_presence_conflict"
    evidence = complete_evidence()
    evidence["recommendations"] = []
    assert reconcile_offline(evidence)["verdict"] == "UNREADABLE"
    evidence["no_recommendations_shown"] = True
    assert reconcile_offline(evidence)["verdict"] == "PASS"


def test_csv_field_limit_is_isolated_and_restored_on_all_paths(monkeypatch):
    _, rows = table_fixture()
    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(1)
        assert compare_csv_view(csv_fixture(rows), rows)["verdict"] == "PASS"
        assert csv.field_size_limit() == 1
        samples = [None, "x" * (MAX_CSV_CHARS + 1), '"bad', "bad,headers\n",
                   csv_fixture(rows + [rows[0]]), csv_fixture(rows[:-1])]
        for sample in samples:
            assert compare_csv_view(sample, rows)["verdict"] == "UNREADABLE"
            assert csv.field_size_limit() == 1
        changed = deepcopy(rows)
        changed[0]["studio"] = 25
        assert compare_csv_view(csv_fixture(changed), rows)["verdict"] == "FAIL"
        assert csv.field_size_limit() == 1

        def broken_reader(*args, **kwargs):
            raise csv.Error("synthetic parser failure")

        monkeypatch.setattr(csv, "reader", broken_reader)
        assert compare_csv_view(csv_fixture(rows), rows)["verdict"] == "UNREADABLE"
        assert csv.field_size_limit() == 1
    finally:
        csv.field_size_limit(previous)


def test_large_string_diagnostics_keep_exact_counts_with_capped_deterministic_examples():
    count = 2000
    oracle = [dict(string_id=f"A{i}", optimizers=5) for i in range(count)]
    disjoint = [dict(string_id=f"B{i}", optimizers=5) for i in range(count)]
    result = compare_strings(oracle, disjoint)
    assert result["counts"] == {"missing": count, "foreign": count, "mismatched": 0}
    assert result["missing_ids"] == sorted(row["string_id"] for row in oracle)[:MAX_STRING_EXAMPLES]
    assert result["foreign_ids"] == sorted(row["string_id"] for row in disjoint)[:MAX_STRING_EXAMPLES]
    changed = [dict(row, optimizers=6) for row in oracle]
    result = compare_strings(oracle, changed)
    assert result["counts"] == {"missing": 0, "foreign": 0, "mismatched": count}
    assert len(result["mismatches"]) == MAX_STRING_EXAMPLES
    assert result == compare_strings(list(reversed(oracle)), list(reversed(changed)))


@pytest.mark.parametrize("collection", ["strings", "table_rows"])
def test_internally_valid_equal_total_snapshots_must_match_saved_evidence(collection):
    evidence = complete_evidence()
    substituted = deepcopy(evidence["before_rows"])
    if collection == "strings":
        substituted["strings"][0]["optimizers"], substituted["strings"][1]["optimizers"] = 7, 5
        assert sum(row["optimizers"] for row in substituted["strings"]) == 12
    else:
        for row in substituted["table_rows"]:
            if row["metric"] == "panels":
                row.update(studio=12, oracle=12)
            elif row["metric"] == "optimizers":
                row.update(studio=24, oracle=24)
    hashes = canonical_snapshot(substituted["strings"], substituted["table_rows"])
    assert hashes["verdict"] == "PASS"
    evidence.update(before_rows=deepcopy(substituted), reopened_rows=deepcopy(substituted),
                    before=dict(hashes), reopened=dict(hashes))
    assert compare_snapshots(evidence["before"], evidence["reopened"])["verdict"] == "PASS"
    result = reconcile_offline(evidence)
    assert result["verdict"] == "FAIL"
    assert result["stages"]["reopen"]["reason"] == "snapshot_saved_evidence_mismatch"
    assert all(stage["verdict"] == "PASS" for name, stage in result["stages"].items() if name != "reopen")


@pytest.mark.parametrize("metric,expected", [("strings", 3), ("optimizers", 13)])
def test_oracle_string_totals_cannot_contradict_approved_exported_values(metric, expected):
    evidence = complete_evidence()
    evidence["oracle_values"][metric] = expected
    for row in evidence["view_rows"]:
        if row["metric"] == metric:
            row.update(oracle=expected, delta=-1, verdict="FAIL")
    evidence["discrepancy_records"] = [dict(fixture_id="synthetic", metric=metric, delta=-1,
        name="synthetic frozen revision", frozen_at="2026-11-15T12:00:00Z")]
    evidence["csv_text"] = csv_fixture(evidence["view_rows"])
    rows = dict(strings=deepcopy(ORACLE), table_rows=deepcopy(evidence["view_rows"]))
    hashes = canonical_snapshot(rows["strings"], rows["table_rows"])
    evidence.update(before_rows=deepcopy(rows), reopened_rows=deepcopy(rows),
                    before=dict(hashes), reopened=dict(hashes))
    result = reconcile_offline(evidence)
    assert result["verdict"] == "FAIL"
    strings = result["stages"]["strings"]
    assert strings["identity"]["verdict"] == strings["totals"]["verdict"] == "PASS"
    assert strings["oracle_totals"]["reason"] == "oracle_string_totals_mismatch"
    assert strings["oracle_totals"]["mismatched_metrics"] == [metric]
    assert all(stage["verdict"] == "PASS" for name, stage in result["stages"].items() if name != "strings")
    assert compare_oracle_string_totals(None, evidence["oracle_values"])["verdict"] == "UNREADABLE"


def test_large_grouping_diagnostics_have_exact_count_and_capped_indices():
    for count in (500, 2000):
        rows = [dict(group_count=2 if index % 2 == 0 else 1) for index in range(count)]
        evidence = compare_recommendations(2, rows)
        assert evidence["verdict"] == "FAIL"
        assert evidence["mismatch_count"] == count // 2
        assert evidence["mismatched_recommendations"] == list(range(1, MAX_RECOMMENDATION_EXAMPLES * 2, 2))


@pytest.mark.parametrize("flag", [None, 0, 1, "false", "true", [], {}, [True]])
def test_nonempty_recommendation_flags_must_be_strict_booleans(flag):
    assert compare_recommendations(2, [{"group_count": 2}], no_recommendations_shown=flag) == {
        "verdict": "UNREADABLE", "reason": "recommendation_flag_unreadable"}
    evidence = complete_evidence()
    evidence["no_recommendations_shown"] = flag
    assert reconcile_offline(evidence)["verdict"] == "UNREADABLE"


@pytest.mark.parametrize("malformed_first", [False, True])
def test_concurrent_csv_calls_serialize_and_restore_global_limit_without_sleeps(monkeypatch, malformed_first):
    from threading import Event, RLock, Thread, current_thread
    from benches import x05_reconcile as module

    first_entered, second_attempt, second_entered, release_first = (Event() for _ in range(4))
    original_reader = csv.reader
    results, errors, started = {}, [], []

    class ObservedLock:
        lock = RLock()

        def __enter__(self):
            if current_thread().name == "csv-second":
                second_attempt.set()
            self.lock.acquire()
            if current_thread().name == "csv-second":
                second_entered.set()
            return self

        def __exit__(self, *args):
            self.lock.release()

    def reader(*args, **kwargs):
        assert csv.field_size_limit() == MAX_CSV_CHARS
        if current_thread().name == "csv-first":
            first_entered.set()
            assert release_first.wait(10), "test coordination did not release first parser"
        return original_reader(*args, **kwargs)

    _, rows = table_fixture()
    valid, malformed = csv_fixture(rows), '"unterminated'
    inputs = (malformed, valid) if malformed_first else (valid, malformed)

    def worker(index):
        try:
            results[index] = compare_csv_view(inputs[index], rows)["verdict"]
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(module, "_CSV_LOCK", ObservedLock())
    monkeypatch.setattr(csv, "reader", reader)
    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(1)
        first = Thread(target=worker, args=(0,), name="csv-first")
        second = Thread(target=worker, args=(1,), name="csv-second")
        first.start()
        started.append(first)
        assert first_entered.wait(10)
        second.start()
        started.append(second)
        assert second_attempt.wait(10)
        assert not second_entered.is_set()  # First parser still holds the lock.
        release_first.set()
        for thread in started:
            thread.join(10)
        assert not any(thread.is_alive() for thread in started)
        assert not errors
        assert results == ({0: "UNREADABLE", 1: "PASS"} if malformed_first else {0: "PASS", 1: "UNREADABLE"})
        assert second_entered.is_set() and csv.field_size_limit() == 1
    finally:
        release_first.set()
        for thread in started:
            thread.join(10)
        csv.field_size_limit(previous)


@pytest.mark.parametrize("case", ["PASS", "FAIL", "UNREADABLE"])
def test_complete_evidence_permutations_preserve_diagnostics_and_caller_inputs(case):
    from random import Random

    baseline = complete_evidence()
    baseline["recommendations"] = [{"group_count": 2 if case == "PASS" else 1}] * 2
    baseline["discrepancy_records"] = [dict(fixture_id="synthetic", metric=metric, delta=0,
        name="synthetic zero revision", frozen_at="2026-11-15T12:00:00Z") for metric in ("panels", "strings")]
    if case == "UNREADABLE":
        del baseline["reopened_rows"]
    expected = reconcile_offline(baseline)
    assert expected["verdict"] == case
    for seed in range(8):
        rng = Random(seed)
        changed = deepcopy(baseline)
        for key in ("oracle_strings", "saved_strings", "view_rows", "recommendations", "discrepancy_records"):
            rng.shuffle(changed[key])
        for key in ("before_rows", "reopened_rows"):
            if key in changed:
                rng.shuffle(changed[key]["strings"])
                rng.shuffle(changed[key]["table_rows"])
                changed[key] = dict(reversed(list(changed[key].items())))
        for key in ("saved_values", "oracle_values", "before", "reopened"):
            items = list(changed[key].items())
            rng.shuffle(items)
            changed[key] = dict(items)
        csv_rows, columns = deepcopy(changed["view_rows"]), list(TABLE_COLUMNS)
        rng.shuffle(csv_rows)
        rng.shuffle(columns)
        changed["csv_text"] = csv_fixture(csv_rows, columns)
        changed = dict(reversed(list(changed.items())))
        preserved = deepcopy(changed)
        assert reconcile_offline(changed) == expected
        assert changed == preserved
