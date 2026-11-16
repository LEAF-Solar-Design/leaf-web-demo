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
    aggregate_seven_steps_json, reconcile_bound_seven_steps,
    reconcile_bound_seven_steps_json, aggregate_bound_seven_steps,
    aggregate_bound_seven_steps_json,
    diff_snapshot_rows,
    validate_grid_string_ownership, compare_grid_optimizer_totals,
    compare_grid_oracle_totals, compare_ownership_snapshots,
    compare_bound_ownership_snapshots, compare_grid_optimizer_snapshots,
    compare_grid_panel_totals, compare_grid_panel_oracle_totals,
    compare_panel_string_ids, compare_grid_panel_snapshots, compare_bound_panel_snapshots,
    compare_grid_dc_totals, compare_grid_dc_oracle_totals, compare_grid_dc_snapshots, compare_dc_string_ids,
    compare_bound_dc_snapshots, compare_before_panel_counts, compare_before_dc_values,
    compare_grid_string_totals, compare_grid_string_oracle_totals,
    validate_grid_inverter_ownership, compare_grid_inverter_totals, compare_grid_inverter_oracle_totals, compare_inverter_snapshots,
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
    ["--aggregate", "--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate", "--seven-step", "--seven-step-aggregate", "--seven-step-bound", "--seven-step-bound-aggregate"], 2)))
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


@pytest.mark.parametrize("mode", ["--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate", "--seven-step", "--seven-step-bound"])
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


@pytest.mark.parametrize("mode", ["--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate", "--seven-step", "--seven-step-bound"])
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


def inverter_evidence():
    return dict(oracle_panels=[dict(string_id="A", panels=10),dict(string_id="B", panels=14)],
                oracle_dc_power=[dict(string_id="A", kw_dc="4"),dict(string_id="B", kw_dc="5.6")], inverter_loads=dict(strings={"INV-A":2}, optimizers={"INV-A":12}, panels={"INV-A":24}, kw_dc={"INV-A":"9.6"}),
                **{field: [dict(inverter_id="INV-A", grid_id="g")]
                 for field in ("saved_inverters", "before_inverters", "reopened_inverters")},
                **{field: [dict(string_id=key, inverter_id="INV-A") for key in ("A", "B")]
                   for field in ("oracle_assignments", "saved_assignments", "before_assignments", "reopened_assignments")})


def seven_step_envelope():
    assurance = assurance_envelope()
    assurance["evidence"].update(inverter_evidence())
    return {"import": dict(normal_user=True, dwg_imported=True, report_imported=True),
            "grid": dict(expected_grid_ids=["g"], saved_group_ids=["p"],
                         rows=[dict(grid_id="g", matches=["p"], cause=None)], require_all_matched=True,
                         string_assignments=[dict(grid_id="g", string_id=row["string_id"]) for row in ORACLE],
                         optimizer_totals={"g": sum(row["optimizers"] for row in ORACLE)},
                         reopened_string_assignments=[dict(grid_id="g", string_id=row["string_id"]) for row in ORACLE],
                         panel_totals={"g": 24}, panel_counts=[dict(string_id="A", panels=10), dict(string_id="B", panels=14)],
                         before_panel_counts=[dict(string_id="A", panels=10), dict(string_id="B", panels=14)],
                         reopened_panel_counts=[dict(string_id="A", panels=10), dict(string_id="B", panels=14)],
                         string_totals={"g": 2}, inverter_totals={"g": 1}, dc_totals={"g": "9.6"}, dc_power=[dict(string_id="A", kw_dc="4"), dict(string_id="B", kw_dc="5.6")],
                         before_dc_power=[dict(string_id="A", kw_dc="4"), dict(string_id="B", kw_dc="5.6")],
                         reopened_dc_power=[dict(string_id="A", kw_dc="4"), dict(string_id="B", kw_dc="5.6")]),
            "assurance": assurance}


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
                 rows=[dict(grid_id="g", matches=["p"], cause=None)], require_all_matched=True,
                         string_assignments=[dict(grid_id="g", string_id=row["string_id"]) for row in ORACLE],
                         optimizer_totals={"g": sum(row["optimizers"] for row in ORACLE)},
                         reopened_string_assignments=[dict(grid_id="g", string_id=row["string_id"]) for row in ORACLE],
                         panel_totals={"g": 24}, panel_counts=[dict(string_id="A", panels=10), dict(string_id="B", panels=14)],
                         before_panel_counts=[dict(string_id="A", panels=10), dict(string_id="B", panels=14)],
                         reopened_panel_counts=[dict(string_id="A", panels=10), dict(string_id="B", panels=14)],
                         string_totals={"g": 2}, inverter_totals={"g": 1}, dc_totals={"g": "9.6"}, dc_power=[dict(string_id="A", kw_dc="4"), dict(string_id="B", kw_dc="5.6")],
                         before_dc_power=[dict(string_id="A", kw_dc="4"), dict(string_id="B", kw_dc="5.6")],
                         reopened_dc_power=[dict(string_id="A", kw_dc="4"), dict(string_id="B", kw_dc="5.6")])
    assurance["evidence"].update(inverter_evidence())
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
    if mode in ("--seven-step-aggregate", "--seven-step-bound-aggregate"):
        expected, records = bound_seven_fixture_records() if mode == "--seven-step-bound-aggregate" else seven_fixture_records()
        return dict(expected_ids=expected, fixtures=records)
    if mode == "--seven-step-bound":
        return bound_seven_envelope()
    if mode == "--seven-step":
        return seven_step_envelope()
    if mode == "--provenance":
        manifest, bindings, _ = provenance_fixture()
        return dict(evidence=complete_evidence(), manifest=manifest, bindings=bindings)
    if mode == "--assurance":
        return assurance_envelope()
    expected, records = assurance_records() if mode == "--assurance-aggregate" else provenance_records()
    return dict(expected_ids=expected, fixtures=records)


@pytest.mark.parametrize("mode,decoder", [
    ("--provenance", reconcile_provenance_json), ("--assurance", reconcile_assurance_json),
    ("--provenance-aggregate", aggregate_provenance_json), ("--assurance-aggregate", aggregate_assurance_json),
    ("--seven-step-aggregate", aggregate_seven_steps_json), ("--seven-step-bound", reconcile_bound_seven_steps_json),
    ("--seven-step-bound-aggregate", aggregate_bound_seven_steps_json)])
@pytest.mark.parametrize("character", ["x", "\U0001f642"])
def test_new_mode_character_and_byte_limits_match_direct_decoder(tmp_path, mode, decoder, character):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    payload = cli_payload(mode)
    target = payload["fixtures"][0] if "fixtures" in payload else payload
    if "envelope" in target:
        target = target["envelope"]
    if "assurance" in target:
        target = target["assurance"]
    evidence = target["evidence"]
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


@pytest.mark.parametrize("mode", ["--provenance", "--assurance", "--provenance-aggregate", "--assurance-aggregate", "--seven-step", "--seven-step-bound"])
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





def test_strict_seven_step_aggregate_json_and_readable_siblings():
    expected, records = seven_fixture_records()
    payload = dict(expected_ids=expected, fixtures=records)
    baseline = aggregate_seven_steps_json(json.dumps(payload))
    assert baseline["verdict"] == "PASS"
    records[0]["envelope"]["import"] = None
    observed = aggregate_seven_steps_json(json.dumps(payload))
    assert observed["verdict"] == "UNREADABLE"
    for key in expected[1:]:
        assert observed["fixtures"][key] == baseline["fixtures"][key]
    for text, reason in [('{}', 'seven_step_aggregate_envelope_unreadable'),
                         ('{"fixtures":[],"fixtures":[]}', 'json_duplicate_key'),
                         (' ' * (MAX_JSON_CHARS + 1), 'json_input_too_large')]:
        assert aggregate_seven_steps_json(text)["reason"] == reason
    for value in (dict(payload, extra=True), dict(fixtures=records), dict(expected_ids=expected)):
        assert aggregate_seven_steps_json(json.dumps(value))["reason"] == "seven_step_aggregate_envelope_unreadable"


def test_bound_seven_steps_retains_diagnostics_for_swapped_equal_total_claims():
    envelope = seven_step_envelope()
    frozen = deepcopy(envelope["assurance"]["manifest"])
    imported, grid = deepcopy(frozen), deepcopy(frozen)
    assert reconcile_bound_seven_steps(envelope, frozen, imported, grid)["verdict"] == "PASS"
    envelope["import"]["dwg_imported"] = False
    assert reconcile_bound_seven_steps(envelope, frozen, imported, grid)["verdict"] == "FAIL"
    for stage in ("import", "grid"):
        for field in ("fixture_id", "inputs"):
            altered = deepcopy(frozen)
            altered[field] = "foreign" if field == "fixture_id" else dict(frozen["inputs"], report="d" * 64)
            preserved = deepcopy((envelope, frozen, altered))
            result = reconcile_bound_seven_steps(envelope, frozen, altered if stage == "import" else imported,
                                                 altered if stage == "grid" else grid)
            assert result["verdict"] == "UNREADABLE" and result["provenance"]["stage"] == stage
            assert result["reconciliation"] == reconcile_seven_steps_json(json.dumps(envelope))
            assert (envelope, frozen, altered) == preserved
    foreign = deepcopy(envelope)
    foreign["assurance"]["manifest"]["fixture_id"] = "foreign"
    result = reconcile_bound_seven_steps(foreign, frozen, imported, grid)
    assert result["provenance"]["reason"] == "seven_step_manifest_conflict"
    assert result["reconciliation"]["steps"]["1"]["verdict"] == "FAIL"


def test_seven_aggregate_ambiguous_duplicate_recovery_preserves_siblings():
    expected, records = seven_fixture_records()
    baseline = aggregate_seven_steps(expected, records)
    valid = records[0]
    malformed = dict(fixture_id=expected[0], envelope=None)
    foreign = deepcopy(valid)
    foreign["envelope"]["assurance"]["evidence"]["fixture_id"] = "foreign"
    for ordering in permutations([valid, malformed, foreign]):
        changed = list(ordering) + records[1:]
        preserved = deepcopy(changed)
        observed = aggregate_seven_steps_json(json.dumps(dict(expected_ids=expected, fixtures=changed)))
        assert observed["verdict"] == "UNREADABLE" and expected[0] not in observed["fixtures"]
        for key in expected[1:]:
            assert observed["fixtures"][key] == baseline["fixtures"][key]
        assert changed == preserved
    assert aggregate_seven_steps(expected, records) == baseline


def test_seven_step_aggregate_deterministic_subprocess_mutation_orders(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    expected, records = seven_fixture_records()
    for row in records:
        grid = row["envelope"]["grid"]
        grid["expected_grid_ids"].append("g2")
        grid["saved_group_ids"].append("p2")
        grid["rows"].append(dict(grid_id="g2", matches=["p2"], cause=None))
        grid["optimizer_totals"]["g2"] = 0
        grid["panel_totals"]["g2"] = 0
        grid["dc_totals"]["g2"] = 0
        grid["string_totals"]["g2"] = 0
        grid["inverter_totals"]["g2"] = 0
    records[0]["envelope"]["import"]["report_imported"] = False
    records[1]["envelope"]["grid"]["rows"][0]["matches"] = ["foreign"]
    records[1]["envelope"]["assurance"]["inputs_hex"]["report"] = b"swapped".hex()
    program = ("import sys;sys.path.insert(0,sys.argv[1]);from benches.x05_reconcile import "
        "aggregate_seven_steps,decode_evidence_json,serialize_result;"
        "v=decode_evidence_json(sys.stdin.read())['evidence'];"
        "r=aggregate_seven_steps(v['expected_ids'],v['fixtures']);print(serialize_result(r));"
        "sys.exit({'PASS':0,'FAIL':1,'UNREADABLE':4}[r['verdict']])")
    outputs = []
    for seed in ("0", "73"):
        for reverse in (False, True):
            changed = deepcopy(records)
            if reverse:
                changed.reverse()
                for row in changed:
                    grid = row["envelope"]["grid"]
                    for field in ("expected_grid_ids", "saved_group_ids", "rows"):
                        grid[field].reverse()
                    identities = row["envelope"]["assurance"]["stage_identities"]
                    row["envelope"]["assurance"]["stage_identities"] = dict(reversed(list(identities.items())))
            result = subprocess.run([sys.executable, "-P", "-c", program, str(Path(module.__file__).resolve().parents[1])],
                input=json.dumps(dict(expected_ids=list(reversed(expected)) if reverse else expected, fixtures=changed)).encode(),
                capture_output=True, cwd=tmp_path, timeout=20,
                env={"PYTHONHASHSEED": seed, "SystemRoot": "C:\\Windows"})
            assert result.returncode == 4 and not result.stderr
            assert json.loads(result.stdout)["fixtures"][expected[2]]["verdict"] == "PASS"
            outputs.append(result.stdout)
    assert len(set(outputs)) == 1


@pytest.mark.parametrize("kind,code", [("PASS",0),("FAIL",1),("UNREADABLE",4)])
def test_seven_step_aggregate_cli_verdicts_and_siblings(tmp_path, kind, code):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    expected, records = seven_fixture_records()
    if kind != "PASS":
        records[0]["envelope"]["import"]["normal_user"] = False
    if kind == "UNREADABLE":
        records[1]["envelope"]["grid"] = None
    result = subprocess.run([sys.executable,"-P",str(Path(module.__file__).resolve()),"--seven-step-aggregate"],
        input=json.dumps(dict(expected_ids=expected,fixtures=records)).encode(),capture_output=True,cwd=tmp_path,timeout=20)
    assert result.returncode == code and not result.stderr and len(result.stdout.splitlines()) == 1
    observed = json.loads(result.stdout)
    assert observed["verdict"] == kind and observed["fixtures"][expected[2]]["verdict"] == "PASS"


def bound_seven_envelope():
    envelope = seven_step_envelope()
    manifest = deepcopy(envelope["assurance"]["manifest"])
    return dict(envelope=envelope,manifest=manifest,import_binding=deepcopy(manifest),grid_binding=deepcopy(manifest))


def test_strict_bound_seven_step_json_envelope_and_duplicate_rejection():
    source = bound_seven_envelope()
    assert reconcile_bound_seven_steps_json(json.dumps(source))["verdict"] == "PASS"
    for field in source:
        changed = deepcopy(source)
        del changed[field]
        assert reconcile_bound_seven_steps_json(json.dumps(changed))["reason"] == "bound_seven_step_envelope_unreadable"
    assert reconcile_bound_seven_steps_json('{"envelope":{},"envelope":{}}')["reason"] == "json_duplicate_key"
    assert reconcile_bound_seven_steps_json(' '*(MAX_JSON_CHARS+1))["reason"] == "json_input_too_large"
    source["import_binding"]["inputs"]["dwg"] = "d"*64
    result = reconcile_bound_seven_steps_json(json.dumps(source))
    assert result["verdict"] == "UNREADABLE" and result["reconciliation"]["verdict"] == "PASS"


def test_bound_seven_step_fixture_aggregation_swaps_preserve_siblings():
    expected, records = seven_fixture_records()
    for row in records:
        manifest = deepcopy(row["envelope"]["assurance"]["manifest"])
        row.update(manifest=manifest,import_binding=deepcopy(manifest),grid_binding=deepcopy(manifest))
    baseline = aggregate_bound_seven_steps(expected,records)
    assert baseline["verdict"] == "PASS"
    for field in ("import_binding","grid_binding"):
        changed = deepcopy(records)
        changed[0][field],changed[1][field] = changed[1][field],changed[0][field]
        preserved = deepcopy(changed)
        result = aggregate_bound_seven_steps(expected,changed)
        assert result["verdict"] == "UNREADABLE"
        assert result["fixtures"][expected[2]] == baseline["fixtures"][expected[2]]
        for key in expected[:2]:
            assert result["fixtures"][key]["provenance"]["reason"] == "import_grid_binding_conflict"
            assert result["fixtures"][key]["reconciliation"]["verdict"] == "PASS"
        assert changed == preserved
    duplicate = [dict(fixture_id=expected[0],envelope=None)]+records
    assert expected[0] not in aggregate_bound_seven_steps(expected,duplicate)["fixtures"]


def test_seven_step_aggregate_direct_json_mutation_diagnostics_match():
    expected, base = seven_fixture_records()
    corpus = [base,base[:2]]
    for stage in ("import","grid","assurance"):
        altered = deepcopy(base)
        altered[0]["envelope"][stage] = None
        corpus.append(altered)
    altered = deepcopy(base)
    altered[0]["envelope"]["import"]["normal_user"] = False
    corpus.append(altered)
    altered = deepcopy(base)
    del altered[0]["envelope"]["grid"]
    corpus.append(altered)
    for records in corpus:
        preserved = deepcopy(records)
        direct = aggregate_seven_steps(expected,records)
        decoded = aggregate_seven_steps_json(json.dumps(dict(expected_ids=expected,fixtures=records)))
        assert json.loads(serialize_result(direct)) == json.loads(serialize_result(decoded))
        assert records == preserved


def test_aggregate_combined_output_budget_is_sanitized_before_emission(monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace
    import sys
    from benches import x05_reconcile as module
    fixtures = {key:dict(verdict="PASS",reason="synthetic",payload="x"*90000,count=123456)
                for key in ("a","b","c")}
    for value in fixtures.values():
        assert len(serialize_result(value)) < MAX_RESULT_CHARS
    combined = dict(verdict="PASS",reason="offline_seven_step_fixtures",fixtures=fixtures)
    with pytest.raises(ValueError,match="^json_result_too_large$"):
        serialize_result(combined)
    # Controlled result injection exercises the CLI emission boundary.
    monkeypatch.setattr(module,"aggregate_seven_steps_json",lambda text:combined)
    output,errors = StringIO(),StringIO()
    monkeypatch.setattr(sys,"stdin",SimpleNamespace(buffer=BytesIO(b"{}")))
    monkeypatch.setattr(sys,"stdout",output)
    monkeypatch.setattr(sys,"stderr",errors)
    assert module.main(["--seven-step-aggregate"]) == 4 and not errors.getvalue()
    assert output.getvalue() == '{"reason":"json_result_too_large","verdict":"UNREADABLE"}\n'
    assert all(value["count"] == 123456 for value in fixtures.values())





def bound_seven_fixture_records():
    expected, records = seven_fixture_records()
    for row in records:
        manifest = deepcopy(row["envelope"]["assurance"]["manifest"])
        row.update(manifest=manifest,import_binding=deepcopy(manifest),grid_binding=deepcopy(manifest))
    return expected,records


def test_strict_bound_seven_aggregate_json_limits_and_readable_siblings():
    expected,records = bound_seven_fixture_records()
    payload = dict(expected_ids=expected,fixtures=records)
    baseline = aggregate_bound_seven_steps_json(json.dumps(payload))
    assert baseline["verdict"] == "PASS"
    records[0]["import_binding"] = None
    observed = aggregate_bound_seven_steps_json(json.dumps(payload))
    assert observed["verdict"] == "UNREADABLE"
    for key in expected[1:]:
        assert observed["fixtures"][key] == baseline["fixtures"][key]
    for text,reason in [('{}','bound_seven_step_aggregate_envelope_unreadable'),
                        ('{"fixtures":[],"fixtures":[]}','json_duplicate_key'),
                        (' '*(MAX_JSON_CHARS+1),'json_input_too_large')]:
        assert aggregate_bound_seven_steps_json(text)["reason"] == reason


@pytest.mark.parametrize("kind,code", [("PASS",0),("FAIL",1),("UNREADABLE",4),("malformed",4)])
def test_bound_seven_cli_verdicts_and_sanitized_envelopes(tmp_path,kind,code):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    payload = bound_seven_envelope()
    if kind == "FAIL":
        payload["envelope"]["import"]["normal_user"] = False
    if kind == "UNREADABLE":
        payload["grid_binding"] = None
    if kind == "malformed":
        payload = {"synthetic_private_canary":True}
    run = subprocess.run([sys.executable,"-P",str(Path(module.__file__).resolve()),"--seven-step-bound"],
        input=json.dumps(payload).encode(),capture_output=True,cwd=tmp_path,timeout=20)
    assert run.returncode == code and not run.stderr and len(run.stdout.splitlines()) == 1
    assert b"synthetic_private_canary" not in run.stdout
    assert json.loads(run.stdout)["verdict"] == ("UNREADABLE" if kind == "malformed" else kind)


def test_bound_seven_aggregate_ambiguous_records_never_pass_and_recover():
    expected,records = bound_seven_fixture_records()
    baseline = aggregate_bound_seven_steps(expected,records)
    valid = records[0]
    malformed = dict(fixture_id=expected[0],envelope=None)
    foreign = deepcopy(valid)
    foreign["envelope"]["assurance"]["evidence"]["fixture_id"] = "foreign"
    for order in permutations([valid,malformed,foreign]):
        changed = list(order)+records[1:]
        preserved = deepcopy(changed)
        result = aggregate_bound_seven_steps_json(json.dumps(dict(expected_ids=expected,fixtures=changed)))
        assert result["verdict"] == "UNREADABLE" and expected[0] not in result["fixtures"]
        for key in expected[1:]:
            assert result["fixtures"][key] == baseline["fixtures"][key]
        assert changed == preserved
    assert aggregate_bound_seven_steps(expected,records) == baseline


@pytest.mark.parametrize("field",["import_binding","grid_binding"])
def test_bound_seven_binding_schema_mutations_keep_reconciliation(field):
    base = bound_seven_envelope()
    mutations = [None,[],42,{},dict(base[field],inputs=dict(base[field]["inputs"],foreign="a"*64)),
                 dict(base[field],inputs={key:value for key,value in base[field]["inputs"].items() if key != "bom"})]
    for mutation in mutations:
        changed = deepcopy(base)
        changed[field] = mutation
        preserved = deepcopy(changed)
        strict = reconcile_bound_seven_steps_json(json.dumps(changed))
        direct = reconcile_bound_seven_steps(changed["envelope"],changed["manifest"],changed["import_binding"],changed["grid_binding"])
        assert strict == direct and strict["verdict"] == "UNREADABLE"
        assert strict["reconciliation"]["verdict"] == "PASS" and changed == preserved
    missing = deepcopy(base)
    del missing[field]
    observed = reconcile_bound_seven_steps_json(json.dumps(missing))
    assert observed["reason"] == "bound_seven_step_envelope_unreadable"
    assert observed["reconciliation"]["verdict"] == "PASS"


def test_bound_aggregate_determinism_subprocess_bindings_and_manifests(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    expected,records = bound_seven_fixture_records()
    records[0]["import_binding"]["inputs"]["dwg"] = "d"*64
    records[1]["grid_binding"]["inputs"]["report"] = "e"*64
    records[1]["envelope"]["import"]["report_imported"] = False
    program = ("import sys;sys.path.insert(0,sys.argv[1]);from benches.x05_reconcile import "
        "aggregate_bound_seven_steps,decode_evidence_json,serialize_result;"
        "v=decode_evidence_json(sys.stdin.read())['evidence'];"
        "r=aggregate_bound_seven_steps(v['expected_ids'],v['fixtures']);print(serialize_result(r));"
        "sys.exit({'PASS':0,'FAIL':1,'UNREADABLE':4}[r['verdict']])")
    outputs=[]
    for seed in ("0","73"):
        for reverse in (False,True):
            changed=deepcopy(records)
            if reverse:
                changed.reverse()
                for row in changed:
                    for field in ("manifest","import_binding","grid_binding"):
                        row[field]["inputs"]=dict(reversed(list(row[field]["inputs"].items())))
                        row[field]=dict(reversed(list(row[field].items())))
            run=subprocess.run([sys.executable,"-P","-c",program,str(Path(module.__file__).resolve().parents[1])],
                input=json.dumps(dict(expected_ids=list(reversed(expected)) if reverse else expected,fixtures=changed)).encode(),
                capture_output=True,cwd=tmp_path,timeout=20,env={"PYTHONHASHSEED":seed,"SystemRoot":"C:\\Windows"})
            assert run.returncode==4 and not run.stderr
            assert json.loads(run.stdout)["fixtures"][expected[2]]["verdict"]=="PASS"
            outputs.append(run.stdout)
    assert len(set(outputs))==1


@pytest.mark.parametrize("kind,code",[("PASS",0),("FAIL",1),("UNREADABLE",4)])
def test_bound_aggregate_cli_verdicts_and_siblings(tmp_path,kind,code):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    expected,records=bound_seven_fixture_records()
    if kind != "PASS":
        records[0]["envelope"]["import"]["normal_user"]=False
    if kind == "UNREADABLE":
        records[1]["import_binding"]=None
    run=subprocess.run([sys.executable,"-P",str(Path(module.__file__).resolve()),"--seven-step-bound-aggregate"],
        input=json.dumps(dict(expected_ids=expected,fixtures=records)).encode(),capture_output=True,cwd=tmp_path,timeout=20)
    assert run.returncode==code and not run.stderr
    result=json.loads(run.stdout)
    assert result["verdict"]==kind and result["fixtures"][expected[2]]["verdict"]=="PASS"


def test_bound_aggregate_direct_json_mutation_differential_corpus():
    expected,base=bound_seven_fixture_records()
    corpus=[base,base[:2]]
    for field in ("envelope","manifest","import_binding","grid_binding"):
        for value in (None,{}):
            changed=deepcopy(base)
            changed[0][field]=value
            corpus.append(changed)
    for records in corpus:
        preserved=deepcopy(records)
        direct=aggregate_bound_seven_steps(expected,records)
        strict=aggregate_bound_seven_steps_json(json.dumps(dict(expected_ids=expected,fixtures=records)))
        assert json.loads(serialize_result(direct))==json.loads(serialize_result(strict))
        assert records==preserved


def test_bound_aggregation_reconciles_only_once_per_readable_unique_fixture(monkeypatch):
    from benches import x05_reconcile as module
    expected,records=bound_seven_fixture_records()
    baseline=aggregate_bound_seven_steps(expected,records)
    seven,offline=module._seven_step_envelope,module.reconcile_offline
    calls=dict(seven=0,offline=0)
    def checked_seven(value):
        calls["seven"]+=1
        return seven(value)
    def checked_offline(value):
        calls["offline"]+=1
        return offline(value)
    monkeypatch.setattr(module,"_seven_step_envelope",checked_seven)
    monkeypatch.setattr(module,"reconcile_offline",checked_offline)
    assert aggregate_bound_seven_steps(expected,records)==baseline
    assert calls==dict(seven=3,offline=3)
    calls.update(seven=0,offline=0)
    observed=aggregate_bound_seven_steps(expected,records[:2])
    assert observed["verdict"]=="UNREADABLE" and calls==dict(seven=2,offline=2)
    for key in expected[:2]:
        assert observed["fixtures"][key]==baseline["fixtures"][key]
    calls.update(seven=0,offline=0)
    ambiguous=[dict(fixture_id=expected[0],envelope=None)]+records
    observed=aggregate_bound_seven_steps(expected,ambiguous)
    assert calls==dict(seven=2,offline=2) and expected[0] not in observed["fixtures"]
    for key in expected[1:]:
        assert observed["fixtures"][key]==baseline["fixtures"][key]


def test_exact_serializer_node_budget_below_character_and_depth_limits():
    from benches.x05_reconcile import MAX_RESULT_NODES
    for leaves,valid in ((MAX_RESULT_NODES-1,True),(MAX_RESULT_NODES,False)):
        value=[0]*leaves
        preserved=list(value)
        # One root list plus each scalar gives exactly100000 or100001 visits.
        assert 2*leaves+1 < MAX_RESULT_CHARS
        if valid:
            encoded=serialize_result(value)
            assert json.loads(encoded)==value
        else:
            with pytest.raises(ValueError,match="^json_result_too_large$"):
                serialize_result(value)
        assert value==preserved


def test_snapshot_diff_exact_totals_capped_examples_and_permutation_invariance():
    _,table=table_fixture()
    before=dict(strings=[dict(string_id=f"S{i:03}",optimizers=5) for i in range(60)],table_rows=table)
    reopened=deepcopy(before)
    for row in reopened["strings"][:30]:
        row["optimizers"]=6
    reopened["strings"]=reopened["strings"][5:]+[dict(string_id="new",optimizers=5)]
    reopened["table_rows"][0]["studio"]+=1
    preserved=deepcopy((before,reopened))
    result=diff_snapshot_rows(before,reopened)
    assert result["verdict"]=="FAIL"
    assert result["counts"]==dict(added=1,removed=5,changed=25,metrics=1)
    assert len(result["string_examples"])==MAX_STRING_EXAMPLES and result["metrics"]==["panels"]
    assert result["string_examples"][0]==dict(string_id="S000",before=5,reopened=None)
    reversed_rows=deepcopy(reopened)
    reversed_rows["strings"].reverse()
    reversed_rows["table_rows"].reverse()
    assert diff_snapshot_rows(before,reversed_rows)==result
    assert (before,reopened)==preserved
    assert diff_snapshot_rows(before,before)["verdict"]=="PASS"
    for invalid in (None,{},dict(strings=[],table_rows=table)):
        assert diff_snapshot_rows(before,invalid)["reason"]=="snapshot_diff_rows_unreadable"


def test_small_grid_exhaustive_independent_reference_verdicts_and_totals():
    from collections import Counter
    from itertools import product
    options=[([],"SOLAREDGE_GRID_UNMATCHED"),([],"SOLAREDGE_GRID_AMBIGUOUS"),([],None),
             (["p1"],None),(["p2"],None),(["foreign"],None),(["p1","p1"],None),(["p1"],"SOLAREDGE_GRID_UNMATCHED")]
    choices=[dict(grid_id=key,matches=matches,cause=cause) for key in ("g1","g2","foreign") for matches,cause in options]
    for first,second in product(choices,repeat=2):
        rows=[first,second]
        for require_all in (False,True):
            result=compare_grid_correspondence(["g1","g2"],["p1","p2"],rows,require_all_matched=require_all)
            if any(not row["matches"] and row["cause"] is None for row in rows):
                assert result["verdict"]=="UNREADABLE"
                continue
            grid_counts=Counter(row["grid_id"] for row in rows)
            assignments=[group for row in rows for group in row["matches"]]
            group_counts=Counter(assignments)
            total=sum(row["grid_id"] not in ["g1","g2"] for row in rows)
            total+=sum(count-1 for count in grid_counts.values())
            total+=sum(key not in grid_counts for key in ("g1","g2"))
            total+=sum(bool(row["matches"]) and (len(row["matches"])!=1 or row["cause"] is not None) for row in rows)
            total+=sum(group not in ["p1","p2"] for group in assignments)
            total+=sum(count-1 for count in group_counts.values())
            total+=sum(not row["matches"] and require_all for row in rows)
            assert result["verdict"]==("FAIL" if total else "PASS")
            assert result["error_count"]==total


def test_conflicting_duplicate_discrepancy_records_reject_in_every_order_and_keep_other_stages():
    evidence=complete_evidence()
    baseline=reconcile_offline(evidence)
    record=dict(fixture_id="synthetic",metric="panels",delta=1,name="revision-a",frozen_at="2026-11-15T12:00:00Z")
    conflicts=[record,dict(record,delta=2,name="revision-b"),dict(record,frozen_at="2026-11-15T12:30:00Z")]
    for order in permutations(conflicts):
        changed=deepcopy(evidence)
        changed["discrepancy_records"]=list(order)
        preserved=deepcopy(changed)
        observed=reconcile_offline(changed)
        assert observed["verdict"]=="UNREADABLE" and observed["stages"]["policy"]["reason"]=="duplicate_discrepancy_record"
        for stage in baseline["stages"]:
            if stage!="policy":
                assert observed["stages"][stage]==baseline["stages"][stage]
        assert changed==preserved


def all_cli_payloads():
    modes=[None,"--aggregate","--provenance","--assurance","--provenance-aggregate","--assurance-aggregate",
           "--seven-step","--seven-step-aggregate","--seven-step-bound","--seven-step-bound-aggregate"]
    return [(mode,framed_payload(mode)) for mode in modes]


def test_cli_ascii_bytes_are_independent_of_output_encoding_and_utf8_mode(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    for mode,payload in all_cli_payloads():
        # Keep supplied identities intact and add a multibyte non-BMP string ID
        # consistently to all synthetic snapshots so it reaches comparison.
        def replace(value):
            if type(value) is str:
                return value.replace('"A"','"\\ud83d\\ude42"')
            return value
        text=replace(json.dumps(payload))
        outputs=[]
        for encoding,utf8 in (("ascii","0"),("utf-16","0"),("utf-8","1")):
            args=[sys.executable,"-P",str(Path(module.__file__).resolve())]+([mode] if mode else [])
            run=subprocess.run(args,input=text.encode(),capture_output=True,cwd=tmp_path,timeout=20,
                env={"PYTHONIOENCODING":encoding,"PYTHONUTF8":utf8,"SystemRoot":"C:\\Windows"})
            assert run.returncode in (0,1,4) and not run.stderr
            outputs.append((run.returncode,run.stdout))
        assert len(set(outputs))==1


def test_serializer_independent_exact_character_accounting_vectors():
    from decimal import Decimal
    vectors=[dict(payload="",items=[0,True,None],quote='"',control="\n",id="\U0001f642"),
             dict(payload="",decimal=Decimal("1.25"),nested={"a":["\t", "\\"]})]
    for value in vectors:
        # Independent literal token lengths: separators and Unicode escapes are
        # counted by standard json for non-Decimal values; Decimal has known1.25.
        reference=json.dumps({key:("DECIMAL" if type(item) is Decimal else item) for key,item in value.items()},
            sort_keys=True,ensure_ascii=True,separators=(",",":"))
        if "decimal" in value:
            reference=reference.replace('"DECIMAL"','1.25')
        padding=MAX_RESULT_CHARS-len(reference)
        value["payload"]="x"*padding
        expected=reference.replace('"payload":""','"payload":"'+"x"*padding+'"')
        assert len(expected)==MAX_RESULT_CHARS and serialize_result(value)==expected
        value["payload"]+="x"
        with pytest.raises(ValueError,match="^json_result_too_large$"):
            serialize_result(value)


def test_explicit_grid_string_ownership_rejects_missing_duplicate_and_foreign_assignments():
    grids,strings=["g1","g2"],["A","B"]
    rows=[dict(grid_id="g1",string_id="A"),dict(grid_id="g2",string_id="B")]
    assert validate_grid_string_ownership(grids,strings,rows)["verdict"]=="PASS"
    cases=[rows[:1],rows+[rows[0]],rows+[dict(grid_id="g1",string_id="foreign")],
           [dict(grid_id="foreign",string_id="A"),rows[1]]]
    for changed in cases:
        preserved=deepcopy(changed)
        result=validate_grid_string_ownership(grids,strings,changed)
        assert result["verdict"]=="FAIL" and any(result["counts"].values())
        assert changed==preserved
        assert validate_grid_string_ownership(list(reversed(grids)),list(reversed(strings)),list(reversed(changed)))==result
    for invalid in (None,[{}],[dict(grid_id="g1",string_id=None)]):
        assert validate_grid_string_ownership(grids,strings,invalid)["verdict"]=="UNREADABLE"
    large=[dict(grid_id="g1",string_id=f"S{i:03}") for i in range(100)]
    result=validate_grid_string_ownership(grids,strings,large)
    assert result["counts"]["foreign_string"]==100 and result["counts"]["missing"]==2
    assert len(result["examples"]["foreign_string"])==MAX_STRING_EXAMPLES


def test_reopen_composition_includes_deterministic_localized_row_changes():
    evidence=complete_evidence()
    evidence["reopened_rows"]["strings"][0]["optimizers"]=7
    evidence["reopened_rows"]["strings"][1]["optimizers"]=5
    evidence["reopened_rows"]["table_rows"][0]["studio"]+=1
    result=reconcile_offline(evidence)["stages"]["reopen"]
    assert result["verdict"]=="FAIL" and result["row_diff"]["counts"]==dict(added=0,removed=0,changed=2,metrics=1)
    expected=deepcopy(result)
    evidence["reopened_rows"]["strings"].reverse()
    evidence["reopened_rows"]["table_rows"].reverse()
    assert reconcile_offline(evidence)["stages"]["reopen"]==expected
    for absent in (None,{},dict(strings=[],table_rows=[])):
        evidence["reopened_rows"]=absent
        assert reconcile_offline(evidence)["stages"]["reopen"]["verdict"]=="UNREADABLE"


def test_small_string_comparison_exhaustive_independent_reference():
    from itertools import product
    from collections import Counter
    oracle=[dict(string_id="A",optimizers=1),dict(string_id="B",optimizers=2)]
    choices=[dict(string_id=key,optimizers=count) for key in ("A","B","foreign") for count in (1,2,3)]
    for length in (0,1,2,3):
        for sequence in product(choices,repeat=length):
            rows=list(sequence)
            observed=compare_strings(oracle,rows)
            counts=Counter(row["string_id"] for row in rows)
            if not rows or any(count>1 for count in counts.values()):
                assert observed["verdict"]=="UNREADABLE"
                continue
            saved={row["string_id"]:row["optimizers"] for row in rows}
            missing=len({"A","B"}-saved.keys())
            foreign=len(saved.keys()-{"A","B"})
            mismatched=sum(saved[key]!=value for key,value in (("A",1),("B",2)) if key in saved)
            assert observed["counts"]==dict(missing=missing,foreign=foreign,mismatched=mismatched)
            assert observed["verdict"]==("FAIL" if missing or foreign or mismatched else "PASS")


def test_signed_discrepancy_delta_and_metric_binding_including_tiny_values():
    from decimal import Decimal
    # Both signs fit the80-character plain numeric width bound.
    for metric,positive in (("panels",Decimal(1)),("kw_dc",Decimal("1e-77"))):
        deltas={key:0 for key in METRICS}
        deltas[metric]=positive
        record=dict(fixture_id="synthetic",metric=metric,delta=positive,name="signed revision",frozen_at="2026-11-15T12:00:00Z")
        assert check_discrepancies("synthetic",deltas,[record],"2026-11-15T13:00:00Z")["verdict"]=="PASS"
        deltas[metric]=-positive
        assert check_discrepancies("synthetic",deltas,[record],"2026-11-15T13:00:00Z")["rejected_metrics"]==[metric]
        other="optimizers" if metric=="panels" else "panels"
        deltas={key:0 for key in METRICS}
        deltas[other]=positive if metric=="panels" else 1
        assert check_discrepancies("synthetic",deltas,[record],"2026-11-15T13:00:00Z")["rejected_metrics"]==[other]
    evidence=complete_evidence()
    evidence["saved_values"]["panels"]=25
    evidence["discrepancy_records"]=[dict(fixture_id="synthetic",metric="panels",delta=1,name="revision",frozen_at="2026-11-15T12:00:00Z")]
    assert reconcile_offline(evidence)["stages"]["policy"]["verdict"]=="PASS"
    evidence["saved_values"],evidence["oracle_values"]=evidence["oracle_values"],evidence["saved_values"]
    assert reconcile_offline(evidence)["stages"]["policy"]["verdict"]=="FAIL"


def test_decimal_sticky_flags_preserved_on_success_and_rejection_paths():
    from decimal import localcontext
    from benches import x05_reconcile as module
    values,table=table_fixture()
    def results():
        return [module._numeric("1.25"),module._numeric("invalid"),
                canonical_snapshot(ORACLE,table),canonical_snapshot([],table),
                reconcile_offline(complete_evidence()),reconcile_offline({}),
                compare_table(table,values,values),compare_table(table,{},values)]
    baseline=serialize_result(results())
    with localcontext() as caller:
        for signal in caller.flags:
            caller.flags[signal]=True
        preserved=dict(caller.flags)
        assert serialize_result(results())==baseline
        assert dict(caller.flags)==preserved


@pytest.mark.parametrize("mutation", ["missing", "null", "duplicate", "foreign", "malformed"])
def test_seven_step_ownership_composition_retains_unrelated_diagnostics(mutation):
    envelope = seven_step_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["ownership"]["verdict"] == "PASS"
    envelope["assurance"]["evidence"]["recommendations"][0]["group_count"] = 1
    rows = envelope["grid"]["string_assignments"]
    if mutation == "missing":
        del envelope["grid"]["string_assignments"]
    elif mutation == "null":
        envelope["grid"]["string_assignments"] = None
    elif mutation == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif mutation == "foreign":
        rows[0]["grid_id"] = "foreign"
    else:
        rows[0] = {}
    preserved = deepcopy(envelope)
    result = reconcile_seven_steps_json(json.dumps(envelope))
    verdict = "FAIL" if mutation in ("duplicate", "foreign") else "UNREADABLE"
    assert result["verdict"] == result["ownership"]["verdict"] == verdict
    assert result["steps"]["4"]["verdict"] == "FAIL"
    for step in ("1", "2", "3", "5", "6", "7"):
        assert result["steps"][step] == baseline["steps"][step]
    assert envelope == preserved
    expected, records = seven_fixture_records()
    records[0]["envelope"] = envelope
    envelope["assurance"]["manifest"]["fixture_id"] = expected[0]
    envelope["assurance"]["evidence"]["fixture_id"] = expected[0]
    envelope["assurance"]["bindings"] = {
        key: deepcopy(envelope["assurance"]["manifest"]) for key in PROVENANCE_STAGES}
    aggregate = aggregate_seven_steps(expected, records)
    assert aggregate["verdict"] == verdict
    assert aggregate["fixtures"][expected[1]]["verdict"] == "PASS"


def test_per_grid_optimizer_totals_reject_equal_grand_total_swaps():
    saved = [dict(string_id="A", optimizers=3), dict(string_id="B", optimizers=7)]
    expected = {"east": 3, "west": 7}
    correct = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")]
    swapped = [dict(grid_id="west", string_id="A"), dict(grid_id="east", string_id="B")]
    preserved = deepcopy((saved, expected, correct, swapped))
    result = compare_grid_optimizer_totals(expected, saved, swapped)
    assert sum(expected.values()) == sum(row["optimizers"] for row in saved)
    assert result == dict(verdict="FAIL", reason="grid_optimizer_totals_mismatch", mismatch_count=2,
        examples=[dict(grid_id="east", oracle=3, saved=7, delta=4),
                  dict(grid_id="west", oracle=7, saved=3, delta=-4)])
    assert compare_grid_optimizer_totals(expected, saved, correct)["verdict"] == "PASS"
    for invalid in (None, {}, {"east": True}, {"east": -1}, {"east": 2**1000}, {None: 3}):
        assert compare_grid_optimizer_totals(invalid, saved, correct)["verdict"] == "UNREADABLE"
    assert compare_grid_optimizer_totals(expected, saved, None)["verdict"] == "UNREADABLE"
    assert compare_grid_optimizer_totals(expected, saved, correct[:1])["verdict"] == "FAIL"
    assert (saved, expected, correct, swapped) == preserved


def test_exhaustive_small_ownership_matches_independent_reference():
    from itertools import product
    grids, strings = ["east", "west"], ["A", "B"]
    choices = [(grid, string) for grid in grids + ["foreign"] for string in strings + ["foreign"]]
    for length in range(4):
        for selection in product(choices, repeat=length):
            rows = [dict(grid_id=grid, string_id=string) for grid, string in selection]
            occurrences = [string for _, string in selection]
            counts = dict(missing=sum(string not in occurrences for string in strings),
                          duplicate=len(occurrences) - len(set(occurrences)),
                          foreign_string=sum(string not in strings for string in occurrences),
                          foreign_grid=sum(grid not in grids for grid, _ in selection))
            observed = validate_grid_string_ownership(grids, strings, rows)
            assert observed["counts"] == counts
            assert observed["verdict"] == ("FAIL" if any(counts.values()) else "PASS")
            # A lawful grid swap needs independent per-grid totals to expose it.
            saved = [dict(string_id="A", optimizers=2), dict(string_id="B", optimizers=5)]
            totals = compare_grid_optimizer_totals({"east": 2, "west": 5}, saved, rows)
            sums = {grid: sum(2 if string == "A" else 5 for owner, string in selection if owner == grid)
                    for grid in grids}
            if not any(counts.values()):
                assert totals["mismatch_count"] == sum(sums[grid] != expected
                    for grid, expected in (("east", 2), ("west", 5)))
            else:
                assert totals["verdict"] == "FAIL"


def test_ownership_lookup_work_uses_linear_indexing(monkeypatch):
    from benches import x05_reconcile as module
    operations = []
    class CountedSet(set):
        def __contains__(self, value):
            operations.append(1)
            return super().__contains__(value)
    monkeypatch.setattr(module, "set", CountedSet, raising=False)
    for size in (10, 100, 1000):
        grids = [f"g{i}" for i in range(size)]
        strings = [f"s{i}" for i in range(size)]
        rows = [dict(grid_id=grid, string_id=string) for grid, string in zip(grids, strings)]
        operations.clear()
        assert module.validate_grid_string_ownership(grids, strings, rows)["verdict"] == "PASS"
        assert len(operations) == 3 * size
        operations.clear()
        saved = [dict(string_id=string, optimizers=1) for string in strings]
        assert module.compare_grid_optimizer_totals(dict.fromkeys(grids, 1), saved, rows)["verdict"] == "PASS"
        assert len(operations) == 3 * size


def test_ownership_diagnostic_caps_exact_totals_permutations_and_no_mutation():
    from random import Random
    grids = [f"g{i:03}" for i in range(60)]
    strings = [f"s{i:03}" for i in range(60)]
    rows = [dict(grid_id=grids[i], string_id=strings[i]) for i in range(30)]
    rows += deepcopy(rows)
    rows += [dict(grid_id="foreign", string_id=f"x{i:03}") for i in range(60)]
    baseline = validate_grid_string_ownership(grids, strings, rows)
    assert baseline["counts"] == dict(missing=30, duplicate=30, foreign_string=60, foreign_grid=60)
    assert all(len(examples) == MAX_STRING_EXAMPLES for examples in baseline["examples"].values())
    for seed in range(8):
        changed = deepcopy((grids, strings, rows))
        rng = Random(seed)
        for values in changed:
            rng.shuffle(values)
        preserved = deepcopy(changed)
        assert validate_grid_string_ownership(*changed) == baseline
        assert changed == preserved


def test_seven_step_grid_totals_swaps_missing_evidence_and_diagnostic_retention():
    envelope = seven_step_envelope()
    grid = envelope["grid"]
    grid.update(expected_grid_ids=["east", "west"], saved_group_ids=["p", "q"],
        rows=[dict(grid_id="east", matches=["p"], cause=None), dict(grid_id="west", matches=["q"], cause=None)],
        string_assignments=[dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")],
        optimizer_totals=dict(east=5, west=7), panel_totals=dict(east=10, west=14), dc_totals=dict(east="4", west="5.6"), string_totals=dict(east=1, west=1), inverter_totals=dict(east=1, west=0),
        reopened_string_assignments=[dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")])
    for field in ("saved_inverters", "before_inverters", "reopened_inverters"):
        envelope["assurance"]["evidence"][field][0]["grid_id"] = "east"
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS"
    grid["string_assignments"][0]["grid_id"] = "west"
    grid["string_assignments"][1]["grid_id"] = "east"
    swapped = reconcile_seven_steps_json(json.dumps(envelope))
    assert swapped["verdict"] == "FAIL" and swapped["steps"] == baseline["steps"]
    assert swapped["ownership"]["verdict"] == "PASS"
    assert swapped["grid_optimizers"]["comparison"]["mismatch_count"] == 2
    assert swapped["grid_optimizers"]["oracle_consistency"]["verdict"] == "PASS"
    del grid["optimizer_totals"]
    envelope["import"]["normal_user"] = False
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["verdict"] == "UNREADABLE" and result["steps"]["1"]["verdict"] == "FAIL"
    assert result["grid_optimizers"]["comparison"]["reason"] == "grid_optimizer_totals_unreadable"
    assert result["steps"]["3"] == baseline["steps"]["3"]


def test_grid_oracle_consistency_requires_explicit_coverage_and_exact_sum():
    assert compare_grid_oracle_totals(["a", "b"], dict(a=5, b=7), dict(optimizers=12))["verdict"] == "PASS"
    result = compare_grid_oracle_totals(["a", "b"], dict(a=6, b=7), dict(optimizers=12))
    assert result["verdict"] == "FAIL" and result["grid_total"] == 13
    for ids, totals, oracle in [([], {}, {}), (["a"], None, {}), (["a"], {}, dict(optimizers=0)),
            (["a", "a"], dict(a=5), dict(optimizers=5)), (["a", "b"], dict(a=5), dict(optimizers=5)),
            (["a"], dict(a=5, b=7), dict(optimizers=12)), (["a"], dict(a=5), {}),
            (["a"], dict(a=5), dict(optimizers=True)), (["a"], dict(a=5), None)]:
        assert compare_grid_oracle_totals(ids, totals, oracle)["verdict"] == "UNREADABLE"
    envelope = seven_step_envelope()
    envelope["grid"]["optimizer_totals"]["g"] += 1
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["grid_optimizers"]["oracle_consistency"]["reason"] == "grid_oracle_total_mismatch"
    assert all(result["steps"][key]["verdict"] == "PASS" for key in ("1", "2", "3", "4", "6", "7"))


def test_ownership_save_reopen_detects_equal_count_owner_swaps_and_missing_evidence():
    grids, strings = ["a", "b"], ["A", "B"]
    before = [dict(grid_id="a", string_id="A"), dict(grid_id="b", string_id="B")]
    reopened = [dict(grid_id="b", string_id="A"), dict(grid_id="a", string_id="B")]
    saved = [dict(string_id=key, optimizers=5) for key in strings]
    assert compare_grid_optimizer_totals(dict(a=5, b=5), saved, reopened)["verdict"] == "PASS"
    preserved = deepcopy((before, reopened))
    changed = compare_ownership_snapshots(grids, strings, before, reopened)
    assert changed == dict(verdict="FAIL", reason="ownership_changed", changed_count=2,
        examples=[dict(string_id="A", before="a", reopened="b"), dict(string_id="B", before="b", reopened="a")])
    assert compare_ownership_snapshots(grids, strings, before, list(reversed(before)))["verdict"] == "PASS"
    absent = compare_ownership_snapshots(grids, strings, before, None)
    assert absent["verdict"] == "UNREADABLE" and absent["reopened"]["reason"] == "ownership_evidence_unreadable"
    assert (before, reopened) == preserved


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, float("nan"), float("inf"), -float("inf"),
    "5", None, [], {}, 2**1000], ids=["true", "false", "negative", "fraction", "nan", "inf", "minus-inf",
    "string", "null", "list", "dict", "huge"])
def test_grid_optimizer_count_schema_is_sanitized_and_preserves_inputs(invalid):
    totals = dict(a=invalid)
    original = dict(totals)
    rows = [dict(string_id="A", optimizers=5)]
    assignments = [dict(grid_id="a", string_id="A")]
    result = compare_grid_optimizer_totals(totals, rows, assignments)
    assert result == dict(verdict="UNREADABLE", reason="grid_optimizer_totals_unreadable")
    assert serialize_result(result) == '{"reason":"grid_optimizer_totals_unreadable","verdict":"UNREADABLE"}'
    assert totals["a"] is original["a"]
    assert compare_grid_oracle_totals(["a"], totals, dict(optimizers=5))["verdict"] == "UNREADABLE"


@pytest.mark.parametrize("invalid", [None, [], (), [dict(grid_id="a", optimizers=5)], "secret-bearing-input"])
def test_grid_optimizer_container_schema_rejects_without_echo(invalid):
    result = compare_grid_optimizer_totals(invalid, ORACLE, [])
    assert result == dict(verdict="UNREADABLE", reason="grid_optimizer_totals_unreadable")
    assert "secret-bearing-input" not in serialize_result(result)
    assert reconcile_seven_steps_json('{"grid":{"optimizer_totals":{"a":5,"a":7}}}')["reason"] == "json_duplicate_key"


def test_grid_optimizer_lookup_work_exact_totals_caps_and_permutations(monkeypatch):
    from benches import x05_reconcile as module
    from random import Random
    lookups = []
    class CountedDict(dict):
        def __getitem__(self, key):
            lookups.append(1)
            return super().__getitem__(key)
    original_index = module._index
    def counted_index(rows, source, **kwargs):
        indexed, error = original_index(rows, source, **kwargs)
        return (CountedDict(indexed) if indexed is not None else None), error
    monkeypatch.setattr(module, "_index", counted_index)
    for size in (10, 100, 1000):
        totals = {f"g{i:04}": 2 for i in range(size)}
        saved = [dict(string_id=f"s{i:04}", optimizers=1) for i in range(size)]
        rows = [dict(grid_id=f"g{i:04}", string_id=f"s{i:04}") for i in range(size)]
        lookups.clear()
        baseline = module.compare_grid_optimizer_totals(totals, saved, rows)
        assert len(lookups) == size
        assert baseline["mismatch_count"] == size
        assert len(baseline["examples"]) == min(size, MAX_STRING_EXAMPLES)
        assert all(row["saved"] == 1 and row["delta"] == -1 for row in baseline["examples"])
        preserved = deepcopy((totals, saved, rows))
        rng = Random(size)
        rng.shuffle(saved)
        rng.shuffle(rows)
        assert module.compare_grid_optimizer_totals(dict(reversed(list(totals.items()))), saved, rows) == baseline
        assert totals == preserved[0]
        assert sorted(saved, key=lambda row: row["string_id"]) == preserved[1]
        assert sorted(rows, key=lambda row: row["string_id"]) == preserved[2]


def equal_count_ownership_envelope():
    envelope = seven_step_envelope()
    evidence = envelope["assurance"]["evidence"]
    for field in ("oracle_strings", "saved_strings"):
        for row in evidence[field]:
            row["optimizers"] = 6
    for field in ("before_rows", "reopened_rows"):
        evidence[field]["strings"] = deepcopy(evidence["saved_strings"])
    hashes = canonical_snapshot(evidence["saved_strings"], evidence["view_rows"])
    evidence["before"], evidence["reopened"] = hashes, dict(hashes)
    envelope["grid"].update(expected_grid_ids=["east", "west"], saved_group_ids=["p", "q"],
        rows=[dict(grid_id="east", matches=["p"], cause=None), dict(grid_id="west", matches=["q"], cause=None)],
        optimizer_totals=dict(east=6, west=6), panel_totals=dict(east=10, west=14), dc_totals=dict(east="4", west="5.6"), string_totals=dict(east=1, west=1), inverter_totals=dict(east=1, west=0),
        string_assignments=[dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")],
        reopened_string_assignments=[dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")])
    for field in ("saved_inverters", "before_inverters", "reopened_inverters"):
        evidence[field][0]["grid_id"] = "east"
    return envelope


def test_composed_ownership_reopen_equal_count_swap_absence_and_unrelated_diagnostics():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS"
    rows = envelope["grid"]["reopened_string_assignments"]
    rows[0]["grid_id"], rows[1]["grid_id"] = "west", "east"
    changed = reconcile_seven_steps_json(json.dumps(envelope))
    assert changed["verdict"] == "FAIL" and changed["ownership_reopen"]["reason"] == "ownership_changed"
    assert changed["ownership_reopen"]["changed_count"] == 2
    for field in ("steps", "assurance", "ownership", "grid_optimizers"):
        assert changed[field] == baseline[field]
    del envelope["grid"]["reopened_string_assignments"]
    envelope["import"]["dwg_imported"] = False
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing["verdict"] == "UNREADABLE" and missing["steps"]["1"]["verdict"] == "FAIL"
    assert missing["ownership_reopen"]["reopened"]["reason"] == "ownership_evidence_unreadable"
    assert missing["steps"]["7"] == baseline["steps"]["7"]


@pytest.mark.parametrize("stage", ["before", "reopened", "both"])
def test_ownership_snapshots_bind_each_map_to_snapshot_string_ids(stage):
    strings = deepcopy(ORACLE)
    unrelated = [dict(string_id="X", optimizers=5), dict(string_id="Y", optimizers=7)]
    owners = [dict(grid_id="g", string_id="A"), dict(grid_id="g", string_id="B")]
    before = unrelated if stage in ("before", "both") else strings
    reopened = unrelated if stage in ("reopened", "both") else strings
    preserved = deepcopy((before, reopened, owners))
    result = compare_bound_ownership_snapshots(["g"], before, reopened, owners, owners)
    assert result["verdict"] == "UNREADABLE"
    assert result["reason"] == ("ownership_snapshot_binding_conflict" if stage == "both"
                                 else "ownership_snapshot_identity_conflict")
    assert (before, reopened, owners) == preserved
    assert compare_bound_ownership_snapshots(["g"], None, strings, owners, owners)["reason"] == "ownership_snapshot_strings_unreadable"


def test_grid_optimizer_snapshots_detect_redistribution_with_equal_grand_totals():
    before = [dict(string_id="A", optimizers=5), dict(string_id="B", optimizers=7)]
    reopened = [dict(string_id="A", optimizers=7), dict(string_id="B", optimizers=5)]
    owners = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")]
    preserved = deepcopy((before, reopened, owners))
    changed = compare_grid_optimizer_snapshots(["east", "west"], before, owners, reopened, owners)
    assert changed == dict(verdict="FAIL", reason="grid_optimizer_snapshots_changed", changed_count=2,
        examples=[dict(grid_id="east", before=5, reopened=7, delta=2),
                  dict(grid_id="west", before=7, reopened=5, delta=-2)])
    assert compare_grid_optimizer_snapshots(["east", "west"], before, owners, before, list(reversed(owners)))["verdict"] == "PASS"
    assert compare_grid_optimizer_snapshots(["east", "west"], None, owners, reopened, None)["verdict"] == "UNREADABLE"
    assert (before, reopened, owners) == preserved


def test_ownership_grid_and_reopen_direct_json_differential_and_bound_siblings():
    base = equal_count_ownership_envelope()
    for mutation in ("none", "ownership", "totals", "reopen", "missing", "binding"):
        envelope = deepcopy(base)
        grid = envelope["grid"]
        if mutation == "ownership":
            grid["string_assignments"].append(deepcopy(grid["string_assignments"][0]))
        elif mutation == "totals":
            grid["optimizer_totals"]["east"] = 5
        elif mutation == "reopen":
            for row in grid["reopened_string_assignments"]:
                row["grid_id"] = "west" if row["grid_id"] == "east" else "east"
        elif mutation == "missing":
            del grid["reopened_string_assignments"]
        elif mutation == "binding":
            envelope["assurance"]["evidence"]["reopened_rows"]["strings"][0]["string_id"] = "foreign"
        source = envelope["assurance"]
        assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
            supplied_inputs={key: bytes.fromhex(value) for key, value in source["inputs_hex"].items()},
            expected_identity=source["identity"], stage_identities=source["stage_identities"])
        preserved = deepcopy(envelope)
        direct = reconcile_seven_steps(envelope["import"], grid, assurance)
        strict = reconcile_seven_steps_json(json.dumps(envelope))
        assert json.loads(serialize_result(direct)) == json.loads(serialize_result(strict))
        assert envelope == preserved
        expected, records = bound_seven_fixture_records()
        envelope["assurance"]["manifest"]["fixture_id"] = expected[0]
        envelope["assurance"]["evidence"]["fixture_id"] = expected[0]
        envelope["assurance"]["bindings"] = {key: deepcopy(envelope["assurance"]["manifest"]) for key in PROVENANCE_STAGES}
        records[0]["envelope"] = envelope
        aggregate = aggregate_bound_seven_steps(expected, records)
        decoded = aggregate_bound_seven_steps_json(json.dumps(dict(expected_ids=expected, fixtures=records)))
        assert json.loads(serialize_result(aggregate)) == json.loads(serialize_result(decoded))
        assert all(aggregate["fixtures"][key]["verdict"] == "PASS" for key in expected[1:])


def test_bound_seven_step_ownership_and_grid_helpers_run_once_per_unique_fixture(monkeypatch):
    from benches import x05_reconcile as module
    expected, records = bound_seven_fixture_records()
    baseline = aggregate_bound_seven_steps(expected, records)
    names = ("_seven_step_ownership", "_seven_step_grid_totals", "_seven_step_ownership_reopen",
             "compare_grid_optimizer_totals", "compare_bound_ownership_snapshots")
    calls = dict.fromkeys(names, 0)
    def counted(name, original):
        def invoke(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)
        return invoke
    for name in names:
        monkeypatch.setattr(module, name, counted(name, getattr(module, name)))
    assert aggregate_bound_seven_steps(expected, records) == baseline
    assert calls == dict.fromkeys(names, 3)
    calls.update(dict.fromkeys(names, 0))
    ambiguous = [dict(fixture_id=expected[0], envelope=None)] + records
    result = aggregate_bound_seven_steps(expected, ambiguous)
    assert result["verdict"] == "UNREADABLE" and expected[0] not in result["fixtures"]
    assert calls == dict.fromkeys(names, 2)
    for key in expected[1:]:
        assert result["fixtures"][key] == baseline["fixtures"][key]


def test_seven_step_grid_optimizer_snapshot_redistribution_and_missing_evidence():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["grid_optimizer_reopen"]["verdict"] == "PASS"
    evidence = envelope["assurance"]["evidence"]
    evidence["reopened_rows"]["strings"][0]["optimizers"] = 7
    evidence["reopened_rows"]["strings"][1]["optimizers"] = 5
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["verdict"] == "FAIL"
    assert result["grid_optimizer_reopen"]["reason"] == "grid_optimizer_snapshots_changed"
    assert result["grid_optimizer_reopen"]["changed_count"] == 2
    assert sum(row["optimizers"] for row in evidence["reopened_rows"]["strings"]) == 12
    for step in ("1", "2", "3", "4", "5", "6"):
        assert result["steps"][step] == baseline["steps"][step]
    assert result["ownership_reopen"] == baseline["ownership_reopen"]
    assert result["steps"]["7"] == reconcile_offline(evidence)["stages"]["reopen"]
    evidence["reopened_rows"]["strings"] = None
    envelope["import"]["normal_user"] = False
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing["verdict"] == "UNREADABLE" and missing["steps"]["1"]["verdict"] == "FAIL"
    assert missing["grid_optimizer_reopen"]["checks"]["reopened"]["reason"] == "grid_snapshot_strings_unreadable"
    assert missing["steps"]["4"] == baseline["steps"]["4"]


def test_independent_grid_panel_totals_detect_equal_total_swaps_and_require_panel_rows():
    rows = [dict(string_id="A", panels=10), dict(string_id="B", panels=14)]
    owners = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")]
    totals = dict(east=10, west=14)
    preserved = deepcopy((rows, owners, totals))
    assert compare_grid_panel_totals(totals, rows, owners)["verdict"] == "PASS"
    swapped = [dict(string_id="A", panels=14), dict(string_id="B", panels=10)]
    result = compare_grid_panel_totals(totals, swapped, owners)
    assert result == dict(verdict="FAIL", reason="grid_panel_totals_mismatch", mismatch_count=2,
        examples=[dict(grid_id="east", oracle=10, saved=14, delta=4),
                  dict(grid_id="west", oracle=14, saved=10, delta=-4)])
    assert sum(row["panels"] for row in swapped) == sum(totals.values())
    assert compare_grid_panel_totals(totals, ORACLE, owners)["verdict"] == "UNREADABLE"
    for value in (True, -1, 1.5, float("nan"), float("inf"), 2**1000, None):
        changed = [dict(string_id="A", panels=value), rows[1]]
        assert compare_grid_panel_totals(totals, changed, owners)["verdict"] == "UNREADABLE"
    assert compare_grid_panel_totals(totals, rows + [rows[0]], owners)["reason"] == "saved_duplicate_id"
    assert compare_grid_panel_totals(totals, rows, None)["verdict"] == "UNREADABLE"
    assert (rows, owners, totals) == preserved


def test_independent_grid_panel_oracle_requires_coverage_and_exact_total():
    baseline = compare_grid_panel_oracle_totals(["east", "west"], dict(east=10, west=14), dict(panels=24))
    assert baseline["verdict"] == "PASS" and baseline["reason"] == "grid_panel_oracle_total_equal"
    result = compare_grid_panel_oracle_totals(["east", "west"], dict(east=10, west=14), dict(panels=23))
    assert result["verdict"] == "FAIL" and result["grid_total"] == 24
    for grids, totals, oracle in [(None, {}, {}), ([], {}, {}), (["east"], None, dict(panels=10)),
            (["east"], {}, dict(panels=0)), (["east", "east"], dict(east=10), dict(panels=10)),
            (["east", "west"], dict(east=10), dict(panels=10)), (["east"], dict(west=10), dict(panels=10)),
            (["east"], dict(east=10), dict(optimizers=10)), (["east"], dict(east=10), dict(panels=True))]:
        assert compare_grid_panel_oracle_totals(grids, totals, oracle)["verdict"] == "UNREADABLE"


def test_exhaustive_grid_optimizer_snapshot_reference_model_and_nonmutation():
    from itertools import product
    states = list(product(("east", "west"), ("east", "west"), (1, 2), (1, 2)))
    for before in states:
        for reopened in states:
            inputs = []
            model = []
            for state in (before, reopened):
                owner_a, owner_b, count_a, count_b = state
                strings = [dict(string_id="A", optimizers=count_a), dict(string_id="B", optimizers=count_b)]
                owners = [dict(string_id="A", grid_id=owner_a), dict(string_id="B", grid_id=owner_b)]
                inputs.extend((strings, owners))
                model.append({grid: sum(count for owner, count in ((owner_a, count_a), (owner_b, count_b)) if owner == grid)
                              for grid in ("east", "west")})
            preserved = deepcopy(inputs)
            result = compare_grid_optimizer_snapshots(["east", "west"], *inputs)
            changed = [grid for grid in ("east", "west") if model[0][grid] != model[1][grid]]
            assert result["changed_count"] == len(changed)
            assert result["verdict"] == ("FAIL" if changed else "PASS")
            assert result["examples"] == [dict(grid_id=grid, before=model[0][grid], reopened=model[1][grid],
                                               delta=model[1][grid] - model[0][grid]) for grid in changed]
            assert inputs == preserved


def test_grid_optimizer_snapshot_caps_permutations_and_exact_counts():
    from random import Random
    grids = [f"g{i:03}" for i in range(60)]
    before = [dict(string_id=f"s{i:03}", optimizers=1) for i in range(60)]
    reopened = [dict(string_id=f"s{i:03}", optimizers=2) for i in range(60)]
    owners = [dict(grid_id=grids[i], string_id=f"s{i:03}") for i in range(60)]
    baseline = compare_grid_optimizer_snapshots(grids, before, owners, reopened, owners)
    assert baseline["changed_count"] == 60 and len(baseline["examples"]) == MAX_STRING_EXAMPLES
    assert [row["grid_id"] for row in baseline["examples"]] == grids[:MAX_STRING_EXAMPLES]
    for seed in range(8):
        values = deepcopy((grids, before, owners, reopened, owners))
        rng = Random(seed)
        for rows in values:
            rng.shuffle(rows)
        preserved = deepcopy(values)
        assert compare_grid_optimizer_snapshots(*values) == baseline
        assert values == preserved


def test_composed_panel_totals_swaps_absence_and_unrelated_diagnostics():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS" and baseline["grid_panels"]["verdict"] == "PASS"
    envelope["grid"]["panel_counts"][0]["panels"] = 14
    envelope["grid"]["panel_counts"][1]["panels"] = 10
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["verdict"] == "FAIL" and result["grid_panels"]["comparison"]["mismatch_count"] == 2
    assert result["grid_panels"]["oracle_consistency"]["verdict"] == "PASS"
    assert result["grid_panels"]["string_binding"]["verdict"] == "PASS"
    for key in ("steps", "assurance", "ownership", "grid_optimizers", "ownership_reopen", "grid_optimizer_reopen"):
        assert result[key] == baseline[key]
    del envelope["grid"]["panel_counts"]
    envelope["import"]["normal_user"] = False
    absent = reconcile_seven_steps_json(json.dumps(envelope))
    assert absent["verdict"] == "UNREADABLE" and absent["steps"]["1"]["verdict"] == "FAIL"
    assert absent["grid_panels"]["string_binding"]["reason"] == "panel_rows_unreadable"
    assert absent["steps"]["7"] == baseline["steps"]["7"]
    envelope = equal_count_ownership_envelope()
    del envelope["grid"]["panel_totals"]
    assert reconcile_seven_steps_json(json.dumps(envelope))["verdict"] == "UNREADABLE"


def test_panel_counts_bind_saved_ids_even_when_grid_and_grand_totals_match():
    saved = deepcopy(ORACLE)
    correct = [dict(string_id="A", panels=10), dict(string_id="B", panels=14)]
    foreign = [dict(string_id="X", panels=10), correct[1]]
    owners = [dict(grid_id="g", string_id="X"), dict(grid_id="g", string_id="B")]
    assert compare_grid_panel_totals(dict(g=24), foreign, owners)["verdict"] == "PASS"
    cases = [correct[:1], foreign, correct + [correct[0]]]
    for rows in cases:
        preserved = deepcopy((saved, rows))
        result = compare_panel_string_ids(saved, rows)
        assert result["verdict"] == "UNREADABLE"
        assert (saved, rows) == preserved
    assert compare_panel_string_ids(saved, foreign)["counts"] == dict(missing=1, foreign=1)
    assert compare_panel_string_ids(saved, correct + [correct[0]])["reason"] == "panel_duplicate_id"
    assert compare_panel_string_ids(saved, correct)["verdict"] == "PASS"
    many = [dict(string_id=f"x{i:03}", panels=1) for i in range(60)]
    bounded = compare_panel_string_ids(saved, many)
    assert bounded["counts"] == dict(missing=2, foreign=60)
    assert len(bounded["foreign_ids"]) == MAX_STRING_EXAMPLES


def test_panel_snapshot_comparator_redistribution_missing_rows_and_recovery():
    before = [dict(string_id="A", panels=10), dict(string_id="B", panels=14)]
    reopened = [dict(string_id="A", panels=14), dict(string_id="B", panels=10)]
    owners = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")]
    preserved = deepcopy((before, reopened, owners))
    changed = compare_grid_panel_snapshots(["east", "west"], before, owners, reopened, owners)
    assert changed == dict(verdict="FAIL", reason="grid_panel_snapshots_changed", changed_count=2,
        examples=[dict(grid_id="east", before=10, reopened=14, delta=4),
                  dict(grid_id="west", before=14, reopened=10, delta=-4)])
    absent = compare_grid_panel_snapshots(["east", "west"], before, owners, None, owners)
    assert absent["verdict"] == "UNREADABLE"
    assert absent["checks"]["reopened"]["reason"] == "grid_panel_snapshot_strings_unreadable"
    assert compare_grid_panel_snapshots(["east", "west"], before, owners, before, owners)["verdict"] == "PASS"
    assert (before, reopened, owners) == preserved


@pytest.mark.parametrize("value", [True, False, 1.5, -1, float("nan"), float("inf"), -float("inf"),
    None, [], {}, "10", 2**1000], ids=["true", "false", "fraction", "negative", "nan", "inf", "minus-inf",
    "null", "list", "dict", "string", "huge"])
def test_panel_count_schema_mutation_matrix(value):
    owners = [dict(grid_id="g", string_id="A")]
    rows = [dict(string_id="A", panels=value)]
    result = compare_grid_panel_totals(dict(g=10), rows, owners)
    assert result == dict(verdict="UNREADABLE", reason="saved_row_unreadable")
    assert compare_panel_string_ids([ORACLE[0]], rows)["verdict"] == "UNREADABLE"
    assert compare_grid_panel_oracle_totals(["g"], dict(g=value), dict(panels=10))["verdict"] == "UNREADABLE"
    assert len(serialize_result(result)) < 100
    assert rows[0]["panels"] is value


@pytest.mark.parametrize("value", [None, {}, (), "private synthetic input", 7])
def test_panel_container_schema_and_duplicate_grid_ids(value):
    owners = [dict(grid_id="g", string_id="A")]
    result = compare_grid_panel_totals(dict(g=10), value, owners)
    assert result["verdict"] == "UNREADABLE" and "private" not in serialize_result(result)
    assert compare_grid_panel_oracle_totals(["g", "g"], dict(g=10), dict(panels=10))["verdict"] == "UNREADABLE"
    assert compare_grid_panel_totals(value, [dict(string_id="A", panels=10)], owners)["verdict"] == "UNREADABLE"
    assert reconcile_seven_steps_json('{"grid":{"panel_totals":{"g":10,"g":20}}}')["reason"] == "json_duplicate_key"


def test_generated_panel_lookup_work_caps_permutations_and_preserved_inputs(monkeypatch):
    from benches import x05_reconcile as module
    from random import Random
    lookups = []
    class CountedDict(dict):
        def __getitem__(self, key):
            lookups.append(1)
            return super().__getitem__(key)
    original_index = module._index
    def counted_index(rows, source, **kwargs):
        indexed, error = original_index(rows, source, **kwargs)
        return (CountedDict(indexed) if indexed is not None else None), error
    monkeypatch.setattr(module, "_index", counted_index)
    for size in (10, 100, 1000):
        totals = {f"g{i:04}": 2 for i in range(size)}
        strings = [dict(string_id=f"s{i:04}", panels=1) for i in range(size)]
        owners = [dict(grid_id=f"g{i:04}", string_id=f"s{i:04}") for i in range(size)]
        lookups.clear()
        result = module.compare_grid_panel_totals(totals, strings, owners)
        assert len(lookups) == size and result["mismatch_count"] == size
        assert len(result["examples"]) == min(size, MAX_STRING_EXAMPLES)
        assert all(row["delta"] == -1 for row in result["examples"])
        for seed in range(3):
            changed = deepcopy((strings, owners))
            rng = Random(seed)
            for rows in changed:
                rng.shuffle(rows)
            preserved = deepcopy(changed)
            assert module.compare_grid_panel_totals(dict(reversed(list(totals.items()))), *changed) == result
            assert changed == preserved


def test_composed_panel_snapshot_redistribution_absence_and_diagnostic_retention():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS"
    envelope["grid"]["reopened_panel_counts"][0]["panels"] = 14
    envelope["grid"]["reopened_panel_counts"][1]["panels"] = 10
    changed = reconcile_seven_steps_json(json.dumps(envelope))
    assert changed["verdict"] == "FAIL"
    assert changed["grid_panel_reopen"]["comparison"]["changed_count"] == 2
    for key in ("steps", "assurance", "ownership", "grid_optimizers", "ownership_reopen", "grid_optimizer_reopen", "grid_panels"):
        assert changed[key] == baseline[key]
    del envelope["grid"]["reopened_panel_counts"]
    envelope["import"]["normal_user"] = False
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing["verdict"] == "UNREADABLE" and missing["steps"]["1"]["verdict"] == "FAIL"
    assert missing["grid_panel_reopen"]["reopened_binding"]["reason"] == "panel_rows_unreadable"
    assert missing["steps"]["7"] == baseline["steps"]["7"]


@pytest.mark.parametrize("stage", ["before", "reopened"])
@pytest.mark.parametrize("mutation", ["missing", "foreign", "duplicate"])
def test_snapshot_panel_ids_bind_despite_equal_total_rows(stage, mutation):
    strings = deepcopy(ORACLE)
    panels = [dict(string_id="A", panels=10), dict(string_id="B", panels=14)]
    owners = [dict(grid_id="g", string_id="A"), dict(grid_id="g", string_id="B")]
    changed = ([dict(string_id="A", panels=24)] if mutation == "missing" else
               [dict(string_id="X", panels=10), panels[1]] if mutation == "foreign" else
               [dict(string_id="A", panels=5), dict(string_id="A", panels=5), panels[1]])
    assert sum(row["panels"] for row in changed) == 24
    changed_owners = [dict(grid_id="g", string_id=row["string_id"]) for row in changed]
    before, reopened = (changed, panels) if stage == "before" else (panels, changed)
    before_owners, reopened_owners = (changed_owners, owners) if stage == "before" else (owners, changed_owners)
    preserved = deepcopy((strings, before, reopened, before_owners, reopened_owners))
    result = compare_bound_panel_snapshots(["g"], strings, strings, before, before_owners, reopened, reopened_owners)
    assert result["verdict"] == "UNREADABLE"
    binding = result[stage + "_binding"]
    assert binding["reason"] == ("panel_duplicate_id" if mutation == "duplicate" else "panel_string_ids_unbound")
    assert len(serialize_result(result)) < 2500
    assert (strings, before, reopened, before_owners, reopened_owners) == preserved


def test_exhaustive_panel_snapshot_reference_counts_and_nonmutation():
    from itertools import product
    states = list(product(("east", "west"), ("east", "west"), (1, 2), (1, 2)))
    for before in states:
        for reopened in states:
            supplied, expected = [], []
            for owner_a, owner_b, panels_a, panels_b in (before, reopened):
                supplied.extend(([dict(string_id="A", panels=panels_a), dict(string_id="B", panels=panels_b)],
                                 [dict(string_id="A", grid_id=owner_a), dict(string_id="B", grid_id=owner_b)]))
                expected.append({grid: sum(count for owner, count in ((owner_a, panels_a), (owner_b, panels_b)) if owner == grid)
                                 for grid in ("east", "west")})
            preserved = deepcopy(supplied)
            result = compare_grid_panel_snapshots(["east", "west"], *supplied)
            changed = [grid for grid in ("east", "west") if expected[0][grid] != expected[1][grid]]
            assert result["changed_count"] == len(changed)
            assert result["verdict"] == ("FAIL" if changed else "PASS")
            assert result["examples"] == [dict(grid_id=grid, before=expected[0][grid], reopened=expected[1][grid],
                                               delta=expected[1][grid] - expected[0][grid]) for grid in changed]
            assert supplied == preserved


def test_panel_snapshot_diagnostics_cap_exact_totals_and_permutation_stability():
    from random import Random
    grids = [f"g{i:03}" for i in range(70)]
    before = [dict(string_id=f"s{i:03}", panels=2) for i in range(70)]
    reopened = [dict(string_id=f"s{i:03}", panels=3) for i in range(70)]
    owners = [dict(grid_id=grids[i], string_id=f"s{i:03}") for i in range(70)]
    baseline = compare_grid_panel_snapshots(grids, before, owners, reopened, owners)
    assert baseline["changed_count"] == 70 and len(baseline["examples"]) == MAX_STRING_EXAMPLES
    assert [row["grid_id"] for row in baseline["examples"]] == grids[:MAX_STRING_EXAMPLES]
    for seed in range(8):
        values = deepcopy((grids, before, owners, reopened, owners))
        rng = Random(seed)
        for rows in values:
            rng.shuffle(rows)
        preserved = deepcopy(values)
        assert compare_grid_panel_snapshots(*values) == baseline
        assert values == preserved


def test_panel_snapshot_direct_json_and_bound_aggregate_diagnostics_match():
    for field in ("before_panel_counts", "reopened_panel_counts"):
        for mutation in ("redistribution", "missing", "foreign", "duplicate"):
            envelope = equal_count_ownership_envelope()
            rows = envelope["grid"][field]
            if mutation == "redistribution":
                rows[0]["panels"], rows[1]["panels"] = 14, 10
            elif mutation == "missing":
                del envelope["grid"][field]
            elif mutation == "foreign":
                rows[0]["string_id"] = "foreign"
            else:
                rows.append(deepcopy(rows[0]))
            source = envelope["assurance"]
            assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
                supplied_inputs={key: bytes.fromhex(value) for key, value in source["inputs_hex"].items()},
                expected_identity=source["identity"], stage_identities=source["stage_identities"])
            preserved = deepcopy(envelope)
            direct = reconcile_seven_steps(envelope["import"], envelope["grid"], assurance)
            strict = reconcile_seven_steps_json(json.dumps(envelope))
            assert json.loads(serialize_result(direct)) == json.loads(serialize_result(strict))
            assert envelope == preserved
            expected, records = bound_seven_fixture_records()
            envelope["assurance"]["manifest"]["fixture_id"] = expected[0]
            envelope["assurance"]["evidence"]["fixture_id"] = expected[0]
            envelope["assurance"]["bindings"] = {key: deepcopy(envelope["assurance"]["manifest"]) for key in PROVENANCE_STAGES}
            records[0]["envelope"] = envelope
            preserved_records = deepcopy(records)
            result = aggregate_bound_seven_steps(expected, records)
            decoded = aggregate_bound_seven_steps_json(json.dumps(dict(expected_ids=expected, fixtures=records)))
            assert json.loads(serialize_result(result)) == json.loads(serialize_result(decoded))
            assert all(result["fixtures"][key]["verdict"] == "PASS" for key in expected[1:])
            assert records == preserved_records


def test_exact_grid_dc_totals_swaps_zero_and_missing_power_without_panel_inference():
    from decimal import Decimal, localcontext
    owners = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")]
    rows = [dict(string_id="A", kw_dc="0.1"), dict(string_id="B", kw_dc="0.2")]
    totals = dict(east="0.1", west="0.2")
    preserved = deepcopy((rows, totals, owners))
    assert compare_grid_dc_totals(totals, rows, owners)["verdict"] == "PASS"
    swapped = [dict(string_id="A", kw_dc="0.2"), dict(string_id="B", kw_dc="0.1")]
    result = compare_grid_dc_totals(totals, swapped, owners)
    assert result == dict(verdict="FAIL", reason="grid_dc_totals_mismatch", mismatch_count=2,
        examples=[dict(grid_id="east", oracle=Decimal("0.1"), saved=Decimal("0.2"), delta=Decimal("0.1")),
                  dict(grid_id="west", oracle=Decimal("0.2"), saved=Decimal("0.1"), delta=Decimal("-0.1"))])
    for malformed in (None, [], [dict(string_id="A", panels=10)], [dict(string_id="A", kw_dc=True)],
                      [dict(string_id="A", kw_dc=float("nan"))], [dict(string_id="A", kw_dc=-1)]):
        assert compare_grid_dc_totals(totals, malformed, owners)["verdict"] == "UNREADABLE"
    assert compare_grid_dc_totals(dict(east=0, west=0), [dict(string_id="A", kw_dc=0), dict(string_id="B", kw_dc=0)], owners)["verdict"] == "PASS"
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.clear_flags()
        flags = dict(ctx.flags)
        exact = compare_grid_dc_totals(dict(g="0.3"), rows,
            [dict(grid_id="g", string_id="A"), dict(grid_id="g", string_id="B")])
        assert exact["verdict"] == "PASS" and dict(ctx.flags) == flags
    assert (rows, totals, owners) == preserved


def test_grid_dc_oracle_coverage_and_exact_decimal_total():
    from decimal import Decimal
    assert compare_grid_dc_oracle_totals(["east", "west"], dict(east="0.1", west="0.2"), dict(kw_dc="0.3"))["verdict"] == "PASS"
    result = compare_grid_dc_oracle_totals(["east", "west"], dict(east="0.1", west="0.2"), dict(kw_dc="0.31"))
    assert result["verdict"] == "FAIL" and result["grid_total"] == Decimal("0.3")
    for grids, totals, values in [([], {}, {}), (["g"], None, {}), (["g"], {}, {}),
            (["g", "g"], dict(g="0.1"), dict(kw_dc="0.1")), (["g", "h"], dict(g="0.1"), dict(kw_dc="0.1")),
            (["g"], dict(g="0.1", h="0.2"), dict(kw_dc="0.3")), (["g"], dict(g="0.1"), {}),
            (["g"], dict(g=True), dict(kw_dc="0.1")), (["g"], dict(g="0.1"), dict(kw_dc=float("inf")))]:
        assert compare_grid_dc_oracle_totals(grids, totals, values)["verdict"] == "UNREADABLE"


def test_grid_dc_snapshots_detect_tiny_redistributions_and_absent_evidence_exactly():
    from decimal import Decimal, localcontext
    owners = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B")]
    before = [dict(string_id="A", kw_dc=Decimal("1e-77")), dict(string_id="B", kw_dc=Decimal("2e-77"))]
    reopened = [dict(string_id="A", kw_dc=Decimal("2e-77")), dict(string_id="B", kw_dc=Decimal("1e-77"))]
    preserved = deepcopy((before, reopened, owners))
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.clear_flags()
        flags = dict(ctx.flags)
        result = compare_grid_dc_snapshots(["east", "west"], before, owners, reopened, owners)
        assert result["verdict"] == "FAIL" and result["changed_count"] == 2
        assert [row["delta"] for row in result["examples"]] == [Decimal("1e-77"), Decimal("-1e-77")]
        assert dict(ctx.flags) == flags
    missing = compare_grid_dc_snapshots(["east", "west"], before, owners, None, owners)
    assert missing["verdict"] == "UNREADABLE" and missing["checks"]["reopened"]["reason"] == "grid_dc_snapshot_strings_unreadable"
    assert compare_grid_dc_snapshots(["east", "west"], before, owners, before, owners)["verdict"] == "PASS"
    assert (before, reopened, owners) == preserved


def test_dc_string_ids_require_exact_saved_coverage_even_for_equal_power_totals():
    correct = [dict(string_id="A", kw_dc="0.1"), dict(string_id="B", kw_dc="0.2")]
    assert compare_dc_string_ids(ORACLE, correct)["verdict"] == "PASS"
    foreign = [dict(string_id="X", kw_dc="0.1"), correct[1]]
    assert compare_grid_dc_totals(dict(g="0.3"), foreign,
        [dict(grid_id="g", string_id="X"), dict(grid_id="g", string_id="B")])["verdict"] == "PASS"
    for rows in (foreign, correct[:1], correct + [correct[0]], None):
        preserved = deepcopy(rows)
        result = compare_dc_string_ids(ORACLE, rows)
        assert result["verdict"] == "UNREADABLE" and rows == preserved
    assert compare_dc_string_ids(ORACLE, foreign)["counts"] == dict(missing=1, foreign=1)
    assert compare_dc_string_ids(ORACLE, correct + [correct[0]])["reason"] == "dc_duplicate_id"
    large = [dict(string_id=f"x{i:03}", kw_dc="0.1") for i in range(60)]
    result = compare_dc_string_ids(ORACLE, large)
    assert result["counts"] == dict(missing=2, foreign=60) and len(result["foreign_ids"]) == MAX_STRING_EXAMPLES


def test_short_decimal_table_and_signed_policy_match_exact_rational_reference():
    from decimal import Decimal, localcontext
    from fractions import Fraction
    values = ("0", "0.1", "0.2", "0.03", "1.25", "9.6")
    for saved_power in values:
        for oracle_power in values:
            expected = Fraction(saved_power) - Fraction(oracle_power)
            with localcontext() as ctx:
                ctx.prec = 200
                delta = Decimal(expected.numerator) / Decimal(expected.denominator)
            assert Fraction(delta) == expected
            saved, rows = table_fixture()
            oracle = dict(saved, kw_dc=oracle_power)
            saved["kw_dc"] = saved_power
            row = next(row for row in rows if row["metric"] == "kw_dc")
            row.update(studio=saved_power, oracle=oracle_power, delta=delta, verdict="PASS" if not expected else "FAIL")
            assert compare_table(rows, saved, oracle)["verdict"] == "PASS"
            if expected:
                wrong = deepcopy(rows)
                next(row for row in wrong if row["metric"] == "kw_dc")["delta"] = -delta
                assert compare_table(wrong, saved, oracle)["verdict"] == "FAIL"
            deltas = dict.fromkeys(METRICS, 0)
            deltas["kw_dc"] = delta
            record = dict(fixture_id="synthetic", metric="kw_dc", delta=delta,
                          name="explicit power revision", frozen_at="2026-11-15T12:00:00Z")
            assert check_discrepancies("synthetic", deltas, [record], "2026-11-15T13:00:00Z")["verdict"] == "PASS"
            if expected:
                assert check_discrepancies("synthetic", deltas, [dict(record, delta=-delta)],
                    "2026-11-15T13:00:00Z")["verdict"] == "FAIL"


def test_composed_dc_totals_swaps_missing_evidence_and_unrelated_diagnostics():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS" and baseline["grid_dc"]["verdict"] == "PASS"
    envelope["grid"]["dc_power"][0]["kw_dc"] = "5.6"
    envelope["grid"]["dc_power"][1]["kw_dc"] = "4"
    changed = reconcile_seven_steps_json(json.dumps(envelope))
    assert changed["verdict"] == "FAIL" and changed["grid_dc"]["comparison"]["mismatch_count"] == 2
    assert changed["grid_dc"]["oracle_consistency"]["verdict"] == "PASS"
    for field in ("steps", "assurance", "ownership", "grid_optimizers", "ownership_reopen", "grid_optimizer_reopen", "grid_panels", "grid_panel_reopen"):
        assert changed[field] == baseline[field]
    del envelope["grid"]["dc_power"]
    envelope["import"]["normal_user"] = False
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["verdict"] == "UNREADABLE" and result["steps"]["1"]["verdict"] == "FAIL"
    assert result["grid_dc"]["string_binding"]["reason"] == "dc_rows_unreadable"


def test_composed_dc_snapshots_tiny_equal_total_redistribution_and_bound_ids():
    from decimal import Decimal
    envelope = equal_count_ownership_envelope()
    envelope["grid"]["before_dc_power"] = [dict(string_id="A", kw_dc=Decimal("1e-77")), dict(string_id="B", kw_dc=Decimal("2e-77"))]
    envelope["grid"]["reopened_dc_power"] = [dict(string_id="A", kw_dc=Decimal("2e-77")), dict(string_id="B", kw_dc=Decimal("1e-77"))]
    result = reconcile_seven_steps_json(serialize_result(envelope))
    assert result["verdict"] == "FAIL" and result["grid_dc_reopen"]["comparison"]["changed_count"] == 2
    assert [row["delta"] for row in result["grid_dc_reopen"]["comparison"]["examples"]] == [Decimal("1e-77"), Decimal("-1e-77")]
    assert result["steps"]["7"]["verdict"] == "PASS"
    envelope["grid"]["reopened_dc_power"][0]["string_id"] = "foreign"
    unbound = reconcile_seven_steps_json(serialize_result(envelope))
    assert unbound["verdict"] == "UNREADABLE"
    assert unbound["grid_dc_reopen"]["reopened_binding"]["counts"] == dict(missing=1, foreign=1)
    del envelope["grid"]["reopened_dc_power"]
    assert reconcile_seven_steps_json(serialize_result(envelope))["grid_dc_reopen"]["verdict"] == "UNREADABLE"


@pytest.mark.parametrize("mutation", ["boolean", "negative", "nan", "infinity", "width", "duplicate", "container"])
def test_dc_schema_direct_json_composition_and_bound_siblings(mutation):
    envelope = equal_count_ownership_envelope()
    values = dict(boolean=True, negative=-1, nan=float("nan"), infinity=float("inf"), width="1"*81)
    if mutation == "duplicate":
        envelope["grid"]["dc_power"].append(deepcopy(envelope["grid"]["dc_power"][0]))
    elif mutation == "container":
        envelope["grid"]["dc_power"] = "private synthetic value"
    else:
        envelope["grid"]["dc_power"][0]["kw_dc"] = values[mutation]
    source = envelope["assurance"]
    assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
        supplied_inputs={key: bytes.fromhex(value) for key, value in source["inputs_hex"].items()},
        expected_identity=source["identity"], stage_identities=source["stage_identities"])
    preserved = deepcopy(envelope)
    direct = reconcile_seven_steps(envelope["import"], envelope["grid"], assurance)
    transport = deepcopy(envelope)
    # Valid JSON strings preserve the invalid numeric evidence without corrupting the whole transport.
    if mutation in ("nan", "infinity"):
        transport["grid"]["dc_power"][0]["kw_dc"] = "NaN" if mutation == "nan" else "Infinity"
    decoded = reconcile_seven_steps_json(json.dumps(transport))
    assert direct["verdict"] == "UNREADABLE"
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(decoded))
    assert "private synthetic value" not in serialize_result(direct)
    assert envelope == preserved
    expected, records = bound_seven_fixture_records()
    transport["assurance"]["manifest"]["fixture_id"] = expected[0]
    transport["assurance"]["evidence"]["fixture_id"] = expected[0]
    transport["assurance"]["bindings"] = {key: deepcopy(transport["assurance"]["manifest"]) for key in PROVENANCE_STAGES}
    records[0]["envelope"] = transport
    baseline = aggregate_bound_seven_steps(expected, records)
    result = aggregate_bound_seven_steps_json(json.dumps(dict(expected_ids=expected, fixtures=records)))
    assert json.loads(serialize_result(baseline)) == json.loads(serialize_result(result))
    assert result["verdict"] == "UNREADABLE"
    assert all(result["fixtures"][key]["verdict"] == "PASS" for key in expected[1:])


def test_exhaustive_dc_snapshots_match_independent_fraction_grid_sums():
    from fractions import Fraction
    from itertools import product
    states = list(product(("east", "west"), ("east", "west"), ("0.1", "0.2"), ("0.1", "0.2")))
    for before in states:
        for reopened in states:
            supplied, totals = [], []
            for owner_a, owner_b, power_a, power_b in (before, reopened):
                supplied.extend(([dict(string_id="A", kw_dc=power_a), dict(string_id="B", kw_dc=power_b)],
                                 [dict(string_id="A", grid_id=owner_a), dict(string_id="B", grid_id=owner_b)]))
                totals.append({grid: sum((Fraction(power) for owner, power in ((owner_a, power_a), (owner_b, power_b)) if owner == grid), Fraction(0))
                               for grid in ("east", "west")})
            preserved = deepcopy(supplied)
            result = compare_grid_dc_snapshots(["east", "west"], *supplied)
            changed = [grid for grid in ("east", "west") if totals[0][grid] != totals[1][grid]]
            assert result["changed_count"] == len(changed)
            assert result["verdict"] == ("FAIL" if changed else "PASS")
            assert [(row["grid_id"], Fraction(row["before"]), Fraction(row["reopened"]), Fraction(row["delta"]))
                    for row in result["examples"]] == [(grid, totals[0][grid], totals[1][grid], totals[1][grid]-totals[0][grid]) for grid in changed]
            assert supplied == preserved


def test_generated_dc_lookup_work_exact_totals_and_bounded_permuted_diagnostics(monkeypatch):
    from benches import x05_reconcile as module
    from decimal import Decimal
    from random import Random
    lookups = []
    class CountedDict(dict):
        def __getitem__(self, key):
            lookups.append(1)
            return super().__getitem__(key)
    original_index = module._index
    def counted_index(rows, source, **kwargs):
        indexed, error = original_index(rows, source, **kwargs)
        return (CountedDict(indexed) if indexed is not None else None), error
    monkeypatch.setattr(module, "_index", counted_index)
    for size in (10, 100, 1000):
        totals = {f"g{i:04}": "0.2" for i in range(size)}
        strings = [dict(string_id=f"s{i:04}", kw_dc="0.1") for i in range(size)]
        owners = [dict(grid_id=f"g{i:04}", string_id=f"s{i:04}") for i in range(size)]
        lookups.clear()
        baseline = module.compare_grid_dc_totals(totals, strings, owners)
        assert len(lookups) == size and baseline["mismatch_count"] == size
        assert len(baseline["examples"]) == min(size, MAX_STRING_EXAMPLES)
        assert all(row["delta"] == Decimal("-0.1") for row in baseline["examples"])
        assert compare_grid_dc_oracle_totals(list(totals), totals, dict(kw_dc=str(Decimal(size)*Decimal("0.2"))))["verdict"] == "PASS"
        for seed in range(3):
            changed = deepcopy((strings, owners))
            rng = Random(seed)
            for rows in changed:
                rng.shuffle(rows)
            preserved = deepcopy(changed)
            assert module.compare_grid_dc_totals(dict(reversed(list(totals.items()))), *changed) == baseline
            assert changed == preserved


def test_before_panel_counts_bind_values_despite_identical_ids_and_grid_totals():
    envelope = seven_step_envelope()
    before = [dict(string_id="A", panels=12), dict(string_id="B", panels=12)]
    envelope["grid"]["before_panel_counts"] = deepcopy(before)
    envelope["grid"]["reopened_panel_counts"] = deepcopy(before)
    preserved = deepcopy(envelope)
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["verdict"] == "FAIL"
    assert result["grid_panels"]["verdict"] == result["grid_panel_reopen"]["comparison"]["verdict"] == "PASS"
    binding = result["grid_panel_reopen"]["saved_binding"]
    assert binding["counts"] == dict(missing=0, foreign=0, mismatched=2)
    assert [row["delta"] for row in binding["examples"]] == [2, -2]
    assert envelope == preserved
    saved = [dict(string_id=f"s{i:03}", panels=1) for i in range(60)]
    changed = [dict(string_id=f"s{i:03}", panels=2) for i in range(60)]
    bounded = compare_before_panel_counts(saved, changed)
    assert bounded["counts"]["mismatched"] == 60 and len(bounded["examples"]) == MAX_STRING_EXAMPLES


def test_before_dc_values_bind_tiny_same_grid_substitution_and_preserve_context():
    from decimal import Decimal, localcontext
    envelope = seven_step_envelope()
    with localcontext() as ctx:
        ctx.prec = 200
        before = [dict(string_id="A", kw_dc=Decimal("4")+Decimal("1e-77")),
                  dict(string_id="B", kw_dc=Decimal("5.6")-Decimal("1e-77"))]
    envelope["grid"]["before_dc_power"] = deepcopy(before)
    envelope["grid"]["reopened_dc_power"] = deepcopy(before)
    preserved = deepcopy(envelope)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.clear_flags()
        flags = dict(ctx.flags)
        binding = compare_before_dc_values(envelope["grid"]["dc_power"], before)
        assert binding["verdict"] == "FAIL" and binding["counts"]["mismatched"] == 2
        assert [row["delta"] for row in binding["examples"]] == [Decimal("1e-77"), Decimal("-1e-77")]
        assert dict(ctx.flags) == flags
    result = reconcile_seven_steps_json(serialize_result(envelope))
    assert result["verdict"] == "FAIL" and result["grid_dc"]["verdict"] == "PASS"
    assert result["grid_dc_reopen"]["comparison"]["verdict"] == "PASS"
    assert result["grid_dc_reopen"]["saved_binding"]["verdict"] == "FAIL"
    assert envelope == preserved


def test_independent_grid_string_totals_reject_equal_total_owner_swaps_and_missing_counts():
    strings = ["A", "B", "C"]
    owners = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B"), dict(grid_id="west", string_id="C")]
    independent = dict(east=1, west=2)
    assert compare_grid_string_totals(independent, strings, owners)["verdict"] == "PASS"
    swapped = deepcopy(owners)
    swapped[0]["grid_id"], swapped[1]["grid_id"], swapped[2]["grid_id"] = "west", "east", "east"
    result = compare_grid_string_totals(independent, strings, swapped)
    assert result == dict(verdict="FAIL", reason="grid_string_totals_mismatch", mismatch_count=2,
        examples=[dict(grid_id="east", oracle=1, saved=2, delta=1), dict(grid_id="west", oracle=2, saved=1, delta=-1)])
    for missing in (None, {}, [], dict(east=True), dict(east=-1)):
        assert compare_grid_string_totals(missing, strings, owners)["verdict"] == "UNREADABLE"
    assert compare_grid_string_totals(independent, strings, owners + [owners[0]])["verdict"] == "FAIL"


def test_grid_string_oracle_requires_exact_coverage_and_total():
    assert compare_grid_string_oracle_totals(["east", "west"], dict(east=1, west=2), dict(strings=3))["verdict"] == "PASS"
    assert compare_grid_string_oracle_totals(["east", "west"], dict(east=1, west=2), dict(strings=4))["verdict"] == "FAIL"
    for grids, totals, report in [(["g", "g"], dict(g=1), dict(strings=1)),
            (["g"], [dict(grid_id="g", strings=1), dict(grid_id="g", strings=1)], dict(strings=2)),
            (["east", "west"], dict(east=1), dict(strings=1)), (["g"], dict(g=1), {}),
            (["g"], dict(g=1), dict(strings=True))]:
        assert compare_grid_string_oracle_totals(grids, totals, report)["verdict"] == "UNREADABLE"


@pytest.mark.parametrize("mode", ["seven", "bound"])
@pytest.mark.parametrize("mutation", ["tiny", "missing"])
def test_composed_dc_cli_matches_direct_results_and_keeps_bound_siblings(tmp_path, mode, mutation):
    import subprocess
    import sys
    from pathlib import Path
    from decimal import Decimal, localcontext
    from benches import x05_reconcile as module
    envelope = equal_count_ownership_envelope()
    if mutation == "tiny":
        with localcontext() as ctx:
            ctx.prec = 200
            envelope["grid"]["reopened_dc_power"] = [dict(string_id="A", kw_dc=Decimal("4")+Decimal("1e-77")),
                dict(string_id="B", kw_dc=Decimal("5.6")-Decimal("1e-77"))]
    else:
        del envelope["grid"]["reopened_dc_power"]
    if mode == "seven":
        source = envelope["assurance"]
        assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
            supplied_inputs={key: bytes.fromhex(value) for key, value in source["inputs_hex"].items()},
            expected_identity=source["identity"], stage_identities=source["stage_identities"])
        direct = reconcile_seven_steps(envelope["import"], envelope["grid"], assurance)
        payload, flag = envelope, "--seven-step"
    else:
        expected, records = bound_seven_fixture_records()
        envelope["assurance"]["manifest"]["fixture_id"] = expected[0]
        envelope["assurance"]["evidence"]["fixture_id"] = expected[0]
        envelope["assurance"]["bindings"] = {key: deepcopy(envelope["assurance"]["manifest"]) for key in PROVENANCE_STAGES}
        records[0]["envelope"] = envelope
        direct = aggregate_bound_seven_steps(expected, records)
        assert all(direct["fixtures"][key]["verdict"] == "PASS" for key in expected[1:])
        payload, flag = dict(expected_ids=expected, fixtures=records), "--seven-step-bound-aggregate"
    run = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), flag],
        input=serialize_result(payload).encode("ascii"), capture_output=True, cwd=tmp_path, timeout=20,
        env={"SystemRoot": "C:\\Windows"})
    assert run.returncode == (1 if mutation == "tiny" else 4) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(direct))


def three_string_envelope():
    envelope = equal_count_ownership_envelope()
    grid, evidence = envelope["grid"], envelope["assurance"]["evidence"]
    strings = [dict(string_id="A", optimizers=5), dict(string_id="B", optimizers=4), dict(string_id="C", optimizers=3)]
    owners = [dict(grid_id="east", string_id="A"), dict(grid_id="west", string_id="B"), dict(grid_id="west", string_id="C")]
    panels = [dict(string_id="A", panels=10), dict(string_id="B", panels=8), dict(string_id="C", panels=6)]
    power = [dict(string_id="A", kw_dc="4"), dict(string_id="B", kw_dc="3.2"), dict(string_id="C", kw_dc="2.4")]
    grid.update(string_totals=dict(east=1, west=2), optimizer_totals=dict(east=5, west=7),
        string_assignments=deepcopy(owners), reopened_string_assignments=deepcopy(owners),
        panel_counts=deepcopy(panels), before_panel_counts=deepcopy(panels), reopened_panel_counts=deepcopy(panels),
        dc_power=deepcopy(power), before_dc_power=deepcopy(power), reopened_dc_power=deepcopy(power))
    evidence["saved_strings"], evidence["oracle_strings"] = deepcopy(strings), deepcopy(strings)
    evidence["saved_values"]["strings"] = evidence["oracle_values"]["strings"] = 3
    next(row for row in evidence["view_rows"] if row["metric"] == "strings").update(studio=3, oracle=3)
    for field in ("before_rows", "reopened_rows"):
        evidence[field] = dict(strings=deepcopy(strings), table_rows=deepcopy(evidence["view_rows"]))
    evidence["before"] = canonical_snapshot(strings, evidence["view_rows"])
    evidence["reopened"] = dict(evidence["before"])
    evidence["csv_text"] = csv_fixture(evidence["view_rows"])
    for field in ("oracle_assignments", "saved_assignments", "before_assignments", "reopened_assignments"):
        evidence[field].append(dict(string_id="C", inverter_id="INV-A"))
    evidence["inverter_loads"]["strings"]["INV-A"] = 3
    evidence["oracle_panels"], evidence["oracle_dc_power"] = deepcopy(panels), deepcopy(power)
    return envelope


def test_composed_grid_string_totals_swaps_missing_counts_and_preserved_diagnostics():
    envelope = three_string_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS"
    envelope["grid"]["string_totals"] = dict(east=2, west=1)
    swapped = reconcile_seven_steps_json(json.dumps(envelope))
    assert swapped["verdict"] == "FAIL" and swapped["grid_strings"]["comparison"]["mismatch_count"] == 2
    assert swapped["grid_strings"]["oracle_consistency"]["verdict"] == "PASS"
    for key in baseline.keys() - {"verdict", "grid_strings"}:
        assert swapped[key] == baseline[key]
    del envelope["grid"]["string_totals"]
    envelope["import"]["normal_user"] = False
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing["verdict"] == "UNREADABLE" and missing["steps"]["1"]["verdict"] == "FAIL"
    assert missing["grid_strings"]["comparison"]["reason"] == "grid_string_totals_unreadable"


@pytest.mark.parametrize("mutation", ["boolean", "fraction", "negative", "nan", "infinity", "width", "duplicate", "container"])
def test_grid_string_schema_direct_and_json_composition(mutation):
    envelope = three_string_envelope()
    values = dict(boolean=True, fraction=1.5, negative=-1, nan=float("nan"), infinity=float("inf"), width="1"*81)
    if mutation == "duplicate":
        envelope["grid"]["expected_grid_ids"].append("east")
    elif mutation == "container":
        envelope["grid"]["string_totals"] = [dict(grid_id="east", strings=1)]
    else:
        envelope["grid"]["string_totals"]["east"] = values[mutation]
    source = envelope["assurance"]
    assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
        supplied_inputs={key: bytes.fromhex(value) for key, value in source["inputs_hex"].items()},
        expected_identity=source["identity"], stage_identities=source["stage_identities"])
    preserved = deepcopy(envelope)
    direct = reconcile_seven_steps(envelope["import"], envelope["grid"], assurance)
    transport = deepcopy(envelope)
    if mutation in ("nan", "infinity"):
        transport["grid"]["string_totals"]["east"] = "NaN" if mutation == "nan" else "Infinity"
    decoded = reconcile_seven_steps_json(json.dumps(transport))
    assert direct["verdict"] == "UNREADABLE"
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(decoded))
    assert envelope == preserved


def test_exhaustive_grid_string_totals_match_separate_counting_model():
    from itertools import product
    strings = ["A", "B", "C"]
    for owners in product(("east", "west"), repeat=3):
        rows = [dict(grid_id=owner, string_id=key) for owner, key in zip(owners, strings)]
        for east, west in product(range(4), repeat=2):
            independent = dict(east=east, west=west)
            preserved = deepcopy((strings, rows, independent))
            result = compare_grid_string_totals(independent, strings, rows)
            observed = dict(east=owners.count("east"), west=owners.count("west"))
            changed = [key for key in ("east", "west") if observed[key] != independent[key]]
            assert result["mismatch_count"] == len(changed)
            assert result["verdict"] == ("FAIL" if changed else "PASS")
            assert result["examples"] == [dict(grid_id=key, oracle=independent[key], saved=observed[key],
                delta=observed[key]-independent[key]) for key in changed]
            assert (strings, rows, independent) == preserved


def test_grid_string_lookup_work_and_capped_permutation_stable_diagnostics(monkeypatch):
    from benches import x05_reconcile as module
    from random import Random
    operations = []
    class CountedSet(set):
        def __contains__(self, key):
            operations.append(1)
            return super().__contains__(key)
    monkeypatch.setattr(module, "set", CountedSet, raising=False)
    for size in (10, 100, 1000):
        totals = {f"g{i:04}": 2 for i in range(size)}
        strings = [f"s{i:04}" for i in range(size)]
        rows = [dict(grid_id=f"g{i:04}", string_id=strings[i]) for i in range(size)]
        operations.clear()
        baseline = module.compare_grid_string_totals(totals, strings, rows)
        assert len(operations) == 3*size and baseline["mismatch_count"] == size
        assert len(baseline["examples"]) == min(size, MAX_STRING_EXAMPLES)
        for seed in range(3):
            changed = deepcopy((strings, rows))
            rng = Random(seed)
            for values in changed:
                rng.shuffle(values)
            preserved = deepcopy(changed)
            assert module.compare_grid_string_totals(dict(reversed(list(totals.items()))), *changed) == baseline
            assert changed == preserved


@pytest.mark.parametrize("mode", ["seven", "bound"])
@pytest.mark.parametrize("mutation", ["swap", "oracle"])
def test_composed_grid_string_cli_direct_json_and_bound_siblings(tmp_path, mode, mutation):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as module
    envelope = three_string_envelope()
    envelope["grid"]["string_totals"] = dict(east=2, west=1) if mutation == "swap" else dict(east=1, west=3)
    if mode == "seven":
        source = envelope["assurance"]
        assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
            supplied_inputs={key: bytes.fromhex(value) for key, value in source["inputs_hex"].items()},
            expected_identity=source["identity"], stage_identities=source["stage_identities"])
        direct = reconcile_seven_steps(envelope["import"], envelope["grid"], assurance)
        assert json.loads(serialize_result(direct)) == json.loads(serialize_result(reconcile_seven_steps_json(json.dumps(envelope))))
        payload, flag = envelope, "--seven-step"
    else:
        expected, records = bound_seven_fixture_records()
        envelope["assurance"]["manifest"]["fixture_id"] = expected[0]
        envelope["assurance"]["evidence"]["fixture_id"] = expected[0]
        envelope["assurance"]["bindings"] = {key: deepcopy(envelope["assurance"]["manifest"]) for key in PROVENANCE_STAGES}
        records[0]["envelope"] = envelope
        direct = aggregate_bound_seven_steps(expected, records)
        assert all(direct["fixtures"][key]["verdict"] == "PASS" for key in expected[1:])
        payload, flag = dict(expected_ids=expected, fixtures=records), "--seven-step-bound-aggregate"
    run = subprocess.run([sys.executable, "-P", str(Path(module.__file__).resolve()), flag],
        input=serialize_result(payload).encode("ascii"), capture_output=True, cwd=tmp_path, timeout=20,
        env={"SystemRoot":"C:\\Windows"})
    assert run.returncode == 1 and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(direct))


def test_explicit_inverter_ownership_rejects_missing_duplicate_and_foreign_assignments():
    grids, devices = ["east", "west"], ["I1", "I2"]
    rows = [dict(grid_id="east", inverter_id="I1"), dict(grid_id="west", inverter_id="I2")]
    assert validate_grid_inverter_ownership(grids, devices, rows)["verdict"] == "PASS"
    cases = [(rows[:1], "missing"), (rows + [rows[0]], "duplicate"),
             (rows + [dict(grid_id="east", inverter_id="foreign")], "foreign_inverter"),
             ([dict(grid_id="foreign", inverter_id="I1"), rows[1]], "foreign_grid")]
    for changed, reason in cases:
        preserved = deepcopy(changed)
        result = validate_grid_inverter_ownership(grids, devices, changed)
        assert result["verdict"] == "FAIL" and result["counts"][reason] == 1
        assert validate_grid_inverter_ownership(list(reversed(grids)), list(reversed(devices)), list(reversed(changed))) == result
        assert changed == preserved
    for ids, assignments in [(None, rows), (["I1", "I1"], rows), (devices, None), (devices, [{}])]:
        assert validate_grid_inverter_ownership(grids, ids, assignments)["verdict"] == "UNREADABLE"
    excess = [dict(grid_id="east", inverter_id=f"x{i:03}") for i in range(60)]
    result = validate_grid_inverter_ownership(grids, devices, excess)
    assert result["counts"]["foreign_inverter"] == 60 and len(result["examples"]["foreign_inverter"]) == MAX_STRING_EXAMPLES


def test_grid_inverter_counts_reject_equal_grand_total_grid_swaps():
    devices = ["I1", "I2", "I3"]
    rows = [dict(grid_id="east", inverter_id="I1"), dict(grid_id="west", inverter_id="I2"), dict(grid_id="west", inverter_id="I3")]
    independent = dict(east=1, west=2)
    assert compare_grid_inverter_totals(independent, devices, rows)["verdict"] == "PASS"
    changed = deepcopy(rows)
    changed[0]["grid_id"], changed[1]["grid_id"], changed[2]["grid_id"] = "west", "east", "east"
    preserved = deepcopy(changed)
    result = compare_grid_inverter_totals(independent, devices, changed)
    assert result == dict(verdict="FAIL", reason="grid_inverter_totals_mismatch", mismatch_count=2,
        examples=[dict(grid_id="east", oracle=1, saved=2, delta=1), dict(grid_id="west", oracle=2, saved=1, delta=-1)])
    assert compare_grid_inverter_totals(None, devices, rows)["verdict"] == "UNREADABLE"
    assert changed == preserved


def test_grid_inverter_oracle_requires_coverage_and_exact_independent_sum():
    assert compare_grid_inverter_oracle_totals(["east", "west"], dict(east=1, west=2), dict(inverters=3))["verdict"] == "PASS"
    assert compare_grid_inverter_oracle_totals(["east", "west"], dict(east=1, west=2), dict(inverters=4))["verdict"] == "FAIL"
    for grids, totals, oracle in [(["g", "g"], dict(g=1), dict(inverters=1)), (["g"], None, {}),
            (["g"], [dict(grid_id="g", inverters=1), dict(grid_id="g", inverters=1)], dict(inverters=2)),
            (["east", "west"], dict(east=1), dict(inverters=1)), (["g"], dict(g=1), {}),
            (["g"], dict(g=True), dict(inverters=1))]:
        assert compare_grid_inverter_oracle_totals(grids, totals, oracle)["verdict"] == "UNREADABLE"


def test_inverter_snapshots_detect_substituted_ids_owner_swaps_and_absence():
    grids, devices = ["east", "west"], ["I1", "I2"]
    rows = [dict(grid_id="east", inverter_id="I1"), dict(grid_id="west", inverter_id="I2")]
    substitution = [rows[0], dict(grid_id="west", inverter_id="I3")]
    result = compare_inverter_snapshots(grids, devices, rows, ["I1", "I3"], substitution)
    assert result["verdict"] == "FAIL" and result["counts"] == dict(added=1, removed=1, changed=0)
    assert result["added_ids"] == ["I3"] and result["removed_ids"] == ["I2"]
    changed = [dict(grid_id="west", inverter_id="I1"), dict(grid_id="east", inverter_id="I2")]
    assert compare_inverter_snapshots(grids, devices, rows, devices, changed)["counts"] == dict(added=0, removed=0, changed=2)
    missing = compare_inverter_snapshots(grids, devices, rows, None, None)
    assert missing["verdict"] == "UNREADABLE" and missing["reopened"]["reason"] == "inverter_ownership_evidence_unreadable"
    preserved = deepcopy((devices, rows))
    assert compare_inverter_snapshots(grids, devices, rows, list(reversed(devices)), list(reversed(rows)))["verdict"] == "PASS"
    assert (devices, rows) == preserved


def test_inverter_integer_reference_table_and_signed_approval_model_preserve_inputs():
    for saved_count in range(5):
        for oracle_count in range(5):
            delta = saved_count - oracle_count
            saved, rows = table_fixture()
            oracle = dict(saved, inverters=oracle_count)
            saved["inverters"] = saved_count
            next(row for row in rows if row["metric"] == "inverters").update(
                studio=saved_count, oracle=oracle_count, delta=delta, verdict="PASS" if delta == 0 else "FAIL")
            preserved = deepcopy((saved, oracle, rows))
            assert compare_table(rows, saved, oracle)["verdict"] == "PASS"
            for approval in range(-4, 5):
                deltas = dict.fromkeys(METRICS, 0)
                deltas["inverters"] = delta
                record = dict(fixture_id="synthetic", metric="inverters", delta=approval,
                              name="explicit inverter revision", frozen_at="2026-11-15T12:00:00Z")
                policy_inputs = deepcopy((deltas, record))
                result = check_discrepancies("synthetic", deltas, [record], "2026-11-15T13:00:00Z")
                assert result["verdict"] == ("PASS" if delta == 0 or approval == delta else "FAIL")
                assert (deltas, record) == policy_inputs
            assert (saved, oracle, rows) == preserved


def test_composed_inverter_grid_counts_swaps_missing_evidence_and_unrelated_diagnostics():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS"
    envelope["grid"]["inverter_totals"] = dict(east=0, west=1)
    changed = reconcile_seven_steps_json(json.dumps(envelope))
    assert changed["verdict"] == "FAIL" and changed["grid_inverters"]["comparison"]["mismatch_count"] == 2
    assert changed["grid_inverters"]["oracle_consistency"]["verdict"] == "PASS"
    for key in baseline.keys() - {"verdict", "grid_inverters"}:
        assert changed[key] == baseline[key]
    del envelope["grid"]["inverter_totals"]
    envelope["import"]["normal_user"] = False
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing["verdict"] == "UNREADABLE" and missing["steps"]["1"]["verdict"] == "FAIL"
    assert missing["grid_inverters"]["comparison"]["reason"] == "grid_inverter_totals_unreadable"


def test_composed_inverter_snapshots_bind_saved_ids_and_owners_and_detect_substitution():
    for field in ("before_inverters", "reopened_inverters"):
        envelope = equal_count_ownership_envelope()
        envelope["assurance"]["evidence"][field][0]["inverter_id"] = "INV-B"
        result = reconcile_seven_steps_json(json.dumps(envelope))
        assert result["verdict"] == "FAIL" and result["inverter_reopen"]["verdict"] == "FAIL"
        assert result["steps"]["7"]["verdict"] == "PASS"
    envelope = equal_count_ownership_envelope()
    for field in ("before_inverters", "reopened_inverters"):
        envelope["assurance"]["evidence"][field][0]["grid_id"] = "west"
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result["inverter_reopen"]["counts"] == dict(added=0, removed=0, changed=0)
    assert result["inverter_reopen"]["saved_binding"]["counts"]["changed"] == 1
    assert result["verdict"] == "FAIL"
    del envelope["assurance"]["evidence"]["reopened_inverters"]
    absent = reconcile_seven_steps_json(json.dumps(envelope))
    assert absent["verdict"] == "UNREADABLE"
    assert absent["inverter_reopen"]["reopened"]["reason"] == "inverter_ownership_evidence_unreadable"


@pytest.mark.parametrize("mutation", ["null_id", "blank_id", "wide_id", "null_owner", "foreign_owner", "boolean", "fraction", "negative", "nan", "duplicate", "container"])
def test_inverter_schema_pure_direct_json_and_bound_siblings(mutation):
    envelope = equal_count_ownership_envelope()
    evidence = envelope["assurance"]["evidence"]
    row = evidence["saved_inverters"][0]
    values = dict(null_id=None, blank_id=" ", wide_id="x"*129)
    if mutation in values:
        row["inverter_id"] = values[mutation]
    elif mutation in ("null_owner", "foreign_owner"):
        row["grid_id"] = None if mutation == "null_owner" else "foreign"
    elif mutation in ("boolean", "fraction", "negative", "nan"):
        envelope["grid"]["inverter_totals"]["east"] = dict(boolean=True, fraction=1.5, negative=-1, nan="NaN")[mutation]
    elif mutation == "duplicate":
        evidence["saved_inverters"].append(deepcopy(row))
    else:
        evidence["saved_inverters"] = {}
    if mutation in ("null_id", "blank_id", "wide_id", "null_owner", "foreign_owner", "duplicate", "container"):
        assert validate_grid_inverter_ownership(["east", "west"], ["INV-A"], evidence["saved_inverters"])["verdict"] in ("FAIL", "UNREADABLE")
    else:
        assert compare_grid_inverter_totals(envelope["grid"]["inverter_totals"], ["INV-A"], evidence["saved_inverters"])["verdict"] == "UNREADABLE"
    source = envelope["assurance"]
    assurance = dict(evidence=source["evidence"], frozen_manifest=source["manifest"], stage_bindings=source["bindings"],
        supplied_inputs={key: bytes.fromhex(value) for key, value in source["inputs_hex"].items()},
        expected_identity=source["identity"], stage_identities=source["stage_identities"])
    preserved = deepcopy(envelope)
    direct = reconcile_seven_steps(envelope["import"], envelope["grid"], assurance)
    decoded = reconcile_seven_steps_json(json.dumps(envelope))
    assert direct["verdict"] == ("FAIL" if mutation == "foreign_owner" else "UNREADABLE")
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(decoded))
    assert envelope == preserved
    expected, records = bound_seven_fixture_records()
    envelope["assurance"]["manifest"]["fixture_id"] = expected[0]
    envelope["assurance"]["evidence"]["fixture_id"] = expected[0]
    envelope["assurance"]["bindings"] = {key: deepcopy(envelope["assurance"]["manifest"]) for key in PROVENANCE_STAGES}
    records[0]["envelope"] = envelope
    aggregate = aggregate_bound_seven_steps_json(json.dumps(dict(expected_ids=expected, fixtures=records)))
    assert all(aggregate["fixtures"][key]["verdict"] == "PASS" for key in expected[1:])


def test_exhaustive_inverter_identity_owner_and_count_reference_models():
    from itertools import product
    devices = ["I1", "I2"]
    choices = [(grid, key) for grid in ("east", "west", "foreign") for key in ("I1", "I2", "foreign")]
    for size in range(4):
        for selection in product(choices, repeat=size):
            rows = [dict(grid_id=grid, inverter_id=key) for grid, key in selection]
            ids = [key for _, key in selection]
            counts = dict(missing=sum(key not in ids for key in devices), duplicate=len(ids)-len(set(ids)),
                foreign_inverter=sum(key not in devices for key in ids), foreign_grid=sum(grid not in ("east", "west") for grid, _ in selection))
            result = validate_grid_inverter_ownership(["east", "west"], devices, rows)
            assert result["counts"] == counts
            assert result["verdict"] == ("FAIL" if any(counts.values()) else "PASS")
    states = []
    for ids in (["I1"], ["I2"], ["I1", "I2"]):
        for owners in product(("east", "west"), repeat=len(ids)):
            rows = [dict(inverter_id=key, grid_id=grid) for key, grid in zip(ids, owners)]
            states.append((ids, rows))
            for east, west in product(range(3), repeat=2):
                expected = dict(east=east, west=west)
                result = compare_grid_inverter_totals(expected, ids, rows)
                observed = {grid: owners.count(grid) for grid in ("east", "west")}
                assert result["mismatch_count"] == sum(observed[grid] != expected[grid] for grid in expected)
    for before in states:
        for reopened in states:
            preserved = deepcopy((before, reopened))
            result = compare_inverter_snapshots(["east", "west"], *before, *reopened)
            old, new = ({row["inverter_id"]: row["grid_id"] for row in state[1]} for state in (before, reopened))
            expected = dict(added=len(set(new)-set(old)), removed=len(set(old)-set(new)),
                            changed=sum(old[key] != new[key] for key in set(old)&set(new)))
            assert result["counts"] == expected and result["verdict"] == ("FAIL" if any(expected.values()) else "PASS")
            assert (before, reopened) == preserved


def test_generated_inverter_lookup_work_and_capped_error_permutations(monkeypatch):
    from benches import x05_reconcile as module
    from random import Random
    operations = []
    class CountedSet(set):
        def __contains__(self, key):
            operations.append(1)
            return super().__contains__(key)
    monkeypatch.setattr(module, "set", CountedSet, raising=False)
    for size in (10, 100, 1000):
        grids = [f"g{i:04}" for i in range(size)]
        devices = [f"i{i:04}" for i in range(size)]
        rows = [dict(grid_id=grid, inverter_id=key) for grid, key in zip(grids, devices)]
        operations.clear()
        assert module.validate_grid_inverter_ownership(grids, devices, rows)["verdict"] == "PASS"
        assert len(operations) == 3*size
        operations.clear()
        totals = module.compare_grid_inverter_totals(dict.fromkeys(grids, 2), devices, rows)
        assert len(operations) == 3*size and totals["mismatch_count"] == size
        assert len(totals["examples"]) == min(size, MAX_STRING_EXAMPLES)
        errors = rows + deepcopy(rows) + [dict(grid_id="foreign", inverter_id=f"x{i:04}") for i in range(size)]
        operations.clear()
        baseline = module.validate_grid_inverter_ownership(grids, devices, errors)
        assert len(operations) == 9*size
        assert baseline["counts"] == dict(missing=0, duplicate=size, foreign_inverter=size, foreign_grid=size)
        assert all(len(values) == min(size, MAX_STRING_EXAMPLES) for values in baseline["examples"].values())
        for seed in range(3):
            changed = deepcopy((grids, devices, errors))
            rng = Random(seed)
            for values in changed:
                rng.shuffle(values)
            preserved = deepcopy(changed)
            assert module.validate_grid_inverter_ownership(*changed) == baseline
            assert changed == preserved


def test_string_inverter_assignment_validation_and_bounded_diagnostics():
    from benches import x05_reconcile as m
    devices, strings = ['I1', 'I2'], ['A', 'B']
    rows = [dict(string_id='A', inverter_id='I1'), dict(string_id='B', inverter_id='I2')]
    assert m.validate_string_inverter_assignments(devices, strings, rows)['verdict'] == 'PASS'
    cases = [(rows[:1], 'missing'), (rows + rows[:1], 'duplicate'),
             (rows + [dict(string_id='C', inverter_id='I1')], 'foreign_string'),
             ([dict(string_id='A', inverter_id='X'), rows[1]], 'foreign_inverter')]
    for values, reason in cases:
        preserved = deepcopy(values)
        result = m.validate_string_inverter_assignments(devices, strings, values)
        assert result['verdict'] == 'FAIL' and result['counts'][reason] == 1
        assert result == m.validate_string_inverter_assignments(devices[::-1], strings[::-1], values[::-1])
        assert values == preserved
    for ids in (None, [], ['I1', 'I1'], [True], ['x'*129]):
        assert m.validate_string_inverter_assignments(ids, strings, rows)['verdict'] == 'UNREADABLE'
    for ids in (None, [], ['A', 'A'], [True], ['x'*129]):
        assert m.validate_string_inverter_assignments(devices, ids, rows)['verdict'] == 'UNREADABLE'
    for values in (None, {}, [{}], [dict(string_id='A', inverter_id=True)],
                   [dict(string_id='A', inverter_id='x'*129)], [dict(string_id='A', inverter_id=' ')],
                   [dict(string_id='A', inverter_id='I1', extra='x')]):
        assert m.validate_string_inverter_assignments(devices, strings, values)['verdict'] == 'UNREADABLE'
    excess = [dict(string_id=f'x{i:03}', inverter_id='foreign') for i in range(70)]
    result = m.validate_string_inverter_assignments(devices, strings, excess)
    assert result['counts'] == dict(missing=2, duplicate=0, foreign_string=70, foreign_inverter=70)
    assert all(len(v) <= MAX_STRING_EXAMPLES for v in result['examples'].values())
    assert result == m.validate_string_inverter_assignments(devices, strings, excess[::-1])


def test_string_inverter_reference_swaps_snapshots_and_missing_evidence():
    from itertools import product
    from benches import x05_reconcile as m
    devices, strings = ['I1', 'I2'], ['A', 'B', 'C']
    for old in product(devices, repeat=3):
        before = [dict(string_id=k, inverter_id=v) for k, v in zip(strings, old)]
        for new in product(devices, repeat=3):
            after = [dict(string_id=k, inverter_id=v) for k, v in zip(strings, new)]
            preserved = deepcopy((before, after))
            result = m.compare_string_inverter_assignments(devices, strings, before, after)
            snapshot = m.compare_string_inverter_snapshots(devices, strings, before, after)
            count = sum(a != b for a, b in zip(old, new))
            assert result['changed_count'] == snapshot['changed_count'] == count
            assert result['verdict'] == snapshot['verdict'] == ('FAIL' if count else 'PASS')
            assert (before, after) == preserved
    old = [dict(string_id='A', inverter_id='I1'), dict(string_id='B', inverter_id='I2')]
    new = [dict(string_id='A', inverter_id='I2'), dict(string_id='B', inverter_id='I1')]
    assert sorted(r['inverter_id'] for r in old) == sorted(r['inverter_id'] for r in new)
    assert m.compare_string_inverter_snapshots(devices, strings[:2], old, new)['changed_count'] == 2
    for left, right in ((None, new), (old, None), (None, None)):
        assert m.compare_string_inverter_assignments(devices, strings[:2], left, right)['verdict'] == 'UNREADABLE'
        assert m.compare_string_inverter_snapshots(devices, strings[:2], left, right)['verdict'] == 'UNREADABLE'
    rows = [dict(string_id=f's{i:03}', inverter_id='I1') for i in range(70)]
    changed = [dict(r, inverter_id='I2') for r in rows]
    result = m.compare_string_inverter_snapshots(devices, [r['string_id'] for r in rows], rows, changed)
    assert result['changed_count'] == 70 and len(result['examples']) == MAX_STRING_EXAMPLES
    assert result == m.compare_string_inverter_snapshots(devices, [r['string_id'] for r in rows][::-1], rows[::-1], changed[::-1])


@pytest.mark.parametrize('mode', ['seven', 'bound'])
@pytest.mark.parametrize('mutation', ['owner', 'substitute', 'missing'])
def test_inverter_composed_cli_differential(tmp_path, mode, mutation):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    evidence = envelope['assurance']['evidence']
    if mutation == 'owner':
        evidence['saved_inverters'][0]['grid_id'] = 'west'
    elif mutation == 'substitute':
        evidence['reopened_inverters'][0]['inverter_id'] = 'replacement'
    else:
        evidence['reopened_inverters'] = None
    if mode == 'seven':
        source = envelope['assurance']
        assurance = dict(evidence=source['evidence'], frozen_manifest=source['manifest'], stage_bindings=source['bindings'],
            supplied_inputs={k: bytes.fromhex(v) for k, v in source['inputs_hex'].items()},
            expected_identity=source['identity'], stage_identities=source['stage_identities'])
        direct = m.reconcile_seven_steps(envelope['import'], envelope['grid'], assurance)
        assert json.loads(serialize_result(direct)) == json.loads(serialize_result(m.reconcile_seven_steps_json(json.dumps(envelope))))
        payload, flag = envelope, '--seven-step'
    else:
        expected, records = bound_seven_fixture_records()
        envelope['assurance']['manifest']['fixture_id'] = expected[0]
        evidence['fixture_id'] = expected[0]
        envelope['assurance']['bindings'] = {k: deepcopy(envelope['assurance']['manifest']) for k in PROVENANCE_STAGES}
        records[0]['envelope'] = envelope
        direct = m.aggregate_bound_seven_steps(expected, records)
        assert all(direct['fixtures'][k]['verdict'] == 'PASS' for k in expected[1:])
        payload, flag = dict(expected_ids=expected, fixtures=records), '--seven-step-bound-aggregate'
    run = subprocess.run([sys.executable, '-P', str(Path(m.__file__).resolve()), flag],
        input=serialize_result(payload).encode('ascii'), capture_output=True, cwd=tmp_path, timeout=20,
        env={'SystemRoot': 'C:\\Windows'})
    assert run.returncode == (4 if mutation == 'missing' else 1) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(direct))


def test_inverter_bound_aggregate_evaluates_only_unique_readable_fixtures(monkeypatch):
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    baseline = m.aggregate_bound_seven_steps(expected, records)
    names = ('compare_grid_inverter_totals', 'compare_grid_inverter_oracle_totals', 'compare_inverter_snapshots')
    calls = dict.fromkeys(names, 0)
    def counted(name, original):
        def invoke(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)
        return invoke
    for name in names:
        monkeypatch.setattr(m, name, counted(name, getattr(m, name)))
    assert m.aggregate_bound_seven_steps(expected, records) == baseline
    assert calls == dict(zip(names, (3, 3, 6)))  # snapshot comparison and saved binding are separate evidence pairs
    calls.update(dict.fromkeys(names, 0))
    result = m.aggregate_bound_seven_steps(expected, [dict(fixture_id=expected[0], envelope=None)] + records)
    assert result['verdict'] == 'UNREADABLE' and expected[0] not in result['fixtures']
    assert calls == dict(zip(names, (2, 2, 4)))
    for key in expected[1:]:
        assert result['fixtures'][key] == baseline['fixtures'][key]


def test_string_inverter_composition_proof_limit_replay():
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    before = [dict(string_id='A', inverter_id='I1'), dict(string_id='B', inverter_id='I2')]
    after = [dict(string_id='A', inverter_id='I2'), dict(string_id='B', inverter_id='I1')]
    assert m.reconcile_seven_steps_json(json.dumps(envelope))['verdict'] == 'PASS'
    assert m.compare_string_inverter_assignments(['I1', 'I2'], ['A', 'B'], before, after)['verdict'] == 'FAIL'
    assert m.compare_string_inverter_snapshots(['I1', 'I2'], ['A', 'B'], before, after)['verdict'] == 'FAIL'
    assert m.compare_string_inverter_snapshots(['I1', 'I2'], ['A', 'B'], before, None)['verdict'] == 'UNREADABLE'
    assert m.compare_string_inverter_snapshots(['I1', 'I2'], ['A', 'B'], before, before)['verdict'] == 'PASS'


def connection_envelope():
    envelope = seven_step_envelope()
    evidence = envelope['assurance']['evidence']
    envelope['grid']['inverter_totals']['g'] = 2
    for field in ('saved_inverters', 'before_inverters', 'reopened_inverters'):
        evidence[field].append(dict(inverter_id='INV-B', grid_id='g'))
    for field in ('oracle_assignments', 'saved_assignments', 'before_assignments', 'reopened_assignments'):
        evidence[field][1]['inverter_id'] = 'INV-B'
    for field in ('saved_values', 'oracle_values'):
        evidence[field]['inverters'] = 2
    for rows in (evidence['view_rows'], evidence['before_rows']['table_rows'], evidence['reopened_rows']['table_rows']):
        next(row for row in rows if row['metric'] == 'inverters').update(studio=2, oracle=2)
    evidence['before'] = canonical_snapshot(evidence['before_rows']['strings'], evidence['before_rows']['table_rows'])
    evidence['reopened'] = canonical_snapshot(evidence['reopened_rows']['strings'], evidence['reopened_rows']['table_rows'])
    evidence['csv_text'] = csv_fixture(evidence['view_rows'])
    evidence['inverter_loads'] = dict(strings={'INV-A':1, 'INV-B':1}, optimizers={'INV-A':5, 'INV-B':7},
        panels={'INV-A':10, 'INV-B':14}, kw_dc={'INV-A':'4', 'INV-B':'5.6'})
    return envelope


def test_composed_assignment_swaps_bindings_and_absence():
    envelope = connection_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline['verdict'] == 'PASS'
    evidence = envelope['assurance']['evidence']
    for field in ('saved_assignments', 'before_assignments', 'reopened_assignments'):
        evidence[field][0]['inverter_id'], evidence[field][1]['inverter_id'] = 'INV-B', 'INV-A'
    preserved = deepcopy(envelope)
    swapped = reconcile_seven_steps_json(json.dumps(envelope))
    assert swapped['verdict'] == 'FAIL' and swapped['assignments']['changed_count'] == 2
    assert swapped['assignment_reopen']['verdict'] == 'PASS'
    for key in baseline.keys() - {'verdict', 'assignments', 'inverter_loads'}:
        assert swapped[key] == baseline[key]
    assert envelope == preserved
    evidence['saved_assignments'] = deepcopy(evidence['oracle_assignments'])
    unrelated = reconcile_seven_steps_json(json.dumps(envelope))
    assert unrelated['assignment_reopen']['saved_binding']['changed_count'] == 2
    evidence['oracle_assignments'] = None
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing['verdict'] == 'UNREADABLE' and missing['assignment_reopen']['verdict'] == 'FAIL'
    evidence['reopened_assignments'] = None
    assert reconcile_seven_steps_json(json.dumps(envelope))['assignment_reopen']['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('stage', ['before', 'reopened'])
def test_assignment_snapshot_requires_own_endpoint_id_sets(stage):
    envelope = connection_envelope()
    evidence = envelope['assurance']['evidence']
    evidence[stage + '_inverters'][0]['inverter_id'] = 'substitute'
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result['assignment_reopen']['states'][stage]['counts']['foreign_inverter'] == 1
    evidence[stage + '_rows']['strings'][0]['string_id'] = 'foreign'
    assert reconcile_seven_steps_json(json.dumps(envelope))['assignment_reopen']['states'][stage]['counts']['missing'] == 1


@pytest.mark.parametrize('rows', [None, {}, 'private-canary', [None], [{}],
    [dict(string_id='', inverter_id='I')], [dict(string_id='A', inverter_id='x'*129)],
    [dict(string_id='x'*129, inverter_id='I')], [dict(string_id='A', inverter_id=' ')],
    [dict(string_id=True, inverter_id='I')]])
def test_assignment_schema_named_bounded_sanitized_and_unchanged(rows):
    from benches import x05_reconcile as m
    preserved = deepcopy(rows)
    results = [m.validate_string_inverter_assignments(['I'], ['A'], rows),
        m.compare_string_inverter_assignments(['I'], ['A'], rows, rows),
        m.compare_string_inverter_snapshots(['I'], ['A'], rows, rows)]
    for result in results:
        assert result['verdict'] == 'UNREADABLE'
        text = serialize_result(result)
        assert len(text) < 1000 and 'private-canary' not in text and 'x'*129 not in text
    assert rows == preserved


def test_generated_assignment_lookup_work_and_caps(monkeypatch):
    from benches import x05_reconcile as m
    from random import Random
    operations = []
    class CountedSet(set):
        def __contains__(self, key):
            operations.append(1)
            return super().__contains__(key)
    monkeypatch.setattr(m, 'set', CountedSet, raising=False)
    for size in (10, 100, 1000):
        ids = [f'I{i:04}' for i in range(size)]
        strings = [f's{i:04}' for i in range(size)]
        rows = [dict(string_id=k, inverter_id=ids[i]) for i, k in enumerate(strings)]
        operations.clear()
        assert m.validate_string_inverter_assignments(ids, strings, rows)['verdict'] == 'PASS'
        assert len(operations) == 3*size
        errors = rows + rows + [dict(string_id=f'x{i:04}', inverter_id='foreign') for i in range(size)]
        baseline = m.validate_string_inverter_assignments(ids, strings, errors)
        assert baseline['counts'] == dict(missing=0, duplicate=size, foreign_string=size, foreign_inverter=size)
        assert all(len(v) == min(size, MAX_STRING_EXAMPLES) for v in baseline['examples'].values())
        for seed in range(3):
            changed = deepcopy(errors)
            Random(seed).shuffle(changed)
            assert m.validate_string_inverter_assignments(ids[::-1], strings[::-1], changed) == baseline


@pytest.mark.parametrize('mutation', ['swap', 'before', 'missing'])
def test_assignment_transport_differential_and_siblings(tmp_path, mutation):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = connection_envelope()
    evidence = envelope['assurance']['evidence']
    if mutation == 'missing':
        evidence['reopened_assignments'] = None
    else:
        fields = ('saved_assignments',) if mutation == 'swap' else ('before_assignments', 'reopened_assignments')
        for field in fields:
            evidence[field][0]['inverter_id'], evidence[field][1]['inverter_id'] = 'INV-B', 'INV-A'
    source = envelope['assurance']
    assurance = dict(evidence=evidence, frozen_manifest=source['manifest'], stage_bindings=source['bindings'],
        supplied_inputs={k: bytes.fromhex(v) for k, v in source['inputs_hex'].items()},
        expected_identity=source['identity'], stage_identities=source['stage_identities'])
    direct = m.reconcile_seven_steps(envelope['import'], envelope['grid'], assurance)
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(m.reconcile_seven_steps_json(json.dumps(envelope))))
    expected, records = bound_seven_fixture_records()
    source['manifest']['fixture_id'] = evidence['fixture_id'] = expected[0]
    source['bindings'] = {k: deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    aggregate = m.aggregate_bound_seven_steps(expected, records)
    assert all(aggregate['fixtures'][k]['verdict'] == 'PASS' for k in expected[1:])
    payload = dict(expected_ids=expected, fixtures=records)
    run = subprocess.run([sys.executable, '-P', str(Path(m.__file__).resolve()), '--seven-step-bound-aggregate'],
        input=serialize_result(payload).encode('ascii'), capture_output=True, cwd=tmp_path, timeout=20,
        env={'SystemRoot': 'C:\\Windows'})
    assert run.returncode == (4 if mutation == 'missing' else 1) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(aggregate))


def test_inverter_string_load_swaps_missing_counts_and_identity_errors():
    from benches import x05_reconcile as m
    strings = ['A', 'B', 'C']
    rows = [dict(string_id=k, inverter_id=v) for k, v in zip(strings, ['I1', 'I2', 'I2'])]
    assert m.compare_inverter_string_totals(dict(I1=1, I2=2), strings, rows)['verdict'] == 'PASS'
    result = m.compare_inverter_string_totals(dict(I1=2, I2=1), strings, rows)
    assert result['mismatch_count'] == 2 and result['verdict'] == 'FAIL'
    assert [r['delta'] for r in result['examples']] == [-1, 1]
    assert m.compare_inverter_string_totals(None, strings, rows)['verdict'] == 'UNREADABLE'
    for changed in (rows[:1], rows + rows[:1], rows + [dict(string_id='X', inverter_id='I1')],
                    [dict(string_id='A', inverter_id='X')] + rows[1:]):
        assert m.compare_inverter_string_totals(dict(I1=1, I2=2), strings, changed)['verdict'] == 'FAIL'


@pytest.mark.parametrize('metric', ['optimizers', 'panels'])
def test_inverter_count_load_reference_swaps_and_id_binding(metric):
    from itertools import product
    from benches import x05_reconcile as m
    strings = ['A', 'B', 'C']
    for owners in product(['I1', 'I2'], repeat=3):
        assignments = [dict(string_id=k, inverter_id=v) for k, v in zip(strings, owners)]
        for values in product([1, 2], repeat=3):
            rows = [dict(string_id=k, **{metric:v}) for k, v in zip(strings, values)]
            expected = {key: sum(v for owner, v in zip(owners, values) if owner == key) for key in ['I1', 'I2']}
            original = deepcopy((expected, rows, assignments))
            def check(totals, supplied=rows):
                if metric == 'optimizers':
                    return m.compare_inverter_optimizer_totals(totals, supplied, assignments)
                return m.compare_inverter_panel_totals(totals, strings, supplied, assignments)
            assert check(expected)['verdict'] == 'PASS'
            if expected['I1'] != expected['I2']:
                assert check(dict(I1=expected['I2'], I2=expected['I1']))['mismatch_count'] == 2
            assert check(None)['verdict'] == 'UNREADABLE'
            assert (expected, rows, assignments) == original
    assert m.compare_inverter_panel_totals(dict(I1=1), ['A'], [dict(string_id='B', panels=1)],
        [dict(string_id='A', inverter_id='I1')])['reason'] == 'inverter_panels_string_identity_conflict'


def test_inverter_dc_tiny_load_changes_context_preservation_and_zero():
    from decimal import Decimal, localcontext, Inexact, Rounded
    from benches import x05_reconcile as m
    strings = ['A', 'B']
    assignments = [dict(string_id='A', inverter_id='I1'), dict(string_id='B', inverter_id='I2')]
    rows = [dict(string_id='A', kw_dc='0.'+'0'*76+'1'), dict(string_id='B', kw_dc='0.'+'0'*76+'2')]
    expected = dict(I1=rows[0]['kw_dc'], I2=rows[1]['kw_dc'])
    original = deepcopy((expected, rows, assignments))
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = context.traps[Rounded] = True
        flags = dict(context.flags)
        assert m.compare_inverter_dc_totals(expected, strings, rows, assignments)['verdict'] == 'PASS'
        result = m.compare_inverter_dc_totals(dict(I1=expected['I2'], I2=expected['I1']), strings, rows, assignments)
        assert result['verdict'] == 'FAIL' and result['mismatch_count'] == 2
        assert result['examples'][0]['delta'] == Decimal('-1e-77')
        assert dict(context.flags) == flags and context.prec == 3
    assert (expected, rows, assignments) == original
    assert m.compare_inverter_dc_totals(dict(I1='0'), ['A'], [dict(string_id='A', kw_dc='0')], assignments[:1])['verdict'] == 'PASS'
    assert m.compare_inverter_dc_totals(None, strings, rows, assignments)['verdict'] == 'UNREADABLE'
    assert m.compare_inverter_dc_totals(expected, strings, None, assignments)['verdict'] == 'UNREADABLE'
    assert m.compare_inverter_dc_totals(expected, strings, rows[:1], assignments)['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('metric', ['strings', 'optimizers', 'panels', 'kw_dc'])
def test_inverter_oracle_coverage_totals_schema_and_contradictions(metric):
    from benches import x05_reconcile as m
    totals = dict(I1='0.1', I2='0.2') if metric == 'kw_dc' else dict(I1=1, I2=2)
    oracle = {metric:'0.3' if metric == 'kw_dc' else 3}
    assert m.compare_inverter_metric_oracle_totals(['I1', 'I2'], totals, oracle, metric)['verdict'] == 'PASS'
    assert m.compare_inverter_metric_oracle_totals(['I1', 'I2'], totals, {metric:4}, metric)['verdict'] == 'FAIL'
    for ids, values in ((['I1', 'I1'], totals), (['I1'], totals), (['I1', 'I2', 'I3'], totals), (['I1', 'I2'], None)):
        assert m.compare_inverter_metric_oracle_totals(ids, values, oracle, metric)['verdict'] == 'UNREADABLE'
    for bad in (True, -1, float('nan'), float('inf'), 2**1000, 'x'*129):
        assert m.compare_inverter_metric_oracle_totals(['I1'], dict(I1=bad), oracle, metric)['verdict'] == 'UNREADABLE'
    assert m.compare_inverter_metric_oracle_totals(['I1'], dict(I1=1), {}, metric)['verdict'] == 'UNREADABLE'


def test_inverter_load_caps_and_permutation_stability():
    from benches import x05_reconcile as m
    strings = [f's{i:03}' for i in range(70)]
    totals = {f'I{i:03}':2 for i in range(70)}
    rows = [dict(string_id=k, inverter_id=f'I{i:03}') for i,k in enumerate(strings)]
    result = m.compare_inverter_string_totals(totals, strings, rows)
    assert result['mismatch_count'] == 70 and len(result['examples']) == MAX_STRING_EXAMPLES
    assert result == m.compare_inverter_string_totals(dict(reversed(list(totals.items()))), strings[::-1], rows[::-1])


def test_composed_inverter_loads_swap_missing_and_unrelated_diagnostics():
    envelope = connection_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline['verdict'] == 'PASS'
    loads = envelope['assurance']['evidence']['inverter_loads']
    loads['optimizers'] = {'INV-A':7, 'INV-B':5}
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result['verdict'] == 'FAIL'
    assert result['inverter_loads']['metrics']['optimizers']['comparison']['mismatch_count'] == 2
    assert result['inverter_loads']['metrics']['optimizers']['oracle_consistency']['verdict'] == 'PASS'
    for key in baseline.keys() - {'verdict', 'inverter_loads'}:
        assert result[key] == baseline[key]
    del loads['kw_dc']
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing['verdict'] == 'UNREADABLE'
    assert missing['inverter_loads']['metrics']['optimizers'] == result['inverter_loads']['metrics']['optimizers']


def test_inverter_load_reopen_binds_before_and_detects_snapshot_redistribution():
    envelope = connection_envelope()
    envelope['grid']['reopened_dc_power'][0]['kw_dc'] = '5.6'
    envelope['grid']['reopened_dc_power'][1]['kw_dc'] = '4'
    result = reconcile_seven_steps_json(json.dumps(envelope))
    check = result['inverter_load_reopen']['metrics']['kw_dc']
    assert check['comparison']['counts']['changed'] == 2 and check['saved_binding']['verdict'] == 'PASS'
    envelope['grid']['before_dc_power'] = deepcopy(envelope['grid']['reopened_dc_power'])
    result = reconcile_seven_steps_json(json.dumps(envelope))
    check = result['inverter_load_reopen']['metrics']['kw_dc']
    assert check['comparison']['verdict'] == 'PASS' and check['saved_binding']['counts']['changed'] == 2
    envelope['assurance']['evidence']['reopened_assignments'] = None
    assert reconcile_seven_steps_json(json.dumps(envelope))['inverter_load_reopen']['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('metric', ['strings', 'optimizers', 'panels', 'kw_dc'])
@pytest.mark.parametrize('bad', [None, [], True, {'INV-A':True}, {'INV-A':-1}, {'INV-A':'NaN'}, {'x'*129:1}])
def test_inverter_load_composition_schema_direct_json_siblings(metric, bad):
    from benches import x05_reconcile as m
    envelope = connection_envelope()
    envelope['assurance']['evidence']['inverter_loads'][metric] = bad
    preserved = deepcopy(envelope)
    direct = m._seven_step_envelope(envelope)
    parsed = reconcile_seven_steps_json(json.dumps(envelope))
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(parsed))
    assert direct['verdict'] == 'UNREADABLE'
    assert direct['inverter_loads']['metrics'][metric]['comparison']['verdict'] == 'UNREADABLE'
    assert 'x'*129 not in serialize_result(direct)
    assert direct['assignment_reopen']['verdict'] == 'PASS' and envelope == preserved


@pytest.mark.parametrize('field', ['saved_assignments', 'saved_inverters'])
def test_inverter_load_composition_duplicate_rows(field):
    envelope = connection_envelope()
    rows = envelope['assurance']['evidence'][field]
    rows.append(deepcopy(rows[0]))
    assert reconcile_seven_steps_json(json.dumps(envelope))['inverter_loads']['verdict'] != 'PASS'


def test_inverter_load_composition_linear_work_caps_and_preservation(monkeypatch):
    from benches import x05_reconcile as m
    from random import Random
    operations = []
    class CountedSet(set):
        def __contains__(self, key):
            operations.append(1)
            return super().__contains__(key)
    monkeypatch.setattr(m, 'set', CountedSet, raising=False)
    for size in (10, 100, 1000):
        strings = [dict(string_id=f's{i:04}', optimizers=1) for i in range(size)]
        devices = [dict(inverter_id=f'I{i:04}', grid_id='g') for i in range(size)]
        owners = [dict(string_id=f's{i:04}', inverter_id=f'I{i:04}') for i in range(size)]
        totals = {f'I{i:04}':2 for i in range(size)}
        evidence = dict(saved_strings=strings, saved_inverters=devices, saved_assignments=owners,
            inverter_loads={metric:dict(totals) for metric in ('strings', 'optimizers', 'panels', 'kw_dc')},
            oracle_values=dict(strings=2*size, optimizers=2*size, panels=2*size, kw_dc=2*size))
        grid = dict(panel_counts=[dict(string_id=r['string_id'], panels=1) for r in strings],
                    dc_power=[dict(string_id=r['string_id'], kw_dc='1') for r in strings])
        preserved = deepcopy((grid, evidence))
        operations.clear()
        baseline = m._seven_step_inverter_loads(grid, evidence)
        assert len(operations) == 12*size
        for check in baseline['metrics'].values():
            assert check['comparison']['mismatch_count'] == size
            assert len(check['comparison']['examples']) == min(size, MAX_STRING_EXAMPLES)
        for seed in range(2):
            changed = deepcopy(evidence)
            for field in ('saved_strings', 'saved_inverters', 'saved_assignments'):
                Random(seed).shuffle(changed[field])
            assert m._seven_step_inverter_loads(grid, changed) == baseline
        assert (grid, evidence) == preserved


@pytest.mark.parametrize('mutation', ['tiny', 'oracle', 'missing'])
def test_inverter_load_transport_direct_json_and_cli(tmp_path, mutation):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = connection_envelope()
    evidence = envelope['assurance']['evidence']
    if mutation == 'tiny':
        envelope['grid']['dc_power'][0]['kw_dc'] = '4.'+'0'*76+'1'
        envelope['grid']['dc_power'][1]['kw_dc'] = '5.'+'5'+'9'*75+'9'
    elif mutation == 'oracle':
        evidence['inverter_loads']['optimizers']['INV-A'] = 6
    else:
        evidence['inverter_loads']['panels'] = None
    source = envelope['assurance']
    assurance = dict(evidence=evidence, frozen_manifest=source['manifest'], stage_bindings=source['bindings'],
        supplied_inputs={k:bytes.fromhex(v) for k,v in source['inputs_hex'].items()},
        expected_identity=source['identity'], stage_identities=source['stage_identities'])
    direct = m.reconcile_seven_steps(envelope['import'], envelope['grid'], assurance)
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(reconcile_seven_steps_json(json.dumps(envelope))))
    expected, records = bound_seven_fixture_records()
    source['manifest']['fixture_id'] = evidence['fixture_id'] = expected[0]
    source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    aggregate = m.aggregate_bound_seven_steps(expected, records)
    assert all(aggregate['fixtures'][k]['verdict'] == 'PASS' for k in expected[1:])
    run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
        input=serialize_result(dict(expected_ids=expected, fixtures=records)).encode('ascii'),
        capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
    assert run.returncode == (4 if mutation == 'missing' else 1) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(aggregate))


@pytest.mark.parametrize('field', ['panel_counts', 'dc_power'])
def test_inverter_load_composition_duplicate_metric_rows(field):
    envelope = connection_envelope()
    envelope['grid'][field].append(deepcopy(envelope['grid'][field][0]))
    result = reconcile_seven_steps_json(json.dumps(envelope))
    metric = 'panels' if field == 'panel_counts' else 'kw_dc'
    assert result['inverter_loads']['metrics'][metric]['comparison']['verdict'] == 'UNREADABLE'
    assert result['inverter_loads']['metrics']['strings']['comparison']['verdict'] == 'PASS'


@pytest.mark.parametrize('metric', ['strings', 'optimizers', 'panels', 'kw_dc'])
@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -float('inf'), 10**90])
def test_inverter_load_raw_numeric_rejection(metric, bad):
    from benches import x05_reconcile as m
    envelope = connection_envelope()
    envelope['assurance']['evidence']['inverter_loads'][metric]['INV-A'] = bad
    direct = m._seven_step_envelope(envelope)
    assert direct['verdict'] == 'UNREADABLE'
    assert direct['inverter_loads']['metrics']['strings' if metric != 'strings' else 'optimizers']['comparison']['verdict'] == 'PASS'
    assert reconcile_seven_steps_json(json.dumps(envelope))['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('metric', ['panels', 'kw_dc'])
def test_independent_string_metrics_equal_total_swaps_identity_and_schema(metric):
    from benches import x05_reconcile as m
    compare = m.compare_string_panel_counts if metric == 'panels' else m.compare_string_dc_power
    values = [10, 14] if metric == 'panels' else ['0.'+'0'*76+'1', '0.'+'0'*76+'2']
    oracle = [dict(string_id=k, **{metric:v}) for k,v in zip(['A','B'],values)]
    saved = [dict(string_id=k, **{metric:v}) for k,v in zip(['A','B'],values[::-1])]
    preserved = deepcopy((oracle, saved))
    result = compare(oracle, saved)
    assert result['verdict'] == 'FAIL' and result['counts']['mismatched'] == 2
    assert result == compare(oracle[::-1], saved[::-1]) and (oracle, saved) == preserved
    assert compare(oracle, oracle)['verdict'] == 'PASS'
    assert compare(oracle, saved[:1])['counts']['missing'] == 1
    assert compare(oracle, [dict(saved[0], string_id='foreign'), saved[1]])['counts']['foreign'] == 1
    for rows in (None, [], {}, oracle+oracle[:1], [dict(string_id='', **{metric:1})],
                 [dict(string_id='x'*129, **{metric:1})], [dict(string_id='A', **{metric:True})],
                 [dict(string_id='A', **{metric:float('nan')})]):
        assert compare(rows, saved)['verdict'] == 'UNREADABLE'
    many = [dict(string_id=f's{i:03}', **{metric:1}) for i in range(60)]
    changed = [dict(row, **{metric:2}) for row in many]
    result = compare(many, changed)
    assert result['counts']['mismatched'] == 60 and len(result['mismatches']) == MAX_STRING_EXAMPLES


def test_exhaustive_inverter_snapshot_loads_against_integer_fraction_models():
    from itertools import product
    from fractions import Fraction
    from benches import x05_reconcile as m
    for owners in product(['INV-A','INV-B'], repeat=2):
        for values in product([1,2], repeat=2):
            envelope = connection_envelope()
            grid, evidence = envelope['grid'], envelope['assurance']['evidence']
            for stage in ('saved','before','reopened'):
                connections = [dict(string_id=k,inverter_id=v) for k,v in zip(['A','B'], owners if stage != 'reopened' else owners[::-1])]
                evidence[stage+'_assignments'] = connections
                strings = [dict(string_id=k,optimizers=v) for k,v in zip(['A','B'],values)]
                if stage == 'saved': evidence['saved_strings'] = strings
                else: evidence[stage+'_rows']['strings'] = strings
                prefix = '' if stage == 'saved' else stage+'_'
                grid[prefix+'panel_counts'] = [dict(string_id=k,panels=3*v) for k,v in zip(['A','B'],values)]
                grid[prefix+'dc_power'] = [dict(string_id=k,kw_dc=f'0.{v}') for k,v in zip(['A','B'],values)]
            preserved = deepcopy((grid,evidence))
            result = m._seven_step_inverter_load_reopen(grid,evidence)
            for metric in ('strings','optimizers','panels','kw_dc'):
                weights = [1,1] if metric == 'strings' else [3*v for v in values] if metric == 'panels' else [Fraction(v,10) for v in values] if metric == 'kw_dc' else list(values)
                old = {key:sum(v for owner,v in zip(owners,weights) if owner==key) for key in ('INV-A','INV-B')}
                new = {key:sum(v for owner,v in zip(owners[::-1],weights) if owner==key) for key in ('INV-A','INV-B')}
                check = result['metrics'][metric]
                assert check['saved_binding']['verdict'] == 'PASS'
                assert check['comparison']['counts']['changed'] == sum(old[k]!=new[k] for k in old)
                for row in check['comparison']['examples']:
                    assert Fraction(row['delta']) == new[row['inverter_id']]-old[row['inverter_id']]
            assert (grid,evidence) == preserved


def test_concurrent_composed_inverter_dc_contexts_preserve_callers():
    from concurrent.futures import ThreadPoolExecutor
    from decimal import localcontext, Inexact
    from threading import Event
    from benches import x05_reconcile as m
    ready, release = [Event(),Event()], Event()
    envelope = connection_envelope()
    envelope['grid']['reopened_dc_power'][0]['kw_dc'] = '4.'+'0'*76+'1'
    envelope['grid']['reopened_dc_power'][1]['kw_dc'] = '5.5'+'9'*76
    def run(index):
        with localcontext() as caller:
            caller.prec,caller.Emin,caller.Emax = (2,-1,1) if index == 0 else (45,-999,999)
            for signal in caller.traps: caller.traps[signal] = bool(index)
            caller.clear_flags()
            caller.flags[Inexact] = bool(index)
            snapshot = lambda: (caller.prec,caller.Emin,caller.Emax,dict(caller.traps),dict(caller.flags))
            preserved = snapshot()
            ready[index].set()
            assert release.wait(10)
            result = m._seven_step_envelope(deepcopy(envelope))
            assert result['inverter_load_reopen']['metrics']['kw_dc']['comparison']['counts']['changed'] == 2
            encoded = serialize_result(result)
            assert snapshot() == preserved
            return encoded
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run,i) for i in range(2)]
        try:
            assert all(event.wait(10) for event in ready)
        finally:
            release.set()
        assert futures[0].result(timeout=20) == futures[1].result(timeout=20)


def test_composed_load_topology_hash_seeds_and_row_permutations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from random import Random
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    for index, record in enumerate(records):
        envelope = connection_envelope()
        source = envelope['assurance']
        source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = record['fixture_id']
        source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
        record['envelope'] = envelope
        evidence = source['evidence']
        evidence['inverter_loads']['optimizers']['INV-A'] = 6
        evidence['reopened_assignments'][0]['inverter_id'] = 'INV-B'
        if index == 2: evidence['inverter_loads']['kw_dc'] = None
    preserved = deepcopy(records)
    baseline = json.loads(serialize_result(m.aggregate_bound_seven_steps(expected,records)))
    outputs = []
    for seed in ('0','73'):
        changed = deepcopy(records)
        Random(int(seed)).shuffle(changed)
        for record in changed:
            evidence = record['envelope']['assurance']['evidence']
            for field in ('saved_assignments','oracle_assignments','before_assignments','reopened_assignments','saved_inverters','before_inverters','reopened_inverters','saved_strings'):
                Random(int(seed)).shuffle(evidence[field])
            for metric, values in evidence['inverter_loads'].items():
                if values is not None: evidence['inverter_loads'][metric] = dict(reversed(list(values.items())))
            for field in ('panel_counts','before_panel_counts','reopened_panel_counts','dc_power','before_dc_power','reopened_dc_power'):
                Random(int(seed)).shuffle(record['envelope']['grid'][field])
        run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
            input=serialize_result(dict(expected_ids=expected[::-1],fixtures=changed)).encode('ascii'),
            capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows','PYTHONHASHSEED':seed})
        assert run.returncode == 4 and not run.stderr
        assert json.loads(run.stdout) == baseline
        outputs.append(run.stdout)
    assert outputs[0] == outputs[1] and records == preserved


def per_string_redistribution_envelope():
    envelope = seven_step_envelope()
    grid = envelope['grid']
    for field in ('panel_counts','before_panel_counts','reopened_panel_counts'):
        grid[field][0]['panels'],grid[field][1]['panels'] = 14,10
    for field in ('dc_power','before_dc_power','reopened_dc_power'):
        grid[field][0]['kw_dc'],grid[field][1]['kw_dc'] = '5.6','4'
    return envelope


def test_per_string_metric_proof_limit_replay():
    from benches import x05_reconcile as m
    envelope = per_string_redistribution_envelope()
    assert m._seven_step_envelope(envelope)['verdict'] == 'FAIL'
    panels = [dict(string_id='A',panels=10),dict(string_id='B',panels=14)]
    power = [dict(string_id='A',kw_dc='4'),dict(string_id='B',kw_dc='5.6')]
    assert m.compare_string_panel_counts(panels,envelope['grid']['panel_counts'])['counts']['mismatched'] == 2
    assert m.compare_string_dc_power(power,envelope['grid']['dc_power'])['counts']['mismatched'] == 2


def test_composed_independent_string_metrics_swaps_and_missing_oracle():
    envelope = per_string_redistribution_envelope()
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result['verdict'] == 'FAIL'
    for metric in ('panels','kw_dc'):
        assert result['string_metrics']['metrics'][metric]['comparison']['counts']['mismatched'] == 2
        assert result['string_metrics']['metrics'][metric]['oracle_consistency']['verdict'] == 'PASS'
    assert result['inverter_loads']['verdict'] == result['inverter_load_reopen']['verdict'] == 'PASS'
    envelope['assurance']['evidence']['oracle_panels'] = None
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing['verdict'] == 'UNREADABLE'
    assert missing['string_metrics']['metrics']['kw_dc'] == result['string_metrics']['metrics']['kw_dc']
    envelope['assurance']['evidence']['oracle_dc_power'] = None
    assert reconcile_seven_steps_json(json.dumps(envelope))['string_metrics']['metrics']['kw_dc']['comparison']['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('metric,rows,total', [('panels',[dict(string_id='A',panels=2),dict(string_id='B',panels=3)],5),
    ('kw_dc',[dict(string_id='A',kw_dc='0.1'),dict(string_id='B',kw_dc='0.2')],'0.3')])
def test_independent_string_metric_oracle_sum_and_explicit_coverage(metric,rows,total):
    from benches import x05_reconcile as m
    original = deepcopy(rows)
    assert m.compare_string_metric_oracle_total(['A','B'],rows,{metric:total},metric)['verdict'] == 'PASS'
    assert m.compare_string_metric_oracle_total(['A','B'],rows,{metric:6},metric)['verdict'] == 'FAIL'
    for ids in (None, [], ['A','A'], ['A'], ['A','C']):
        assert m.compare_string_metric_oracle_total(ids,rows,{metric:total},metric)['verdict'] == 'UNREADABLE'
    assert rows == original


def test_mutually_matching_per_string_values_cannot_hide_report_contradiction():
    envelope = seven_step_envelope()
    envelope['assurance']['evidence']['oracle_panels'][0]['panels'] = 11
    envelope['grid']['panel_counts'][0]['panels'] = 11
    result = reconcile_seven_steps_json(json.dumps(envelope))
    check = result['string_metrics']['metrics']['panels']
    assert check['comparison']['verdict'] == 'PASS'
    assert check['oracle_consistency']['verdict'] == 'FAIL' and result['verdict'] == 'FAIL'


@pytest.mark.parametrize('metric', ['panels','kw_dc'])
@pytest.mark.parametrize('bad', [None, {}, 'private-canary', [None], [{}],
    [dict(string_id='',panels=1,kw_dc='1')], [dict(string_id='x'*129,panels=1,kw_dc='1')],
    [dict(string_id='A',panels=True,kw_dc=True)], [dict(string_id='A',panels=-1,kw_dc='NaN')]])
def test_independent_string_metric_schema_sanitized_unchanged(metric,bad):
    from benches import x05_reconcile as m
    compare = m.compare_string_panel_counts if metric == 'panels' else m.compare_string_dc_power
    original = deepcopy(bad)
    result = compare(bad,[dict(string_id='A',**{metric:1})])
    assert result['verdict'] == 'UNREADABLE'
    assert len(serialize_result(result)) < 200 and 'private-canary' not in serialize_result(result) and 'x'*129 not in serialize_result(result)
    assert bad == original
    rows = [dict(string_id='A',**{metric:1})]*2
    assert compare(rows,rows)['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('metric', ['panels','kw_dc'])
@pytest.mark.parametrize('missing', [False,True])
def test_independent_string_metric_strict_transport_preserves_exact_diagnostics(metric,missing):
    from benches import x05_reconcile as m
    values = [10,14] if metric == 'panels' else ['0.'+'0'*76+'1','0.'+'0'*76+'2']
    oracle = None if missing else [dict(string_id=k,**{metric:v}) for k,v in zip(['A','B'],values)]
    saved = [dict(string_id=k,**{metric:v}) for k,v in zip(['A','B'],values[::-1])]
    compare = m.compare_string_panel_counts if metric == 'panels' else m.compare_string_dc_power
    original = deepcopy((oracle,saved))
    decoded = m.decode_evidence_json(json.dumps(dict(oracle=oracle,saved=saved)))
    assert decoded['verdict'] == 'PASS'
    assert compare(oracle,saved) == compare(decoded['evidence']['oracle'],decoded['evidence']['saved'])
    assert (oracle,saved) == original


def test_composed_independent_string_dc_tiny_swaps_preserve_context():
    from decimal import localcontext, Inexact, Rounded
    envelope = seven_step_envelope()
    envelope['grid']['dc_power'][0]['kw_dc'] = '4.'+'0'*76+'1'
    envelope['grid']['dc_power'][1]['kw_dc'] = '5.5'+'9'*76
    with localcontext() as caller:
        caller.prec,caller.Emin,caller.Emax = 2,-1,1
        caller.traps[Inexact] = caller.traps[Rounded] = True
        flags = dict(caller.flags)
        result = reconcile_seven_steps_json(json.dumps(envelope))
        check = result['string_metrics']['metrics']['kw_dc']
        assert check['comparison']['verdict'] == 'FAIL' and check['comparison']['counts']['mismatched'] == 2
        assert check['oracle_consistency']['verdict'] == 'PASS'
        assert result['string_metrics']['metrics']['panels']['comparison']['verdict'] == 'PASS'
        assert dict(caller.flags) == flags and caller.prec == 2


@pytest.mark.parametrize('metric,field', [('panels','panel_counts'),('kw_dc','dc_power')])
def test_composed_per_string_snapshot_swaps_hidden_by_inverter_totals(metric,field):
    from decimal import localcontext, Inexact, Rounded
    envelope = seven_step_envelope()
    rows = envelope['grid']['reopened_'+field]
    if metric == 'panels':
        rows[0][metric],rows[1][metric] = rows[1][metric],rows[0][metric]
    else:
        rows[0][metric],rows[1][metric] = '4.'+'0'*76+'1','5.5'+'9'*76
    with localcontext() as caller:
        caller.prec = 2
        caller.traps[Inexact] = caller.traps[Rounded] = True
        flags = dict(caller.flags)
        result = reconcile_seven_steps_json(json.dumps(envelope))
        check = result['string_metrics']['metrics'][metric]['snapshots']
        assert check['comparison']['counts']['mismatched'] == 2
        assert check['saved_binding']['verdict'] == 'PASS'
        assert result['inverter_load_reopen']['verdict'] == 'PASS'
        assert dict(caller.flags) == flags and caller.prec == 2
    envelope['grid']['before_'+field] = deepcopy(rows)
    result = reconcile_seven_steps_json(json.dumps(envelope))
    check = result['string_metrics']['metrics'][metric]['snapshots']
    assert check['comparison']['verdict'] == 'PASS' and check['saved_binding']['counts']['mismatched'] == 2
    envelope['grid']['reopened_'+field] = None
    assert reconcile_seven_steps_json(json.dumps(envelope))['string_metrics']['metrics'][metric]['snapshots']['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('metric', ['panels','kw_dc'])
def test_exhaustive_independent_per_string_metric_reference(metric):
    from itertools import product
    from fractions import Fraction
    from benches import x05_reconcile as m
    compare = m.compare_string_panel_counts if metric == 'panels' else m.compare_string_dc_power
    for old in product([1,2,3],repeat=2):
        for new in product([1,2,3],repeat=2):
            oracle = [dict(string_id=k,**{metric:v if metric=='panels' else f'0.{v}'}) for k,v in zip(['A','B'],old)]
            saved = [dict(string_id=k,**{metric:v if metric=='panels' else f'0.{v}'}) for k,v in zip(['A','B'],new)]
            original = deepcopy((oracle,saved))
            result = compare(oracle,saved)
            changed = [i for i in range(2) if old[i] != new[i]]
            assert result['counts'] == dict(missing=0,foreign=0,mismatched=len(changed))
            assert result['verdict'] == ('FAIL' if changed else 'PASS')
            for row in result['mismatches']:
                index = ['A','B'].index(row['string_id'])
                scale = 1 if metric == 'panels' else 10
                assert Fraction(row['saved'])-Fraction(row['oracle']) == Fraction(new[index]-old[index],scale)
            assert (oracle,saved) == original


@pytest.mark.parametrize('mode',['seven','bound'])
@pytest.mark.parametrize('mutation',['swaps','missing','sum'])
def test_composed_string_metrics_cli_differential(tmp_path,mode,mutation):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = per_string_redistribution_envelope() if mutation == 'swaps' else seven_step_envelope()
    evidence = envelope['assurance']['evidence']
    if mutation == 'missing': evidence['oracle_dc_power'] = None
    if mutation == 'sum':
        evidence['oracle_panels'][0]['panels'] = envelope['grid']['panel_counts'][0]['panels'] = 11
    source = envelope['assurance']
    assurance = dict(evidence=evidence, frozen_manifest=source['manifest'], stage_bindings=source['bindings'],
        supplied_inputs={k:bytes.fromhex(v) for k,v in source['inputs_hex'].items()},
        expected_identity=source['identity'], stage_identities=source['stage_identities'])
    direct = m.reconcile_seven_steps(envelope['import'],envelope['grid'],assurance)
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(reconcile_seven_steps_json(json.dumps(envelope))))
    if mode == 'seven':
        payload,flag = envelope,'--seven-step'
    else:
        expected,records = bound_seven_fixture_records()
        source['manifest']['fixture_id'] = evidence['fixture_id'] = expected[0]
        source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
        records[0]['envelope'] = envelope
        direct = m.aggregate_bound_seven_steps(expected,records)
        assert all(direct['fixtures'][k]['verdict'] == 'PASS' for k in expected[1:])
        payload,flag = dict(expected_ids=expected,fixtures=records),'--seven-step-bound-aggregate'
    run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),flag],
        input=serialize_result(payload).encode('ascii'),capture_output=True,cwd=tmp_path,timeout=20,
        env={'SystemRoot':'C:\\Windows'})
    assert run.returncode == (4 if mutation=='missing' else 1) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(direct))


def test_string_metric_index_work_cap_permutation_and_preservation(monkeypatch):
    from random import Random
    from benches import x05_reconcile as m
    operations = []
    class CountedDict(dict):
        def __getitem__(self,key):
            operations.append(1)
            return super().__getitem__(key)
    original_index = m._index
    def indexed(*args,**kwargs):
        values,error = original_index(*args,**kwargs)
        return (CountedDict(values) if values is not None else None),error
    monkeypatch.setattr(m,'_index',indexed)
    for metric,compare in [('panels',m.compare_string_panel_counts),('kw_dc',m.compare_string_dc_power)]:
        for size in (10,100,1000):
            oracle = [dict(string_id=f's{i:04}',**{metric:1}) for i in range(size)]
            saved = [dict(row,**{metric:2}) for row in oracle]
            preserved = deepcopy((oracle,saved))
            operations.clear()
            baseline = compare(oracle,saved)
            assert len(operations) == 2*size+2*min(size,MAX_STRING_EXAMPLES)
            assert baseline['counts']['mismatched'] == size and len(baseline['mismatches']) == min(size,MAX_STRING_EXAMPLES)
            for seed in range(2):
                left,right = deepcopy((oracle,saved))
                Random(seed).shuffle(left)
                Random(seed+1).shuffle(right)
                assert compare(left,right) == baseline
            assert (oracle,saved) == preserved


def test_cross_topology_balanced_connections_missing_owners_and_bounded_examples():
    from benches import x05_reconcile as m
    grids,strings,devices = ['east','west'],['A','B'],['I1','I2']
    string_owners = [dict(string_id='A',grid_id='east'),dict(string_id='B',grid_id='west')]
    inverter_owners = [dict(inverter_id='I1',grid_id='east'),dict(inverter_id='I2',grid_id='west')]
    crossed = [dict(string_id='A',inverter_id='I2'),dict(string_id='B',inverter_id='I1')]
    inputs = (grids,strings,devices,string_owners,inverter_owners,crossed)
    preserved = deepcopy(inputs)
    result = m.validate_cross_topology(*inputs)
    assert result['verdict'] == 'FAIL' and result['mismatch_count'] == 2
    assert result == m.validate_cross_topology(*(list(reversed(v)) for v in inputs))
    assert inputs == preserved
    correct = [dict(string_id='A',inverter_id='I1'),dict(string_id='B',inverter_id='I2')]
    assert m.validate_cross_topology(grids,strings,devices,string_owners,inverter_owners,correct)['verdict'] == 'PASS'
    for position in (0,1,2,3,4,5):
        changed = list(inputs)
        changed[position] = None
        assert m.validate_cross_topology(*changed)['verdict'] == 'UNREADABLE'
    assert m.validate_cross_topology(grids,strings,devices,string_owners[:1],inverter_owners,crossed)['verdict'] == 'FAIL'
    many = [f's{i:03}' for i in range(70)]
    owners = [dict(string_id=k,grid_id='east') for k in many]
    connections = [dict(string_id=k,inverter_id='I2') for k in many]
    capped = m.validate_cross_topology(grids,many,devices,owners,inverter_owners,connections)
    assert capped['mismatch_count'] == 70 and len(capped['examples']) == MAX_STRING_EXAMPLES
    assert capped == m.validate_cross_topology(grids,many[::-1],devices,owners[::-1],inverter_owners,connections[::-1])


@pytest.mark.parametrize('field', ['before_panel_counts','reopened_panel_counts','before_dc_power','reopened_dc_power'])
@pytest.mark.parametrize('bad', [None, {}, 'private-canary', [None], [{}],
    [dict(string_id='',panels=1,kw_dc='1')], [dict(string_id='x'*129,panels=1,kw_dc='1')],
    [dict(string_id='A',panels=True,kw_dc=True)], [dict(string_id='A',panels=-1,kw_dc='NaN')],
    [dict(string_id='A',panels=1,kw_dc='1')]*2])
def test_per_string_snapshot_schema_mutations_preserve_siblings_and_inputs(field,bad):
    from benches import x05_reconcile as m
    envelope = seven_step_envelope()
    envelope['grid'][field] = bad
    preserved = deepcopy(envelope)
    result = m._seven_step_envelope(envelope)
    metric = 'panels' if 'panel' in field else 'kw_dc'
    sibling = 'kw_dc' if metric == 'panels' else 'panels'
    assert result['string_metrics']['metrics'][metric]['snapshots']['verdict'] == 'UNREADABLE'
    assert result['string_metrics']['metrics'][sibling]['snapshots']['verdict'] == 'PASS'
    assert 'private-canary' not in serialize_result(result) and 'x'*129 not in serialize_result(result)
    assert envelope == preserved


def test_concurrent_per_string_snapshot_decimal_contexts_preserved():
    from concurrent.futures import ThreadPoolExecutor
    from decimal import localcontext, Inexact
    from threading import Event
    from benches import x05_reconcile as m
    ready,release = [Event(),Event()],Event()
    envelope = seven_step_envelope()
    envelope['grid']['reopened_dc_power'][0]['kw_dc'] = '4.'+'0'*76+'1'
    envelope['grid']['reopened_dc_power'][1]['kw_dc'] = '5.5'+'9'*76
    def run(index):
        with localcontext() as caller:
            caller.prec,caller.Emin,caller.Emax = (2,-1,1) if index == 0 else (39,-999,999)
            for signal in caller.traps: caller.traps[signal] = bool(index)
            caller.clear_flags()
            caller.flags[Inexact] = bool(index)
            state = lambda: (caller.prec,caller.Emin,caller.Emax,dict(caller.traps),dict(caller.flags))
            preserved = state()
            ready[index].set()
            assert release.wait(10)
            result = m._seven_step_envelope(deepcopy(envelope))
            assert result['string_metrics']['metrics']['kw_dc']['snapshots']['comparison']['counts']['mismatched'] == 2
            text = serialize_result(result)
            assert state() == preserved
            return text
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run,i) for i in range(2)]
        try: assert all(event.wait(10) for event in ready)
        finally: release.set()
        assert futures[0].result(timeout=20) == futures[1].result(timeout=20)


def test_string_snapshot_hash_seeds_and_all_phase_row_permutations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from random import Random
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    for index,record in enumerate(records):
        grid = record['envelope']['grid']
        evidence = record['envelope']['assurance']['evidence']
        grid['reopened_panel_counts'][0]['panels'],grid['reopened_panel_counts'][1]['panels'] = 14,10
        grid['reopened_dc_power'][0]['kw_dc'],grid['reopened_dc_power'][1]['kw_dc'] = '5.6','4'
        if index == 2: evidence['oracle_dc_power'] = None
    preserved = deepcopy(records)
    baseline = json.loads(serialize_result(m.aggregate_bound_seven_steps(expected,records)))
    outputs = []
    for seed in ('0','73'):
        changed = deepcopy(records)
        Random(int(seed)).shuffle(changed)
        for record in changed:
            envelope = record['envelope']
            for field in ('panel_counts','before_panel_counts','reopened_panel_counts','dc_power','before_dc_power','reopened_dc_power'):
                Random(int(seed)).shuffle(envelope['grid'][field])
            for field in ('oracle_panels','oracle_dc_power'):
                rows = envelope['assurance']['evidence'][field]
                if rows is not None: Random(int(seed)).shuffle(rows)
        run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
            input=serialize_result(dict(expected_ids=expected[::-1],fixtures=changed)).encode('ascii'),
            capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows','PYTHONHASHSEED':seed})
        assert run.returncode == 4 and not run.stderr and json.loads(run.stdout) == baseline
        outputs.append(run.stdout)
    assert outputs[0] == outputs[1] and records == preserved


def test_string_metric_comparison_pairs_once_per_unique_fixture(monkeypatch):
    from collections import Counter
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    baseline = m.aggregate_bound_seven_steps(expected,records)
    calls = Counter()
    def counted(name,original):
        def run(left,right):
            calls[(name,id(left),id(right))] += 1
            return original(left,right)
        return run
    for name in ('compare_string_panel_counts','compare_string_dc_power'):
        monkeypatch.setattr(m,name,counted(name,getattr(m,name)))
    assert m.aggregate_bound_seven_steps(expected,records) == baseline
    assert len(calls) == 18 and set(calls.values()) == {1}
    calls.clear()
    result = m.aggregate_bound_seven_steps(expected,[dict(fixture_id=expected[0],envelope=None)]+records)
    assert len(calls) == 12 and set(calls.values()) == {1}
    assert expected[0] not in result['fixtures']
    for key in expected[1:]: assert result['fixtures'][key] == baseline['fixtures'][key]
