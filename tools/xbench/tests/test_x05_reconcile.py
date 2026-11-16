"""Synthetic evidence for X05's offline comparator, no transport or real input."""

from itertools import combinations, permutations
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
    PROVENANCE_STAGES, validate_fixture_provenance,
    IDENTITY_STAGES, validate_saved_design_identity, verify_input_bytes, reconcile_with_provenance,
    reconcile_assured, aggregate_provenance, reconcile_provenance_json,
    reconcile_assurance_json, aggregate_provenance_json,
    aggregate_assurance, aggregate_assurance_json, MAX_INPUT_BYTES, MAX_ENVELOPE_EXAMPLES,
    MAX_RESULT_CHARS, compare_grid_correspondence, reconcile_seven_steps,
    reconcile_seven_steps_json,
    aggregate_seven_steps, validate_import_grid_provenance,
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


def assurance_records():
    expected = ["case-a", "case-b", "case-c"]
    records = []
    for key in expected:
        envelope = assurance_envelope()
        envelope["evidence"]["fixture_id"] = envelope["manifest"]["fixture_id"] = key
        for binding in envelope["bindings"].values():
            binding["fixture_id"] = key
        records.append(dict(envelope, fixture_id=key))
    return expected, records


@pytest.mark.parametrize("mode", ["--assurance", "--provenance-aggregate"])
@pytest.mark.parametrize("kind,exit_code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4), ("malformed", 4)])
def test_new_cli_modes_verdicts_and_sanitized_errors(tmp_path, mode, kind, exit_code):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module
    if mode == "--assurance":
        payload = assurance_envelope()
        target = payload["evidence"]
    else:
        expected, records = provenance_records()
        payload = dict(expected_ids=expected, fixtures=records)
        target = records[0]["evidence"]
    if kind == "FAIL":
        target["recommendations"][0]["group_count"] = 1
    elif kind == "UNREADABLE":
        del target["saved_strings"]
    elif kind == "malformed":
        payload = {"synthetic_private_canary": True}
    completed = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), mode],
        input=json.dumps(payload).encode(), capture_output=True, cwd=tmp_path, timeout=20)
    assert completed.returncode == exit_code and completed.stderr == b""
    assert len(completed.stdout.splitlines()) == 1 and b"synthetic_private_canary" not in completed.stdout
    observed = json.loads(completed.stdout)
    assert observed["verdict"] == ("UNREADABLE" if kind == "malformed" else kind)
    if mode == "--provenance-aggregate" and kind == "UNREADABLE":
        assert observed["fixtures"]["case-b"]["verdict"] == observed["fixtures"]["case-c"]["verdict"] == "PASS"


@pytest.mark.parametrize("left,right", list(combinations(
    ["--aggregate", "--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate", "--seven-step"], 2)))
def test_cli_modes_are_pairwise_exclusive(tmp_path, left, right):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    completed = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), left, right],
        input=b"synthetic", capture_output=True, cwd=tmp_path, timeout=20)
    assert completed.returncode == 2 and completed.stdout == b""
    assert b"not allowed with argument" in completed.stderr and b"Traceback" not in completed.stderr


def test_assurance_aggregate_strict_coverage_and_sibling_preservation():
    expected, records = assurance_records()
    payload = dict(expected_ids=expected, fixtures=records)
    baseline = aggregate_assurance_json(json.dumps(payload))
    assert baseline["verdict"] == "PASS"
    records[0]["inputs_hex"]["report"] = b"substitution".hex()
    records[1]["evidence"]["recommendations"][0]["group_count"] = 1
    observed = aggregate_assurance(expected, records)
    assert observed["verdict"] == "UNREADABLE"
    assert [observed["fixtures"][key]["verdict"] for key in expected] == ["UNREADABLE", "FAIL", "PASS"]
    assert observed["fixtures"][expected[2]] == baseline["fixtures"][expected[2]]
    for changed in (records[:2], [records[0]] * 3):
        assert aggregate_assurance(expected, changed)["verdict"] == "UNREADABLE"
    records[0]["fixture_id"] = "foreign"
    assert aggregate_assurance(expected, records)["reason"] == "foreign_fixture_id"
    assert aggregate_assurance_json('{}')["reason"] == "assurance_aggregate_envelope_unreadable"
    assert aggregate_assurance_json('{"fixtures":[],"fixtures":[]}')["reason"] == "json_duplicate_key"


@pytest.mark.parametrize("role", ["dwg", "report", "bom"])
def test_hex_exact_limit_rejects_excess_before_allocation(role, monkeypatch):
    from benches import x05_reconcile as module
    envelope = assurance_envelope()
    payload = b"x" * MAX_INPUT_BYTES
    envelope["inputs_hex"][role] = payload.hex()
    envelope["manifest"]["inputs"][role] = sha256(payload).hexdigest()
    for binding in envelope["bindings"].values():
        binding["inputs"][role] = sha256(payload).hexdigest()
    assert reconcile_assurance_json(json.dumps(envelope))["verdict"] == "PASS"
    envelope["inputs_hex"][role] += "00"
    preserved = deepcopy(envelope)
    class ForbiddenBytes:
        @staticmethod
        def fromhex(value):
            pytest.fail("Oversized input must reject before allocating any bytes")
    monkeypatch.setattr(module, "bytes", ForbiddenBytes, raising=False)
    assert reconcile_assurance_json(json.dumps(envelope)) == {
        "verdict": "UNREADABLE", "reason": "input_hex_too_large"}
    assert envelope == preserved


@pytest.mark.parametrize("assured", [False, True])
def test_large_malformed_fixture_diagnostics_are_exact_bounded_and_keep_siblings(assured):
    expected, records = assurance_records() if assured else provenance_records()
    aggregate = aggregate_assurance if assured else aggregate_provenance
    baseline = aggregate(expected, records)
    records += [{"fixture_id": "synthetic_private_canary"}] * 1000 + [None] * 1000
    preserved = deepcopy(records)
    first = aggregate(expected, records)
    assert first["verdict"] == "UNREADABLE" and first["envelope_error_count"] == 2000
    assert len(first["envelope_errors"]) == MAX_ENVELOPE_EXAMPLES
    assert [item["index"] for item in first["envelope_errors"]] == list(range(3, 3 + MAX_ENVELOPE_EXAMPLES))
    assert first["fixtures"] == baseline["fixtures"]
    assert "synthetic_private_canary" not in serialize_result(first)
    assert first == aggregate(expected, records) and records == preserved


def test_provenance_cli_matches_direct_json_generated_mutation_corpus(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    manifest, bindings, _ = provenance_fixture()
    base = dict(evidence=complete_evidence(), manifest=manifest, bindings=bindings)
    corpus = [base]
    for field in base:
        altered = deepcopy(base)
        del altered[field]
        corpus.append(altered)
    for field in complete_evidence():
        altered = deepcopy(base)
        del altered["evidence"][field]
        corpus.append(altered)
    altered = deepcopy(base)
    altered["evidence"]["recommendations"][0]["group_count"] = 1
    corpus.append(altered)
    for payload in corpus:
        text = json.dumps(payload)
        direct = reconcile_provenance_json(text)
        completed = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--provenance"],
            input=text.encode(), capture_output=True, cwd=tmp_path, timeout=20)
        assert completed.returncode == {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[direct["verdict"]]
        assert completed.stderr == b"" and json.loads(completed.stdout) == json.loads(serialize_result(direct))


@pytest.mark.parametrize("kind,exit_code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4)])
def test_assurance_aggregate_cli_preserves_readable_siblings(tmp_path, kind, exit_code):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    expected, records = assurance_records()
    if kind != "PASS":
        records[1]["evidence"]["recommendations"][0]["group_count"] = 1
    if kind == "UNREADABLE":
        records[2]["inputs_hex"]["report"] = b"substitution".hex()
    result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--assurance-aggregate"],
        input=json.dumps(dict(expected_ids=expected, fixtures=records)).encode(),
        capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == exit_code and not result.stderr
    observed = json.loads(result.stdout)
    assert observed["verdict"] == kind
    assert observed["fixtures"][expected[0]]["verdict"] == "PASS"
    if kind != "PASS":
        assert observed["fixtures"][expected[1]]["verdict"] == "FAIL"


@pytest.mark.parametrize("mode", ["--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate"])
@pytest.mark.parametrize("raw", [b"\xffsynthetic_private_canary", b"\xf0\x9f\x99"])
def test_new_modes_invalid_utf8_always_one_sanitized_result(tmp_path, mode, raw):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), mode],
        input=raw, capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == 4 and not result.stderr
    assert json.loads(result.stdout) == {"verdict": "UNREADABLE", "reason": "cli_evidence_unreadable"}
    assert len(result.stdout.splitlines()) == 1


@pytest.mark.parametrize("mode", ["--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate"])
def test_new_modes_read_error_is_sanitized(monkeypatch, mode):
    import sys
    from types import SimpleNamespace
    from benches import x05_reconcile as module
    class BrokenInput:
        def read(self, limit):
            raise OSError("synthetic_private_canary")
    output, errors = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BrokenInput()))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    assert module.main([mode]) == 4 and not errors.getvalue()
    assert json.loads(output.getvalue()) == {"verdict": "UNREADABLE", "reason": "cli_evidence_unreadable"}
    assert len(output.getvalue().splitlines()) == 1


@pytest.mark.parametrize("kind", ["clean", "bytes", "identity", "grouping", "all"])
def test_assurance_aggregate_direct_and_json_diagnostics_equal(kind):
    expected, records = assurance_records()
    if kind in ("bytes", "all"):
        records[0]["inputs_hex"]["dwg"] = b"substituted".hex()
    if kind in ("identity", "all"):
        records[1]["stage_identities"]["csv"]["version_id"] = "foreign"
    if kind in ("grouping", "all"):
        records[2]["evidence"]["recommendations"][0]["group_count"] = 1
    preserved = deepcopy((expected, records))
    direct = aggregate_assurance(expected, records)
    decoded = aggregate_assurance_json(json.dumps(dict(expected_ids=expected, fixtures=records)))
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(decoded))
    assert (expected, records) == preserved


@pytest.mark.parametrize("manifest_bom,bytes_bom,binding_bom", [
    (a, b, c) for a in (False, True) for b in (False, True) for c in (False, True)])
def test_optional_bom_presence_requires_complete_agreement(manifest_bom, bytes_bom, binding_bom):
    envelope = assurance_envelope()
    if not manifest_bom:
        del envelope["manifest"]["inputs"]["bom"]
    if not bytes_bom:
        del envelope["inputs_hex"]["bom"]
    if not binding_bom:
        for binding in envelope["bindings"].values():
            del binding["inputs"]["bom"]
    preserved = deepcopy(envelope)
    result = reconcile_assurance_json(json.dumps(envelope))
    assert result["verdict"] == ("PASS" if manifest_bom == bytes_bom == binding_bom else "UNREADABLE")
    if not manifest_bom and bytes_bom:
        assert result["reason"] == "input_hex_roles_unreadable"
        assert envelope == preserved
        return
    checks = result["checks"]
    assert checks["input_bytes"]["reason"] == ("supplied_input_bytes_equal" if manifest_bom == bytes_bom else "input_bytes_missing")
    assert checks["provenance"]["provenance"]["reason"] == ("supplied_provenance_equal" if manifest_bom == binding_bom else "stage_binding_conflict")
    assert checks["provenance"]["reconciliation"]["verdict"] == "PASS"
    assert envelope == preserved


def test_provenance_aggregate_subprocess_bytes_deterministic_with_simultaneous_conflicts(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    expected, records = provenance_records()
    records[0]["bindings"]["strings"]["inputs"]["report"] = "d" * 64
    records[0]["bindings"]["table"]["inputs"]["dwg"] = "e" * 64
    records[1]["evidence"]["recommendations"][0]["group_count"] = 1
    outputs = []
    for seed in ("0", "73"):
        for reverse in (False, True):
            changed = deepcopy(records)
            if reverse:
                changed.reverse()
                for row in changed:
                    row["manifest"]["inputs"] = dict(reversed(list(row["manifest"]["inputs"].items())))
                    row["bindings"] = dict(reversed(list(row["bindings"].items())))
                    for binding in row["bindings"].values():
                        binding["inputs"] = dict(reversed(list(binding["inputs"].items())))
            payload = dict(expected_ids=list(reversed(expected)) if reverse else expected, fixtures=changed)
            result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--provenance-aggregate"],
                input=json.dumps(payload).encode(), capture_output=True, cwd=tmp_path, timeout=20,
                env={"PYTHONHASHSEED": seed, "SystemRoot": "C:\\Windows"})
            assert result.returncode == 4 and not result.stderr
            outputs.append(result.stdout)
    assert len(set(outputs)) == 1


def seven_step_envelope():
    return {"import": dict(normal_user=True, dwg_imported=True, report_imported=True),
            "grid": dict(expected_grid_ids=["g"], saved_group_ids=["p"],
                         rows=[dict(grid_id="g", matches=["p"], cause=None)], require_all_matched=True),
            "assurance": assurance_envelope()}


def test_strict_seven_step_json_envelope_limits_and_diagnostics():
    envelope = seven_step_envelope()
    assert reconcile_seven_steps_json(json.dumps(envelope))["verdict"] == "PASS"
    for key in envelope:
        changed = deepcopy(envelope)
        del changed[key]
        assert reconcile_seven_steps_json(json.dumps(changed))["reason"] == "seven_step_envelope_unreadable"
    assert reconcile_seven_steps_json('{"import":{},"\\u0069mport":{}}')["reason"] == "json_duplicate_key"
    assert reconcile_seven_steps_json(" " * (MAX_JSON_CHARS + 1))["reason"] == "json_input_too_large"
    envelope["assurance"]["evidence"]["recommendations"][0]["group_count"] = 1
    envelope["import"] = None
    observed = reconcile_seven_steps_json(json.dumps(envelope))
    assert observed["verdict"] == "UNREADABLE"
    assert observed["steps"]["4"]["verdict"] == "FAIL" and observed["steps"]["2"]["verdict"] == "PASS"


def test_grid_index_lookup_work_is_linear_without_timing_thresholds(monkeypatch):
    from benches import x05_reconcile as module
    builtin_set = set
    class CountedSet(builtin_set):
        lookups = 0
        def __contains__(self, key):
            type(self).lookups += 1
            return super().__contains__(key)
    monkeypatch.setattr(module, "set", CountedSet, raising=False)
    totals = []
    for size in (1000, 2000):
        expected = ["g" + str(index) for index in range(size)]
        groups = ["p" + str(index) for index in range(size)]
        rows = [dict(grid_id=key, matches=[groups[index]], cause=None) for index, key in enumerate(expected)]
        CountedSet.lookups = 0
        assert compare_grid_correspondence(expected, groups, rows)["verdict"] == "PASS"
        totals.append(CountedSet.lookups)
    assert totals[0] > 0 and totals[1] == 2 * totals[0] and totals[1] <= 6 * 2000


def test_grid_error_examples_are_deterministic_beyond_cap_across_permutations():
    expected = ["g1", "g2", "missing"]
    groups = ["p1", "p2"]
    rows = [dict(grid_id="g1", matches=["p1", "foreign"], cause=None),
            dict(grid_id="g1", matches=["p1"], cause=None),
            dict(grid_id="g2", matches=[], cause="SOLAREDGE_GRID_UNMATCHED"),
            dict(grid_id="foreign", matches=["p1", "foreign"], cause=None)] * 4
    baseline = compare_grid_correspondence(expected, groups, rows, require_all_matched=True)
    assert baseline["verdict"] == "FAIL" and baseline["error_count"] > 10 and len(baseline["errors"]) == 10
    preserved = deepcopy(rows)
    for ids in permutations(expected):
        for reverse in (False, True):
            changed = list(reversed(rows)) if reverse else rows[3:] + rows[:3]
            assert compare_grid_correspondence(list(ids), list(reversed(groups)), changed, require_all_matched=True) == baseline
    assert rows == preserved


@pytest.mark.parametrize("field", ["normal_user", "dwg_imported", "report_imported"])
def test_seven_step_import_flag_mutations_preserve_other_steps(field):
    envelope = seven_step_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    for value in (False, None, 0, "true", [], {}):
        changed = deepcopy(envelope)
        changed["import"][field] = value
        observed = reconcile_seven_steps_json(json.dumps(changed))
        assert observed["verdict"] == ("FAIL" if value is False else "UNREADABLE")
        assert observed["steps"]["1"]["verdict"] == observed["verdict"]
        for step in "234567":
            assert observed["steps"][step] == baseline["steps"][step]
        assert observed["assurance"] == baseline["assurance"]
    for altered in ({key: value for key, value in envelope["import"].items() if key != field},
                    dict(envelope["import"], extra=True)):
        changed = dict(envelope, **{"import": altered})
        assert reconcile_seven_steps_json(json.dumps(changed))["steps"]["1"]["reason"] == "import_evidence_unreadable"


@pytest.mark.parametrize("identifier,valid", [("a" * 128, True), ("a" * 129, False),
    ("\U0001f642" * 128, True), ("\U0001f642" * 129, False), (" \t", False)],
    ids=["limit", "excess", "multibyte-limit", "multibyte-excess", "blank"])
def test_grid_identity_boundaries_and_inputs(identifier, valid):
    expected, groups = [identifier], [identifier]
    rows = [dict(grid_id=identifier, matches=[identifier], cause=None)]
    preserved = deepcopy((expected, groups, rows))
    result = compare_grid_correspondence(expected, groups, rows)
    assert result["verdict"] == ("PASS" if valid else "UNREADABLE")
    assert (expected, groups, rows) == preserved
    if not valid:
        for row in (dict(grid_id=identifier, matches=["p"], cause=None),
                    dict(grid_id="g", matches=[identifier], cause=None)):
            assert compare_grid_correspondence(["g"], ["p"], [row])["reason"] == "grid_row_unreadable"


def test_grid_exact_unicode_identity_and_repeated_assignments():
    result = compare_grid_correspondence(["\u00e9"], ["p"], [dict(grid_id="e\u0301", matches=["p"], cause=None)])
    assert result["verdict"] == "FAIL" and result["error_count"] == 2
    rows = [dict(grid_id="g1", matches=["p"], cause=None), dict(grid_id="g2", matches=["p"], cause=None)]
    result = compare_grid_correspondence(["g1", "g2"], ["p"], rows)
    assert result["errors"] == ["duplicate_group_match"]


def test_independent_grid_correspondence_cardinality_causes_and_foreign_matches():
    expected, groups = ["g1", "g2"], ["p1", "p2"]
    rows = [dict(grid_id="g1", matches=["p1"], cause=None), dict(grid_id="g2", matches=["p2"], cause=None)]
    assert compare_grid_correspondence(expected, groups, rows, require_all_matched=True)["verdict"] == "PASS"
    for changed in (rows + [rows[0]], rows[:1], [dict(rows[0], grid_id="foreign"), rows[1]],
                    [dict(rows[0], matches=["foreign"]), rows[1]],
                    [rows[0], dict(rows[1], matches=["p1"])],
                    [dict(rows[0], matches=["p1", "p2"]), rows[1]]):
        result = compare_grid_correspondence(expected, groups, changed)
        assert result["verdict"] == "FAIL" and result["error_count"] > 0
    for cause in ("SOLAREDGE_GRID_UNMATCHED", "SOLAREDGE_GRID_AMBIGUOUS"):
        changed = [rows[0], dict(rows[1], matches=[], cause=cause)]
        assert compare_grid_correspondence(expected, groups, changed)["verdict"] == "PASS"
        assert compare_grid_correspondence(expected, groups, changed, require_all_matched=True)["verdict"] == "FAIL"
    assert compare_grid_correspondence(expected, groups, [dict(rows[0], matches=[], cause="private")])["reason"] == "grid_cause_unreadable"
    for invalid in (None, [], [{"grid_id": None}], [dict(rows[0], matches=None)]):
        assert compare_grid_correspondence(expected, groups, invalid)["verdict"] == ("FAIL" if invalid == [] else "UNREADABLE")


def test_seven_step_offline_composer_preserves_every_diagnostic_and_missing_evidence():
    manifest, bindings, inputs = provenance_fixture()
    identity = dict(project_id="p", version_id="v")
    assurance = dict(evidence=complete_evidence(), frozen_manifest=manifest, stage_bindings=bindings,
                     supplied_inputs=inputs, expected_identity=identity,
                     stage_identities={key: dict(identity) for key in IDENTITY_STAGES})
    imported = dict(normal_user=True, dwg_imported=True, report_imported=True)
    grids = dict(expected_grid_ids=["g"], saved_group_ids=["p"],
                 rows=[dict(grid_id="g", matches=["p"], cause=None)], require_all_matched=True)
    result = reconcile_seven_steps(imported, grids, assurance)
    assert result["verdict"] == "PASS" and set(result["steps"]) == set("1234567")
    assert result["steps"]["3"] == result["assurance"]["checks"]["provenance"]["reconciliation"]["stages"]["strings"]
    assurance["evidence"]["recommendations"][0]["group_count"] = 1
    result = reconcile_seven_steps(imported, grids, assurance)
    assert result["verdict"] == "FAIL" and result["steps"]["4"]["verdict"] == "FAIL"
    result = reconcile_seven_steps(None, grids, assurance)
    assert result["verdict"] == "UNREADABLE" and result["steps"]["4"]["verdict"] == "FAIL"
    assert result["steps"]["2"]["verdict"] == "PASS"
    for args in ((imported, None, assurance), (imported, grids, None)):
        assert reconcile_seven_steps(*args)["verdict"] == "UNREADABLE"


def test_result_output_budget_exact_boundary_and_cli_sanitized_overflow(monkeypatch):
    from io import BytesIO
    import sys
    from types import SimpleNamespace
    from benches import x05_reconcile as module
    assert len(serialize_result("x" * (MAX_RESULT_CHARS - 2))) == MAX_RESULT_CHARS
    with pytest.raises(ValueError, match="^json_result_too_large$"):
        serialize_result("x" * (MAX_RESULT_CHARS - 1))
    with pytest.raises(ValueError, match="^json_result_too_large$"):
        serialize_result({"x" * (MAX_RESULT_CHARS + 1): 1})
    result = dict(count=123456, examples=["x" * MAX_RESULT_CHARS])
    preserved = deepcopy(result)
    with pytest.raises(ValueError, match="^json_result_too_large$"):
        serialize_result(result)
    assert result == preserved
    monkeypatch.setattr(module, "reconcile_json", lambda text: result)
    output = StringIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(b"{}")))
    monkeypatch.setattr(sys, "stdout", output)
    assert module.main([]) == 4
    assert json.loads(output.getvalue()) == {"verdict": "UNREADABLE", "reason": "json_result_too_large"}


def test_shared_acyclic_expansion_stops_at_budget_without_mutating_graph():
    child = ["synthetic"]
    small = dict(a=child, b=child)
    assert json.loads(serialize_result(small)) == small
    graph = child
    for _ in range(24):
        graph = [graph, graph]
    with pytest.raises(ValueError, match="^json_result_too_large$"):
        serialize_result(graph)
    assert graph[0] is graph[1] and child == ["synthetic"]


CLI_DECODERS = [(None, reconcile_json), ("--aggregate", aggregate_fixtures_json),
    ("--provenance", reconcile_provenance_json), ("--assurance", reconcile_assurance_json),
    ("--provenance-aggregate", aggregate_provenance_json), ("--assurance-aggregate", aggregate_assurance_json)]


def framed_payload(mode):
    if mode is None:
        return complete_evidence()
    if mode == "--aggregate":
        expected, records = fixture_records()
        return dict(expected_ids=expected, fixtures=records)
    return cli_payload(mode)


@pytest.mark.parametrize("mode,decoder", CLI_DECODERS)
def test_short_reads_split_utf8_json_tokens_and_failures_safely(monkeypatch, mode, decoder):
    import sys
    from types import SimpleNamespace
    from benches import x05_reconcile as module
    payload = framed_payload(mode)
    text = json.dumps(payload, ensure_ascii=False)
    # Legal surrounding whitespace and multibyte characters in JSON strings.
    text = text.replace('"synthetic"', '"synthetic\U0001f642"') if mode is None else text
    raw = text.encode("utf-8")
    class ShortReads:
        def __init__(self, fail=False):
            self.position, self.fail, self.calls = 0, fail, 0
        def read(self, limit):
            self.calls += 1
            if self.fail and self.calls == 2:
                raise OSError("synthetic_private_canary")
            chunk = raw[self.position:self.position + min(1 if self.calls < 8 else 17, limit)]
            self.position += len(chunk)
            return chunk
    for fail in (False, True):
        output, errors, source = StringIO(), StringIO(), ShortReads(fail)
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=source))
        monkeypatch.setattr(sys, "stdout", output)
        monkeypatch.setattr(sys, "stderr", errors)
        exit_code = module.main([mode] if mode else [])
        expected = {"verdict": "UNREADABLE", "reason": "cli_evidence_unreadable"} if fail else decoder(text)
        assert json.loads(output.getvalue()) == json.loads(serialize_result(expected))
        assert exit_code == {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[expected["verdict"]]
        assert not errors.getvalue() and source.calls > 1


@pytest.mark.parametrize("mode,decoder", CLI_DECODERS)
@pytest.mark.parametrize("framing", ["whitespace", "bom", "concatenated", "trailing"])
def test_json_framing_consistent_in_strict_decoders_and_cli(monkeypatch, mode, decoder, framing):
    from io import BytesIO
    from types import SimpleNamespace
    import sys
    from benches import x05_reconcile as module
    text = json.dumps(framed_payload(mode))
    text = {"whitespace": " \r\n\t" + text + " \t\r\n", "bom": "\ufeff" + text,
            "concatenated": text + "{}", "trailing": text + "private"}[framing]
    expected = decoder(text)
    assert expected["verdict"] == ("PASS" if framing == "whitespace" else "UNREADABLE")
    if framing != "whitespace":
        assert expected["reason"] == "json_malformed"
    output = StringIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(text.encode("utf-8"))))
    monkeypatch.setattr(sys, "stdout", output)
    assert module.main([mode] if mode else []) == (0 if framing == "whitespace" else 4)
    assert json.loads(output.getvalue()) == json.loads(serialize_result(expected))


@pytest.mark.parametrize("assured", [False, True])
def test_duplicate_identity_never_passes_regardless_of_invalid_record_order(assured):
    expected, records = assurance_records() if assured else provenance_records()
    aggregate = aggregate_assurance if assured else aggregate_provenance
    baseline = aggregate(expected, records)
    valid = records[0]
    unbound = deepcopy(valid)
    unbound["evidence"]["fixture_id"] = "foreign"
    malformed = dict(fixture_id=expected[0], evidence=None)
    for ordering in permutations([valid, unbound, malformed]):
        result = aggregate(expected, list(ordering) + records[1:])
        assert result["verdict"] == "UNREADABLE"
        assert expected[0] not in result["fixtures"]
        for key in expected[1:]:
            assert result["fixtures"][key] == baseline["fixtures"][key]
    for bad in (unbound, malformed):
        for ordering in permutations([valid, bad]):
            result = aggregate(expected, list(ordering) + records[1:])
            assert expected[0] not in result["fixtures"]


@pytest.mark.parametrize("payload", ["", "zz", "0" * 40000, None, []], ids=["empty", "nonhex", "oversized", "null", "list"])
def test_foreign_hex_role_rejected_before_any_decoding(payload, monkeypatch):
    from benches import x05_reconcile as module
    envelope = assurance_envelope()
    envelope["inputs_hex"]["synthetic_private_canary"] = payload
    preserved = deepcopy(envelope)
    class ForbiddenBytes:
        @staticmethod
        def fromhex(value):
            pytest.fail("Foreign roles must fail before decoding any payload")
    monkeypatch.setattr(module, "bytes", ForbiddenBytes, raising=False)
    assert reconcile_assurance_json(json.dumps(envelope)) == {
        "verdict": "UNREADABLE", "reason": "input_hex_roles_unreadable"}
    assert envelope == preserved


def cli_payload(mode):
    if mode == "--provenance":
        manifest, bindings, _ = provenance_fixture()
        return dict(evidence=complete_evidence(), manifest=manifest, bindings=bindings)
    if mode == "--assurance":
        return assurance_envelope()
    expected, records = assurance_records() if mode == "--assurance-aggregate" else provenance_records()
    return dict(expected_ids=expected, fixtures=records)


@pytest.mark.parametrize("mode,decoder", [
    ("--provenance", reconcile_provenance_json), ("--assurance", reconcile_assurance_json),
    ("--provenance-aggregate", aggregate_provenance_json), ("--assurance-aggregate", aggregate_assurance_json)])
@pytest.mark.parametrize("character", ["x", "\U0001f642"])
def test_new_mode_character_and_byte_limits_match_direct_decoder(tmp_path, mode, decoder, character):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    payload = cli_payload(mode)
    evidence = payload["evidence"] if "evidence" in payload else payload["fixtures"][0]["evidence"]
    evidence["notes"] = ""
    base = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    def run(padding, expected_exit):
        evidence["notes"] = padding
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), mode],
            input=text.encode("utf-8"), capture_output=True, cwd=tmp_path, timeout=20)
        assert result.returncode == expected_exit and not result.stderr
        assert len(result.stdout.splitlines()) == 1
        assert json.loads(result.stdout) == json.loads(serialize_result(decoder(text)))
        return text
    for extra in (0, 1):
        text = run(character * (MAX_JSON_CHARS + extra - len(base)), 4 if extra else 0)
        assert len(text) == MAX_JSON_CHARS + extra
    width = len(character.encode("utf-8"))
    for extra in (0, 1):
        remaining = 4 * MAX_JSON_CHARS + extra - len(base.encode("utf-8"))
        text = run(character * (remaining // width) + "x" * (remaining % width), 4)
        assert len(text.encode("utf-8")) == 4 * MAX_JSON_CHARS + extra


@pytest.mark.parametrize("mode", ["--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate"])
@pytest.mark.parametrize("failure", ["write", "flush", "pipe"])
def test_new_modes_output_failures_exit_five_without_tracebacks(tmp_path, monkeypatch, mode, failure):
    import subprocess
    import sys
    from pathlib import Path
    from io import BytesIO
    from types import SimpleNamespace
    from benches import x05_reconcile as module
    raw = json.dumps(cli_payload(mode)).encode()
    if failure != "pipe":
        class BrokenOutput:
            writes = flushes = 0
            def write(self, value):
                self.writes += 1
                if failure == "write":
                    raise BrokenPipeError("synthetic_private_canary")
                return len(value)
            def flush(self):
                self.flushes += 1
                raise BrokenPipeError("synthetic_private_canary")
        output, errors = BrokenOutput(), StringIO()
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(raw)))
        monkeypatch.setattr(sys, "stdout", output)
        monkeypatch.setattr(sys, "stderr", errors)
        assert module.main([mode]) == 5 and not errors.getvalue()
        assert output.writes == 1 and output.flushes == (1 if failure == "flush" else 0)
    else:
        child = subprocess.Popen([sys.executable, "-P", str(Path(module.__file__).resolve()), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path)
        try:
            child.stdout.close()
            child.stdout = None
            _, errors = child.communicate(raw, timeout=20)
            assert child.returncode == 5 and not errors
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()


def test_assurance_aggregate_cli_deterministic_with_simultaneous_conflicts(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    expected, records = assurance_records()
    records[0]["inputs_hex"]["report"] = b"substituted".hex()
    records[0]["stage_identities"]["csv"]["version_id"] = "foreign"
    records[1]["evidence"]["recommendations"][0]["group_count"] = 1
    outputs = []
    for seed in ("0", "73"):
        for reverse in (False, True):
            changed = deepcopy(records)
            if reverse:
                changed.reverse()
                for row in changed:
                    row["inputs_hex"] = dict(reversed(list(row["inputs_hex"].items())))
                    row["stage_identities"] = dict(reversed(list(row["stage_identities"].items())))
            result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--assurance-aggregate"],
                input=json.dumps(dict(expected_ids=expected, fixtures=changed)).encode(), capture_output=True,
                cwd=tmp_path, timeout=20, env={"PYTHONHASHSEED": seed, "SystemRoot": "C:\\Windows"})
            assert result.returncode == 4 and not result.stderr
            assert json.loads(result.stdout)["fixtures"][expected[2]]["verdict"] == "PASS"
            outputs.append(result.stdout)
    assert len(set(outputs)) == 1


@pytest.mark.parametrize("key", REQUIRED_STAGES)
def test_required_field_type_substitutions_preserve_unaffected_stages(key):
    complete = complete_evidence()
    baseline = reconcile_offline(complete)
    array_fields = {"oracle_strings", "saved_strings", "view_rows", "recommendations", "discrepancy_records"}
    map_fields = {"saved_values", "oracle_values", "before", "reopened", "before_rows", "reopened_rows"}
    bad = [None, 42, {}] if key in array_fields else [None, 42, []] if key in map_fields else [None, False, {}]
    for value in bad:
        evidence = deepcopy(complete)
        evidence[key] = value
        result = reconcile_offline(evidence)
        assert result["verdict"] == "UNREADABLE"
        for name, observed in result["stages"].items():
            if name in REQUIRED_STAGES[key]:
                assert observed["verdict"] == "UNREADABLE"
                assert type(observed["reason"]) is str and observed["reason"] != "stage_evidence_missing"
            else:
                assert observed == baseline["stages"][name]


@pytest.mark.parametrize("text", [
    r'{"saved_strings":[],"\u0073aved_strings":[]}',
    r'{"before_rows":{"strings":[],"\u0073trings":[]}}',
    r'{"expected_ids":[],"\u0065xpected_ids":[],"fixtures":[]}',
    r'{"expected_ids":["a","b","c"],"fixtures":[{"evidence":{"fixture_id":"a","\u0066ixture_id":"a"}}]}',
])
def test_unicode_escaped_json_key_collisions_are_rejected_in_both_modes(text):
    for decode in (reconcile_json, aggregate_fixtures_json):
        assert decode(text) == {"verdict": "UNREADABLE", "reason": "json_duplicate_key"}


def test_raw_numeric_tokens_are_bounded_independently_of_caller_decimal_traps():
    from decimal import localcontext

    tokens = ["1e999999", "1e-999999", "1e999999999999999999999999999", "9" * 81,
              "9" * 5000, "1e" + "0" * 200 + "1"]
    for trapped in (False, True):
        with localcontext() as caller:
            caller.prec, caller.Emin, caller.Emax = 2, -1, 1
            for signal in caller.traps:
                caller.traps[signal] = trapped
            caller.clear_flags()
            preserved = (caller.prec, caller.Emin, caller.Emax, dict(caller.traps), dict(caller.flags))
            for token in tokens:
                for decode in (reconcile_json, aggregate_fixtures_json):
                    assert decode('{"number":' + token + '}') == {
                        "verdict": "UNREADABLE", "reason": "json_numeric_unreadable"}
            assert decode_evidence_json('{"number":' + "9" * 80 + '}')["verdict"] == "PASS"
            assert (caller.prec, caller.Emin, caller.Emax, dict(caller.traps), dict(caller.flags)) == preserved


@pytest.mark.parametrize("failure", ["broken-write", "encoding-write", "broken-flush"])
def test_stdout_failure_returns_nonzero_without_recursive_output(monkeypatch, failure):
    from io import BytesIO
    from types import SimpleNamespace
    import sys
    from benches import x05_reconcile as module

    class FailedOutput:
        writes = 0
        flushes = 0

        def write(self, value):
            self.writes += 1
            if failure == "encoding-write":
                raise UnicodeEncodeError("ascii", "synthetic_private_canary", 0, 1, "synthetic")
            if failure == "broken-write":
                raise BrokenPipeError("synthetic_private_canary")
            return len(value)

        def flush(self):
            self.flushes += 1
            raise BrokenPipeError("synthetic_private_canary")

    class ForbiddenErrorOutput:
        def write(self, value):
            pytest.fail("Output failure must not report to stderr")

    failed = FailedOutput()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(json.dumps(complete_evidence()).encode())))
    monkeypatch.setattr(sys, "stdout", failed)
    monkeypatch.setattr(sys, "stderr", ForbiddenErrorOutput())
    assert module.main([]) == 5
    assert failed.writes == 1 and failed.flushes == (1 if failure == "broken-flush" else 0)


@pytest.mark.parametrize("case,exit_code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4)])
def test_aggregate_cli_bytes_are_deterministic_across_hash_seeds(tmp_path, case, exit_code):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module

    expected, fixtures = fixture_records()
    if case != "PASS":
        fixtures[0]["evidence"]["recommendations"][0]["group_count"] = 1
    if case == "UNREADABLE":
        del fixtures[1]["evidence"]["reopened"]
    outputs = []
    for seed in ("0", "1", "73"):
        for reverse in (False, True):
            envelope = dict(expected_ids=list(reversed(expected)) if reverse else expected,
                            fixtures=list(reversed(fixtures)) if reverse else fixtures)
            result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--aggregate"],
                input=json.dumps(envelope).encode(), capture_output=True, cwd=tmp_path, timeout=20,
                env={"PYTHONHASHSEED": seed, "SystemRoot": "C:\\Windows"})
            assert result.returncode == exit_code and result.stderr == b""
            outputs.append(result.stdout)
    assert len(set(outputs)) == 1


@pytest.mark.parametrize("aggregate", [False, True])
@pytest.mark.parametrize("character", ["x", "\U0001f642"])
def test_cli_exact_character_and_utf8_byte_boundaries(tmp_path, aggregate, character):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module

    if aggregate:
        expected, fixtures = fixture_records()
        payload = dict(expected_ids=expected, fixtures=fixtures)
        target = fixtures[0]["evidence"]
    else:
        payload = target = complete_evidence()
    target["notes"] = ""
    base = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    command = [sys.executable, "-P", str(Path(module.__file__).resolve())] + (["--aggregate"] if aggregate else [])

    def run(padding, expected_exit):
        target["notes"] = padding
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = subprocess.run(command, input=text.encode("utf-8"), capture_output=True, cwd=tmp_path, timeout=20)
        assert result.returncode == expected_exit and result.stderr == b""
        assert len(result.stdout.splitlines()) == 1
        decoded = json.loads(result.stdout)
        if expected_exit:
            assert decoded == {"verdict": "UNREADABLE", "reason": "json_input_too_large"}
        else:
            assert decoded["verdict"] == "PASS"
        return text

    size = MAX_JSON_CHARS - len(base)
    assert len(run(character * size, 0)) == MAX_JSON_CHARS
    assert len(run(character * (size + 1), 4)) == MAX_JSON_CHARS + 1
    width = len(character.encode("utf-8"))
    for extra in (0, 1):
        remaining = 4 * MAX_JSON_CHARS + extra - len(base.encode("utf-8"))
        padding = character * (remaining // width) + "x" * (remaining % width)
        # At the raw-byte limit, JSON framing already makes the character
        # limit stricter; both constraints apply, rather than one overriding it.
        assert len(run(padding, 4).encode("utf-8")) == 4 * MAX_JSON_CHARS + extra


@pytest.mark.parametrize("identifier,valid", [
    ("a" * 128, True), ("a" * 129, False),
    ("\U0001f642" * 128, True), ("\U0001f642" * 129, False), (" \t\n", False),
])
def test_fixture_and_string_identifier_boundaries(identifier, valid):
    rows = [{"string_id": identifier, "optimizers": 1}]
    strings = compare_strings(rows, rows)
    expected, fixtures = fixture_records()
    expected[0] = fixtures[0]["fixture_id"] = fixtures[0]["evidence"]["fixture_id"] = identifier
    aggregate = aggregate_fixtures(expected, fixtures)
    assert strings["verdict"] == aggregate["verdict"] == ("PASS" if valid else "UNREADABLE")
    if valid:
        assert identifier in aggregate["fixtures"]
    else:
        assert strings["reason"] == "oracle_row_unreadable"
        assert aggregate["reason"] == "expected_fixtures_unreadable"


@pytest.mark.parametrize("left,right", [(" A ", "A"), ("\u00e9", "e\u0301")])
def test_identifiers_preserve_spaces_and_unicode_identity(left, right):
    rows = lambda value: [{"string_id": value, "optimizers": 1}]
    result = compare_strings(rows(left), rows(right))
    assert result["verdict"] == "FAIL"
    assert result["missing_ids"] == [left] and result["foreign_ids"] == [right]
    expected, fixtures = fixture_records()
    expected[0] = left
    fixtures[0]["fixture_id"] = fixtures[0]["evidence"]["fixture_id"] = right
    assert aggregate_fixtures(expected, fixtures)["reason"] == "foreign_fixture_id"


@pytest.mark.parametrize("depth", [0, 1, 6])
def test_unsupported_serialization_types_have_fixed_errors_without_object_text(depth):
    class PrivateObject:
        def __repr__(self):
            pytest.fail("Serializer must not inspect injected object text")

    for unsupported in (("synthetic_private_canary",), {"synthetic_private_canary"},
                        b"synthetic_private_canary", PrivateObject(), {1: "synthetic_private_canary"}):
        value = unsupported
        for _ in range(depth):
            value = {"nested": [value]}
        with pytest.raises(ValueError) as error:
            serialize_result(value)
        assert str(error.value) == "json_result_type_unreadable"


@pytest.mark.parametrize("aggregate", [False, True])
def test_cli_stdin_read_error_emits_one_sanitized_result(monkeypatch, aggregate):
    from types import SimpleNamespace
    import sys
    from benches import x05_reconcile as module

    class FailedInput:
        def read(self, size):
            assert size == 4 * MAX_JSON_CHARS + 1
            raise OSError("synthetic_private_canary")

    output, errors = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=FailedInput()))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    assert module.main(["--aggregate"] if aggregate else []) == 4
    assert output.getvalue() == '{"reason":"cli_evidence_unreadable","verdict":"UNREADABLE"}\n'
    assert errors.getvalue() == ""


@pytest.mark.parametrize("aggregate", [False, True])
@pytest.mark.parametrize("option,exit_code", [("--help", 0), ("--unknown-option", 2)])
def test_cli_usage_produces_no_reconciliation_result(tmp_path, aggregate, option, exit_code):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module

    command = [sys.executable, "-P", str(Path(module.__file__).resolve())]
    result = subprocess.run(command + (["--aggregate"] if aggregate else []) + [option],
        input=b"synthetic_private_canary", capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == exit_code
    if option == "--help":
        assert b"Offline X05 evidence comparison" in result.stdout and result.stderr == b""
    else:
        assert result.stdout == b"" and b"unrecognized arguments" in result.stderr
    assert b'"verdict"' not in result.stdout + result.stderr
    assert b"synthetic_private_canary" not in result.stdout + result.stderr
    assert b"Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("identifier", ["\U0001f642", "\ud800", "\udfff"])
def test_escaped_unicode_identity_round_trips_with_ascii_safe_hashes(identifier):
    _, table = table_fixture()
    rows = [{"string_id": identifier, "optimizers": 1}]
    text = json.dumps({"strings": rows}, ensure_ascii=True)
    decoded = decode_evidence_json(text)
    assert decoded["verdict"] == "PASS" and decoded["evidence"]["strings"] == rows
    snapshot = canonical_snapshot(rows, table)
    assert snapshot["verdict"] == "PASS"
    expected = json.dumps([[identifier, 1]], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    assert snapshot["strings_sha256"] == sha256(expected).hexdigest()
    result = compare_strings(rows, [{"string_id": "other", "optimizers": 1}])
    serialized = serialize_result(result)
    assert serialized.isascii() and json.loads(serialized)["missing_ids"] == [identifier]
    assert serialize_result(decode_evidence_json(serialized)["evidence"]) == serialized
    expected_ids, fixtures = fixture_records()
    expected_ids[0] = fixtures[0]["fixture_id"] = fixtures[0]["evidence"]["fixture_id"] = identifier
    aggregate = aggregate_fixtures_json(json.dumps(dict(expected_ids=expected_ids, fixtures=fixtures)))
    assert aggregate["verdict"] == "PASS" and identifier in aggregate["fixtures"]
    assert serialize_result(aggregate).isascii()


def test_supplied_provenance_requires_frozen_manifest_and_every_stage_binding():
    frozen = dict(fixture_id="synthetic", inputs=dict(dwg="a" * 64, report="b" * 64, bom="c" * 64))
    bindings = {stage: deepcopy(frozen) for stage in PROVENANCE_STAGES}
    preserved = deepcopy((frozen, bindings))
    assert validate_fixture_provenance(frozen, bindings) == {
        "verdict": "PASS", "reason": "supplied_provenance_equal", "stage_count": 6}
    for stage in PROVENANCE_STAGES:
        for field in ("fixture_id", "inputs"):
            altered = deepcopy(bindings)
            del altered[stage][field]
            assert validate_fixture_provenance(frozen, altered) == {
                "verdict": "UNREADABLE", "reason": "stage_binding_unreadable", "stage": stage}
        for key, value in (("fixture_id", "unrelated"), ("inputs", dict(frozen["inputs"], dwg="d" * 64))):
            altered = deepcopy(bindings)
            altered[stage][key] = value
            assert validate_fixture_provenance(frozen, altered) == {
                "verdict": "UNREADABLE", "reason": "stage_binding_conflict", "stage": stage}
        altered = deepcopy(bindings)
        del altered[stage]
        assert validate_fixture_provenance(frozen, altered)["reason"] == "provenance_stages_unreadable"
    for invalid in (None, {}, dict(frozen, extra=True), dict(frozen, fixture_id=" "),
                    dict(frozen, inputs={}), dict(frozen, inputs=dict(dwg="a" * 63, report="b" * 64)),
                    dict(frozen, inputs=dict(dwg=True, report="b" * 64)),
                    dict(frozen, inputs=dict(dwg="A" * 64, report="b" * 64))):
        assert validate_fixture_provenance(invalid, bindings)["reason"] == "frozen_manifest_unreadable"
    assert (frozen, bindings) == preserved
    without_bom = dict(frozen, inputs={key: value for key, value in frozen["inputs"].items() if key != "bom"})
    assert validate_fixture_provenance(without_bom,
        {stage: deepcopy(without_bom) for stage in PROVENANCE_STAGES})["verdict"] == "PASS"


@pytest.mark.parametrize("kind", ["dict", "list", "mixed"])
@pytest.mark.parametrize("containers", [31, 32, 33])
def test_serializer_container_depth_boundaries_preserve_inputs(kind, containers):
    value = "synthetic"
    for level in range(containers):
        value = {"nested": value} if kind == "dict" or (kind == "mixed" and level % 2 == 0) else [value]
    # Strict evidence requires an object root; wrap a list root at the same depth.
    if type(value) is list:
        value = {"nested": value[0]}
    preserved = deepcopy(value)
    if containers <= MAX_JSON_DEPTH:
        encoded = serialize_result(value)
        assert decode_evidence_json(encoded)["evidence"] == value
    else:
        with pytest.raises(ValueError, match="^json_result_too_deep$"):
            serialize_result(value)
    assert value == preserved


def test_concurrent_decimal_contexts_preserve_callers_and_identical_results():
    from concurrent.futures import ThreadPoolExecutor
    from decimal import localcontext
    from threading import Event

    ready, release = [Event(), Event()], Event()

    def run(index):
        with localcontext() as caller:
            caller.prec, caller.Emin, caller.Emax = (2, -1, 1) if index == 0 else (37, -999, 999)
            for signal in caller.traps:
                caller.traps[signal] = bool(index)
            caller.clear_flags()
            context = lambda: (caller.prec, caller.Emin, caller.Emax, dict(caller.traps), dict(caller.flags))
            preserved = context()
            ready[index].set()
            assert release.wait(10)
            evidence = complete_evidence()
            result = reconcile_offline(evidence)
            snapshot = canonical_snapshot(evidence["saved_strings"], evidence["view_rows"])
            assert context() == preserved
            return result, snapshot

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, index) for index in range(2)]
        try:
            assert all(event.wait(10) for event in ready)
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1] and results[0][0]["verdict"] == "PASS"


@pytest.mark.parametrize("aggregate", [False, True])
def test_cli_real_closed_stdout_pipe_exits_five_without_shutdown_traceback(tmp_path, aggregate):
    from pathlib import Path
    import subprocess
    import sys
    from benches import x05_reconcile as module

    if aggregate:
        expected, fixtures = fixture_records()
        evidence = dict(expected_ids=expected, fixtures=fixtures)
    else:
        evidence = complete_evidence()
    child = subprocess.Popen([sys.executable, "-P", str(Path(module.__file__).resolve())]
        + (["--aggregate"] if aggregate else []), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path)
    try:
        # Close the only reader before supplying any input. No timing race or sleep.
        child.stdout.close()
        child.stdout = None
        _, errors = child.communicate(json.dumps(evidence).encode(), timeout=20)
        assert child.returncode == 5 and errors == b""
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("lexeme", ["9.6", "96e-1", "0.96E+1", "960e-2"])
@pytest.mark.parametrize("zero", ["0", "-0", "0.0", "-0e+0"])
def test_numeric_json_lexemes_preserve_verdict_and_canonical_hashes(lexeme, zero):
    evidence = complete_evidence()
    evidence["saved_values"]["kw_dc"] = evidence["oracle_values"]["kw_dc"] = "LEXEME"
    evidence["view_rows"][-1].update(studio="LEXEME", oracle="LEXEME", delta="ZERO")
    text = json.dumps(evidence).replace('"LEXEME"', lexeme).replace('"ZERO"', zero)
    decoded = decode_evidence_json(text)["evidence"]
    assert reconcile_json(text)["verdict"] == "PASS"
    assert canonical_snapshot(decoded["saved_strings"], decoded["view_rows"]) == complete_evidence()["before"]
    fixtures = []
    for key in ("a", "b", "c"):
        current = deepcopy(decoded)
        current["fixture_id"] = key
        fixtures.append(dict(fixture_id=key, evidence=current))
    envelope = serialize_result(dict(expected_ids=["a", "b", "c"], fixtures=fixtures))
    assert aggregate_fixtures_json(envelope)["verdict"] == "PASS"
    for tiny in ("1e-78", "0." + "0" * 77 + "1"):
        # Replace only the kW delta marker, never a prefix of another number.
        tiny_evidence = deepcopy(evidence)
        tiny_evidence["view_rows"][-1]["delta"] = "TINY"
        tiny_text = (json.dumps(tiny_evidence).replace('"LEXEME"', lexeme)
                     .replace('"TINY"', tiny))
        tiny_result = reconcile_json(tiny_text)
        assert tiny_result["verdict"] == "FAIL"
        assert tiny_result["stages"]["table"]["verdict"] == "FAIL"
        tiny_decoded = decode_evidence_json(tiny_text)["evidence"]
        assert tiny_decoded["view_rows"][-1]["delta"] != 0
        tiny_decoded["fixture_id"] = "a"
        fixtures[0]["evidence"] = tiny_decoded
        tiny_aggregate = aggregate_fixtures_json(serialize_result(
            dict(expected_ids=["a", "b", "c"], fixtures=fixtures)))
        assert tiny_aggregate["verdict"] == "FAIL"
        assert tiny_aggregate["fixtures"]["a"]["stages"]["table"]["verdict"] == "FAIL"


@pytest.mark.parametrize("kind,affected", [
    ("string", {"strings", "reopen"}), ("recommendation", {"grouping"}),
    ("table", {"table", "reopen", "csv_view"}), ("policy", {"table", "policy"}),
    ("reopen", {"reopen"}), ("csv", {"csv_view"}),
])
def test_readable_corruption_failures_survive_composition_and_aggregation(kind, affected):
    evidence = complete_evidence()
    baseline = reconcile_offline(evidence)
    if kind == "string":
        evidence["saved_strings"][0]["optimizers"] += 1
    elif kind == "recommendation":
        evidence["recommendations"][0]["group_count"] += 1
    elif kind == "table":
        evidence["view_rows"][0]["studio"] += 1
    elif kind == "policy":
        evidence["saved_values"]["panels"] += 1
    elif kind == "reopen":
        evidence["reopened_rows"]["strings"][0]["optimizers"] += 1
    else:
        altered = deepcopy(evidence["view_rows"])
        altered[0]["studio"] += 1
        evidence["csv_text"] = csv_fixture(altered)
    result = reconcile_offline(evidence)
    assert result["verdict"] == "FAIL"
    for stage, observed in result["stages"].items():
        if stage in affected:
            assert observed["verdict"] == "FAIL" and type(observed["reason"]) is str
        else:
            assert observed == baseline["stages"][stage]
    expected, fixtures = fixture_records()
    evidence["fixture_id"] = expected[0]
    fixtures[0]["evidence"] = evidence
    aggregate = aggregate_fixtures(expected, fixtures)
    assert aggregate["verdict"] == "FAIL"
    assert aggregate["fixtures"][expected[0]] == reconcile_offline(evidence)
    assert all(aggregate["fixtures"][key]["verdict"] == "PASS" for key in expected[1:])


def provenance_fixture():
    inputs = dict(dwg=b"synthetic drawing", report=b"synthetic report", bom=b"synthetic BOM")
    frozen = dict(fixture_id="synthetic", inputs={key: sha256(value).hexdigest() for key, value in inputs.items()})
    return frozen, {stage: deepcopy(frozen) for stage in PROVENANCE_STAGES}, inputs


def test_provenance_composition_keeps_all_diagnostics_and_unreadable_precedence():
    frozen, bindings, _ = provenance_fixture()
    evidence = complete_evidence()
    baseline = reconcile_offline(evidence)
    result = reconcile_with_provenance(evidence, frozen, bindings)
    assert result["verdict"] == "PASS" and result["reconciliation"] == baseline
    evidence["recommendations"][0]["group_count"] = 1
    failed = reconcile_offline(evidence)
    assert reconcile_with_provenance(evidence, frozen, bindings)["verdict"] == "FAIL"
    bindings["strings"]["inputs"]["report"] = "d" * 64
    result = reconcile_with_provenance(evidence, frozen, bindings)
    assert result["verdict"] == "UNREADABLE" and result["reconciliation"] == failed
    _, bindings, _ = provenance_fixture()
    evidence["fixture_id"] = "foreign"
    result = reconcile_with_provenance(evidence, frozen, bindings)
    assert result["provenance"]["reason"] == "evidence_fixture_identity_conflict"
    assert result["reconciliation"] == reconcile_offline(evidence)
    for missing in (None, {}, {"fixture_id": "synthetic"}):
        result = reconcile_with_provenance(missing, frozen, bindings)
        assert result["verdict"] == "UNREADABLE"
        assert result["reconciliation"] == reconcile_offline(missing)


@pytest.mark.parametrize("bom", [False, True])
def test_input_byte_hashes_detect_one_byte_substitutions_and_uncertainty(bom):
    frozen, _, inputs = provenance_fixture()
    if not bom:
        del frozen["inputs"]["bom"]
        del inputs["bom"]
    preserved = deepcopy((frozen, inputs))
    assert verify_input_bytes(frozen, inputs)["verdict"] == "PASS"
    for role in inputs:
        swapped = dict(inputs)
        swapped[role] = bytes([inputs[role][0] ^ 1]) + inputs[role][1:]
        assert verify_input_bytes(frozen, swapped) == {
            "verdict": "UNREADABLE", "reason": "input_digest_conflict", "roles": [role]}
        for wrong in (None, "synthetic_private_canary", bytearray(inputs[role]), 42):
            swapped[role] = wrong
            assert verify_input_bytes(frozen, swapped) == {
                "verdict": "UNREADABLE", "reason": "input_bytes_unreadable", "role": role}
        missing = dict(inputs)
        del missing[role]
        assert verify_input_bytes(frozen, missing)["reason"] == "input_bytes_missing"
    for bad in (None, [], {}, dict(inputs, extra=b"synthetic")):
        assert verify_input_bytes(frozen, bad)["verdict"] == "UNREADABLE"
    assert verify_input_bytes({}, inputs)["reason"] == "frozen_manifest_unreadable"
    assert (frozen, inputs) == preserved


def test_saved_design_identity_rejects_mixed_projects_and_versions_even_with_equal_totals():
    expected = dict(project_id="project-a", version_id="version-a")
    stages = {stage: dict(expected) for stage in IDENTITY_STAGES}
    preserved = deepcopy((expected, stages))
    assert validate_saved_design_identity(expected, stages)["verdict"] == "PASS"
    assert reconcile_offline(complete_evidence())["verdict"] == "PASS"
    for stage in IDENTITY_STAGES:
        for key in expected:
            changed = deepcopy(stages)
            changed[stage][key] = "foreign"
            assert validate_saved_design_identity(expected, changed) == {
                "verdict": "UNREADABLE", "reason": "design_identity_conflict", "stage": stage}
            for wrong in (None, " ", True, [], "x" * 129):
                changed[stage][key] = wrong
                assert validate_saved_design_identity(expected, changed)["reason"] == "stage_design_identity_unreadable"
        missing = deepcopy(stages)
        del missing[stage]
        assert validate_saved_design_identity(expected, missing)["reason"] == "design_identity_stages_unreadable"
    for bad in (None, {}, dict(expected, extra=True)):
        assert validate_saved_design_identity(bad, stages)["reason"] == "design_identity_unreadable"
    assert (expected, stages) == preserved


def test_provenance_conflicts_have_order_independent_named_diagnostics():
    frozen, bindings, _ = provenance_fixture()
    for stage in ("strings", "table", "reopen"):
        bindings[stage]["inputs"]["report"] = "d" * 64
    preserved = deepcopy((frozen, bindings))
    expected = {"verdict": "UNREADABLE", "reason": "stage_binding_conflict", "stage": "strings"}
    for roles in permutations(frozen["inputs"]):
        manifest = dict(frozen, inputs={key: frozen["inputs"][key] for key in roles})
        for stage_order in permutations(PROVENANCE_STAGES):
            assert validate_fixture_provenance(manifest, {key: bindings[key] for key in stage_order}) == expected
    assert (frozen, bindings) == preserved


@pytest.mark.parametrize("stage", PROVENANCE_STAGES)
def test_provenance_schema_mutations_are_fixed_and_sanitized(stage):
    frozen, bindings, _ = provenance_fixture()
    for bad_container in (None, [], "synthetic_private_canary", 42):
        changed = deepcopy(bindings)
        changed[stage] = bad_container
        assert validate_fixture_provenance(frozen, changed) == {
            "verdict": "UNREADABLE", "reason": "stage_binding_unreadable", "stage": stage}
    for bad in ([], None, dict(frozen["inputs"], unexpected="a" * 64),
                dict(frozen["inputs"], report="synthetic_private_canary"),
                dict(frozen["inputs"], report="z" * 64)):
        changed = deepcopy(bindings)
        changed[stage]["inputs"] = bad
        observed = validate_fixture_provenance(frozen, changed)
        assert observed == {"verdict": "UNREADABLE", "reason": "stage_binding_unreadable", "stage": stage}
        assert "synthetic_private_canary" not in serialize_result(observed)
    changed = deepcopy(bindings)
    del changed[stage]["inputs"]["bom"]
    assert validate_fixture_provenance(frozen, changed)["reason"] == "stage_binding_conflict"
    no_bom = dict(frozen, inputs={key: value for key, value in frozen["inputs"].items() if key != "bom"})
    changed = {name: deepcopy(no_bom) for name in PROVENANCE_STAGES}
    changed[stage]["inputs"]["bom"] = "c" * 64
    assert validate_fixture_provenance(no_bom, changed)["reason"] == "stage_binding_conflict"
    for top in (None, [], "synthetic_private_canary", dict(bindings, unexpected={} )):
        assert validate_fixture_provenance(frozen, top)["reason"] == "provenance_stages_unreadable"


def test_assurance_retains_every_diagnostic_and_unreadable_precedence():
    manifest, bindings, inputs = provenance_fixture()
    evidence = complete_evidence()
    identity = dict(project_id="project", version_id="version")
    stages = {key: dict(identity) for key in IDENTITY_STAGES}
    args = (evidence, manifest, bindings, inputs, identity, stages)
    preserved = deepcopy(args)
    assert reconcile_assured(*args)["verdict"] == "PASS"
    assert args == preserved
    evidence["recommendations"][0]["group_count"] = 1
    assert reconcile_assured(*args)["verdict"] == "FAIL"
    stages["csv"]["version_id"] = "foreign"
    inputs["report"] = b"substituted"
    bindings["table"]["inputs"]["dwg"] = "d" * 64
    result = reconcile_assured(*args)
    assert result["verdict"] == "UNREADABLE"
    checks = result["checks"]
    assert checks["provenance"]["reconciliation"]["stages"]["grouping"]["verdict"] == "FAIL"
    assert checks["input_bytes"]["reason"] == "input_digest_conflict"
    assert checks["design_identity"]["reason"] == "design_identity_conflict"
    assert checks["provenance"]["provenance"]["reason"] == "stage_binding_conflict"


def provenance_records():
    expected, fixtures = fixture_records()
    for row in fixtures:
        manifest, bindings, _ = provenance_fixture()
        manifest["fixture_id"] = row["fixture_id"]
        for binding in bindings.values():
            binding["fixture_id"] = row["fixture_id"]
        row.update(manifest=manifest, bindings=bindings)
    return expected, fixtures


def test_provenance_aggregate_rejects_substitution_and_preserves_siblings():
    expected, fixtures = provenance_records()
    baseline = aggregate_provenance(expected, fixtures)
    assert baseline["verdict"] == "PASS"
    fixtures[0]["manifest"]["fixture_id"] = "foreign"
    result = aggregate_provenance(expected, fixtures)
    assert result["verdict"] == "UNREADABLE"
    assert result["fixtures"][expected[0]]["provenance"]["reason"] == "stage_binding_conflict"
    for key in expected[1:]:
        assert result["fixtures"][key] == baseline["fixtures"][key]
    fixtures[1]["evidence"]["recommendations"][0]["group_count"] = 1
    assert aggregate_provenance(expected, fixtures)["fixtures"][expected[1]]["verdict"] == "FAIL"
    for altered in (fixtures[:2], fixtures + [fixtures[0]], [fixtures[0]] * 3):
        assert aggregate_provenance(expected, altered)["verdict"] == "UNREADABLE"
    expected, fixtures = provenance_records()
    fixtures[0]["fixture_id"] = "foreign"
    assert aggregate_provenance(expected, fixtures)["reason"] == "foreign_fixture_id"


def test_strict_provenance_envelope_uses_bounded_decoder():
    manifest, bindings, _ = provenance_fixture()
    envelope = dict(evidence=complete_evidence(), manifest=manifest, bindings=bindings)
    assert reconcile_provenance_json(json.dumps(envelope))["verdict"] == "PASS"
    for key in envelope:
        missing = dict(envelope)
        del missing[key]
        assert reconcile_provenance_json(json.dumps(missing))["reason"] == "provenance_envelope_unreadable"
    assert reconcile_provenance_json(json.dumps(dict(envelope, extra=True)))["reason"] == "provenance_envelope_unreadable"
    for text, reason in [('{"evidence":{},"\\u0065vidence":{}}', "json_duplicate_key"),
                         ('{"manifest":{"inputs":{"dwg":"a","dwg":"b"}}}', "json_duplicate_key"),
                         ('x' * (MAX_JSON_CHARS + 1), "json_input_too_large"),
                         ('{"x":NaN}', "json_nonfinite_number")]:
        assert reconcile_provenance_json(text) == {"verdict": "UNREADABLE", "reason": reason}


@pytest.mark.parametrize("payload,digest", [
    (b"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    (b"a\x00b", "59b271ae1bbcb1d31d41929817f4b16fb439eb4f31520b5ad1d5ce98920a7138"),
    (b"\xff\xfe\x80", "b632bfa9c60bc17f7fd78dba311fd3f5dfa66cb8c017a4278e2bae28d668844d"),
])
def test_input_hash_literal_vectors_and_deterministic_simultaneous_conflicts(payload, digest):
    # Literal vectors cross-checked outside the helper with standard hashlib.
    for role in ("dwg", "report", "bom"):
        manifest, _, inputs = provenance_fixture()
        inputs[role], manifest["inputs"][role] = payload, digest
        assert verify_input_bytes(manifest, inputs)["verdict"] == "PASS"
        altered = {key: value + b"!" for key, value in inputs.items()}
        preserved = deepcopy((manifest, altered))
        for order in permutations(altered):
            assert verify_input_bytes(manifest, {key: altered[key] for key in order}) == {
                "verdict": "UNREADABLE", "reason": "input_digest_conflict", "roles": ["bom", "dwg", "report"]}
        assert (manifest, altered) == preserved


@pytest.mark.parametrize("stage", IDENTITY_STAGES)
@pytest.mark.parametrize("field", ["project_id", "version_id"])
def test_design_identity_exact_boundaries_and_unicode_distinctions(stage, field):
    for value, valid in [("a" * 128, True), ("a" * 129, False),
                         ("\U0001f642" * 128, True), ("\U0001f642" * 129, False), (" \t", False)]:
        expected = dict(project_id="project", version_id="version")
        expected[field] = value
        stages = {key: dict(expected) for key in IDENTITY_STAGES}
        result = validate_saved_design_identity(expected, stages)
        assert result["verdict"] == ("PASS" if valid else "UNREADABLE")
        if not valid:
            assert result["reason"] == "design_identity_unreadable"
        normal = dict(project_id="project", version_id="version")
        stages = {key: dict(normal) for key in IDENTITY_STAGES}
        stages[stage][field] = value
        assert validate_saved_design_identity(normal, stages)["reason"] == (
            "design_identity_conflict" if valid else "stage_design_identity_unreadable")
    for exact, other in [("\u00e9", "e\u0301"), (" value ", "value")]:
        expected = dict(project_id="project", version_id="version")
        expected[field] = exact
        stages = {key: dict(expected) for key in IDENTITY_STAGES}
        stages[stage][field] = other
        assert validate_saved_design_identity(expected, stages) == {
            "verdict": "UNREADABLE", "reason": "design_identity_conflict", "stage": stage}


def assurance_envelope():
    manifest, bindings, inputs = provenance_fixture()
    identity = dict(project_id="project", version_id="version")
    return dict(evidence=complete_evidence(), manifest=manifest, bindings=bindings,
                inputs_hex={key: value.hex() for key, value in inputs.items()}, identity=identity,
                stage_identities={key: dict(identity) for key in IDENTITY_STAGES})


def test_strict_assurance_json_hex_and_envelope_boundaries():
    envelope = assurance_envelope()
    assert reconcile_assurance_json(json.dumps(envelope))["verdict"] == "PASS"
    for bad in (None, [], {"dwg": None}, {"dwg": "0"}, {"dwg": "gg"}, {"dwg": "ab cd"}):
        changed = dict(envelope, inputs_hex=bad)
        assert reconcile_assurance_json(json.dumps(changed))["reason"] == "input_hex_unreadable"
    for field in envelope:
        changed = dict(envelope)
        del changed[field]
        assert reconcile_assurance_json(json.dumps(changed))["reason"] == "assurance_envelope_unreadable"
    for value in ("", "00", "FF"):
        changed = deepcopy(envelope)
        changed["inputs_hex"]["dwg"] = value
        assert reconcile_assurance_json(json.dumps(changed))["checks"]["input_bytes"]["reason"] == "input_digest_conflict"
    assert reconcile_assurance_json('{"inputs_hex":{},"inputs_hex":{}}')["reason"] == "json_duplicate_key"


@pytest.mark.parametrize("kind,exit_code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4), ("malformed", 4)])
def test_provenance_cli_mode_uses_sanitized_offline_envelope(tmp_path, kind, exit_code):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    manifest, bindings, _ = provenance_fixture()
    evidence = complete_evidence()
    if kind == "FAIL":
        evidence["recommendations"][0]["group_count"] = 1
    if kind == "UNREADABLE":
        evidence["fixture_id"] = "foreign"
    text = json.dumps(dict(evidence=evidence, manifest=manifest, bindings=bindings))
    if kind == "malformed":
        text = '{"synthetic_private_canary":true}'
    result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--provenance"],
        input=text.encode(), capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == exit_code and result.stderr == b""
    assert len(result.stdout.splitlines()) == 1 and b"synthetic_private_canary" not in result.stdout
    observed = json.loads(result.stdout)
    assert observed["verdict"] == ("UNREADABLE" if kind == "malformed" else kind)
    if kind == "malformed":
        assert observed["reason"] == "provenance_envelope_unreadable"


def test_provenance_aggregate_json_preserves_siblings_and_strict_decoding():
    expected, fixtures = provenance_records()
    envelope = dict(expected_ids=expected, fixtures=fixtures)
    baseline = aggregate_provenance_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS"
    del fixtures[0]["bindings"]
    result = aggregate_provenance_json(json.dumps(envelope))
    assert result["verdict"] == "UNREADABLE"
    for key in expected[1:]:
        assert result["fixtures"][key] == baseline["fixtures"][key]
    assert aggregate_provenance_json('{"expected_ids":[],"expected_ids":[]}')["reason"] == "json_duplicate_key"
    for value in ({}, dict(envelope, extra=True)):
        assert aggregate_provenance_json(json.dumps(value))["reason"] == "provenance_aggregate_envelope_unreadable"


@pytest.mark.parametrize("bytes_bad,version_bad,readable_bad", [
    (a, b, c) for a in (False, True) for b in (False, True) for c in (False, True)])
def test_assurance_failure_isolation_keeps_all_diagnostics_and_inputs(bytes_bad, version_bad, readable_bad):
    envelope = assurance_envelope()
    if bytes_bad:
        envelope["inputs_hex"]["report"] = b"substituted".hex()
    if version_bad:
        envelope["stage_identities"]["table"]["version_id"] = "foreign"
    if readable_bad:
        envelope["evidence"]["recommendations"][0]["group_count"] = 1
    preserved = deepcopy(envelope)
    result = reconcile_assurance_json(json.dumps(envelope))
    assert result["verdict"] == ("UNREADABLE" if bytes_bad or version_bad else "FAIL" if readable_bad else "PASS")
    checks = result["checks"]
    assert checks["input_bytes"]["verdict"] == ("UNREADABLE" if bytes_bad else "PASS")
    assert checks["design_identity"]["verdict"] == ("UNREADABLE" if version_bad else "PASS")
    assert checks["provenance"]["reconciliation"] == reconcile_offline(envelope["evidence"])
    assert envelope == preserved


@pytest.mark.parametrize("swap", ["manifest", "bindings", "both"])
def test_equal_total_cross_fixture_manifest_swaps_are_rejected(swap):
    expected, fixtures = provenance_records()
    baseline = aggregate_provenance(expected, fixtures)
    for field in (["manifest", "bindings"] if swap == "both" else [swap]):
        fixtures[0][field], fixtures[1][field] = fixtures[1][field], fixtures[0][field]
    preserved = deepcopy(fixtures)
    result = aggregate_provenance_json(json.dumps(dict(expected_ids=expected, fixtures=fixtures)))
    assert result["verdict"] == "UNREADABLE"
    for key in expected[:2]:
        assert result["fixtures"][key]["verdict"] == "UNREADABLE"
        assert result["fixtures"][key]["reconciliation"]["verdict"] == "PASS"
    assert result["fixtures"][expected[2]] == baseline["fixtures"][expected[2]]
    assert fixtures == preserved


@pytest.mark.parametrize("kind,exit_code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4), ("typo", 4)])
def test_seven_step_cli_verdicts_and_sanitized_errors(tmp_path, kind, exit_code):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    envelope = seven_step_envelope()
    if kind == "FAIL":
        envelope["import"]["dwg_imported"] = False
    if kind == "UNREADABLE":
        envelope["import"] = None
    if kind == "typo":
        envelope["synthetic_private_canary"] = envelope.pop("import")
    result = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), "--seven-step"],
        input=json.dumps(envelope).encode(), capture_output=True, cwd=tmp_path, timeout=20)
    assert result.returncode == exit_code and not result.stderr
    assert len(result.stdout.splitlines()) == 1 and b"synthetic_private_canary" not in result.stdout
    observed = json.loads(result.stdout)
    assert observed["verdict"] == ("UNREADABLE" if kind == "typo" else kind)
    if kind == "typo":
        assert observed["reason"] == "seven_step_envelope_unreadable"


def seven_fixture_records():
    expected = ["case-a", "case-b", "case-c"]
    records = []
    for key in expected:
        envelope = seven_step_envelope()
        assurance = envelope["assurance"]
        assurance["evidence"]["fixture_id"] = assurance["manifest"]["fixture_id"] = key
        for binding in assurance["bindings"].values():
            binding["fixture_id"] = key
        records.append(dict(fixture_id=key, envelope=envelope))
    return expected, records


def test_three_fixture_seven_steps_preserves_every_sibling_and_coverage():
    expected, records = seven_fixture_records()
    baseline = aggregate_seven_steps(expected, records)
    assert baseline["verdict"] == "PASS"
    records[0]["envelope"]["import"]["normal_user"] = False
    records[1]["envelope"]["grid"] = None
    result = aggregate_seven_steps(expected, records)
    assert result["verdict"] == "UNREADABLE"
    assert [result["fixtures"][key]["verdict"] for key in expected] == ["FAIL", "UNREADABLE", "PASS"]
    assert result["fixtures"][expected[2]] == baseline["fixtures"][expected[2]]
    assert result["fixtures"][expected[1]]["assurance"]["verdict"] == "PASS"
    for altered in (records[:2], [records[0]] * 3, None):
        assert aggregate_seven_steps(expected, altered)["verdict"] == "UNREADABLE"
    records[0]["fixture_id"] = "foreign"
    assert aggregate_seven_steps(expected, records)["reason"] == "foreign_fixture_id"


def test_import_grid_provenance_rejects_swapped_claims_without_mutation():
    frozen, _, _ = provenance_fixture()
    imported, grid = deepcopy(frozen), deepcopy(frozen)
    assert validate_import_grid_provenance(frozen, imported, grid)["verdict"] == "PASS"
    for stage in ("import", "grid"):
        for field in ("fixture_id", "inputs"):
            altered = deepcopy(frozen)
            altered[field] = "foreign" if field == "fixture_id" else dict(frozen["inputs"], report="d" * 64)
            preserved = deepcopy(altered)
            result = validate_import_grid_provenance(frozen, altered if stage == "import" else imported,
                                                     altered if stage == "grid" else grid)
            assert result == {"verdict": "UNREADABLE", "reason": "import_grid_binding_conflict", "stage": stage}
            assert altered == preserved
        for malformed in (None, {}, dict(frozen, inputs={})):
            result = validate_import_grid_provenance(frozen, malformed if stage == "import" else imported,
                                                     malformed if stage == "grid" else grid)
            assert result["reason"] == "import_grid_binding_unreadable" and result["stage"] == stage
    assert validate_import_grid_provenance({}, imported, grid)["reason"] == "frozen_manifest_unreadable"


@pytest.mark.parametrize("bad_import,bad_grid,bad_assurance", [
    (a,b,c) for a in (False,True) for b in (False,True) for c in (False,True)])
def test_seven_step_cross_stage_failures_preserve_diagnostics(bad_import, bad_grid, bad_assurance):
    envelope = seven_step_envelope()
    if bad_import:
        envelope["import"]["report_imported"] = False
    if bad_grid:
        envelope["grid"]["rows"][0]["matches"] = ["foreign"]
    if bad_assurance:
        envelope["assurance"]["inputs_hex"]["report"] = b"substituted".hex()
    preserved = deepcopy(envelope)
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["verdict"] == ("UNREADABLE" if bad_assurance else "FAIL" if bad_import or bad_grid else "PASS")
    assert result["steps"]["1"]["verdict"] == ("FAIL" if bad_import else "PASS")
    assert result["steps"]["2"]["verdict"] == ("FAIL" if bad_grid else "PASS")
    assert result["assurance"]["verdict"] == ("UNREADABLE" if bad_assurance else "PASS")
    assert envelope == preserved


def test_seven_step_direct_json_differential_mutation_corpus():
    base = seven_step_envelope()
    corpus = [base]
    for value in (None, {}, dict(base["import"], normal_user=False), dict(base["import"], normal_user=1)):
        corpus.append(dict(deepcopy(base), **{"import": value}))
    for value in (None, {}, dict(base["grid"], rows=[])):
        corpus.append(dict(deepcopy(base), grid=value))
    for field in ("normal_user", "dwg_imported", "report_imported"):
        altered = deepcopy(base)
        del altered["import"][field]
        corpus.append(altered)
    altered = deepcopy(base)
    altered["assurance"]["evidence"]["recommendations"][0]["group_count"] = 1
    corpus.append(altered)
    for envelope in corpus:
        source = envelope["assurance"]
        assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
            supplied_inputs={key: bytes.fromhex(value) for key,value in source["inputs_hex"].items()},
            expected_identity=source["identity"], stage_identities=source["stage_identities"])
        preserved = deepcopy(envelope)
        direct = reconcile_seven_steps(envelope["import"], envelope["grid"], assurance)
        strict = reconcile_seven_steps_json(json.dumps(envelope))
        assert json.loads(serialize_result(direct)) == json.loads(serialize_result(strict))
        assert strict == reconcile_seven_steps_json(json.dumps(envelope)) and envelope == preserved
