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
    aggregate_fixtures, decode_evidence_json, reconcile_json,
    aggregate_fixtures_json, MAX_JSON_CHARS, MAX_JSON_DEPTH, serialize_result,
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


def fixture_records():
    expected = ["case-a", "case-b", "case-c"]
    rows = []
    for key in expected:
        evidence = complete_evidence()
        evidence["fixture_id"] = key
        rows.append(dict(fixture_id=key, evidence=evidence))
    return expected, rows


def test_three_fixture_aggregation_requires_coverage_and_unreadable_precedes_fail():
    expected, rows = fixture_records()
    assert aggregate_fixtures(expected, rows)["verdict"] == "PASS"
    rows[0]["evidence"]["recommendations"][0]["group_count"] = 1
    assert aggregate_fixtures(expected, rows)["verdict"] == "FAIL"
    del rows[1]["evidence"]["reopened"]
    result = aggregate_fixtures(expected, rows)
    assert result["verdict"] == "UNREADABLE"
    assert result["fixtures"]["case-a"]["verdict"] == "FAIL"
    assert result == aggregate_fixtures(list(reversed(expected)), list(reversed(rows)))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign", "unbound", "expected-missing", "expected-duplicate"])
def test_fixture_aggregator_rejects_missing_duplicate_foreign_and_unbound_ids(mutation):
    expected, rows = fixture_records()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1] = rows[0]
    elif mutation == "foreign":
        rows[-1]["fixture_id"] = "foreign"
    elif mutation == "unbound":
        rows[-1]["evidence"]["fixture_id"] = "case-a"
    elif mutation == "expected-missing":
        expected = None
    else:
        expected[-1] = expected[0]
    assert aggregate_fixtures(expected, rows)["verdict"] == "UNREADABLE"


@pytest.mark.parametrize("text,reason", [
    ('{"a":1,"a":2}', "json_duplicate_key"),
    ('{"nested":{"a":1,"a":2}}', "json_duplicate_key"),
    ('{"number":NaN}', "json_nonfinite_number"),
    ('{"number":Infinity}', "json_nonfinite_number"),
    ('{"number":-Infinity}', "json_nonfinite_number"),
    ('{"missing":', "json_malformed"),
    ('{} trailing', "json_malformed"),
    ('[]', "json_evidence_unreadable"),
    (None, "json_input_unreadable"),
])
def test_strict_json_refuses_invalid_evidence_before_reconciliation(monkeypatch, text, reason):
    from benches import x05_reconcile as module

    def forbidden(evidence):
        pytest.fail("Invalid JSON must not reach reconciliation")

    monkeypatch.setattr(module, "reconcile_offline", forbidden)
    assert reconcile_json(text) == {"verdict": "UNREADABLE", "reason": reason}


def test_strict_json_valid_evidence_reconciles_and_keeps_exact_decimal():
    from decimal import Decimal

    assert decode_evidence_json('{"value":0.123456789012345678901}')['evidence']["value"] == Decimal("0.123456789012345678901")
    assert reconcile_json(json.dumps(complete_evidence()))["verdict"] == "PASS"


def test_equivalent_numeric_table_values_have_identical_snapshot_hashes():
    from decimal import Decimal

    _, baseline = table_fixture()
    for row in baseline:
        row.update(studio=4, oracle=4, delta=0)
    expected = canonical_snapshot(ORACLE, baseline)
    for equivalent, zero in [(4, 0), (4.0, -0.0), (Decimal("4.000"), Decimal("-0.000")),
                             ("4.0000", "-0.0000")]:
        rows = deepcopy(baseline)
        for row in rows:
            row.update(studio=equivalent, oracle=equivalent, delta=zero)
        assert canonical_snapshot(ORACLE, rows) == expected
    changed = deepcopy(baseline)
    changed[-1]["studio"] = "4.0001"
    assert canonical_snapshot(ORACLE, changed)["table_sha256"] != expected["table_sha256"]


@pytest.mark.parametrize("width,verdict", [(80, "PASS"), (81, "UNREADABLE")])
def test_exact_numeric_width_boundaries_across_table_policy_and_snapshot(width, verdict):
    values, rows = table_fixture()
    number = "1" + "0" * (width - 1)
    values["panels"] = number
    rows[0].update(studio=number, oracle=0, delta=number, verdict="FAIL")
    oracle = dict(values, panels=0)
    assert compare_table(rows, values, oracle)["verdict"] == verdict
    assert canonical_snapshot(ORACLE, rows)["verdict"] == verdict
    deltas = dict.fromkeys(METRICS, 0)
    deltas["panels"] = number
    record = dict(fixture_id="synthetic", metric="panels", delta=number,
                  name="synthetic numeric boundary", frozen_at="2026-11-15T12:00:00Z")
    assert check_discrepancies("synthetic", deltas, [record], "2026-11-15T13:00:00Z")["verdict"] == verdict


def test_tiny_nonzero_kw_delta_remains_exact_and_never_becomes_zero():
    from decimal import Decimal

    tiny = "0." + "0" * 77 + "1"  # Exactly80 characters.
    assert len(tiny) == 80
    values, rows = table_fixture()
    values["kw_dc"] = tiny
    rows[-1].update(studio=tiny, oracle=0, delta=Decimal(tiny), verdict="FAIL")
    assert compare_table(rows, values, dict(values, kw_dc=0))["verdict"] == "PASS"
    assert canonical_snapshot(ORACLE, rows)["verdict"] == "PASS"
    deltas = dict.fromkeys(METRICS, 0)
    deltas["kw_dc"] = Decimal(tiny)
    assert check_discrepancies("synthetic", deltas, [], "2026-11-15T13:00:00Z")["verdict"] == "FAIL"
    record = dict(fixture_id="synthetic", metric="kw_dc", delta=tiny,
                  name="synthetic tiny delta", frozen_at="2026-11-15T12:00:00Z")
    assert check_discrepancies("synthetic", deltas, [record], "2026-11-15T13:00:00Z")["verdict"] == "PASS"
    rows[-1]["delta"] = 0
    assert compare_table(rows, values, dict(values, kw_dc=0))["verdict"] == "FAIL"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_csv_newline_round_trips_and_quoted_embedded_newlines(newline):
    _, rows = table_fixture()
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=TABLE_COLUMNS, lineterminator=newline, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    original = csv.field_size_limit()
    try:
        csv.field_size_limit(1)
        assert compare_csv_view(output.getvalue(), rows)["verdict"] == "PASS"
        assert csv.field_size_limit() == 1
        malformed = output.getvalue().replace('"panels"', '"pan' + newline + 'els"', 1)
        assert compare_csv_view(malformed, rows)["verdict"] == "UNREADABLE"
        malformed = output.getvalue().replace('"24"', '"2' + newline + '4"', 1)
        assert compare_csv_view(malformed, rows)["verdict"] == "UNREADABLE"
        assert csv.field_size_limit() == 1
    finally:
        csv.field_size_limit(original)


def test_strict_aggregate_json_composes_three_fixtures_and_precedence():
    expected, fixtures = fixture_records()
    envelope = dict(expected_ids=expected, fixtures=fixtures)
    assert aggregate_fixtures_json(json.dumps(envelope))["verdict"] == "PASS"
    fixtures[0]["evidence"]["recommendations"][0]["group_count"] = 1
    assert aggregate_fixtures_json(json.dumps(envelope))["verdict"] == "FAIL"
    del fixtures[1]["evidence"]["reopened"]
    assert aggregate_fixtures_json(json.dumps(envelope))["verdict"] == "UNREADABLE"
    assert aggregate_fixtures_json('{"expected_ids":[],"expected_ids":[],"fixtures":[]}')["reason"] == "json_duplicate_key"
    assert aggregate_fixtures_json('{}')["reason"] == "aggregate_envelope_unreadable"


def test_exact_json_length_and_nesting_boundaries_before_reconciliation(monkeypatch):
    from benches import x05_reconcile as module

    base = json.dumps({"pad": ""}, separators=(",", ":"))
    supported = base.replace('""', '"' + "x" * (MAX_JSON_CHARS - len(base)) + '"')
    assert len(supported) == MAX_JSON_CHARS
    assert decode_evidence_json(supported)["verdict"] == "PASS"
    nested = '{"pad":' + "[" * (MAX_JSON_DEPTH - 1) + "0" + "]" * (MAX_JSON_DEPTH - 1) + "}"
    assert decode_evidence_json(nested)["verdict"] == "PASS"

    def forbidden(evidence):
        pytest.fail("Unsupported JSON must not reach reconciliation")

    monkeypatch.setattr(module, "reconcile_offline", forbidden)
    assert reconcile_json(supported + " ")["reason"] == "json_input_too_large"
    assert reconcile_json('{"pad":' + "[" * MAX_JSON_DEPTH + "0" + "]" * MAX_JSON_DEPTH + "}")["reason"] == "json_nesting_too_deep"
    # Brackets and escaped quotes inside a string do not consume nesting.
    assert decode_evidence_json(json.dumps({"pad": '[\\\"{}]' * 100}))["verdict"] == "PASS"


def test_lossless_result_serializer_round_trips_exact_decimal_and_large_counts():
    from decimal import Decimal

    evidence = dict(tiny=Decimal("1E-78"), integral=Decimal("12345"),
                    negative_zero=Decimal("-0"), count=10**70, nested=[Decimal("9.600")])
    decoded = decode_evidence_json(serialize_result(evidence))["evidence"]
    assert decoded == evidence
    for key in ("tiny", "integral", "negative_zero"):
        assert decoded[key].as_tuple() == evidence[key].as_tuple()
    assert decoded["nested"][0].as_tuple() == evidence["nested"][0].as_tuple()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_result_serializer_rejects_nonfinite_output(value):
    from decimal import Decimal

    with pytest.raises(ValueError):
        serialize_result({"value": value})
    with pytest.raises(ValueError):
        serialize_result({"value": Decimal(str(value))})


@pytest.mark.parametrize("case,exit_code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4), ("malformed", 4)])
def test_offline_stdin_cli_emits_one_sanitized_result(tmp_path, case, exit_code):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module

    evidence = complete_evidence()
    if case == "FAIL":
        evidence["recommendations"][0]["group_count"] = 1
    elif case == "UNREADABLE":
        del evidence["reopened"]
    text = '{"synthetic_private_canary":' if case == "malformed" else json.dumps(evidence)
    result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve())],
        input=text, text=True, encoding="utf-8", capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == exit_code
    assert len(result.stdout.splitlines()) == 1 and result.stderr == ""
    assert json.loads(result.stdout)["verdict"] == ("UNREADABLE" if case == "malformed" else case)
    assert "synthetic_private_canary" not in result.stdout


def test_offline_aggregate_cli_accepts_one_explicit_envelope(tmp_path):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module

    expected, fixtures = fixture_records()
    result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--aggregate"],
        input=json.dumps(dict(expected_ids=expected, fixtures=fixtures)), text=True, encoding="utf-8",
        capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["verdict"] == "PASS" and len(result.stdout.splitlines()) == 1


def test_canonical_snapshot_literal_compatibility_vectors():
    # Independent reference bytes, not generated by the production serializer.
    strings_bytes = rb'[["\u00c5/\u7532",5],["\u03b2",7]]'
    table_bytes = b'[["panels","24","24","0","PASS"],["optimizers","12","12","0","PASS"],["inverters","1","1","0","PASS"],["strings","2","2","0","PASS"],["kw_dc","9.6","9.6","0","PASS"]]'
    strings_digest = "711f41979914629b8d9d0a4b9996cd97fb805d667533298f98f5c89c0368e70d"
    table_digest = "5694f6c3f6fb0736fdb05ddae835185570106f7fe0c57543a4312af71aa27dec"
    empty_digest = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    assert sha256(strings_bytes).hexdigest() == strings_digest
    assert sha256(table_bytes).hexdigest() == table_digest
    assert sha256(b"[]").hexdigest() == empty_digest
    _, rows = table_fixture()
    result = canonical_snapshot([dict(string_id="\u00c5/\u7532", optimizers=5),
                                 dict(string_id="\u03b2", optimizers=7)], rows)
    assert result["strings_sha256"] == strings_digest and result["table_sha256"] == table_digest
    for strings in ([], [dict(string_id="", optimizers=5)]):
        invalid = canonical_snapshot(strings, rows)
        assert invalid == {"verdict": "UNREADABLE", "reason": "snapshot_rows_unreadable"}
        assert empty_digest not in invalid.values()  # Absence never acquires a valid snapshot hash.


@pytest.mark.parametrize("slashes", range(8))
def test_json_preflight_handles_escaped_quotes_braces_and_backslash_parity(slashes):
    value = "\\" * slashes + '"' + "}][{" * 20
    inner = json.dumps({"value": value})
    supported = '{"nested":' + "[" * (MAX_JSON_DEPTH - 2) + inner + "]" * (MAX_JSON_DEPTH - 2) + "}"
    decoded = decode_evidence_json(supported)
    assert decoded["verdict"] == "PASS"
    observed = decoded["evidence"]["nested"]
    for _ in range(MAX_JSON_DEPTH - 2):
        observed = observed[0]
    assert observed["value"] == value
    unsupported = '{"nested":' + "[" * (MAX_JSON_DEPTH - 1) + inner + "]" * (MAX_JSON_DEPTH - 1) + "}"
    assert decode_evidence_json(unsupported)["reason"] == "json_nesting_too_deep"
    raw = '{"value":"' + "\\" * slashes + '"}'
    parity = decode_evidence_json(raw)
    assert parity["verdict"] == ("PASS" if slashes % 2 == 0 else "UNREADABLE")
    if slashes % 2:
        assert parity["reason"] == "json_malformed"


@pytest.mark.parametrize("kind", ["list", "dict", "mixed"])
def test_serializer_cycles_have_bounded_named_errors(kind):
    if kind == "list":
        value = []
        value.append(value)
    elif kind == "dict":
        value = {}
        value["self"] = value
    else:
        value = []
        value.append({"self": value})
    with pytest.raises(ValueError, match="json_result_too_deep") as error:
        serialize_result(value)
    assert len(error.traceback) < MAX_JSON_DEPTH * 4 + 20


def test_serializer_repeated_acyclic_children_are_lossless_and_unchanged():
    from decimal import Decimal

    child = {"count": 10**60, "tiny": Decimal("1E-78"), "nested": ["synthetic"]}
    value = {"first": child, "second": child}
    preserved = deepcopy(value)
    decoded = decode_evidence_json(serialize_result(value))["evidence"]
    assert decoded["first"] == decoded["second"] == child
    assert value == preserved and value["first"] is value["second"]


@pytest.mark.parametrize("aggregate", [False, True])
@pytest.mark.parametrize("invalid", [b"\xff", b"\xe2\x82"])
def test_cli_invalid_utf8_never_echoes_input_or_traceback(tmp_path, aggregate, invalid):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module

    evidence = complete_evidence()
    evidence["notes"] = "synthetic_private_marker_PLACEHOLDER"
    if aggregate:
        expected, fixtures = fixture_records()
        fixtures[0]["evidence"]["notes"] = evidence["notes"]
        payload = dict(expected_ids=expected, fixtures=fixtures)
    else:
        payload = evidence
    raw = json.dumps(payload).encode("utf-8").replace(b"PLACEHOLDER", invalid)
    command = [sys.executable, "-P", str(Path(module.__file__).resolve())]
    if aggregate:
        command.append("--aggregate")
    result = subprocess.run(command, input=raw, capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == 4 and result.stderr == b""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {"verdict": "UNREADABLE", "reason": "cli_evidence_unreadable"}
    assert b"synthetic_private_marker" not in result.stdout and b"Traceback" not in result.stdout


REQUIRED_STAGES = {
    "oracle_strings": {"strings"}, "saved_strings": {"strings", "reopen"},
    "saved_values": {"strings", "table", "policy"}, "oracle_values": {"strings", "table", "policy"},
    "view_rows": {"table", "reopen", "csv_view"}, "csv_text": {"csv_view"},
    "report_grid_count": {"grouping"}, "recommendations": {"grouping"},
    "fixture_id": {"policy"}, "discrepancy_records": {"policy"}, "run_started_at": {"policy"},
    "before": {"reopen"}, "reopened": {"reopen"}, "before_rows": {"reopen"}, "reopened_rows": {"reopen"},
}


@pytest.mark.parametrize("key", REQUIRED_STAGES)
def test_required_field_deletion_retains_every_unaffected_stage(key):
    evidence = complete_evidence()
    assert set(evidence) == set(REQUIRED_STAGES)
    complete = reconcile_offline(evidence)
    del evidence[key]
    result = reconcile_offline(evidence)
    assert result["verdict"] == "UNREADABLE"
    for stage, observation in result["stages"].items():
        if stage in REQUIRED_STAGES[key]:
            assert observation == {"verdict": "UNREADABLE", "reason": "stage_evidence_missing"}
        else:
            assert observation == complete["stages"][stage]


def test_aggregate_isolates_readable_failure_missing_evidence_and_complete_fixture():
    expected, fixtures = fixture_records()
    fixtures[0]["evidence"]["recommendations"][0]["group_count"] = 1
    del fixtures[1]["evidence"]["csv_text"]
    del fixtures[1]["evidence"]["reopened_rows"]
    preserved = deepcopy(fixtures)
    result = aggregate_fixtures(expected, fixtures)
    assert result["verdict"] == "UNREADABLE"
    assert {key: item["verdict"] for key, item in result["fixtures"].items()} == {
        "case-a": "FAIL", "case-b": "UNREADABLE", "case-c": "PASS"}
    incomplete = result["fixtures"]["case-b"]["stages"]
    assert incomplete["csv_view"]["reason"] == incomplete["reopen"]["reason"] == "stage_evidence_missing"
    assert incomplete["table"]["verdict"] == incomplete["strings"]["verdict"] == "PASS"
    assert fixtures == preserved
