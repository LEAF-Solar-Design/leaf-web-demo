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
    return dict(oracle_inverter_ids=["INV-A"], oracle_inverter_owners=[dict(inverter_id="INV-A",grid_id="g")],
                oracle_string_owners=[dict(string_id=k,grid_id="g") for k in ("A","B")], oracle_panels=[dict(string_id="A", panels=10),dict(string_id="B", panels=14)],
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
                         before_string_assignments=[dict(grid_id="g", string_id=row["string_id"]) for row in ORACLE],
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
                         before_string_assignments=[dict(grid_id="g", string_id=row["string_id"]) for row in ORACLE],
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
    align_two_grid_inverters(envelope)
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


def align_two_grid_inverters(envelope):
    grid, evidence = envelope["grid"], envelope["assurance"]["evidence"]
    grid["before_string_assignments"] = deepcopy(grid["string_assignments"])
    grid["inverter_totals"] = dict(east=1, west=1)
    for field in ("saved_inverters", "before_inverters", "reopened_inverters"):
        evidence[field] = [dict(inverter_id="INV-A", grid_id="east"), dict(inverter_id="INV-B", grid_id="west")]
    for field in ("oracle_assignments", "saved_assignments", "before_assignments", "reopened_assignments"):
        evidence[field][1]["inverter_id"] = "INV-B"
    for field in ("saved_values", "oracle_values"):
        evidence[field]["inverters"] = 2
    for rows in (evidence["view_rows"], evidence["before_rows"]["table_rows"], evidence["reopened_rows"]["table_rows"]):
        next(row for row in rows if row["metric"] == "inverters").update(studio=2, oracle=2)
    evidence["before"] = canonical_snapshot(evidence["before_rows"]["strings"], evidence["before_rows"]["table_rows"])
    evidence["reopened"] = canonical_snapshot(evidence["reopened_rows"]["strings"], evidence["reopened_rows"]["table_rows"])
    evidence["csv_text"] = csv_fixture(evidence["view_rows"])
    optimizer_counts = [row["optimizers"] for row in evidence["saved_strings"]]
    evidence["inverter_loads"] = dict(strings={"INV-A":1,"INV-B":1},
        optimizers=dict(zip(("INV-A","INV-B"),optimizer_counts)), panels={"INV-A":10,"INV-B":14}, kw_dc={"INV-A":"4","INV-B":"5.6"})
    evidence["oracle_inverter_ids"] = ["INV-A","INV-B"]
    evidence["oracle_inverter_owners"] = [dict(inverter_id="INV-A",grid_id="east"),dict(inverter_id="INV-B",grid_id="west")]
    evidence["oracle_string_owners"] = [dict(string_id="A",grid_id="east"),dict(string_id="B",grid_id="west")]


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
    align_two_grid_inverters(envelope)
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
    assert calls == dict(dict.fromkeys(names, 3), compare_grid_optimizer_totals=6)  # saved and oracle pairs
    calls.update(dict.fromkeys(names, 0))
    ambiguous = [dict(fixture_id=expected[0], envelope=None)] + records
    result = aggregate_bound_seven_steps(expected, ambiguous)
    assert result["verdict"] == "UNREADABLE" and expected[0] not in result["fixtures"]
    assert calls == dict(dict.fromkeys(names, 2), compare_grid_optimizer_totals=4)
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
        string_assignments=deepcopy(owners), before_string_assignments=deepcopy(owners), reopened_string_assignments=deepcopy(owners),
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
        evidence[field].append(dict(string_id="C", inverter_id="INV-B"))
    evidence["inverter_loads"]["strings"] = {"INV-A":1,"INV-B":2}
    evidence["inverter_loads"]["optimizers"] = {"INV-A":5,"INV-B":7}
    evidence["oracle_panels"], evidence["oracle_dc_power"] = deepcopy(panels), deepcopy(power)
    evidence["oracle_string_owners"].append(dict(string_id="C",grid_id="west"))
    return envelope


def test_composed_grid_string_totals_swaps_missing_counts_and_preserved_diagnostics():
    envelope = three_string_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline["verdict"] == "PASS"
    envelope["grid"]["string_totals"] = dict(east=2, west=1)
    swapped = reconcile_seven_steps_json(json.dumps(envelope))
    assert swapped["verdict"] == "FAIL" and swapped["grid_strings"]["comparison"]["mismatch_count"] == 2
    assert swapped["grid_strings"]["oracle_consistency"]["verdict"] == "PASS"
    for key in baseline.keys() - {"verdict", "grid_strings", "oracle_loads"}:
        assert swapped[key] == baseline[key]
    assert swapped["oracle_loads"]["metrics"]["strings"]["grid_comparison"]["mismatch_count"] == 2
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
    envelope["grid"]["inverter_totals"] = dict(east=0, west=2)
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
        envelope["assurance"]["evidence"][field][0]["inverter_id"] = "INV-X"
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
    evidence["oracle_inverter_ids"] = ["INV-A","INV-B"]
    evidence["oracle_inverter_owners"] = [dict(inverter_id=k,grid_id="g") for k in ("INV-A","INV-B")]
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
    for key in baseline.keys() - {'verdict', 'inverter_loads', 'oracle_loads'}:
        assert result[key] == baseline[key]
    assert result['oracle_loads']['metrics']['optimizers']['inverter_comparison']['mismatch_count'] == 2
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


def test_composed_cross_topology_balanced_connections_and_own_snapshots():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline['verdict'] == 'PASS'
    evidence = envelope['assurance']['evidence']
    for field in ('oracle_assignments','saved_assignments','before_assignments','reopened_assignments'):
        evidence[field][0]['inverter_id'],evidence[field][1]['inverter_id'] = 'INV-B','INV-A'
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result['verdict'] == 'FAIL'
    assert all(check['mismatch_count'] == 2 for check in result['cross_topology']['checks'].values())
    assert result['assignments']['verdict'] == result['assignment_reopen']['verdict'] == 'PASS'
    del envelope['grid']['before_string_assignments']
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing['verdict'] == 'UNREADABLE'
    assert missing['cross_topology']['checks']['saved'] == result['cross_topology']['checks']['saved']


def test_snapshot_cross_topology_joint_owner_and_connection_changes():
    envelope = equal_count_ownership_envelope()
    rows = envelope['assurance']['evidence']['reopened_inverters']
    rows[0]['grid_id'],rows[1]['grid_id'] = 'west','east'
    owners = envelope['grid']['reopened_string_assignments']
    owners[0]['grid_id'],owners[1]['grid_id'] = 'west','east'
    assignments = envelope['assurance']['evidence']['reopened_assignments']
    assignments[0]['inverter_id'],assignments[1]['inverter_id'] = 'INV-B','INV-A'
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result['cross_topology']['checks']['saved']['verdict'] == 'PASS'
    assert result['cross_topology']['checks']['before']['verdict'] == 'PASS'
    assert result['cross_topology']['checks']['reopened']['mismatch_count'] == 2


def test_exhaustive_cross_topology_independent_equality_model():
    from itertools import product
    from benches import x05_reconcile as m
    for string_grids in product(['e','w'],repeat=2):
        for inverter_grids in product(['e','w'],repeat=2):
            for connections in product(['I1','I2'],repeat=2):
                strings = [dict(string_id=k,grid_id=v) for k,v in zip(['A','B'],string_grids)]
                inverters = [dict(inverter_id=k,grid_id=v) for k,v in zip(['I1','I2'],inverter_grids)]
                rows = [dict(string_id=k,inverter_id=v) for k,v in zip(['A','B'],connections)]
                inputs = (['e','w'],['A','B'],['I1','I2'],strings,inverters,rows)
                original = deepcopy(inputs)
                expected = sum(string_grids[i] != inverter_grids[['I1','I2'].index(connections[i])] for i in range(2))
                result = m.validate_cross_topology(*inputs)
                assert result['mismatch_count'] == expected
                assert result['verdict'] == ('FAIL' if expected else 'PASS')
                assert inputs == original


@pytest.mark.parametrize('position',range(6))
@pytest.mark.parametrize('bad',[None,{},'private-canary',[],[None],[''],['x'*129]])
def test_cross_topology_schema_sanitized_and_inputs_preserved(position,bad):
    from benches import x05_reconcile as m
    values = [['g'],['A'],['I'],[dict(string_id='A',grid_id='g')],
        [dict(inverter_id='I',grid_id='g')],[dict(string_id='A',inverter_id='I')]]
    values[position] = bad
    original = deepcopy(values)
    result = m.validate_cross_topology(*values)
    assert result['verdict'] != 'PASS'
    text = serialize_result(result)
    assert len(text)<2500 and 'private-canary' not in text and 'x'*129 not in text
    assert values == original


@pytest.mark.parametrize('position',[3,4,5])
def test_cross_topology_duplicate_owners_and_foreign_endpoints(position):
    from benches import x05_reconcile as m
    values = [['g'],['A'],['I'],[dict(string_id='A',grid_id='g')],
        [dict(inverter_id='I',grid_id='g')],[dict(string_id='A',inverter_id='I')]]
    values[position].append(deepcopy(values[position][0]))
    assert m.validate_cross_topology(*values)['verdict'] == 'FAIL'
    values[position].pop()
    if position == 3: values[position][0]['grid_id'] = 'foreign'
    elif position == 4: values[position][0]['inverter_id'] = 'foreign'
    else: values[position][0]['inverter_id'] = 'foreign'
    assert m.validate_cross_topology(*values)['verdict'] == 'FAIL'


def test_cross_topology_linear_membership_work_caps_and_permutations(monkeypatch):
    from random import Random
    from benches import x05_reconcile as m
    work = []
    class CountedSet(set):
        def __contains__(self,key):
            work.append(1)
            return super().__contains__(key)
    monkeypatch.setattr(m,'set',CountedSet,raising=False)
    for size in (10,100,1000):
        strings,devices = [f's{i:04}' for i in range(size)],[f'I{i:04}' for i in range(size)]
        owners = [dict(string_id=k,grid_id='east') for k in strings]
        inverters = [dict(inverter_id=k,grid_id='west') for k in devices]
        rows = [dict(string_id=k,inverter_id=v) for k,v in zip(strings,devices)]
        values = (['east','west'],strings,devices,owners,inverters,rows)
        preserved = deepcopy(values)
        work.clear()
        baseline = m.validate_cross_topology(*values)
        assert len(work) == 9*size and baseline['mismatch_count'] == size
        assert len(baseline['examples']) == min(size,MAX_STRING_EXAMPLES)
        for seed in range(2):
            changed = deepcopy(values)
            for sequence in changed: Random(seed).shuffle(sequence)
            assert m.validate_cross_topology(*changed) == baseline
        assert values == preserved


def cross_topology_phase_envelope(phase, missing=False):
    envelope = equal_count_ownership_envelope()
    if missing:
        field = 'string_assignments' if phase == 'saved' else phase+'_string_assignments'
        envelope['grid'][field] = None
    else:
        rows = envelope['assurance']['evidence'][phase+'_assignments']
        rows[0]['inverter_id'],rows[1]['inverter_id'] = 'INV-B','INV-A'
    return envelope


@pytest.mark.parametrize('phase',['saved','before','reopened'])
@pytest.mark.parametrize('missing',[False,True])
def test_composed_cross_topology_transport_differential(tmp_path,phase,missing):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = cross_topology_phase_envelope(phase,missing)
    source = envelope['assurance']
    assurance = dict(evidence=source['evidence'],frozen_manifest=source['manifest'],stage_bindings=source['bindings'],
        supplied_inputs={k:bytes.fromhex(v) for k,v in source['inputs_hex'].items()},
        expected_identity=source['identity'],stage_identities=source['stage_identities'])
    direct = m.reconcile_seven_steps(envelope['import'],envelope['grid'],assurance)
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(reconcile_seven_steps_json(json.dumps(envelope))))
    expected,records = bound_seven_fixture_records()
    source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = expected[0]
    source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    aggregate = m.aggregate_bound_seven_steps(expected,records)
    assert all(aggregate['fixtures'][k]['verdict'] == 'PASS' for k in expected[1:])
    original = deepcopy(records)
    run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
        input=serialize_result(dict(expected_ids=expected,fixtures=records)).encode('ascii'),
        capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
    assert run.returncode == (4 if missing else 1) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(aggregate)) and records == original


def test_composed_cross_topology_hash_seed_phase_permutations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from random import Random
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    for index,record in enumerate(records):
        envelope = cross_topology_phase_envelope(('saved','before','reopened')[index])
        source = envelope['assurance']
        source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = record['fixture_id']
        source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
        record['envelope'] = envelope
    baseline = json.loads(serialize_result(m.aggregate_bound_seven_steps(expected,records)))
    original = deepcopy(records)
    outputs = []
    for seed in ('0','73'):
        changed = deepcopy(records)
        Random(int(seed)).shuffle(changed)
        for record in changed:
            grid,evidence = record['envelope']['grid'],record['envelope']['assurance']['evidence']
            for field in ('string_assignments','before_string_assignments','reopened_string_assignments'):
                Random(int(seed)).shuffle(grid[field])
            for stage in ('saved','before','reopened'):
                for suffix in ('_inverters','_assignments'):
                    Random(int(seed)).shuffle(evidence[stage+suffix])
        run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
            input=serialize_result(dict(expected_ids=expected[::-1],fixtures=changed)).encode('ascii'),
            capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows','PYTHONHASHSEED':seed})
        assert run.returncode == 1 and not run.stderr and json.loads(run.stdout) == baseline
        outputs.append(run.stdout)
    assert outputs[0] == outputs[1] and records == original


def test_cross_topology_once_per_phase_unique_fixture(monkeypatch):
    from collections import Counter
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    baseline = m.aggregate_bound_seven_steps(expected,records)
    original = m.validate_cross_topology
    calls = Counter()
    def counted(*args):
        calls[(id(args[3]),id(args[4]),id(args[5]))] += 1
        return original(*args)
    monkeypatch.setattr(m,'validate_cross_topology',counted)
    assert m.aggregate_bound_seven_steps(expected,records) == baseline
    assert len(calls) == 12 and set(calls.values()) == {1}  # three snapshots plus independent oracle per fixture
    calls.clear()
    result = m.aggregate_bound_seven_steps(expected,[dict(fixture_id=expected[0],envelope=None)]+records)
    assert len(calls) == 8 and set(calls.values()) == {1} and expected[0] not in result['fixtures']
    for key in expected[1:]:
        assert result['fixtures'][key] == baseline['fixtures'][key]


def test_cross_topology_failure_isolation_all_phases_and_unchanged_inputs():
    from benches import x05_reconcile as m
    envelope = cross_topology_phase_envelope('saved')
    rows = envelope['assurance']['evidence']['reopened_assignments']
    rows[0]['inverter_id'],rows[1]['inverter_id'] = 'INV-B','INV-A'
    envelope['grid']['before_string_assignments'] = None
    original = deepcopy(envelope)
    result = m._seven_step_envelope(envelope)
    checks = result['cross_topology']['checks']
    assert result['verdict'] == 'UNREADABLE'
    assert checks['saved']['mismatch_count'] == checks['reopened']['mismatch_count'] == 2
    assert checks['before']['verdict'] == 'UNREADABLE'
    assert result['string_metrics']['verdict'] == 'PASS' and envelope == original


@pytest.mark.parametrize('broken',[False,True])
def test_cross_topology_bijective_relabeling_all_phases(broken):
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    if broken:
        for stage in ('saved','before','reopened'):
            envelope['assurance']['evidence'][stage+'_assignments'][0]['inverter_id'] = 'INV-B'
    baseline = m._seven_step_cross_topology(envelope['grid'],envelope['assurance']['evidence'])
    names = {'east':'grid-two','west':'grid-one','A':'string-z','B':'string-y','INV-A':'device-nine','INV-B':'device-eight'}
    def rename(value):
        if type(value) is str:
            return names.get(value,value)
        if type(value) is list:
            return [rename(item) for item in value]
        if type(value) is dict:
            return {names.get(key,key):rename(item) for key,item in value.items()}
        return value
    original = deepcopy(envelope)
    renamed = rename(envelope)
    result = m._seven_step_cross_topology(renamed['grid'],renamed['assurance']['evidence'])
    assert result['verdict'] == baseline['verdict'] == ('FAIL' if broken else 'PASS')
    for stage in ('saved','before','reopened'):
        assert result['checks'][stage]['mismatch_count'] == baseline['checks'][stage]['mismatch_count'] == int(broken)
    assert envelope == original
    renamed['grid']['before_string_assignments'][0]['grid_id'] = 'grid-one'
    changed = m._seven_step_cross_topology(renamed['grid'],renamed['assurance']['evidence'])
    assert changed['checks']['before']['verdict'] == ('PASS' if broken else 'FAIL')
    assert changed['verdict'] == 'FAIL'


@pytest.mark.parametrize('label',['string','inverter'])
def test_independent_grid_owners_balanced_swaps_missing_and_caps(label):
    from benches import x05_reconcile as m
    compare = m.compare_string_grid_owners if label == 'string' else m.compare_inverter_grid_owners
    key = label+'_id'
    expected = [{key:'A','grid_id':'east'},{key:'B','grid_id':'west'}]
    saved = [{key:'A','grid_id':'west'},{key:'B','grid_id':'east'}]
    original = deepcopy((expected,saved))
    result = compare(['east','west'],['A','B'],expected,saved)
    assert result['verdict'] == 'FAIL' and result['mismatch_count'] == 2
    assert result == compare(['west','east'],['B','A'],expected[::-1],saved[::-1])
    assert compare(['east','west'],['A','B'],expected,expected)['verdict'] == 'PASS'
    for bad in (None,{},[],[{}],expected+expected[:1],[{key:'x'*129,'grid_id':'east'}]):
        result = compare(['east','west'],['A','B'],bad,saved)
        assert result['verdict'] != 'PASS'
        assert 'x'*129 not in serialize_result(result)
    assert (expected,saved) == original
    expected = [{key:f'id{i:03}','grid_id':'east'} for i in range(70)]
    saved = [dict(row,grid_id='west') for row in expected]
    ids = [row[key] for row in expected]
    result = compare(['east','west'],ids,expected,saved)
    assert result['mismatch_count'] == 70 and len(result['examples']) == MAX_STRING_EXAMPLES
    assert result == compare(['west','east'],ids[::-1],expected[::-1],saved[::-1])


def test_independent_inverter_ids_substituted_unconnected_devices_and_caps():
    from benches import x05_reconcile as m
    oracle,saved = ['connected','empty-old'],['connected','empty-new']
    rows = [dict(string_id='A',inverter_id='connected')]
    assert m.compare_inverter_string_totals({'connected':1,'empty-new':0},['A'],rows)['verdict'] == 'PASS'
    original = deepcopy((oracle,saved))
    result = m.compare_inverter_id_sets(oracle,saved)
    assert result == dict(verdict='FAIL',reason='inverter_ids_mismatch',counts=dict(missing=1,foreign=1),
        missing_ids=['empty-old'],foreign_ids=['empty-new'])
    assert m.compare_inverter_id_sets(oracle,oracle[::-1])['verdict'] == 'PASS'
    for bad in (None,{},[],[''],[True],['x'*129],['A','A']):
        assert m.compare_inverter_id_sets(bad,saved)['verdict'] == 'UNREADABLE'
        assert m.compare_inverter_id_sets(oracle,bad)['verdict'] == 'UNREADABLE'
    assert (oracle,saved) == original
    left,right = [f'old{i:03}' for i in range(70)],[f'new{i:03}' for i in range(70)]
    result = m.compare_inverter_id_sets(left,right)
    assert result['counts'] == dict(missing=70,foreign=70)
    assert len(result['missing_ids']) == len(result['foreign_ids']) == MAX_STRING_EXAMPLES
    assert result == m.compare_inverter_id_sets(left[::-1],right[::-1])


def test_cross_topology_disjoint_unions_add_mismatches_and_clean_components_do_not_mask():
    from itertools import product
    from benches import x05_reconcile as m
    def component(prefix,broken):
        grids,strings,devices = [prefix+'e',prefix+'w'],[prefix+'A',prefix+'B'],[prefix+'I1',prefix+'I2']
        owners = [dict(string_id=k,grid_id=v) for k,v in zip(strings,grids)]
        inverters = [dict(inverter_id=k,grid_id=v) for k,v in zip(devices,grids)]
        rows = [dict(string_id=k,inverter_id=v) for k,v in zip(strings,devices[::-1] if broken else devices)]
        return grids,strings,devices,owners,inverters,rows
    for flags in product([False,True],repeat=3):
        parts = [component(str(index),flag) for index,flag in enumerate(flags)]
        union = tuple(sum((part[index] for part in parts),[]) for index in range(6))
        original = deepcopy(union)
        expected = sum(m.validate_cross_topology(*part)['mismatch_count'] for part in parts)
        result = m.validate_cross_topology(*union)
        assert result['mismatch_count'] == expected == 2*sum(flags)
        assert result['verdict'] == ('FAIL' if expected else 'PASS')
        clean = component('clean',False)
        augmented = tuple(union[index]+clean[index] for index in range(6))
        assert m.validate_cross_topology(*augmented)['mismatch_count'] == expected
        assert union == original


def test_bound_aggregate_shared_owner_connection_containers_are_unchanged_and_isolated():
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    owners = deepcopy(records[0]['envelope']['grid']['string_assignments'])
    connections = deepcopy(records[0]['envelope']['assurance']['evidence']['saved_assignments'])
    inverters = deepcopy(records[0]['envelope']['assurance']['evidence']['saved_inverters'])
    for record in records:
        grid,evidence = record['envelope']['grid'],record['envelope']['assurance']['evidence']
        for field in ('string_assignments','before_string_assignments','reopened_string_assignments'):
            grid[field] = owners
        for stage in ('saved','before','reopened'):
            evidence[stage+'_assignments'] = connections
            evidence[stage+'_inverters'] = inverters
    baseline = m.aggregate_bound_seven_steps(expected,records)
    assert baseline['verdict'] == 'PASS'
    records[0]['envelope']['import']['normal_user'] = False
    original = deepcopy((records,owners,connections,inverters))
    result = m.aggregate_bound_seven_steps(expected,records)
    assert result['fixtures'][expected[0]]['verdict'] == 'FAIL'
    for key in expected[1:]:
        assert result['fixtures'][key] == baseline['fixtures'][key]
    assert (records,owners,connections,inverters) == original
    for record in records:
        grid,evidence = record['envelope']['grid'],record['envelope']['assurance']['evidence']
        for field in ('string_assignments','before_string_assignments','reopened_string_assignments'):
            assert grid[field] is owners
        for stage in ('saved','before','reopened'):
            assert evidence[stage+'_assignments'] is connections
            assert evidence[stage+'_inverters'] is inverters


def test_composed_independent_owner_balanced_swaps_and_absent_oracle():
    envelope = equal_count_ownership_envelope()
    baseline = reconcile_seven_steps_json(json.dumps(envelope))
    assert baseline['verdict'] == 'PASS'
    evidence = envelope['assurance']['evidence']
    for field in ('string_assignments','before_string_assignments','reopened_string_assignments'):
        rows = envelope['grid'][field]
        rows[0]['grid_id'],rows[1]['grid_id'] = 'west','east'
    for field in ('saved_inverters','before_inverters','reopened_inverters'):
        evidence[field][0]['grid_id'],evidence[field][1]['grid_id'] = 'west','east'
    result = reconcile_seven_steps_json(json.dumps(envelope))
    assert result['cross_topology']['verdict'] == 'PASS'
    checks = result['independent_owners']['checks']
    assert checks['strings']['mismatch_count'] == checks['inverters']['mismatch_count'] == 2
    assert checks['inverter_ids']['verdict'] == 'PASS' and result['verdict'] == 'FAIL'
    evidence['oracle_string_owners'] = None
    missing = reconcile_seven_steps_json(json.dumps(envelope))
    assert missing['verdict'] == 'UNREADABLE'
    assert missing['independent_owners']['checks']['inverters'] == checks['inverters']


def test_composed_independent_ids_reject_substituted_unconnected_inverter():
    envelope = connection_envelope()
    evidence = envelope['assurance']['evidence']
    evidence['oracle_inverter_ids'].append('empty-old')
    evidence['oracle_inverter_owners'].append(dict(inverter_id='empty-old',grid_id='g'))
    for field in ('saved_inverters','before_inverters','reopened_inverters'):
        evidence[field].append(dict(inverter_id='empty-new',grid_id='g'))
    result = reconcile_seven_steps_json(json.dumps(envelope))
    check = result['independent_owners']['checks']['inverter_ids']
    assert check['verdict'] == 'FAIL'
    assert check['missing_ids'] == ['empty-old'] and check['foreign_ids'] == ['empty-new']


@pytest.mark.parametrize('label',['string','inverter'])
def test_exhaustive_independent_owner_models(label):
    from itertools import product
    from benches import x05_reconcile as m
    compare = m.compare_string_grid_owners if label=='string' else m.compare_inverter_grid_owners
    key = label+'_id'
    for old in product(['e','w'],repeat=3):
        for new in product(['e','w'],repeat=3):
            oracle = [{key:k,'grid_id':v} for k,v in zip(['A','B','C'],old)]
            saved = [{key:k,'grid_id':v} for k,v in zip(['A','B','C'],new)]
            original = deepcopy((oracle,saved))
            result = compare(['e','w'],['A','B','C'],oracle,saved)
            count = sum(a!=b for a,b in zip(old,new))
            assert result['mismatch_count'] == count and result['verdict'] == ('FAIL' if count else 'PASS')
            assert (oracle,saved) == original
    candidates = [('A',),('B',),('C',),('A','B'),('A','C'),('B','C'),('A','B','C')]
    for left in candidates:
        for right in candidates:
            result = m.compare_inverter_id_sets(list(left),list(right))
            assert result['counts'] == dict(missing=len(set(left)-set(right)),foreign=len(set(right)-set(left)))


@pytest.mark.parametrize('label',['string','inverter'])
@pytest.mark.parametrize('side',['oracle','saved'])
@pytest.mark.parametrize('kind',['none','container','duplicate','missing','foreign','wide'])
def test_independent_owner_schema_mutations_bounded_sanitized_unchanged(label,side,kind):
    from benches import x05_reconcile as m
    compare = m.compare_string_grid_owners if label=='string' else m.compare_inverter_grid_owners
    key = label+'_id'
    good = [{key:'A','grid_id':'e'},{key:'B','grid_id':'w'}]
    bad = deepcopy(good)
    if kind=='none': bad = None
    elif kind=='container': bad = 'private-canary'
    elif kind=='duplicate': bad.append(deepcopy(bad[0]))
    elif kind=='missing': bad.pop()
    elif kind=='foreign': bad[0]['grid_id'] = 'foreign'
    else: bad[0][key] = 'x'*129
    left,right = (bad,good) if side=='oracle' else (good,bad)
    original = deepcopy((left,right))
    result = compare(['e','w'],['A','B'],left,right)
    assert result['verdict'] != 'PASS'
    text = serialize_result(result)
    assert len(text)<2500 and 'private-canary' not in text and 'x'*129 not in text
    assert (left,right) == original


def test_independent_owner_linear_work_caps_and_stable_permutations(monkeypatch):
    from random import Random
    from benches import x05_reconcile as m
    work = []
    class CountedSet(set):
        def __contains__(self,key):
            work.append(1)
            return super().__contains__(key)
    monkeypatch.setattr(m,'set',CountedSet,raising=False)
    for label,compare in [('string',m.compare_string_grid_owners),('inverter',m.compare_inverter_grid_owners)]:
        key = label+'_id'
        for size in (10,100,1000):
            ids = [f'id{i:04}' for i in range(size)]
            oracle = [{key:k,'grid_id':'east'} for k in ids]
            saved = [{key:k,'grid_id':'west'} for k in ids]
            original = deepcopy((oracle,saved))
            work.clear()
            baseline = compare(['east','west'],ids,oracle,saved)
            assert len(work) == 6*size and baseline['mismatch_count'] == size
            assert len(baseline['examples']) == min(size,MAX_STRING_EXAMPLES)
            for seed in range(2):
                left,right = deepcopy((oracle,saved))
                Random(seed).shuffle(left)
                Random(seed+1).shuffle(right)
                assert compare(['west','east'],ids[::-1],left,right) == baseline
            assert (oracle,saved) == original
            substituted = [f'new{i:04}' for i in range(size)]
            sets = m.compare_inverter_id_sets(ids,substituted)
            assert sets['counts'] == dict(missing=size,foreign=size)
            assert len(sets['missing_ids']) == len(sets['foreign_ids']) == min(size,MAX_STRING_EXAMPLES)
            assert sets == m.compare_inverter_id_sets(ids[::-1],substituted[::-1])


def test_independent_inverter_id_set_linear_index_builds(monkeypatch):
    from benches import x05_reconcile as m
    sizes = []
    class CountedSet(set):
        def __init__(self, values=()):
            sizes.append(len(values))
            super().__init__(values)
    monkeypatch.setattr(m,'set',CountedSet,raising=False)
    for size in (10,100,1000):
        oracle = [f'old{i:04}' for i in range(size)]
        saved = [f'new{i:04}' for i in range(size)]
        sizes.clear()
        result = m.compare_inverter_id_sets(oracle,saved)
        assert sum(sizes) == 4*size
        assert result['counts'] == dict(missing=size,foreign=size)


def independent_owner_conflict_envelope(kind):
    envelope = equal_count_ownership_envelope()
    evidence = envelope['assurance']['evidence']
    if kind == 'owners':
        for field in ('oracle_string_owners','oracle_inverter_owners'):
            evidence[field][0]['grid_id'],evidence[field][1]['grid_id'] = 'west','east'
    elif kind == 'ids':
        evidence['oracle_inverter_ids'][1] = 'replacement'
        evidence['oracle_inverter_owners'][1]['inverter_id'] = 'replacement'
    else:
        evidence['oracle_inverter_owners'] = None
    return envelope


@pytest.mark.parametrize('kind',['owners','ids','missing'])
def test_composed_independent_owner_transport_differential(tmp_path,kind):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = independent_owner_conflict_envelope(kind)
    source = envelope['assurance']
    assurance = dict(evidence=source['evidence'],frozen_manifest=source['manifest'],stage_bindings=source['bindings'],
        supplied_inputs={k:bytes.fromhex(v) for k,v in source['inputs_hex'].items()},
        expected_identity=source['identity'],stage_identities=source['stage_identities'])
    direct = m.reconcile_seven_steps(envelope['import'],envelope['grid'],assurance)
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(reconcile_seven_steps_json(json.dumps(envelope))))
    expected,records = bound_seven_fixture_records()
    source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = expected[0]
    source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    aggregate = m.aggregate_bound_seven_steps(expected,records)
    assert all(aggregate['fixtures'][key]['verdict'] == 'PASS' for key in expected[1:])
    run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
        input=serialize_result(dict(expected_ids=expected,fixtures=records)).encode('ascii'),
        capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
    assert run.returncode == (4 if kind in ('missing','ids') else 1) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(aggregate))

    if kind == 'ids':
        check = aggregate['fixtures'][expected[0]]['reconciliation']
        assert check['independent_owners']['checks']['inverter_ids']['verdict'] == 'FAIL'
        assert check['oracle_loads']['metrics']['optimizers']['inverter_report']['verdict'] == 'UNREADABLE'


def test_independent_owners_all_fixture_hash_seeds_and_permutations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from random import Random
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    for record,kind in zip(records,['owners','ids','missing']):
        envelope = independent_owner_conflict_envelope(kind)
        source = envelope['assurance']
        source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = record['fixture_id']
        source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
        record['envelope'] = envelope
    original = deepcopy(records)
    baseline = json.loads(serialize_result(m.aggregate_bound_seven_steps(expected,records)))
    outputs = []
    for seed in ('0','73'):
        changed = deepcopy(records)
        Random(int(seed)).shuffle(changed)
        for record in changed:
            grid,evidence = record['envelope']['grid'],record['envelope']['assurance']['evidence']
            for field in ('string_assignments','before_string_assignments','reopened_string_assignments'):
                Random(int(seed)).shuffle(grid[field])
            for field in ('oracle_string_owners','oracle_inverter_owners','oracle_inverter_ids','saved_inverters'):
                if evidence[field] is not None:
                    Random(int(seed)).shuffle(evidence[field])
        run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
            input=serialize_result(dict(expected_ids=expected[::-1],fixtures=changed)).encode('ascii'),
            capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows','PYTHONHASHSEED':seed})
        assert run.returncode == 4 and not run.stderr and json.loads(run.stdout) == baseline
        outputs.append(run.stdout)
    assert outputs[0] == outputs[1] and records == original


def test_independent_owner_comparators_once_per_unique_fixture(monkeypatch):
    from collections import Counter
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    baseline = m.aggregate_bound_seven_steps(expected,records)
    names = ('compare_string_grid_owners','compare_inverter_grid_owners','compare_inverter_id_sets')
    calls = Counter()
    def counted(name,original):
        def run(*args):
            calls[name] += 1
            return original(*args)
        return run
    for name in names:
        monkeypatch.setattr(m,name,counted(name,getattr(m,name)))
    assert m.aggregate_bound_seven_steps(expected,records) == baseline
    assert calls == dict.fromkeys(names,3)
    calls.clear()
    result = m.aggregate_bound_seven_steps(expected,[dict(fixture_id=expected[0],envelope=None)]+records)
    assert calls == dict.fromkeys(names,2) and expected[0] not in result['fixtures']
    for key in expected[1:]:
        assert result['fixtures'][key] == baseline['fixtures'][key]


def test_composed_independent_owner_simultaneous_failure_isolation():
    from benches import x05_reconcile as m
    envelope = independent_owner_conflict_envelope('owners')
    evidence = envelope['assurance']['evidence']
    evidence['oracle_inverter_owners'] = None
    evidence['oracle_inverter_ids'][1] = 'replacement'
    original = deepcopy(envelope)
    result = m._seven_step_envelope(envelope)
    checks = result['independent_owners']['checks']
    assert result['verdict'] == 'UNREADABLE'
    assert checks['strings']['verdict'] == 'FAIL' and checks['strings']['mismatch_count'] == 2
    assert checks['inverters']['verdict'] == 'UNREADABLE'
    assert checks['inverter_ids']['counts'] == dict(missing=1,foreign=1)
    assert result['cross_topology']['verdict'] == 'PASS' and envelope == original


@pytest.mark.parametrize('broken',[False,True])
def test_independent_owner_bijective_relabeling_preserves_counts(broken):
    from benches import x05_reconcile as m
    envelope = independent_owner_conflict_envelope('owners') if broken else equal_count_ownership_envelope()
    baseline = m._seven_step_independent_owners(envelope['grid'],envelope['assurance']['evidence'])
    names = {'east':'grid-z','west':'grid-y','A':'string-z','B':'string-y','INV-A':'device-z','INV-B':'device-y'}
    def rename(value):
        if type(value) is str:
            return names.get(value,value)
        if type(value) is list:
            return [rename(item) for item in value]
        if type(value) is dict:
            return {names.get(key,key):rename(item) for key,item in value.items()}
        return value
    original = deepcopy(envelope)
    renamed = rename(envelope)
    result = m._seven_step_independent_owners(renamed['grid'],renamed['assurance']['evidence'])
    assert result['verdict'] == baseline['verdict'] == ('FAIL' if broken else 'PASS')
    for label in ('strings','inverters'):
        assert result['checks'][label]['mismatch_count'] == baseline['checks'][label]['mismatch_count']
    rows = renamed['assurance']['evidence']['oracle_string_owners']
    rows[0]['grid_id'] = 'grid-z' if rows[0]['grid_id']=='grid-y' else 'grid-y'
    changed = m._seven_step_independent_owners(renamed['grid'],renamed['assurance']['evidence'])
    assert changed['checks']['strings']['verdict'] == changed['verdict'] == 'FAIL'
    assert envelope == original


def test_independent_oracle_topology_contradictions_and_named_uncertainty():
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    baseline = m._seven_step_envelope(envelope)
    assert baseline['verdict'] == 'PASS'
    evidence = envelope['assurance']['evidence']
    evidence['oracle_assignments'][0]['inverter_id'],evidence['oracle_assignments'][1]['inverter_id'] = 'INV-B','INV-A'
    result = m._seven_step_envelope(envelope)
    check = result['independent_owners']['checks']['oracle_topology']
    assert check['verdict'] == 'FAIL' and check['mismatch_count'] == 2
    assert result['cross_topology']['verdict'] == 'PASS'
    evidence['oracle_inverter_owners'] = None
    original = deepcopy(envelope)
    missing = m._seven_step_envelope(envelope)
    assert missing['verdict'] == 'UNREADABLE'
    assert missing['independent_owners']['checks']['oracle_topology']['reason'] == 'cross_topology_evidence_invalid'
    assert envelope == original


def test_independent_owner_disjoint_union_counts_and_clean_component_extension():
    from itertools import product
    from benches import x05_reconcile as m
    for flags in product([False,True],repeat=3):
        grids,strings,devices,old_strings,new_strings,old_devices,new_devices = [],[],[],[],[],[],[]
        for index,broken in enumerate(flags):
            east,west = f'{index}e',f'{index}w'
            grids.extend([east,west])
            for suffix,owner in (('A',east),('B',west)):
                string,device = f'{index}s{suffix}',f'{index}i{suffix}'
                changed = west if owner==east else east
                strings.append(string)
                devices.append(device)
                old_strings.append(dict(string_id=string,grid_id=owner))
                new_strings.append(dict(string_id=string,grid_id=changed if broken else owner))
                old_devices.append(dict(inverter_id=device,grid_id=owner))
                new_devices.append(dict(inverter_id=device,grid_id=changed if broken else owner))
        assert m.compare_string_grid_owners(grids,strings,old_strings,new_strings)['mismatch_count'] == 2*sum(flags)
        assert m.compare_inverter_grid_owners(grids,devices,old_devices,new_devices)['mismatch_count'] == 2*sum(flags)
        expected_ids = devices+['old-orphan']
        saved_ids = devices+['new-orphan']
        assert m.compare_inverter_id_sets(expected_ids,saved_ids)['counts'] == dict(missing=1,foreign=1)
        assert m.compare_inverter_id_sets(expected_ids+['clean'],saved_ids+['clean'])['counts'] == dict(missing=1,foreign=1)
        grids.append('clean')
        strings.append('clean-string')
        old_strings.append(dict(string_id='clean-string',grid_id='clean'))
        new_strings.append(dict(string_id='clean-string',grid_id='clean'))
        assert m.compare_string_grid_owners(grids,strings,old_strings,new_strings)['mismatch_count'] == 2*sum(flags)


@pytest.mark.parametrize('label',['string','inverter'])
def test_independent_owner_symmetry_counts_and_capped_identity_examples(label):
    from benches import x05_reconcile as m
    compare = m.compare_string_grid_owners if label=='string' else m.compare_inverter_grid_owners
    key = label+'_id'
    ids = [f'id{i:03}' for i in range(70)]
    left = [{key:identity,'grid_id':'east'} for identity in ids]
    right = [dict(row,grid_id='west') for row in left]
    original = deepcopy((left,right))
    forward = compare(['east','west'],ids,left,right)
    reverse = compare(['east','west'],ids,right,left)
    assert forward['mismatch_count'] == reverse['mismatch_count'] == 70
    assert [{key:row[key],'oracle':row['saved'],'saved':row['oracle']} for row in forward['examples']] == reverse['examples']
    orphan = [f'other{i:03}' for i in range(60)]
    a,b = m.compare_inverter_id_sets(ids,orphan),m.compare_inverter_id_sets(orphan,ids)
    assert a['missing_ids'] == b['foreign_ids'] and a['foreign_ids'] == b['missing_ids']
    assert a['counts']['missing'] == b['counts']['foreign'] == 70
    assert (left,right) == original


def test_independent_owner_namespaces_collide_without_masking_through_bound_json():
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    names = {'A':'east','B':'west','INV-A':'east','INV-B':'west'}
    def rename(value):
        if type(value) is str:
            return names.get(value,value)
        if type(value) is list:
            return [rename(item) for item in value]
        if type(value) is dict:
            return {names.get(key,key):rename(item) for key,item in value.items()}
        return value
    envelope = rename(envelope)
    source = envelope['assurance']
    evidence = source['evidence']
    for phase,hash_key in (('before_rows','before'),('reopened_rows','reopened')):
        evidence[hash_key] = canonical_snapshot(evidence[phase]['strings'],evidence[phase]['table_rows'])
    assert m._seven_step_envelope(envelope)['verdict'] == 'PASS'
    rows = evidence['oracle_string_owners']
    rows[0]['grid_id'],rows[1]['grid_id'] = 'west','east'
    direct = m._seven_step_envelope(envelope)
    assert direct['independent_owners']['checks']['strings']['mismatch_count'] == 2
    expected,records = bound_seven_fixture_records()
    source['manifest']['fixture_id'] = evidence['fixture_id'] = expected[0]
    source['bindings'] = {key:deepcopy(source['manifest']) for key in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    aggregate = m.aggregate_bound_seven_steps(expected,records)
    parsed = m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected,fixtures=records)))
    assert json.loads(serialize_result(aggregate)) == json.loads(serialize_result(parsed))
    assert all(parsed['fixtures'][key]['verdict'] == 'PASS' for key in expected[1:])


@pytest.mark.parametrize('kind',['duplicate','foreign','container'])
def test_composed_independent_owner_schema_strict_json_and_cli(tmp_path,kind):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    source = envelope['assurance']
    evidence = source['evidence']
    if kind=='duplicate': evidence['oracle_inverter_ids'] = ['INV-A','INV-A']
    elif kind=='foreign': evidence['oracle_inverter_owners'][0]['grid_id'] = 'foreign'
    else: evidence['oracle_string_owners'] = 'private-canary'
    parsed = reconcile_seven_steps_json(json.dumps(envelope))
    assert parsed['verdict'] == ('FAIL' if kind=='foreign' else 'UNREADABLE')
    assert 'private-canary' not in serialize_result(parsed)
    expected,records = bound_seven_fixture_records()
    source['manifest']['fixture_id'] = evidence['fixture_id'] = expected[0]
    source['bindings'] = {key:deepcopy(source['manifest']) for key in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    original = deepcopy(records)
    aggregate = m.aggregate_bound_seven_steps(expected,records)
    assert all(aggregate['fixtures'][key]['verdict'] == 'PASS' for key in expected[1:])
    run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
        input=serialize_result(dict(expected_ids=expected,fixtures=records)).encode('ascii'),
        capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
    assert run.returncode == (1 if kind=='foreign' else 4) and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(aggregate)) and records == original


def test_disjoint_inverter_identity_sets_add_missing_foreign_counts():
    from itertools import product
    from benches import x05_reconcile as m
    for flags in product([False,True],repeat=3):
        oracle,saved = [],[]
        for index,broken in enumerate(flags):
            oracle.extend([f'{index}-connected',f'{index}-old'])
            saved.extend([f'{index}-connected',f'{index}-new' if broken else f'{index}-old'])
        original = deepcopy((oracle,saved))
        result = m.compare_inverter_id_sets(oracle,saved)
        assert result['counts'] == dict(missing=sum(flags),foreign=sum(flags))
        assert m.compare_inverter_id_sets(oracle+['clean'],saved+['clean'])['counts'] == result['counts']
        assert (oracle,saved) == original


def test_oracle_grid_load_consistency_rejects_equal_total_contradictions():
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    grid,evidence = envelope['grid'],envelope['assurance']['evidence']
    assert m._seven_step_oracle_loads(grid,evidence)['verdict'] == 'PASS'
    grid['string_totals'] = dict(east=2,west=0)
    evidence['oracle_strings'][0]['optimizers'],evidence['oracle_strings'][1]['optimizers'] = 7,5
    evidence['oracle_panels'][0]['panels'],evidence['oracle_panels'][1]['panels'] = 14,10
    evidence['oracle_dc_power'][0]['kw_dc'],evidence['oracle_dc_power'][1]['kw_dc'] = '5.6','4'
    original = deepcopy((grid,evidence))
    result = m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict'] == 'FAIL'
    for metric in ('strings','optimizers','panels','kw_dc'):
        check = result['metrics'][metric]
        assert check['grid_comparison']['mismatch_count'] == 2
        assert check['grid_report']['verdict'] == 'PASS'
    assert (grid,evidence) == original


def test_oracle_inverter_load_consistency_exact_dc_and_missing_values():
    from decimal import localcontext,Inexact,Rounded,Decimal
    from benches import x05_reconcile as m
    envelope = connection_envelope()
    grid,evidence = envelope['grid'],envelope['assurance']['evidence']
    evidence['inverter_loads']['strings'] = {'INV-A':2,'INV-B':0}
    evidence['inverter_loads']['optimizers'] = {'INV-A':7,'INV-B':5}
    evidence['inverter_loads']['panels'] = {'INV-A':14,'INV-B':10}
    evidence['oracle_dc_power'][0]['kw_dc'] = '4.'+'0'*76+'1'
    evidence['oracle_dc_power'][1]['kw_dc'] = '5.5'+'9'*76
    with localcontext() as caller:
        caller.prec = 2
        caller.traps[Inexact] = caller.traps[Rounded] = True
        flags = dict(caller.flags)
        result = m._seven_step_oracle_loads(grid,evidence)
        assert result['verdict'] == 'FAIL'
        for metric in ('strings','optimizers','panels','kw_dc'):
            check = result['metrics'][metric]
            assert check['inverter_comparison']['mismatch_count'] == 2
            assert check['inverter_report']['verdict'] == 'PASS'
        assert result['metrics']['kw_dc']['inverter_comparison']['examples'][0]['delta'] == Decimal('1e-77')
        assert result['metrics']['kw_dc']['grid_comparison']['verdict'] == 'PASS'
        assert dict(caller.flags) == flags and caller.prec == 2
    evidence['oracle_dc_power'] = None
    missing = m._seven_step_oracle_loads(grid,evidence)
    assert missing['verdict'] == 'UNREADABLE'
    assert missing['metrics']['panels'] == result['metrics']['panels']


def test_oracle_topology_composed_transport_differential_with_saved_pass(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    source = envelope['assurance']
    rows = source['evidence']['oracle_assignments']
    rows[0]['inverter_id'],rows[1]['inverter_id'] = 'INV-B','INV-A'
    assurance = dict(evidence=source['evidence'],frozen_manifest=source['manifest'],stage_bindings=source['bindings'],
        supplied_inputs={k:bytes.fromhex(v) for k,v in source['inputs_hex'].items()},
        expected_identity=source['identity'],stage_identities=source['stage_identities'])
    direct = m.reconcile_seven_steps(envelope['import'],envelope['grid'],assurance)
    assert direct['cross_topology']['verdict'] == 'PASS'
    assert direct['independent_owners']['checks']['oracle_topology']['mismatch_count'] == 2
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(reconcile_seven_steps_json(json.dumps(envelope))))
    expected,records = bound_seven_fixture_records()
    source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = expected[0]
    source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    aggregate = m.aggregate_bound_seven_steps(expected,records)
    assert all(aggregate['fixtures'][key]['verdict'] == 'PASS' for key in expected[1:])
    run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
        input=serialize_result(dict(expected_ids=expected,fixtures=records)).encode('ascii'),
        capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
    assert run.returncode == 1 and not run.stderr
    assert json.loads(run.stdout) == json.loads(serialize_result(aggregate))


def test_oracle_topology_hash_seeds_all_oracle_map_permutations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from random import Random
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    for record in records:
        envelope = equal_count_ownership_envelope()
        source = envelope['assurance']
        source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = record['fixture_id']
        source['bindings'] = {k:deepcopy(source['manifest']) for k in PROVENANCE_STAGES}
        rows = source['evidence']['oracle_assignments']
        rows[0]['inverter_id'],rows[1]['inverter_id'] = 'INV-B','INV-A'
        record['envelope'] = envelope
    original = deepcopy(records)
    baseline = json.loads(serialize_result(m.aggregate_bound_seven_steps(expected,records)))
    outputs = []
    for seed in ('0','73'):
        changed = deepcopy(records)
        Random(int(seed)).shuffle(changed)
        for record in changed:
            evidence = record['envelope']['assurance']['evidence']
            for field in ('oracle_assignments','oracle_string_owners','oracle_inverter_owners','oracle_inverter_ids','oracle_strings'):
                Random(int(seed)).shuffle(evidence[field])
        run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],
            input=serialize_result(dict(expected_ids=expected[::-1],fixtures=changed)).encode('ascii'),
            capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows','PYTHONHASHSEED':seed})
        assert run.returncode == 1 and not run.stderr and json.loads(run.stdout) == baseline
        outputs.append(run.stdout)
    assert outputs[0] == outputs[1] and records == original


def test_oracle_topology_missing_connections_preserves_all_saved_phase_failures():
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    evidence = envelope['assurance']['evidence']
    evidence['oracle_assignments'] = None
    for stage in ('saved','before','reopened'):
        rows = evidence[stage+'_assignments']
        rows[0]['inverter_id'],rows[1]['inverter_id'] = 'INV-B','INV-A'
    original = deepcopy(envelope)
    result = m._seven_step_envelope(envelope)
    assert result['verdict'] == 'UNREADABLE'
    assert result['independent_owners']['checks']['oracle_topology']['verdict'] == 'UNREADABLE'
    assert all(check['mismatch_count'] == 2 for check in result['cross_topology']['checks'].values())
    assert envelope == original


def test_exhaustive_oracle_loads_against_independent_integer_fraction_sums():
    from fractions import Fraction
    from itertools import product
    from benches import x05_reconcile as m
    def dc(number):
        return f'0.{int(number*10)}'
    for owners in product(['g0','g1'],repeat=2):
        for connections in product(['i0','i1'],repeat=2):
            for values in product([1,2],repeat=2):
                ids = ['A','B']
                weights = dict(strings=[1,1],optimizers=list(values),panels=[3*v for v in values],kw_dc=[Fraction(v,10) for v in values])
                grid_totals = {metric:{key:sum(v for owner,v in zip(owners,counts) if owner==key) for key in ['g0','g1']} for metric,counts in weights.items()}
                device_totals = {metric:{key:sum(v for owner,v in zip(connections,counts) if owner==key) for key in ['i0','i1']} for metric,counts in weights.items()}
                grid_totals['kw_dc'] = {key:dc(value) for key,value in grid_totals['kw_dc'].items()}
                device_totals['kw_dc'] = {key:dc(value) for key,value in device_totals['kw_dc'].items()}
                fields = dict(strings='string_totals',optimizers='optimizer_totals',panels='panel_totals',kw_dc='dc_totals')
                grid = dict(expected_grid_ids=['g0','g1'],**{fields[metric]:counts for metric,counts in grid_totals.items()})
                evidence = dict(oracle_strings=[dict(string_id=k,optimizers=v) for k,v in zip(ids,values)],
                    oracle_panels=[dict(string_id=k,panels=3*v) for k,v in zip(ids,values)],
                    oracle_dc_power=[dict(string_id=k,kw_dc=dc(Fraction(v,10))) for k,v in zip(ids,values)],
                    oracle_string_owners=[dict(string_id=k,grid_id=v) for k,v in zip(ids,owners)],
                    oracle_assignments=[dict(string_id=k,inverter_id=v) for k,v in zip(ids,connections)],
                    oracle_inverter_ids=['i0','i1'],inverter_loads=device_totals,
                    oracle_values=dict(strings=2,optimizers=sum(values),panels=3*sum(values),kw_dc=dc(Fraction(sum(values),10))))
                original = deepcopy((grid,evidence))
                result = m._seven_step_oracle_loads(grid,evidence)
                assert result['verdict'] == 'PASS'
                for metric,field in fields.items():
                    for target,keys in [('grid',['g0','g1']),('inverter',['i0','i1'])]:
                        source = grid[field] if target=='grid' else evidence['inverter_loads'][metric]
                        if source[keys[0]] == source[keys[1]]:
                            continue
                        changed_grid,changed_evidence = deepcopy((grid,evidence))
                        changed = changed_grid[field] if target=='grid' else changed_evidence['inverter_loads'][metric]
                        changed[keys[0]],changed[keys[1]] = changed[keys[1]],changed[keys[0]]
                        observed = m._seven_step_oracle_loads(changed_grid,changed_evidence)
                        check = observed['metrics'][metric]
                        assert observed['verdict'] == 'FAIL' and check[target+'_comparison']['mismatch_count'] == 2
                        assert check[target+'_report']['verdict'] == 'PASS'
                        for row in check[target+'_comparison']['examples']:
                            assert Fraction(row['delta']) == Fraction(row['saved'])-Fraction(row['oracle'])
                assert (grid,evidence) == original


@pytest.mark.parametrize('field',['oracle_strings','oracle_panels','oracle_dc_power'])
@pytest.mark.parametrize('kind',['none','container','duplicate','foreign','boolean','negative','nonfinite','wide'])
def test_oracle_loads_schema_direct_bound_json_sanitized_and_readable_metrics(field,kind):
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    envelope = records[0]['envelope']
    evidence = envelope['assurance']['evidence']
    rows = evidence[field]
    metric = {'oracle_strings':'optimizers','oracle_panels':'panels','oracle_dc_power':'kw_dc'}[field]
    if kind=='none': evidence[field] = None
    elif kind=='container': evidence[field] = 'private-canary'
    elif kind=='duplicate': rows.append(deepcopy(rows[0]))
    elif kind=='foreign': rows[0]['string_id'] = 'foreign'
    elif kind=='boolean': rows[0][metric] = True
    elif kind=='negative': rows[0][metric] = -1
    elif kind=='nonfinite': rows[0][metric] = 'NaN'
    else: rows[0]['string_id'] = 'x'*129
    original = deepcopy(records)
    direct = m._seven_step_oracle_loads(envelope['grid'],evidence)
    parsed = m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected,fixtures=records)))
    observed = parsed['fixtures'][expected[0]]['reconciliation']['oracle_loads']
    assert json.loads(serialize_result(direct)) == json.loads(serialize_result(observed))
    assert direct['verdict'] != 'PASS'
    for checks in direct['metrics'].values():
        assert checks['grid_report']['verdict'] == checks['inverter_report']['verdict'] == 'PASS'
    assert all(parsed['fixtures'][key]['verdict'] == 'PASS' for key in expected[1:])
    text = serialize_result(parsed)
    assert 'private-canary' not in text and 'x'*129 not in text and records == original


def test_oracle_loads_linear_index_work_exact_caps_permutations_and_preservation(monkeypatch):
    from random import Random
    from benches import x05_reconcile as m
    work = []
    class CountedDict(dict):
        def __getitem__(self,key):
            work.append(1)
            return super().__getitem__(key)
    original_index = m._index
    def indexed(*args,**kwargs):
        values,error = original_index(*args,**kwargs)
        return (CountedDict(values) if values is not None else None),error
    monkeypatch.setattr(m,'_index',indexed)
    for size in (10,100,1000):
        ids = [f's{i:04}' for i in range(size)]
        grids,devices = [f'g{i:04}' for i in range(size)],[f'i{i:04}' for i in range(size)]
        totals = {key:2 for key in grids}
        grid = dict(expected_grid_ids=grids,string_totals=dict(totals),optimizer_totals=dict(totals),panel_totals=dict(totals),dc_totals=dict(totals))
        evidence = dict(oracle_strings=[dict(string_id=k,optimizers=1) for k in ids],
            oracle_panels=[dict(string_id=k,panels=1) for k in ids],oracle_dc_power=[dict(string_id=k,kw_dc='1') for k in ids],
            oracle_string_owners=[dict(string_id=k,grid_id=v) for k,v in zip(ids,grids)],
            oracle_assignments=[dict(string_id=k,inverter_id=v) for k,v in zip(ids,devices)],oracle_inverter_ids=devices,
            inverter_loads={metric:{key:2 for key in devices} for metric in ('strings','optimizers','panels','kw_dc')},
            oracle_values=dict(strings=2*size,optimizers=2*size,panels=2*size,kw_dc=2*size))
        original = deepcopy((grid,evidence))
        work.clear()
        baseline = m._seven_step_oracle_loads(grid,evidence)
        assert len(work) == 6*size
        for checks in baseline['metrics'].values():
            for field in ('grid_comparison','inverter_comparison'):
                assert checks[field]['mismatch_count'] == size
                assert len(checks[field]['examples']) == min(size,MAX_STRING_EXAMPLES)
        for seed in range(2):
            changed = deepcopy(evidence)
            for field in ('oracle_strings','oracle_panels','oracle_dc_power','oracle_string_owners','oracle_assignments','oracle_inverter_ids'):
                Random(seed).shuffle(changed[field])
            assert m._seven_step_oracle_loads(grid,changed) == baseline
        assert (grid,evidence) == original


def test_concurrent_oracle_loads_exact_dc_contexts_are_isolated():
    from concurrent.futures import ThreadPoolExecutor
    from decimal import localcontext,Inexact,Decimal
    from threading import Event
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    evidence = envelope['assurance']['evidence']
    evidence['oracle_dc_power'][0]['kw_dc'] = '4.'+'0'*76+'1'
    evidence['oracle_dc_power'][1]['kw_dc'] = '5.5'+'9'*76
    ready,release = [Event(),Event()],Event()
    def run(index):
        with localcontext() as caller:
            caller.prec,caller.Emin,caller.Emax = (2,-1,1) if index==0 else (43,-999,999)
            for signal in caller.traps: caller.traps[signal] = bool(index)
            caller.clear_flags()
            caller.flags[Inexact] = bool(index)
            state = lambda: (caller.prec,caller.Emin,caller.Emax,dict(caller.traps),dict(caller.flags))
            original = state()
            ready[index].set()
            assert release.wait(10)
            result = m._seven_step_oracle_loads(envelope['grid'],evidence)
            checks = result['metrics']['kw_dc']
            for target in ('grid','inverter'):
                check = checks[target+'_comparison']
                assert check['mismatch_count'] == 2 and check['examples'][0]['delta'] == Decimal('1e-77')
            encoded = serialize_result(result)
            assert state() == original
            return encoded
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run,index) for index in range(2)]
        try: assert all(event.wait(10) for event in ready)
        finally: release.set()
        assert futures[0].result(timeout=20) == futures[1].result(timeout=20)


def test_oracle_loads_bijective_relabeling_and_single_load_redistribution():
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    baseline = m._seven_step_oracle_loads(envelope['grid'],envelope['assurance']['evidence'])
    assert baseline['verdict'] == 'PASS'
    names = {'east':'g-z','west':'g-y','A':'s-z','B':'s-y','INV-A':'i-z','INV-B':'i-y'}
    def rename(value):
        if type(value) is str:
            return names.get(value,value)
        if type(value) is list:
            return [rename(item) for item in value]
        if type(value) is dict:
            return {names.get(key,key):rename(item) for key,item in value.items()}
        return value
    original = deepcopy(envelope)
    changed = rename(envelope)
    result = m._seven_step_oracle_loads(changed['grid'],changed['assurance']['evidence'])
    assert result['verdict'] == 'PASS'
    for metric in baseline['metrics']:
        for field in ('grid_comparison','inverter_comparison'):
            assert result['metrics'][metric][field]['mismatch_count'] == baseline['metrics'][metric][field]['mismatch_count'] == 0
    rows = changed['assurance']['evidence']['oracle_dc_power']
    rows[0]['kw_dc'],rows[1]['kw_dc'] = rows[1]['kw_dc'],rows[0]['kw_dc']
    result = m._seven_step_oracle_loads(changed['grid'],changed['assurance']['evidence'])
    assert result['verdict'] == 'FAIL'
    assert result['metrics']['kw_dc']['grid_comparison']['mismatch_count'] == 2
    assert envelope == original


def oracle_load_component(prefix, broken=False, scale=1):
    """Explicit disjoint synthetic material, independent of saved snapshots."""
    grids = [prefix+'g0',prefix+'g1']
    devices = [prefix+'i0',prefix+'i1']
    strings = [prefix+'s0',prefix+'s1']
    fields = dict(strings='string_totals',optimizers='optimizer_totals',panels='panel_totals',kw_dc='dc_totals')
    weights = dict(strings=[1,1],optimizers=[scale,2*scale],panels=[3*scale,6*scale],kw_dc=[f'{5*scale//4}.{(5*scale%4)*25:02d}',f'{7*scale//4}.{(7*scale%4)*25:02d}'])
    grid = dict(expected_grid_ids=grids)
    evidence = dict(oracle_inverter_ids=devices,
        oracle_strings=[dict(string_id=k,optimizers=v) for k,v in zip(strings,weights['optimizers'])],
        oracle_panels=[dict(string_id=k,panels=v) for k,v in zip(strings,weights['panels'])],
        oracle_dc_power=[dict(string_id=k,kw_dc=v) for k,v in zip(strings,weights['kw_dc'])],
        oracle_string_owners=[dict(string_id=k,grid_id=v) for k,v in zip(strings,grids)],
        oracle_assignments=[dict(string_id=k,inverter_id=v) for k,v in zip(strings,devices)],inverter_loads={},oracle_values={})
    from decimal import Decimal
    for metric,values in weights.items():
        grid[fields[metric]] = dict(zip(grids,values))
        evidence['inverter_loads'][metric] = dict(zip(devices,values))
        evidence['oracle_values'][metric] = str(sum(map(Decimal,values))) if metric=='kw_dc' else sum(values)
        if broken:
            # Strings also get an explicit equal-total contradiction, not inferred IDs.
            changed = [0,2] if metric=='strings' else list(reversed(values))
            grid[fields[metric]] = dict(zip(grids,changed))
            evidence['inverter_loads'][metric] = dict(zip(devices,changed))
    return grid,evidence


def union_oracle_load_components(*components):
    from decimal import Decimal
    grid,evidence = deepcopy(components[0])
    for other_grid,other_evidence in components[1:]:
        for field,value in other_grid.items():
            if isinstance(value,list): grid[field].extend(value)
            else: grid[field].update(value)
        for field,value in other_evidence.items():
            if isinstance(value,list): evidence[field].extend(deepcopy(value))
        for metric,counts in other_evidence['inverter_loads'].items():
            evidence['inverter_loads'][metric].update(counts)
        for metric,value in other_evidence['oracle_values'].items():
            evidence['oracle_values'][metric] = str(Decimal(evidence['oracle_values'][metric])+Decimal(value)) if metric=='kw_dc' else evidence['oracle_values'][metric]+value
    return grid,evidence


def test_oracle_loads_disjoint_union_adds_all_mismatches_without_clean_masking():
    from benches import x05_reconcile as m
    parts = [oracle_load_component('a',True),oracle_load_component('b',True),oracle_load_component('c')]
    original = deepcopy(parts)
    singles = [m._seven_step_oracle_loads(*part) for part in parts]
    for count in (2,3):
        combined = m._seven_step_oracle_loads(*union_oracle_load_components(*parts[:count]))
        assert combined['verdict'] == 'FAIL'
        for metric,checks in combined['metrics'].items():
            for target in ('grid','inverter'):
                check = checks[target+'_comparison']
                assert check['mismatch_count'] == sum(r['metrics'][metric][target+'_comparison']['mismatch_count'] for r in singles[:count]) == 4
                assert checks[target+'_report']['verdict'] == 'PASS'
    assert parts == original


@pytest.mark.parametrize('factor',[2,3,7])
def test_oracle_loads_scaling_covariance_against_fraction_model(factor):
    from fractions import Fraction
    from benches import x05_reconcile as m
    def decimal_text(value):
        value = Fraction(value)
        assert 100 % value.denominator == 0
        cents = value.numerator*(100//value.denominator)
        return f'{cents//100}.{cents%100:02d}'
    grid,evidence = oracle_load_component('scale',True)
    baseline = m._seven_step_oracle_loads(grid,evidence)
    for field,metric in [('optimizer_totals','optimizers'),('panel_totals','panels'),('dc_totals','kw_dc')]:
        for key,value in grid[field].items(): grid[field][key] = str(Fraction(value)*factor) if metric=='kw_dc' else value*factor
        for key,value in evidence['inverter_loads'][metric].items(): evidence['inverter_loads'][metric][key] = str(Fraction(value)*factor) if metric=='kw_dc' else value*factor
        rows = evidence[dict(optimizers='oracle_strings',panels='oracle_panels',kw_dc='oracle_dc_power')[metric]]
        for row in rows:
            value = Fraction(row[metric])*factor
            row[metric] = decimal_text(value) if metric=='kw_dc' else int(value)
        # Render Fraction targets as exact decimal quarter units.
        if metric=='kw_dc':
            grid[field] = {k:decimal_text(v) for k,v in grid[field].items()}
            evidence['inverter_loads'][metric] = {k:decimal_text(v) for k,v in evidence['inverter_loads'][metric].items()}
        value = Fraction(evidence['oracle_values'][metric])*factor
        evidence['oracle_values'][metric] = decimal_text(value) if metric=='kw_dc' else int(value)
    result = m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict'] == baseline['verdict'] == 'FAIL'
    for metric,checks in result['metrics'].items():
        for target in ('grid','inverter'):
            before,after = baseline['metrics'][metric][target+'_comparison'],checks[target+'_comparison']
            assert after['mismatch_count'] == before['mismatch_count'] == 2
            for old,new in zip(before['examples'],after['examples']):
                assert Fraction(new['delta']) == Fraction(old['delta'])*(1 if metric=='strings' else factor)


def test_oracle_loads_zero_extension_and_phantom_load():
    from benches import x05_reconcile as m
    grid,evidence = oracle_load_component('zero',True)
    baseline = m._seven_step_oracle_loads(grid,evidence)
    grid['expected_grid_ids'].append('empty-grid')
    evidence['oracle_inverter_ids'].append('idle-device')
    for field in ('string_totals','optimizer_totals','panel_totals','dc_totals'): grid[field]['empty-grid'] = 0
    for counts in evidence['inverter_loads'].values(): counts['idle-device'] = 0
    extended = m._seven_step_oracle_loads(grid,evidence)
    for metric,checks in baseline['metrics'].items():
        for target in ('grid','inverter'):
            assert extended['metrics'][metric][target+'_comparison']['examples'] == checks[target+'_comparison']['examples']
            assert extended['metrics'][metric][target+'_comparison']['mismatch_count'] == 2
    grid['panel_totals']['empty-grid'] = 1
    evidence['inverter_loads']['panels']['idle-device'] = 1
    result = m._seven_step_oracle_loads(grid,evidence)
    for target in ('grid','inverter'):
        assert result['metrics']['panels'][target+'_comparison']['mismatch_count'] == 3
        assert result['metrics']['panels'][target+'_report']['verdict'] == 'FAIL'


def test_oracle_loads_shared_aliases_survive_aggregate_failure():
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    shared = records[0]['envelope']['assurance']['evidence']
    fields = ('oracle_strings','oracle_panels','oracle_dc_power','oracle_string_owners','oracle_assignments')
    for record in records:
        grid,evidence = record['envelope']['grid'],record['envelope']['assurance']['evidence']
        for field in fields: evidence[field] = shared[field]
        for phase in ('saved','before','reopened'):
            evidence[phase+'_panels'] = shared['oracle_panels']
        for field in ('string_assignments','before_string_assignments','reopened_string_assignments'):
            grid[field] = shared['oracle_string_owners']
    baseline = m.aggregate_bound_seven_steps(expected,records)
    assert baseline['verdict'] == 'PASS'
    records[0]['envelope']['grid']['panel_totals'] = {'g':25}
    original = deepcopy(records)
    result = m.aggregate_bound_seven_steps(expected,records)
    assert result['fixtures'][expected[0]]['reconciliation']['oracle_loads']['verdict'] == 'FAIL'
    assert all(result['fixtures'][key] == baseline['fixtures'][key] for key in expected[1:])
    assert records == original
    for record in records:
        evidence = record['envelope']['assurance']['evidence']
        for field in fields: assert evidence[field] is shared[field]
        for phase in ('saved','before','reopened'): assert evidence[phase+'_panels'] is shared['oracle_panels']
        for field in ('string_assignments','before_string_assignments','reopened_string_assignments'):
            assert record['envelope']['grid'][field] is shared['oracle_string_owners']


def test_oracle_loads_repeated_repair_has_no_stale_state():
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    evidence = envelope['assurance']['evidence']
    initial = m.reconcile_seven_steps_json(serialize_result(envelope))
    assert initial['verdict'] == 'PASS'
    rows = evidence.pop('oracle_panels')
    missing = m.reconcile_seven_steps_json(serialize_result(envelope))
    assert missing['oracle_loads']['verdict'] == 'UNREADABLE'
    evidence['oracle_panels'] = rows
    envelope['grid']['panel_totals']['east'],envelope['grid']['panel_totals']['west'] = 14,10
    failed = m.reconcile_seven_steps_json(serialize_result(envelope))
    assert failed['oracle_loads']['verdict'] == 'FAIL'
    envelope['grid']['panel_totals']['east'],envelope['grid']['panel_totals']['west'] = 10,14
    repaired = m.reconcile_seven_steps_json(serialize_result(envelope))
    assert repaired == initial
    assert missing['oracle_loads']['verdict'] == 'UNREADABLE' and failed['oracle_loads']['verdict'] == 'FAIL'


@pytest.mark.parametrize('factor',[2,3])
def test_oracle_loads_replication_scales_all_four_extensive_counts(factor):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence = oracle_load_component('replicate',True)
    baseline = m._seven_step_oracle_loads(grid,evidence)
    for field in ('oracle_strings','oracle_panels','oracle_dc_power','oracle_string_owners','oracle_assignments'):
        evidence[field] = [dict(row,string_id=row['string_id']+f'-copy{i}') for i in range(factor) for row in evidence[field]]
    def scaled(value):
        exact = Fraction(value)*factor
        assert 100 % exact.denominator == 0
        cents = exact.numerator*(100//exact.denominator)
        return f'{cents//100}.{cents%100:02d}'
    for field in ('string_totals','optimizer_totals','panel_totals','dc_totals'):
        grid[field] = {key:scaled(value) if field=='dc_totals' else value*factor for key,value in grid[field].items()}
    for metric,counts in evidence['inverter_loads'].items():
        for key,value in counts.items():
            counts[key] = scaled(value) if metric=='kw_dc' else value*factor
    for metric,value in evidence['oracle_values'].items():
        evidence['oracle_values'][metric] = scaled(value) if metric=='kw_dc' else value*factor
    result = m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict'] == 'FAIL'
    for metric,checks in result['metrics'].items():
        for target in ('grid','inverter'):
            old,new = baseline['metrics'][metric][target+'_comparison'],checks[target+'_comparison']
            assert new['mismatch_count'] == old['mismatch_count'] == 2
            assert [Fraction(row['delta']) for row in new['examples']] == [Fraction(row['delta'])*factor for row in old['examples']]
            assert checks[target+'_report']['verdict'] == 'PASS'


def test_oracle_load_metric_comparators_once_per_unique_fixture(monkeypatch):
    from collections import Counter
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    baseline = m.aggregate_bound_seven_steps(expected,records)
    names = ('compare_grid_string_totals','compare_grid_optimizer_totals','compare_grid_panel_totals','compare_grid_dc_totals',
             'compare_inverter_string_totals','compare_inverter_optimizer_totals','compare_inverter_panel_totals','compare_inverter_dc_totals')
    calls,fixtures = Counter(),[]
    active = []
    original = m._seven_step_oracle_loads
    def load(grid,evidence):
        fixtures.append(evidence)
        active.append(True)
        try: return original(grid,evidence)
        finally: active.pop()
    def counted(name,original):
        def invoke(*args,**kwargs):
            if active: calls[name] += 1
            return original(*args,**kwargs)
        return invoke
    monkeypatch.setattr(m,'_seven_step_oracle_loads',load)
    for name in names: monkeypatch.setattr(m,name,counted(name,getattr(m,name)))
    assert m.aggregate_bound_seven_steps(expected,records) == baseline
    assert calls == dict.fromkeys(names,3) and len(fixtures)==3
    calls.clear(); fixtures.clear()
    result = m.aggregate_bound_seven_steps(expected,[dict(fixture_id=expected[0],envelope=None)]+records)
    assert calls == dict.fromkeys(names,2) and len(fixtures)==2
    assert all(item is not records[0]['envelope']['assurance']['evidence'] for item in fixtures)
    assert expected[0] not in result['fixtures'] and result['verdict']=='UNREADABLE'
    assert all(result['fixtures'][key]==baseline['fixtures'][key] for key in expected[1:])


@pytest.mark.parametrize('field,metric',[('oracle_panels','panels'),('oracle_dc_power','kw_dc')])
@pytest.mark.parametrize('bad',[None,True])
def test_oracle_load_metric_uncertainty_retains_other_mismatch_and_healthy_results(field,metric,bad):
    from benches import x05_reconcile as m
    grid,evidence = oracle_load_component('isolate')
    grid['optimizer_totals']['isolateg0'],grid['optimizer_totals']['isolateg1'] = 2,1
    baseline = m._seven_step_oracle_loads(grid,evidence)
    if bad is None: evidence.pop(field)
    else: evidence[field][0][metric] = bad
    original = deepcopy((grid,evidence))
    result = m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict']=='UNREADABLE'
    assert result['metrics'][metric]['grid_comparison']['verdict']=='UNREADABLE'
    assert result['metrics']['optimizers']==baseline['metrics']['optimizers']
    assert result['metrics']['optimizers']['grid_comparison']['mismatch_count']==2
    healthy = 'kw_dc' if metric=='panels' else 'panels'
    assert result['metrics'][healthy]==baseline['metrics'][healthy]
    assert result['metrics']['strings']==baseline['metrics']['strings']
    assert (grid,evidence)==original


def coarsen_oracle_load_grids(grid,evidence):
    """Explicit grid quotient; string IDs, device IDs and connections stay fixed."""
    from fractions import Fraction
    grid,evidence = deepcopy((grid,evidence))
    grid['expected_grid_ids'] = ['coarse']
    for row in evidence['oracle_string_owners']: row['grid_id']='coarse'
    for field in ('string_totals','optimizer_totals','panel_totals','dc_totals'):
        exact = sum(Fraction(value) for value in grid[field].values())
        if field=='dc_totals':
            assert 100 % exact.denominator==0
            cents = exact.numerator*(100//exact.denominator)
            value = f'{cents//100}.{cents%100:02d}'
        else:
            assert exact.denominator==1
            value = int(exact)
        grid[field] = {'coarse':value}
    return grid,evidence


@pytest.mark.parametrize('broken',[False,True])
def test_oracle_load_grid_coarsening_preserves_conservation_and_reveals_cancellation(broken):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence = oracle_load_component('fine',broken)
    original = deepcopy((grid,evidence))
    fine = m._seven_step_oracle_loads(grid,evidence)
    coarse_grid,coarse_evidence = coarsen_oracle_load_grids(grid,evidence)
    coarse = m._seven_step_oracle_loads(coarse_grid,coarse_evidence)
    for metric,field in dict(strings='string_totals',optimizers='optimizer_totals',panels='panel_totals',kw_dc='dc_totals').items():
        assert Fraction(coarse_grid[field]['coarse'])==sum(Fraction(value) for value in grid[field].values())
        assert fine['metrics'][metric]['grid_comparison']['mismatch_count']==(2 if broken else 0)
        assert coarse['metrics'][metric]['grid_comparison']['mismatch_count']==0
        assert coarse['metrics'][metric]['grid_report']['verdict']=='PASS'
        assert coarse['metrics'][metric]['inverter_comparison']==fine['metrics'][metric]['inverter_comparison']
    assert coarse['verdict']==('FAIL' if broken else 'PASS')
    assert (grid,evidence)==original


@pytest.mark.parametrize('broken',[False,True])
def test_oracle_load_namespaces_and_distinct_unicode_direct_bound_json(broken):
    from benches import x05_reconcile as m
    expected,records = bound_seven_fixture_records()
    envelope = equal_count_ownership_envelope()
    names = {'east':'\u00e9','A':'\u00e9','INV-A':'\u00e9','west':'e\u0301','B':'e\u0301','INV-B':'e\u0301'}
    def rename(value):
        if type(value) is str: return names.get(value,value)
        if type(value) is list: return [rename(item) for item in value]
        if type(value) is dict: return {names.get(key,key):rename(item) for key,item in value.items()}
        return value
    envelope = rename(envelope)
    # Rebind only the chosen fixture's manifest identity to its existing record.
    source = envelope['assurance']
    source['manifest']['fixture_id'] = source['evidence']['fixture_id'] = expected[0]
    source['bindings'] = {key:deepcopy(source['manifest']) for key in PROVENANCE_STAGES}
    records[0]['envelope'] = envelope
    evidence = envelope['assurance']['evidence']
    for phase,hash_key in [('before_rows','before'),('reopened_rows','reopened')]:
        evidence[hash_key] = canonical_snapshot(evidence[phase]['strings'],evidence[phase]['table_rows'])
    if broken: envelope['grid']['panel_totals']['\u00e9'],envelope['grid']['panel_totals']['e\u0301'] = 14,10
    original = deepcopy(records)
    direct = m._seven_step_oracle_loads(envelope['grid'],evidence)
    result = m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected,fixtures=records)))
    assert json.loads(serialize_result(direct))==json.loads(serialize_result(result['fixtures'][expected[0]]['reconciliation']['oracle_loads']))
    assert result['fixtures'][expected[0]]['verdict']==('FAIL' if broken else 'PASS')
    assert direct['verdict']==('FAIL' if broken else 'PASS')
    assert direct['metrics']['panels']['grid_comparison']['mismatch_count']==(2 if broken else 0)
    assert all(result['fixtures'][key]['verdict']=='PASS' for key in expected[1:])
    assert len(set(envelope['grid']['expected_grid_ids']))==2 and records==original


@pytest.mark.parametrize('tiny',[False,True])
@pytest.mark.parametrize('spelling',['decimal','exponent','signed_zero'])
def test_oracle_load_numeric_lexemes_direct_strict_json_cli(tmp_path,tiny,spelling):
    import re,subprocess,sys
    from pathlib import Path
    from decimal import Decimal
    from benches import x05_reconcile as m
    envelope = equal_count_ownership_envelope()
    evidence = envelope['assurance']['evidence']
    evidence['oracle_dc_power'][0]['kw_dc'] = Decimal('0')
    evidence['oracle_dc_power'][1]['kw_dc'] = Decimal('9.6')
    envelope['grid']['dc_totals'] = dict(east=Decimal('0'),west=Decimal('9.6'))
    evidence['inverter_loads']['kw_dc'] = {'INV-A':Decimal('0'),'INV-B':Decimal('9.6')}
    if tiny:
        evidence['oracle_dc_power'][0]['kw_dc'] = Decimal('1e-77')
        evidence['oracle_dc_power'][1]['kw_dc'] = Decimal('9.5'+'9'*76)
    plain = serialize_result(envelope)
    def encoded(value,path=()):
        if type(value) is dict:
            return '{'+','.join(json.dumps(key)+':'+encoded(item,path+(key,)) for key,item in value.items())+'}'
        if type(value) is list:
            return '['+','.join(encoded(item,path+(index,)) for index,item in enumerate(value))+']'
        numeric_path = 'oracle_values' in path or 'dc_totals' in path or ('inverter_loads' in path and 'kw_dc' in path) or ('oracle_dc_power' in path and path[-1]=='kw_dc')
        if numeric_path and type(value) in (int,Decimal):
            number = format(Decimal(value),'f')
            if spelling=='signed_zero': return '-0.0' if Decimal(value)==0 else number
            return number+('e0' if spelling=='exponent' else '.0' if '.' not in number else '')
        return serialize_result(value)
    text = encoded(envelope)
    decoded = json.loads(text,parse_float=Decimal)
    direct = m._seven_step_envelope(decoded)
    strict = m.reconcile_seven_steps_json(text)
    assert strict==direct
    baseline = m.reconcile_seven_steps_json(plain)
    assert strict['oracle_loads']==baseline['oracle_loads']
    power = strict['oracle_loads']['metrics']['kw_dc']
    assert power['grid_comparison']['mismatch_count']==(2 if tiny else 0)
    if tiny: assert power['grid_comparison']['examples'][0]['delta']==Decimal('1e-77')
    run = subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step'],input=text.encode(),capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
    assert run.returncode==(4 if strict['verdict']=='UNREADABLE' else 1 if strict['verdict']=='FAIL' else 0) and not run.stderr
    assert json.loads(run.stdout)==json.loads(serialize_result(strict))
    invalid = deepcopy(envelope)
    invalid['assurance']['evidence']['oracle_strings'][0]['optimizers'] = Decimal('6.0')
    assert m._seven_step_oracle_loads(invalid['grid'],invalid['assurance']['evidence'])['verdict']=='UNREADABLE'


def coarsen_oracle_load_devices(grid,evidence):
    from fractions import Fraction
    grid,evidence = deepcopy((grid,evidence))
    evidence['oracle_inverter_ids'] = ['coarse-device']
    for row in evidence['oracle_assignments']: row['inverter_id']='coarse-device'
    for metric,totals in evidence['inverter_loads'].items():
        exact = sum(Fraction(value) for value in totals.values())
        if metric=='kw_dc':
            assert 100 % exact.denominator==0
            cents = exact.numerator*(100//exact.denominator)
            value = f'{cents//100}.{cents%100:02d}'
        else:
            assert exact.denominator==1
            value = int(exact)
        evidence['inverter_loads'][metric] = {'coarse-device':value}
    return grid,evidence


@pytest.mark.parametrize('broken',[False,True])
def test_oracle_load_device_coarsening_cancels_opposite_errors_only(broken):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence = oracle_load_component('device',broken)
    original = deepcopy((grid,evidence))
    fine = m._seven_step_oracle_loads(grid,evidence)
    coarse_grid,coarse_evidence = coarsen_oracle_load_devices(grid,evidence)
    coarse = m._seven_step_oracle_loads(coarse_grid,coarse_evidence)
    for metric,checks in fine['metrics'].items():
        assert Fraction(coarse_evidence['inverter_loads'][metric]['coarse-device'])==sum(Fraction(v) for v in evidence['inverter_loads'][metric].values())
        assert checks['inverter_comparison']['mismatch_count']==(2 if broken else 0)
        assert coarse['metrics'][metric]['inverter_comparison']['mismatch_count']==0
        assert coarse['metrics'][metric]['inverter_report']['verdict']=='PASS'
        assert coarse['metrics'][metric]['grid_comparison']==checks['grid_comparison']
    assert (grid,evidence)==original


@pytest.mark.parametrize('stale',[False,True])
def test_oracle_load_string_refinement_preserves_amounts_requires_new_counts(stale):
    from benches import x05_reconcile as m
    grid,evidence = oracle_load_component('split')
    baseline = m._seven_step_oracle_loads(grid,evidence)
    old,new = 'splits1','splits2'
    evidence['oracle_strings'][1]['optimizers']=1
    evidence['oracle_strings'].append(dict(string_id=new,optimizers=1))
    evidence['oracle_panels'][1]['panels']=3
    evidence['oracle_panels'].append(dict(string_id=new,panels=3))
    evidence['oracle_dc_power'][1]['kw_dc']='0.75'
    evidence['oracle_dc_power'].append(dict(string_id=new,kw_dc='1'))
    evidence['oracle_string_owners'].append(dict(string_id=new,grid_id='splitg1'))
    evidence['oracle_assignments'].append(dict(string_id=new,inverter_id='spliti1'))
    if not stale:
        grid['string_totals']['splitg1']=2
        evidence['inverter_loads']['strings']['spliti1']=2
        evidence['oracle_values']['strings']=3
    original = deepcopy((grid,evidence))
    result = m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict']==('FAIL' if stale else 'PASS')
    for metric in ('optimizers','panels','kw_dc'): assert result['metrics'][metric]==baseline['metrics'][metric]
    for target in ('grid','inverter'):
        assert result['metrics']['strings'][target+'_comparison']['mismatch_count']==(1 if stale else 0)
    assert (grid,evidence)==original


def test_oracle_load_grid_device_duality_exchanges_diagnostics():
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence = oracle_load_component('dual',True)
    # Keep grids clean but devices contradictory to make the transpose observable.
    clean_grid,_ = oracle_load_component('dual')
    grid=clean_grid
    baseline=m._seven_step_oracle_loads(grid,evidence)
    original=deepcopy((grid,evidence))
    changed_grid,changed=deepcopy((grid,evidence))
    changed_grid['expected_grid_ids']=list(evidence['oracle_inverter_ids'])
    changed['oracle_inverter_ids']=list(grid['expected_grid_ids'])
    owners={row['string_id']:row['grid_id'] for row in evidence['oracle_string_owners']}
    connections={row['string_id']:row['inverter_id'] for row in evidence['oracle_assignments']}
    changed['oracle_string_owners']=[dict(string_id=k,grid_id=v) for k,v in connections.items()]
    changed['oracle_assignments']=[dict(string_id=k,inverter_id=v) for k,v in owners.items()]
    fields=dict(strings='string_totals',optimizers='optimizer_totals',panels='panel_totals',kw_dc='dc_totals')
    for metric,field in fields.items():
        changed_grid[field]=deepcopy(evidence['inverter_loads'][metric])
        changed['inverter_loads'][metric]=deepcopy(grid[field])
    result=m._seven_step_oracle_loads(changed_grid,changed)
    for metric in fields:
        for old_target,new_target in [('grid','inverter'),('inverter','grid')]:
            before=baseline['metrics'][metric][old_target+'_comparison']
            after=result['metrics'][metric][new_target+'_comparison']
            assert before['verdict']==after['verdict'] and before['mismatch_count']==after['mismatch_count']
            assert [Fraction(row['delta']) for row in before['examples']]==[Fraction(row['delta']) for row in after['examples']]
            for row in after['examples']: assert Fraction(row['delta'])==Fraction(row['saved'])-Fraction(row['oracle'])
    assert (grid,evidence)==original


def test_exhaustive_composed_oracle_topology_against_independent_maps():
    from itertools import product
    from benches import x05_reconcile as m
    for string_owners in product(['east','west'],repeat=2):
        for device_owners in product(['east','west'],repeat=2):
            for connections in product(['INV-A','INV-B'],repeat=2):
                envelope=equal_count_ownership_envelope()
                evidence=envelope['assurance']['evidence']
                evidence['oracle_string_owners']=[dict(string_id=k,grid_id=v) for k,v in zip(['A','B'],string_owners)]
                evidence['oracle_inverter_owners']=[dict(inverter_id=k,grid_id=v) for k,v in zip(['INV-A','INV-B'],device_owners)]
                evidence['oracle_assignments']=[dict(string_id=k,inverter_id=v) for k,v in zip(['A','B'],connections)]
                original=deepcopy(envelope)
                result=m.reconcile_seven_steps_json(serialize_result(envelope))
                topology=result['independent_owners']['checks']['oracle_topology']
                expected=sum(owner!=device_owners[['INV-A','INV-B'].index(connection)] for owner,connection in zip(string_owners,connections))
                assert topology['mismatch_count']==expected
                assert topology['verdict']==('FAIL' if expected else 'PASS')
                assert result['cross_topology']['verdict']=='PASS' and envelope==original
    for field in ('oracle_string_owners','oracle_inverter_owners','oracle_assignments','oracle_inverter_ids','oracle_strings'):
        envelope=equal_count_ownership_envelope()
        envelope['assurance']['evidence'].pop(field)
        result=m.reconcile_seven_steps_json(serialize_result(envelope))
        assert result['independent_owners']['checks']['oracle_topology']['verdict']=='UNREADABLE'
        assert result['cross_topology']['verdict']=='PASS'


def test_oracle_load_all_metric_saturation_bound_cli_is_stable(tmp_path):
    import subprocess,sys
    from pathlib import Path
    from random import Random
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    grid,evidence=union_oracle_load_components(*(oracle_load_component(f'part{i:02}',True) for i in range(13)))
    envelope=records[0]['envelope']
    envelope['grid'].update(grid)
    envelope['assurance']['evidence'].update(evidence)
    envelope['assurance']['evidence']['fixture_id']=expected[0]
    original=deepcopy(records)
    outputs=[]
    for seed in (0,1):
        changed=deepcopy(records)
        material=changed[0]['envelope']['assurance']['evidence']
        for field in ('oracle_strings','oracle_panels','oracle_dc_power','oracle_string_owners','oracle_assignments','oracle_inverter_ids'):
            Random(seed).shuffle(material[field])
        run=subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],input=serialize_result(dict(expected_ids=expected,fixtures=changed)).encode(),capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
        assert run.returncode in (1,4) and not run.stderr
        parsed=json.loads(run.stdout)
        check=parsed['fixtures'][expected[0]]['reconciliation']['oracle_loads']
        for metric in ('strings','optimizers','panels','kw_dc'):
            for target in ('grid','inverter'):
                detail=check['metrics'][metric][target+'_comparison']
                assert detail['mismatch_count']==26 and len(detail['examples'])==MAX_STRING_EXAMPLES
                assert detail['examples']==sorted(detail['examples'],key=lambda row:row['grid_id' if target=='grid' else 'inverter_id'])
        assert all(parsed['fixtures'][key]['verdict']=='PASS' for key in expected[1:])
        outputs.append(run.stdout)
    assert outputs[0]==outputs[1] and records==original


@pytest.mark.parametrize('metric,field,increment',[('panels','oracle_panels',1),('kw_dc','oracle_dc_power','0.25')])
def test_oracle_load_single_string_influence_fraction_and_bound_siblings(metric,field,increment):
    from fractions import Fraction
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    baseline=m.aggregate_bound_seven_steps(expected,records)
    envelope=records[0]['envelope']; evidence=envelope['assurance']['evidence']
    row=evidence[field][0]
    delta=Fraction(increment)
    row[metric]=row[metric]+increment if metric=='panels' else '4.25'
    original=deepcopy(records)
    result=m.aggregate_bound_seven_steps(expected,records)
    before=baseline['fixtures'][expected[0]]['reconciliation']['oracle_loads']
    after=result['fixtures'][expected[0]]['reconciliation']['oracle_loads']
    owners={r['string_id']:r['grid_id'] for r in evidence['oracle_string_owners']}
    connections={r['string_id']:r['inverter_id'] for r in evidence['oracle_assignments']}
    for target,identifier,key in [('grid',owners[row['string_id']],'grid_id'),('inverter',connections[row['string_id']],'inverter_id')]:
        check=after['metrics'][metric][target+'_comparison']
        assert check['mismatch_count']==1 and check['examples'][0][key]==identifier
        assert Fraction(check['examples'][0]['delta'])==delta
        assert Fraction(check['examples'][0]['saved'])-Fraction(check['examples'][0]['oracle'])==delta
    for other in ('strings','optimizers','panels','kw_dc'):
        if other!=metric: assert after['metrics'][other]==before['metrics'][other]
    assert all(result['fixtures'][key]==baseline['fixtures'][key] for key in expected[1:])
    assert records==original


@pytest.mark.parametrize('stale',[False,True])
def test_oracle_load_inverter_refinement_exact_targets_and_stale_evidence(stale):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=coarsen_oracle_load_devices(*oracle_load_component('refine'))
    baseline=m._seven_step_oracle_loads(grid,evidence)
    evidence['oracle_inverter_ids'].append('new-device')
    evidence['oracle_assignments'][1]['inverter_id']='new-device'
    if not stale:
        fields=dict(optimizers='oracle_strings',panels='oracle_panels',kw_dc='oracle_dc_power')
        for metric in ('strings','optimizers','panels','kw_dc'):
            weights={row['string_id']:Fraction(1 if metric=='strings' else row[metric]) for row in evidence['oracle_strings' if metric=='strings' else fields[metric]]}
            totals={device:sum(weights[row['string_id']] for row in evidence['oracle_assignments'] if row['inverter_id']==device) for device in evidence['oracle_inverter_ids']}
            evidence['inverter_loads'][metric]={key:(f'{value.numerator*(100//value.denominator)//100}.{value.numerator*(100//value.denominator)%100:02d}' if metric=='kw_dc' else int(value)) for key,value in totals.items()}
    original=deepcopy((grid,evidence))
    result=m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict']==('UNREADABLE' if stale else 'PASS')
    for metric,checks in result['metrics'].items():
        assert checks['grid_comparison']==baseline['metrics'][metric]['grid_comparison']
        assert checks['grid_report']==baseline['metrics'][metric]['grid_report']
        assert checks['inverter_comparison']['verdict']==('FAIL' if stale else 'PASS')
        if stale: assert checks['inverter_report']['verdict']=='UNREADABLE'
    assert (grid,evidence)==original


@pytest.mark.parametrize('metric,field',[('optimizers','oracle_strings'),('panels','oracle_panels')])
def test_oracle_zero_count_row_schema_is_named_unreadable(metric,field):
    from benches import x05_reconcile as m
    grid,evidence=oracle_load_component('zero-row')
    evidence[field][0][metric]=0
    baseline=m._seven_step_oracle_loads(grid,evidence)
    assert baseline['verdict']=='UNREADABLE'
    assert baseline['metrics'][metric]['grid_comparison']['reason'].endswith('unreadable')
    for missing in ('oracle_string_owners','oracle_assignments'):
        changed=deepcopy(evidence); changed.pop(missing)
        result=m._seven_step_oracle_loads(grid,changed)
        assert result['verdict']=='UNREADABLE'


def test_oracle_zero_dc_still_needs_explicit_ownership_and_connection():
    from benches import x05_reconcile as m
    grid,evidence=oracle_load_component('zero-dc')
    evidence['oracle_dc_power'][0]['kw_dc']='0'
    grid['dc_totals']['zero-dcg0']='0'
    evidence['inverter_loads']['kw_dc']['zero-dci0']='0'
    evidence['oracle_values']['kw_dc']='1.75'
    assert m._seven_step_oracle_loads(grid,evidence)['verdict']=='PASS'
    for missing,binding in [('oracle_string_owners','owners'),('oracle_assignments','connections')]:
        changed=deepcopy(evidence); changed.pop(missing)
        result=m._seven_step_oracle_loads(grid,changed)
        assert result['verdict']=='UNREADABLE' and result['bindings'][binding]['verdict']=='UNREADABLE'
        assert changed['oracle_values']==evidence['oracle_values']


def test_oracle_load_evidence_completion_subsets_orders_preserve_readable_failure():
    from itertools import combinations,permutations
    from benches import x05_reconcile as m
    fields=('oracle_panels','oracle_dc_power','oracle_string_owners','oracle_assignments')
    grid,complete=oracle_load_component('complete')
    grid['optimizer_totals']['completeg0'],grid['optimizer_totals']['completeg1']=2,1
    original=deepcopy((grid,complete))
    for size in range(1,5):
        for missing in combinations(fields,size):
            for order in permutations(missing):
                evidence=deepcopy(complete)
                for field in missing: evidence.pop(field)
                for field in order:
                    result=m._seven_step_oracle_loads(grid,evidence)
                    assert result['verdict']=='UNREADABLE'
                    if 'oracle_string_owners' in evidence:
                        assert result['metrics']['optimizers']['grid_comparison']['mismatch_count']==2
                    if 'oracle_assignments' in evidence:
                        assert result['metrics']['optimizers']['inverter_comparison']['verdict']=='PASS'
                    evidence[field]=deepcopy(complete[field])
                result=m._seven_step_oracle_loads(grid,evidence)
                assert result['verdict']=='FAIL' and result['metrics']['optimizers']['grid_comparison']['mismatch_count']==2
                repaired=deepcopy(grid); repaired['optimizer_totals']=dict(completeg0=1,completeg1=2)
                assert m._seven_step_oracle_loads(repaired,evidence)['verdict']=='PASS'
    assert (grid,complete)==original


def test_oracle_load_panel_optimizer_exchange_preserves_other_metrics():
    from benches import x05_reconcile as m
    grid,evidence=oracle_load_component('exchange')
    grid['panel_totals']['exchangeg0'],grid['panel_totals']['exchangeg1']=6,3
    baseline=m._seven_step_oracle_loads(grid,evidence)
    changed_grid,changed=deepcopy((grid,evidence))
    changed['oracle_strings']=[dict(string_id=row['string_id'],optimizers=row['panels']) for row in evidence['oracle_panels']]
    changed['oracle_panels']=[dict(string_id=row['string_id'],panels=row['optimizers']) for row in evidence['oracle_strings']]
    changed_grid['optimizer_totals'],changed_grid['panel_totals']=deepcopy(grid['panel_totals']),deepcopy(grid['optimizer_totals'])
    changed['inverter_loads']['optimizers'],changed['inverter_loads']['panels']=deepcopy(evidence['inverter_loads']['panels']),deepcopy(evidence['inverter_loads']['optimizers'])
    changed['oracle_values']['optimizers'],changed['oracle_values']['panels']=evidence['oracle_values']['panels'],evidence['oracle_values']['optimizers']
    result=m._seven_step_oracle_loads(changed_grid,changed)
    for metric in ('strings','kw_dc'): assert result['metrics'][metric]==baseline['metrics'][metric]
    for old,new in [('panels','optimizers'),('optimizers','panels')]:
        for target in ('grid','inverter'):
            before,after=baseline['metrics'][old][target+'_comparison'],result['metrics'][new][target+'_comparison']
            assert before['verdict']==after['verdict'] and before['mismatch_count']==after['mismatch_count']
            assert before['examples']==after['examples']
            assert baseline['metrics'][old][target+'_report']['verdict']==result['metrics'][new][target+'_report']['verdict']


@pytest.mark.parametrize('missing',['oracle_string_owners','oracle_assignments'])
def test_all_zero_oracle_rows_remain_unreadable_after_omission_without_sum_change(missing):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=oracle_load_component('all-zero')
    for field,metric in [('oracle_strings','optimizers'),('oracle_panels','panels'),('oracle_dc_power','kw_dc')]:
        for row in evidence[field]: row[metric]=0
        evidence['oracle_values'][metric]=0
        for key in evidence['inverter_loads'][metric]: evidence['inverter_loads'][metric][key]=0
    for field in ('optimizer_totals','panel_totals','dc_totals'):
        for key in grid[field]: grid[field][key]=0
    assert m._seven_step_oracle_loads(grid,evidence)['verdict']=='UNREADABLE'
    evidence[missing].pop(0)
    result=m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict']=='UNREADABLE'
    assert sum(Fraction(row['panels']) for row in evidence['oracle_panels'])==0
    assert sum(Fraction(row['kw_dc']) for row in evidence['oracle_dc_power'])==0
    # Zero panel/optimizer rows already violate the positive-count schema;
    # the separate zero-DC case isolates valid-row ownership uncertainty.
    assert result['metrics']['optimizers']['grid_comparison']['verdict']=='UNREADABLE'


def fraction_fixed_text(value):
    from fractions import Fraction
    value=Fraction(value)
    places=0
    while 10**places % value.denominator: places+=1
    units=abs(value.numerator)*(10**places//value.denominator)
    sign='-' if value<0 else ''
    if not places: return sign+str(units)
    whole,fraction=divmod(units,10**places)
    return f'{sign}{whole}.{fraction:0{places}d}'


@pytest.mark.parametrize('shared_grid',[False,True])
@pytest.mark.parametrize('shared_device',[False,True])
@pytest.mark.parametrize('metric,field,opposite',[('panels','oracle_panels',False),('panels','oracle_panels',True),('kw_dc','oracle_dc_power',False),('kw_dc','oracle_dc_power',True)])
def test_paired_oracle_perturbations_match_fraction_owner_sums(shared_grid,shared_device,metric,field,opposite):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=oracle_load_component('pair')
    if shared_grid: grid,evidence=coarsen_oracle_load_grids(grid,evidence)
    if shared_device: grid,evidence=coarsen_oracle_load_devices(grid,evidence)
    baseline=m._seven_step_oracle_loads(grid,evidence)
    assert baseline['verdict']=='PASS'
    amount=Fraction(1,4) if metric=='kw_dc' else Fraction(1)
    changes=[amount,-amount if opposite else amount]
    for row,change in zip(evidence[field],changes):
        value=Fraction(row[metric])+change
        row[metric]=fraction_fixed_text(value) if metric=='kw_dc' else int(value)
    original=deepcopy((grid,evidence))
    result=m._seven_step_oracle_loads(grid,evidence)
    for target,owners,id_key in [('grid',evidence['oracle_string_owners'],'grid_id'),('inverter',evidence['oracle_assignments'],'inverter_id')]:
        predicted={}
        for owner,change in zip(owners,changes): predicted[owner[id_key]]=predicted.get(owner[id_key],Fraction(0))+change
        predicted={key:value for key,value in predicted.items() if value}
        check=result['metrics'][metric][target+'_comparison']
        assert check['mismatch_count']==len(predicted)
        assert {row[id_key]:Fraction(row['delta']) for row in check['examples']}==predicted
        assert check['verdict']==('FAIL' if predicted else 'PASS')
    for other in ('strings','optimizers','panels','kw_dc'):
        if other!=metric: assert result['metrics'][other]==baseline['metrics'][other]
    assert (grid,evidence)==original


def test_disconnected_oracle_component_removal_exposes_exact_capped_examples():
    from fractions import Fraction
    from benches import x05_reconcile as m
    parts=[oracle_load_component(f'remove{i:02}',True) for i in range(13)]
    grid,evidence=union_oracle_load_components(*parts)
    baseline=m._seven_step_oracle_loads(grid,evidence)
    removed_grid,removed=parts[0]
    grids=set(removed_grid['expected_grid_ids']); devices=set(removed['oracle_inverter_ids'])
    strings={row['string_id'] for row in removed['oracle_strings']}
    grid['expected_grid_ids']=[key for key in grid['expected_grid_ids'] if key not in grids]
    for field in ('string_totals','optimizer_totals','panel_totals','dc_totals'):
        grid[field]={key:value for key,value in grid[field].items() if key not in grids}
    evidence['oracle_inverter_ids']=[key for key in evidence['oracle_inverter_ids'] if key not in devices]
    for field in ('oracle_strings','oracle_panels','oracle_dc_power','oracle_string_owners','oracle_assignments'):
        evidence[field]=[row for row in evidence[field] if row['string_id'] not in strings]
    for metric,totals in evidence['inverter_loads'].items():
        evidence['inverter_loads'][metric]={key:value for key,value in totals.items() if key not in devices}
        value=Fraction(evidence['oracle_values'][metric])-Fraction(removed['oracle_values'][metric])
        evidence['oracle_values'][metric]=fraction_fixed_text(value) if metric=='kw_dc' else int(value)
    original=deepcopy((grid,evidence))
    result=m._seven_step_oracle_loads(grid,evidence)
    assert result==m._seven_step_oracle_loads(*union_oracle_load_components(*parts[1:]))
    for metric,checks in result['metrics'].items():
        for target,id_key in [('grid','grid_id'),('inverter','inverter_id')]:
            before=baseline['metrics'][metric][target+'_comparison']; after=checks[target+'_comparison']
            assert before['mismatch_count']==26 and after['mismatch_count']==24
            assert len(after['examples'])==MAX_STRING_EXAMPLES
            assert [row[id_key] for row in after['examples']]==[f'remove{i:02}{"g" if target=="grid" else "i"}{j}' for i in range(1,11) for j in (0,1)]
            assert Fraction(after['examples'][-1]['delta'])==Fraction(after['examples'][-1]['saved'])-Fraction(after['examples'][-1]['oracle'])
    assert (grid,evidence)==original


@pytest.mark.parametrize('broken',[False,True])
def test_joint_oracle_grid_device_coarsening_commutes_exactly(broken):
    from benches import x05_reconcile as m
    grid,evidence=oracle_load_component('joint',broken)
    original=deepcopy((grid,evidence))
    first=coarsen_oracle_load_devices(*coarsen_oracle_load_grids(grid,evidence))
    second=coarsen_oracle_load_grids(*coarsen_oracle_load_devices(grid,evidence))
    assert first==second
    assert m._seven_step_oracle_loads(*first)==m._seven_step_oracle_loads(*second)
    assert m._seven_step_oracle_loads(*first)['verdict']=='PASS'
    assert (grid,evidence)==original


@pytest.mark.parametrize('width',[78,79,80])
@pytest.mark.parametrize('tiny_places',[1,76,77])
def test_oracle_dc_accumulation_width_edges_never_round_to_pass(width,tiny_places):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=coarsen_oracle_load_devices(*coarsen_oracle_load_grids(*oracle_load_component('wide')))
    major='9'*width; tiny='0.'+'0'*(tiny_places-1)+'1'
    evidence['oracle_dc_power'][0]['kw_dc']=major
    evidence['oracle_dc_power'][1]['kw_dc']=tiny
    grid['dc_totals']={'coarse':major}
    evidence['inverter_loads']['kw_dc']={'coarse-device':major}
    evidence['oracle_values']['kw_dc']=major
    exact=Fraction(major)+Fraction(tiny)
    result=m._seven_step_oracle_loads(grid,evidence)
    checks=result['metrics']['kw_dc']
    for target in ('grid','inverter'):
        check=checks[target+'_comparison']
        assert check['verdict']=='FAIL' and check['mismatch_count']==1
        row=check['examples'][0]
        assert Fraction(row['saved'])==exact and Fraction(row['delta'])==Fraction(tiny)
        assert Fraction(row['saved'])-Fraction(row['oracle'])==Fraction(tiny)
    supplied=fraction_fixed_text(exact)
    grid['dc_totals']['coarse']=supplied
    evidence['inverter_loads']['kw_dc']['coarse-device']=supplied
    result=m._seven_step_oracle_loads(grid,evidence)
    for target in ('grid','inverter'):
        check=result['metrics']['kw_dc'][target+'_comparison']
        assert check['verdict']==('UNREADABLE' if len(supplied)>80 else 'PASS')
        if len(supplied)>80: assert 'unreadable' in check['reason']


@pytest.mark.parametrize('mutation',['power','connections','both'])
def test_oracle_loads_ignore_reopen_mutations_while_composed_failures_survive(mutation):
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    envelope=connection_envelope(); source=envelope['assurance']
    source['manifest']['fixture_id']=source['evidence']['fixture_id']=expected[0]
    source['bindings']={key:deepcopy(source['manifest']) for key in PROVENANCE_STAGES}
    records[0]['envelope']=envelope
    baseline=m.aggregate_bound_seven_steps(expected,records)
    assert baseline['verdict']=='PASS'
    evidence=source['evidence']
    if mutation in ('power','both'):
        rows=envelope['grid']['reopened_dc_power']; rows[0]['kw_dc'],rows[1]['kw_dc']=rows[1]['kw_dc'],rows[0]['kw_dc']
    if mutation in ('connections','both'):
        rows=evidence['reopened_assignments']; rows[0]['inverter_id'],rows[1]['inverter_id']=rows[1]['inverter_id'],rows[0]['inverter_id']
    original=deepcopy(records)
    result=m.aggregate_bound_seven_steps(expected,records)
    before=baseline['fixtures'][expected[0]]['reconciliation']; after=result['fixtures'][expected[0]]['reconciliation']
    assert after['oracle_loads']==before['oracle_loads']
    if mutation in ('power','both'): assert after['string_metrics']['verdict']=='FAIL'
    if mutation in ('connections','both'): assert after['assignment_reopen']['verdict']=='FAIL'
    assert result['fixtures'][expected[0]]['verdict']=='FAIL'
    assert all(result['fixtures'][key]==baseline['fixtures'][key] for key in expected[1:])
    assert records==original


def oracle_metric_weights(evidence,metric):
    from fractions import Fraction
    field=dict(strings='oracle_strings',optimizers='oracle_strings',panels='oracle_panels',kw_dc='oracle_dc_power')[metric]
    return {row['string_id']:Fraction(1 if metric=='strings' else row[metric]) for row in evidence[field]}


@pytest.mark.parametrize('mutation',['connections','owners'])
def test_oracle_rerouting_and_migration_exact_four_metric_deltas_with_topology(mutation):
    from fractions import Fraction
    from benches import x05_reconcile as m
    envelope=equal_count_ownership_envelope()
    baseline=m.reconcile_seven_steps_json(serialize_result(envelope))
    evidence=envelope['assurance']['evidence']
    if mutation=='connections': evidence['oracle_assignments'][0]['inverter_id']='INV-B'
    else: evidence['oracle_string_owners'][0]['grid_id']='west'
    original=deepcopy(envelope)
    result=m.reconcile_seven_steps_json(serialize_result(envelope))
    before,after=baseline['oracle_loads'],result['oracle_loads']
    changed,unchanged=('inverter','grid') if mutation=='connections' else ('grid','inverter')
    keys=('INV-A','INV-B') if mutation=='connections' else ('east','west')
    id_key='inverter_id' if mutation=='connections' else 'grid_id'
    for metric in ('strings','optimizers','panels','kw_dc'):
        amount=oracle_metric_weights(evidence,metric)['A']
        check=after['metrics'][metric][changed+'_comparison']
        assert check['mismatch_count']==2 and check['verdict']=='FAIL'
        assert {row[id_key]:Fraction(row['delta']) for row in check['examples']}==dict(zip(keys,[-amount,amount]))
        assert after['metrics'][metric][unchanged+'_comparison']==before['metrics'][metric][unchanged+'_comparison']
    topology=result['independent_owners']['checks']['oracle_topology']
    assert topology['verdict']=='FAIL' and topology['mismatch_count']==1
    assert result['cross_topology']['verdict']=='PASS' and envelope==original


@pytest.mark.parametrize('shared_grid',[False,True])
@pytest.mark.parametrize('shared_device',[False,True])
@pytest.mark.parametrize('offset_text',['0.25','0.'+'0'*76+'1'])
def test_affine_oracle_dc_offsets_follow_explicit_membership_counts(shared_grid,shared_device,offset_text):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=union_oracle_load_components(*(oracle_load_component(f'offset{i}') for i in range(3)))
    if shared_grid: grid,evidence=coarsen_oracle_load_grids(grid,evidence)
    if shared_device: grid,evidence=coarsen_oracle_load_devices(grid,evidence)
    baseline=m._seven_step_oracle_loads(grid,evidence)
    assert baseline['verdict']=='PASS'
    offset=Fraction(offset_text)
    for row in evidence['oracle_dc_power']: row['kw_dc']=fraction_fixed_text(Fraction(row['kw_dc'])+offset)
    original=deepcopy((grid,evidence))
    result=m._seven_step_oracle_loads(grid,evidence)
    for target,rows,key in [('grid',evidence['oracle_string_owners'],'grid_id'),('inverter',evidence['oracle_assignments'],'inverter_id')]:
        members={owner:sum(row[key]==owner for row in rows) for owner in {row[key] for row in rows}}
        check=result['metrics']['kw_dc'][target+'_comparison']
        assert check['mismatch_count']==len(members)
        assert {row[key]:Fraction(row['delta']) for row in check['examples']}=={owner:count*offset for owner,count in members.items()}
        targets=grid['dc_totals'] if target=='grid' else evidence['inverter_loads']['kw_dc']
        for owner,count in members.items(): targets[owner]=fraction_fixed_text(Fraction(targets[owner])+count*offset)
    assert m._seven_step_oracle_loads(grid,evidence)['verdict']=='FAIL'  # report remains independent and stale
    evidence['oracle_values']['kw_dc']=fraction_fixed_text(Fraction(evidence['oracle_values']['kw_dc'])+len(evidence['oracle_dc_power'])*offset)
    repaired=m._seven_step_oracle_loads(grid,evidence)
    assert repaired['verdict']=='PASS'
    for metric in ('strings','optimizers','panels'): assert repaired['metrics'][metric]==baseline['metrics'][metric]
    assert original[1]['oracle_dc_power']==evidence['oracle_dc_power']


def test_oracle_target_reflection_negates_exact_capped_deltas():
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=union_oracle_load_components(*(oracle_load_component(f'reflect{i:02}',True) for i in range(13)))
    baseline=m._seven_step_oracle_loads(grid,evidence)
    original=deepcopy((grid,evidence))
    fields=dict(strings='string_totals',optimizers='optimizer_totals',panels='panel_totals',kw_dc='dc_totals')
    for metric,field in fields.items():
        weights=oracle_metric_weights(evidence,metric)
        for target,rows,key in [(grid[field],evidence['oracle_string_owners'],'grid_id'),(evidence['inverter_loads'][metric],evidence['oracle_assignments'],'inverter_id')]:
            loads={owner:sum(weights[row['string_id']] for row in rows if row[key]==owner) for owner in target}
            for owner in target:
                reflected=2*loads[owner]-Fraction(target[owner])
                assert reflected>=0
                target[owner]=fraction_fixed_text(reflected) if metric=='kw_dc' else int(reflected)
    result=m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict']==baseline['verdict']=='FAIL'
    for metric in fields:
        for target,key in [('grid','grid_id'),('inverter','inverter_id')]:
            before=baseline['metrics'][metric][target+'_comparison']; after=result['metrics'][metric][target+'_comparison']
            assert before['mismatch_count']==after['mismatch_count']==26
            assert len(after['examples'])==MAX_STRING_EXAMPLES
            assert [row[key] for row in before['examples']]==[row[key] for row in after['examples']]
            assert [Fraction(row['delta']) for row in after['examples']]==[-Fraction(row['delta']) for row in before['examples']]
    # The independent original report, oracle rows and ownership are untouched.
    for field in ('oracle_values','oracle_strings','oracle_panels','oracle_dc_power','oracle_string_owners','oracle_assignments'):
        assert evidence[field]==original[1][field]


def test_mutated_returned_oracle_diagnostics_do_not_alias_inputs_prior_or_fresh_results():
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    records[0]['envelope']['grid']['panel_totals']['g']=25
    original=deepcopy(records)
    prior=m.aggregate_bound_seven_steps(expected,records)
    preserved=deepcopy(prior)
    current=m.aggregate_bound_seven_steps(expected,records)
    load=current['fixtures'][expected[0]]['reconciliation']['oracle_loads']
    load['metrics']['panels']['grid_comparison']['examples'][0]['delta']='mutated-diagnostic'
    load['metrics']['panels']['grid_comparison']['examples'].append({'grid_id':'new'})
    load['bindings']['owners']['counts']['missing']=999
    current['fixtures'][expected[1]]['verdict']='mutated-sibling'
    assert records==original and prior==preserved
    fresh=m.aggregate_bound_seven_steps(expected,records)
    assert fresh==preserved
    assert current['fixtures'][expected[2]]==prior['fixtures'][expected[2]]


def explicit_oracle_load_material(ids,grids,devices,powers):
    from fractions import Fraction
    grid=dict(expected_grid_ids=sorted(set(grids)))
    evidence=dict(oracle_strings=[dict(string_id=key,optimizers=i+1) for i,key in enumerate(ids)],
        oracle_panels=[dict(string_id=key,panels=2*(i+1)) for i,key in enumerate(ids)],
        oracle_dc_power=[dict(string_id=key,kw_dc=value) for key,value in zip(ids,powers)],
        oracle_string_owners=[dict(string_id=key,grid_id=owner) for key,owner in zip(ids,grids)],
        oracle_assignments=[dict(string_id=key,inverter_id=owner) for key,owner in zip(ids,devices)],
        oracle_inverter_ids=sorted(set(devices)),inverter_loads={},oracle_values={})
    fields=dict(strings='string_totals',optimizers='optimizer_totals',panels='panel_totals',kw_dc='dc_totals')
    for metric,field in fields.items():
        weights=oracle_metric_weights(evidence,metric)
        for targets,owners in [(grid,grids),(evidence['inverter_loads'],devices)]:
            totals={owner:sum(weights[key] for key,actual in zip(ids,owners) if actual==owner) for owner in set(owners)}
            targets[field if targets is grid else metric]={key:fraction_fixed_text(value) if metric=='kw_dc' else int(value) for key,value in totals.items()}
        total=sum(weights.values(),Fraction(0))
        evidence['oracle_values'][metric]=fraction_fixed_text(total) if metric=='kw_dc' else int(total)
    return grid,evidence


def three_device_oracle_material():
    return explicit_oracle_load_material(['s0','s1','s2'],['g']*3,['i0','i1','i2'],['1.25','2.50','3.75'])


def test_reversible_three_device_oracle_routing_loop_restores_diagnostics():
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=three_device_oracle_material()
    original=deepcopy((grid,evidence)); baseline=m._seven_step_oracle_loads(grid,evidence)
    prior=[]; snapshots=[]
    for device in ('i1','i2','i0'):
        evidence['oracle_assignments'][0]['inverter_id']=device
        supplied=deepcopy((grid,evidence)); result=m._seven_step_oracle_loads(grid,evidence)
        for metric,checks in result['metrics'].items():
            amount=oracle_metric_weights(evidence,metric)['s0']
            expected={} if device=='i0' else {'i0':-amount,device:amount}
            check=checks['inverter_comparison']
            assert check['mismatch_count']==len(expected)
            assert {row['inverter_id']:Fraction(row['delta']) for row in check['examples']}==expected
            assert checks['grid_comparison']==baseline['metrics'][metric]['grid_comparison']
        assert (grid,evidence)==supplied
        prior.append(result); snapshots.append(deepcopy(result))
    assert prior[-1]==baseline and (grid,evidence)==original and prior==snapshots


def alternating_rectangle_material():
    # Negative topology fixture for marginal algebra, not a valid design PASS.
    return explicit_oracle_load_material(['s00','s01','s10','s11'],['g0','g0','g1','g1'],['i0','i1','i0','i1'],['1','2','3','4'])


@pytest.mark.parametrize('amount',['0.25','0.'+'0'*76+'1'])
def test_alternating_oracle_rectangle_cancels_marginals_not_string_values(amount):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=alternating_rectangle_material()
    before=deepcopy(evidence['oracle_dc_power']); baseline=m._seven_step_oracle_loads(grid,evidence)
    for row,sign in zip(evidence['oracle_dc_power'],[1,-1,-1,1]):
        row['kw_dc']=fraction_fixed_text(Fraction(row['kw_dc'])+sign*Fraction(amount))
    original=deepcopy((grid,evidence)); result=m._seven_step_oracle_loads(grid,evidence)
    assert result==baseline and result['verdict']=='PASS'
    strings=m.compare_string_dc_power(before,evidence['oracle_dc_power'])
    assert strings['verdict']=='FAIL' and strings['counts']['mismatched']==4
    assert [Fraction(row['saved'])-Fraction(row['oracle']) for row in strings['mismatches']]==[Fraction(amount)*sign for sign in [1,-1,-1,1]]
    topology=m.validate_cross_topology(grid['expected_grid_ids'],[row['string_id'] for row in evidence['oracle_strings']],evidence['oracle_inverter_ids'],evidence['oracle_string_owners'],[dict(inverter_id='i0',grid_id='g0'),dict(inverter_id='i1',grid_id='g1')],evidence['oracle_assignments'])
    assert topology['verdict']=='FAIL' and topology['mismatch_count']==2
    assert (grid,evidence)==original


def test_bound_oracle_error_cap_crossings_exact_examples_and_prior_output_preserved():
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    grid,evidence=union_oracle_load_components(*(oracle_load_component(f'cap{i:02}') for i in range(11)))
    records[0]['envelope']['grid'].update(grid)
    records[0]['envelope']['assurance']['evidence'].update(evidence)
    owners=sorted(grid['panel_totals']); devices=sorted(evidence['inverter_loads']['panels'])
    actual=deepcopy((grid['panel_totals'],evidence['inverter_loads']['panels']))
    history=[]; frozen=[]
    for count in (21,20,19,20,21):
        # Repaired low IDs reveal the next example when crossing the cap.
        indices=list(range(22-count,22))
        targets=records[0]['envelope']['grid']['panel_totals']
        device_targets=records[0]['envelope']['assurance']['evidence']['inverter_loads']['panels']
        targets.update({key:actual[0][key]+int(i in indices) for i,key in enumerate(owners)})
        device_targets.update({key:actual[1][key]+int(i in indices) for i,key in enumerate(devices)})
        supplied=deepcopy(records); result=m.aggregate_bound_seven_steps(expected,records)
        # This projection intentionally has unrelated saved material; never claim fixture PASS.
        assert result['fixtures'][expected[0]]['verdict']=='UNREADABLE'
        checks=result['fixtures'][expected[0]]['reconciliation']['oracle_loads']['metrics']['panels']
        for target,key,ids in [('grid','grid_id',owners),('inverter','inverter_id',devices)]:
            check=checks[target+'_comparison']
            assert check['mismatch_count']==count
            assert [row[key] for row in check['examples']]==[ids[i] for i in indices][:MAX_STRING_EXAMPLES]
            assert len(check['examples'])==min(count,MAX_STRING_EXAMPLES)
        assert all(result['fixtures'][key]['verdict']=='PASS' for key in expected[1:])
        assert records==supplied
        history.append(result); frozen.append(deepcopy(result))
    assert history==frozen and history[0]==history[-1]


def test_heterogeneous_oracle_dc_superposition_is_exact_and_order_independent():
    from fractions import Fraction
    from itertools import permutations
    from benches import x05_reconcile as m
    grid,evidence=explicit_oracle_load_material(['s0','s1','s2'],['g0','g0','g1'],['i0','i1','i0'],['1.25','2.50','3.75'])
    changes=[Fraction(1,4),Fraction(-1,2),Fraction(3,4)]
    original=deepcopy((grid,evidence)); simultaneous=deepcopy(evidence)
    for row,delta in zip(simultaneous['oracle_dc_power'],changes): row['kw_dc']=fraction_fixed_text(Fraction(row['kw_dc'])+delta)
    reference=m._seven_step_oracle_loads(grid,simultaneous)
    for order in permutations(range(3)):
        sequential=deepcopy(evidence)
        for index in order:
            row=sequential['oracle_dc_power'][index]; row['kw_dc']=fraction_fixed_text(Fraction(row['kw_dc'])+changes[index])
        result=m._seven_step_oracle_loads(grid,sequential)
        assert result==reference
        for target,owners,key in [('grid',['g0','g0','g1'],'grid_id'),('inverter',['i0','i1','i0'],'inverter_id')]:
            predicted={owner:sum(delta for actual,delta in zip(owners,changes) if actual==owner) for owner in set(owners)}
            check=result['metrics']['kw_dc'][target+'_comparison']
            assert check['mismatch_count']==len([v for v in predicted.values() if v])
            assert {row[key]:Fraction(row['delta']) for row in check['examples']}==predicted
    assert (grid,evidence)==original


def test_closed_unequal_string_device_rotation_and_inverse():
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=three_device_oracle_material(); original=deepcopy((grid,evidence))
    baseline=m._seven_step_oracle_loads(grid,evidence)
    before=deepcopy(evidence['oracle_assignments'])
    for row,device in zip(evidence['oracle_assignments'],['i1','i2','i0']): row['inverter_id']=device
    result=m._seven_step_oracle_loads(grid,evidence); preserved=deepcopy(result)
    assert result['metrics']['strings']==baseline['metrics']['strings']
    for metric in ('optimizers','panels','kw_dc'):
        weights=oracle_metric_weights(evidence,metric)
        predicted={f'i{i}':weights[f's{(i-1)%3}']-weights[f's{i}'] for i in range(3)}
        check=result['metrics'][metric]['inverter_comparison']
        assert check['mismatch_count']==3
        assert {row['inverter_id']:Fraction(row['delta']) for row in check['examples']}==predicted
        assert result['metrics'][metric]['grid_comparison']==baseline['metrics'][metric]['grid_comparison']
    evidence['oracle_assignments']=before
    assert m._seven_step_oracle_loads(grid,evidence)==baseline and result==preserved and (grid,evidence)==original


def test_whole_oracle_device_migration_preserves_topology_and_device_loads():
    from fractions import Fraction
    from benches import x05_reconcile as m
    envelope=equal_count_ownership_envelope(); evidence=envelope['assurance']['evidence']
    baseline=m.reconcile_seven_steps_json(serialize_result(envelope))
    moving={row['string_id'] for row in evidence['oracle_assignments'] if row['inverter_id']=='INV-A'}
    for row in evidence['oracle_string_owners']:
        if row['string_id'] in moving: row['grid_id']='west'
    evidence['oracle_inverter_owners'][0]['grid_id']='west'
    original=deepcopy(envelope); result=m.reconcile_seven_steps_json(serialize_result(envelope))
    assert result['independent_owners']['checks']['oracle_topology']['verdict']=='PASS'
    for metric in ('strings','optimizers','panels','kw_dc'):
        weights=oracle_metric_weights(evidence,metric); moved=sum(weights[key] for key in moving)
        before=baseline['oracle_loads']['metrics'][metric]; after=result['oracle_loads']['metrics'][metric]
        assert after['grid_comparison']['mismatch_count']==2
        assert {row['grid_id']:Fraction(row['delta']) for row in after['grid_comparison']['examples']}=={'east':-moved,'west':moved}
        assert after['inverter_comparison']==before['inverter_comparison']
    assert result['independent_owners']['checks']['strings']['mismatch_count']==1
    assert result['independent_owners']['checks']['inverters']['mismatch_count']==1
    assert result['cross_topology']['verdict']=='PASS' and envelope==original


@pytest.mark.parametrize('metric',['panels','kw_dc'])
def test_numeric_oracle_saved_exchange_reverses_exact_string_deltas(metric):
    from fractions import Fraction
    from benches import x05_reconcile as m
    oracle={
        'panels':[dict(string_id=key,panels=value) for key,value in zip(['A','B','C'],[2,4,6])],
        'kw_dc':[dict(string_id=key,kw_dc=value) for key,value in zip(['A','B','C'],['1.25','2.50','3.75'])]}
    saved=deepcopy(oracle)
    saved['panels'][0]['panels'],saved['panels'][1]['panels']=3,3
    saved['kw_dc'][0]['kw_dc'],saved['kw_dc'][1]['kw_dc']='1.50','2.25'
    compare=dict(panels=m.compare_string_panel_counts,kw_dc=m.compare_string_dc_power)
    before={key:compare[key](oracle[key],saved[key]) for key in compare}
    original=deepcopy((oracle,saved))
    oracle[metric],saved[metric]=saved[metric],oracle[metric]
    after={key:compare[key](oracle[key],saved[key]) for key in compare}
    other='kw_dc' if metric=='panels' else 'panels'
    assert after[other]==before[other]
    assert after[metric]['counts']==before[metric]['counts']==dict(missing=0,foreign=0,mismatched=2)
    assert [row['string_id'] for row in after[metric]['mismatches']]==['A','B']
    for old,new in zip(before[metric]['mismatches'],after[metric]['mismatches']):
        assert Fraction(new['saved'])-Fraction(new['oracle'])==-(Fraction(old['saved'])-Fraction(old['oracle']))
    oracle[metric],saved[metric]=saved[metric],oracle[metric]
    assert (oracle,saved)==original


@pytest.mark.parametrize('metric',['strings','optimizers','panels','kw_dc'])
@pytest.mark.parametrize('direction',[-1,1])
def test_report_only_oracle_load_corruption_is_isolated(metric,direction):
    from fractions import Fraction
    from benches import x05_reconcile as m
    grid,evidence=oracle_load_component('report-only'); baseline=m._seven_step_oracle_loads(grid,evidence)
    amount=Fraction(direction,4) if metric=='kw_dc' else Fraction(direction)
    total=Fraction(evidence['oracle_values'][metric])+amount
    evidence['oracle_values'][metric]=fraction_fixed_text(total) if metric=='kw_dc' else int(total)
    original=deepcopy((grid,evidence)); result=m._seven_step_oracle_loads(grid,evidence)
    assert result['verdict']=='FAIL' and result['bindings']==baseline['bindings']
    for key,checks in result['metrics'].items():
        for target in ('grid','inverter'):
            assert checks[target+'_comparison']==baseline['metrics'][key][target+'_comparison']
            assert checks[target+'_report']['verdict']==('FAIL' if key==metric else 'PASS')
    assert (grid,evidence)==original


@pytest.mark.parametrize('repair_order',[('digest','load'),('load','digest')])
def test_provenance_and_oracle_load_failures_repair_independently_in_bound_aggregate(repair_order):
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records(); source=records[0]['envelope']['assurance']
    baseline=m.aggregate_bound_seven_steps(expected,records)
    role=sorted(source['inputs_hex'])[0]; original_hex=source['inputs_hex'][role]
    original_power=source['evidence']['oracle_dc_power'][0]['kw_dc']
    source['inputs_hex'][role]=original_hex+'00'
    source['evidence']['oracle_dc_power'][0]['kw_dc']='4.25'
    remaining={'digest','load'}; previous=[]; frozen=[]
    for repair in repair_order+(None,):
        supplied=deepcopy(records); result=m.aggregate_bound_seven_steps(expected,records)
        fixture=result['fixtures'][expected[0]]; loads=fixture['reconciliation']['oracle_loads']
        assert loads['verdict']==('FAIL' if 'load' in remaining else 'PASS')
        assert fixture['verdict']==('UNREADABLE' if 'digest' in remaining else 'FAIL' if 'load' in remaining else 'PASS')
        assert all(result['fixtures'][key]==baseline['fixtures'][key] for key in expected[1:])
        assert records==supplied
        previous.append(result); frozen.append(deepcopy(result))
        if repair=='digest': source['inputs_hex'][role]=original_hex
        if repair=='load': source['evidence']['oracle_dc_power'][0]['kw_dc']=original_power
        if repair is not None: remaining.remove(repair)
    assert previous==frozen and previous[-1]==baseline


def test_topology_valid_opposite_multi_metric_changes_hide_only_in_marginals():
    from fractions import Fraction
    from benches import x05_reconcile as m
    envelope=seven_step_envelope(); evidence=envelope['assurance']['evidence']
    baseline=m.reconcile_seven_steps_json(serialize_result(envelope))
    original_rows={field:deepcopy(evidence[field]) for field in ('oracle_strings','oracle_panels','oracle_dc_power')}
    for metric,field,amount in [('optimizers','oracle_strings',Fraction(1)),('panels','oracle_panels',Fraction(1)),('kw_dc','oracle_dc_power',Fraction(1,4))]:
        for row,sign in zip(evidence[field],[1,-1]):
            value=Fraction(row[metric])+sign*amount
            row[metric]=fraction_fixed_text(value) if metric=='kw_dc' else int(value)
            assert value>0
    supplied=deepcopy(envelope); result=m.reconcile_seven_steps_json(serialize_result(envelope))
    assert result['oracle_loads']==baseline['oracle_loads'] and result['oracle_loads']['verdict']=='PASS'
    assert result['independent_owners']['checks']['oracle_topology']['verdict']=='PASS'
    for field,compare in [('oracle_strings',m.compare_strings),('oracle_panels',m.compare_string_panel_counts),('oracle_dc_power',m.compare_string_dc_power)]:
        assert compare(original_rows[field],evidence[field])['counts']['mismatched']==2
    assert result['steps']['3']['verdict']=='FAIL' and result['string_metrics']['verdict']=='FAIL'
    assert result['verdict']=='FAIL' and envelope==supplied


def test_exhaustive_grouping_sequences_against_independent_index_reference():
    from itertools import product
    from benches import x05_reconcile as m
    for size in range(5):
        for counts in product(range(1,4),repeat=size):
            for report in range(1,4):
                rows=[dict(group_count=value) for value in counts]
                original=deepcopy(rows)
                expected=[index for index,value in enumerate(counts) if value!=report]
                result=m.compare_recommendations(report,rows,no_recommendations_shown=not rows)
                assert result['verdict']==('FAIL' if expected else 'PASS')
                assert result['mismatch_count']==len(expected)
                assert result['mismatched_recommendations']==expected[:MAX_RECOMMENDATION_EXAMPLES]
                assert rows==original


@pytest.mark.parametrize('prefix',[0,1,7,31])
def test_grouping_matching_prefix_translates_capped_indices_only(prefix):
    from benches import x05_reconcile as m
    rows=[dict(group_count=1) for _ in range(MAX_RECOMMENDATION_EXAMPLES+7)]
    original=deepcopy(rows); prior=m.compare_recommendations(2,rows); preserved=deepcopy(prior)
    translated=[dict(group_count=2) for _ in range(prefix)]+rows
    supplied=deepcopy(translated); result=m.compare_recommendations(2,translated)
    assert result['mismatch_count']==prior['mismatch_count']==27
    assert result['mismatched_recommendations']==[index+prefix for index in prior['mismatched_recommendations']]
    assert rows==original and translated==supplied and prior==preserved


@pytest.mark.parametrize('bad',[None,{},[],True,dict(group_count=True),dict(group_count=0),dict(group_count='private-group-canary'),dict(group_count=2**53)])
def test_grouping_late_invalid_row_overrides_capped_readable_failures(bad):
    from benches import x05_reconcile as m
    rows=[dict(group_count=1) for _ in range(MAX_RECOMMENDATION_EXAMPLES+7)]+[deepcopy(bad)]
    original=deepcopy(rows); result=m.compare_recommendations(2,rows)
    assert result==dict(verdict='UNREADABLE',reason='recommendation_count_unreadable')
    assert 'private-group-canary' not in serialize_result(result) and rows==original
    rows[-1]=dict(group_count=1)
    repaired=m.compare_recommendations(2,rows)
    assert repaired['verdict']=='FAIL' and repaired['mismatch_count']==28
    assert repaired['mismatched_recommendations']==list(range(MAX_RECOMMENDATION_EXAMPLES))
    assert result==dict(verdict='UNREADABLE',reason='recommendation_count_unreadable')


def test_grouping_report_retargeting_bound_aggregate_preserves_siblings():
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    baseline=m.aggregate_bound_seven_steps(expected,records)
    counts=[1,2,3]*13
    evidence=records[0]['envelope']['assurance']['evidence']
    evidence['recommendations']=[dict(group_count=value) for value in counts]
    prior=[]; frozen=[]
    for report in (1,2,3):
        evidence['report_grid_count']=report
        supplied=deepcopy(records); result=m.aggregate_bound_seven_steps(expected,records)
        grouping=result['fixtures'][expected[0]]['reconciliation']['steps']['4']
        indices=[index for index,value in enumerate(counts) if value!=report]
        assert grouping['verdict']=='FAIL' and grouping['mismatch_count']==len(indices)==26
        assert grouping['mismatched_recommendations']==indices[:MAX_RECOMMENDATION_EXAMPLES]
        assert result['fixtures'][expected[0]]['verdict']=='FAIL'
        assert all(result['fixtures'][key]==baseline['fixtures'][key] for key in expected[1:])
        assert records==supplied
        prior.append(result); frozen.append(deepcopy(result))
    assert prior==frozen


@pytest.mark.parametrize('repair_first',['grouping','table'])
def test_grouping_failure_and_frozen_table_policy_repair_independently(repair_first):
    from benches import x05_reconcile as m
    evidence=complete_evidence()
    evidence['recommendations']=[dict(group_count=1)]
    evidence['saved_values']['panels']-=1
    unaccepted=m.reconcile_offline(evidence)
    assert unaccepted['stages']['policy']['verdict']=='FAIL'
    _,record=discrepancy_fixture()
    evidence['discrepancy_records']=[record]
    accepted=m.reconcile_offline(evidence); preserved=deepcopy(accepted)
    assert accepted['stages']['policy']['verdict']=='PASS'
    assert accepted['stages']['grouping']['verdict']=='FAIL' and accepted['verdict']=='FAIL'
    if repair_first=='grouping':
        evidence['recommendations']=[dict(group_count=2)]
    else:
        evidence['saved_values']['panels']+=1
        evidence['discrepancy_records']=[]
    intermediate=m.reconcile_offline(evidence)
    assert intermediate['stages']['grouping']['verdict']==('PASS' if repair_first=='grouping' else 'FAIL')
    assert intermediate['stages']['table']['verdict']==('FAIL' if repair_first=='grouping' else 'PASS')
    assert intermediate['stages']['policy']['verdict']=='PASS' and intermediate['verdict']=='FAIL'
    evidence['recommendations']=[dict(group_count=2)]
    evidence['saved_values']['panels']=evidence['oracle_values']['panels']
    evidence['discrepancy_records']=[]
    assert m.reconcile_offline(evidence)['verdict']=='PASS' and accepted==preserved


@pytest.mark.parametrize('report',[1,2,3])
@pytest.mark.parametrize('left,right',[([2]*4,[2,1,2]),([1,2,3]*12,[3,2,1]*11),([],[1,2,1])])
def test_grouping_concatenation_adds_totals_offsets_indices_and_caps(report,left,right):
    from benches import x05_reconcile as m
    rows_left=[dict(group_count=value) for value in left]; rows_right=[dict(group_count=value) for value in right]
    original=deepcopy((rows_left,rows_right))
    first=m.compare_recommendations(report,rows_left,no_recommendations_shown=not rows_left)
    second=m.compare_recommendations(report,rows_right,no_recommendations_shown=not rows_right)
    left_bad=[i for i,value in enumerate(left) if value!=report]
    right_bad=[len(left)+i for i,value in enumerate(right) if value!=report]
    expected=left_bad+right_bad
    prior=deepcopy((first,second)); result=m.compare_recommendations(report,rows_left+rows_right)
    assert result['mismatch_count']==first['mismatch_count']+second['mismatch_count']==len(expected)
    assert result['mismatched_recommendations']==expected[:MAX_RECOMMENDATION_EXAMPLES]
    assert result['verdict']==('FAIL' if expected else 'PASS')
    assert (rows_left,rows_right)==original and (first,second)==prior


@pytest.mark.parametrize('removed',[(0,),(1,),(36,37,38,39,40),(38,39,40,41),(0,1,48,49)])
def test_grouping_deletion_compacts_independent_surviving_indices(removed):
    from benches import x05_reconcile as m
    counts=[1,2]*25; rows=[dict(group_count=value) for value in counts]
    original=deepcopy(rows); before=m.compare_recommendations(2,rows); preserved=deepcopy(before)
    kept=[(index,row) for index,row in enumerate(rows) if index not in removed]
    compact=[row for _,row in kept]
    expected=[old_index-sum(deleted<old_index for deleted in removed) for old_index,row in kept if row['group_count']!=2]
    result=m.compare_recommendations(2,compact)
    assert result['mismatch_count']==25-sum(counts[index]!=2 for index in removed)==len(expected)
    assert result['mismatched_recommendations']==expected[:MAX_RECOMMENDATION_EXAMPLES]
    assert result['verdict']=='FAIL' and before==preserved and rows==original


@pytest.mark.parametrize('length',[8,24])
@pytest.mark.parametrize('bump',[0,1,2])
def test_grouping_unequal_run_replication_matches_expanded_reference(length,bump):
    from benches import x05_reconcile as m
    counts=([1,2,3,2]*6)[:length]
    factors=[1+(index%3)+bump for index in range(length)]
    rows=[dict(group_count=value) for value in counts]; original=deepcopy((rows,factors))
    expected=[]; offset=0
    for value,factor in zip(counts,factors):
        if value!=2: expected.extend(range(offset,offset+factor))
        offset+=factor
    expanded=[dict(row) for row,factor in zip(rows,factors) for _ in range(factor)]
    supplied=deepcopy(expanded); result=m.compare_recommendations(2,expanded)
    assert result['mismatch_count']==len(expected)
    assert result['mismatched_recommendations']==expected[:MAX_RECOMMENDATION_EXAMPLES]
    assert len(result['mismatched_recommendations'])==min(len(expected),MAX_RECOMMENDATION_EXAMPLES)
    assert (rows,factors)==original and expanded==supplied


@pytest.mark.parametrize('report',[1,2,3])
def test_grouping_bijective_count_renaming_keeps_bound_diagnostics(report):
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    evidence=records[0]['envelope']['assurance']['evidence']
    evidence['report_grid_count']=report
    evidence['recommendations']=[dict(group_count=value) for value in [1,2,3]*13]
    baseline=m.aggregate_bound_seven_steps(expected,records); preserved=deepcopy(baseline)
    rename={1:11,2:37,3:5}
    evidence['report_grid_count']=rename[report]
    for row in evidence['recommendations']: row['group_count']=rename[row['group_count']]
    supplied=deepcopy(records); result=m.aggregate_bound_seven_steps(expected,records)
    first,second=baseline['fixtures'][expected[0]],result['fixtures'][expected[0]]
    assert second['reconciliation']['steps']['4']==first['reconciliation']['steps']['4']
    assert second['verdict']==first['verdict']=='FAIL'
    assert all(result['fixtures'][key]==baseline['fixtures'][key] for key in expected[1:])
    assert records==supplied and baseline==preserved


@pytest.mark.parametrize('count',[19,20,21])
def test_grouping_cap_boundary_direct_strict_json_bound_cli_agree(tmp_path,count):
    import subprocess,sys
    from pathlib import Path
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    evidence=records[0]['envelope']['assurance']['evidence']
    evidence['report_grid_count']=2
    evidence['recommendations']=[dict(group_count=value) for value in [1,2]*count]
    original=deepcopy(records)
    direct=m.compare_recommendations(2,evidence['recommendations'])
    strict=m.reconcile_seven_steps_json(serialize_result(records[0]['envelope']))['steps']['4']
    assert strict==direct
    assert direct['mismatch_count']==count
    assert direct['mismatched_recommendations']==list(range(0,2*count,2))[:MAX_RECOMMENDATION_EXAMPLES]
    run=subprocess.run([sys.executable,'-P',str(Path(m.__file__).resolve()),'--seven-step-bound-aggregate'],input=serialize_result(dict(expected_ids=expected,fixtures=records)).encode(),capture_output=True,cwd=tmp_path,timeout=20,env={'SystemRoot':'C:\\Windows'})
    assert run.returncode==1 and not run.stderr
    parsed=json.loads(run.stdout)
    assert parsed['fixtures'][expected[0]]['reconciliation']['steps']['4']==direct
    assert parsed['fixtures'][expected[0]]['verdict']=='FAIL'
    assert all(parsed['fixtures'][key]['verdict']=='PASS' for key in expected[1:])
    assert records==original


def stable_grouping_weave(left,right,schedule):
    sources=[left,right]; cursors=[0,0]; origins=[]; rows=[]
    while any(cursors[source]<len(sources[source]) for source in (0,1)):
        for source in schedule:
            index=cursors[source]
            if index<len(sources[source]):
                origins.append((source,index)); rows.append(sources[source][index]); cursors[source]+=1
    return rows,origins


@pytest.mark.parametrize('schedule',[(0,1),(0,0,1),(0,1,1,1)])
@pytest.mark.parametrize('report',[1,2,3])
def test_grouping_stable_weaves_match_explicit_origin_reference(schedule,report):
    from benches import x05_reconcile as m
    counts=([1,2,3]*13,[2,3,1]*9)
    left,right=([dict(group_count=value) for value in values] for values in counts)
    original=deepcopy((left,right))
    rows,origins=stable_grouping_weave(left,right,schedule)
    assert [index for source,index in origins if source==0]==list(range(len(left)))
    assert [index for source,index in origins if source==1]==list(range(len(right)))
    expected=[position for position,(source,index) in enumerate(origins) if counts[source][index]!=report]
    result=m.compare_recommendations(report,rows)
    assert result['verdict']=='FAIL'
    assert result['mismatch_count']==len(expected)==sum(value!=report for values in counts for value in values)
    assert result['mismatched_recommendations']==expected[:MAX_RECOMMENDATION_EXAMPLES]
    assert (left,right)==original


@pytest.mark.parametrize('report',[1,2,3])
def test_grouping_reversal_uses_full_reference_positions_beyond_cap(report):
    from benches import x05_reconcile as m
    counts=[1,2,3,2]*17; rows=[dict(group_count=value) for value in counts]
    original=deepcopy(rows); before=m.compare_recommendations(report,rows); preserved=deepcopy(before)
    full=[index for index,value in enumerate(counts) if value!=report]
    expected=sorted(len(rows)-1-index for index in full)
    result=m.compare_recommendations(report,list(reversed(rows)))
    assert len(full)>MAX_RECOMMENDATION_EXAMPLES
    assert result['mismatch_count']==before['mismatch_count']==len(full)
    assert result['mismatched_recommendations']==expected[:MAX_RECOMMENDATION_EXAMPLES]
    assert result['mismatched_recommendations']!=sorted(len(rows)-1-index for index in before['mismatched_recommendations'])[:MAX_RECOMMENDATION_EXAMPLES]
    assert before==preserved and rows==original


def test_grouping_every_cyclic_offset_remaps_full_positions_and_new_cap():
    from benches import x05_reconcile as m
    counts=[1,2,3]*17; rows=[dict(group_count=value) for value in counts]
    full=[index for index,value in enumerate(counts) if value!=2]
    original=deepcopy(rows); history=[]; frozen=[]
    for offset in range(len(rows)):
        rotated=rows[offset:]+rows[:offset]
        expected=sorted((index-offset)%len(rows) for index in full)
        supplied=deepcopy(rotated); result=m.compare_recommendations(2,rotated)
        assert result['mismatch_count']==len(full)==34
        assert result['mismatched_recommendations']==expected[:MAX_RECOMMENDATION_EXAMPLES]
        assert rotated==supplied
        history.append(result); frozen.append(deepcopy(result))
    assert history==frozen and rows==original
    assert len({tuple(result['mismatched_recommendations']) for result in history})>1


@pytest.mark.parametrize('size',[10,100,1000])
@pytest.mark.parametrize('mode',['valid','mismatch','late-invalid'])
def test_grouping_recommendation_access_is_exactly_linear(size,mode):
    from benches import x05_reconcile as m
    work=dict(iterated=0,gets=0)
    class Row(dict):
        def get(self,*args):
            work['gets']+=1
            return super().get(*args)
    class Rows(list):
        def __iter__(self):
            for row in super().__iter__():
                work['iterated']+=1
                yield row
    values=[2 if mode=='valid' else 1 for _ in range(size)]
    if mode=='late-invalid': values[-1]=True
    rows=Rows(Row(group_count=value) for value in values)
    result=m.compare_recommendations(2,rows)
    assert work==dict(iterated=size,gets=size)
    assert result['verdict']==dict(valid='PASS',mismatch='FAIL',**{'late-invalid':'UNREADABLE'})[mode]
    if mode!='late-invalid': assert result['mismatch_count']==(0 if mode=='valid' else size)
    assert [dict.get(row,'group_count') for row in list.__iter__(rows)]==values


def test_grouping_shared_rows_refresh_only_sharing_fixtures_and_preserve_prior():
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    shared=dict(group_count=2)
    for record in records[:2]:
        record['envelope']['assurance']['evidence']['recommendations']=[shared]
    baseline=m.aggregate_bound_seven_steps(expected,records); frozen=deepcopy(baseline)
    assert baseline['verdict']=='PASS'
    shared['group_count']=1
    supplied=deepcopy(records); result=m.aggregate_bound_seven_steps(expected,records)
    for key in expected[:2]:
        grouping=result['fixtures'][key]['reconciliation']['steps']['4']
        assert grouping['verdict']=='FAIL' and grouping['mismatch_count']==1 and grouping['mismatched_recommendations']==[0]
        assert result['fixtures'][key]['verdict']=='FAIL'
    assert result['fixtures'][expected[2]]==baseline['fixtures'][expected[2]]
    assert records==supplied and baseline==frozen
    assert all(record['envelope']['assurance']['evidence']['recommendations'][0] is shared for record in records[:2])
    preserved=deepcopy(result)
    shared['group_count']=2
    assert m.aggregate_bound_seven_steps(expected,records)==baseline and result==preserved


def csv_records_text(records,newline='\n'):
    output=StringIO(newline='')
    writer=csv.writer(output,lineterminator=newline,quoting=csv.QUOTE_ALL)
    writer.writerows(records)
    return output.getvalue()


@pytest.mark.parametrize('record',range(6))
@pytest.mark.parametrize('broken',['unterminated','after-close'])
def test_csv_strict_quote_grammar_sanitized_and_field_limit_restored(record,broken):
    from benches import x05_reconcile as m
    _,view=table_fixture(); matrix=list(csv.reader(StringIO(csv_fixture(view))))
    prefix=csv_records_text(matrix[:record])
    bad='"private-csv-canary' if broken=='unterminated' else '"private-csv-canary"text'
    text=prefix+','.join([bad]+matrix[record][1:])+'\n'
    original=csv.field_size_limit()
    try:
        csv.field_size_limit(1)
        result=m.compare_csv_view(text,view)
        assert result==dict(verdict='UNREADABLE',reason='csv_evidence_unreadable')
        assert 'private-csv-canary' not in serialize_result(result)
        assert csv.field_size_limit()==1
        assert m.compare_csv_view(csv_fixture(view),view)['verdict']=='PASS'
        assert csv.field_size_limit()==1
    finally: csv.field_size_limit(original)


@pytest.mark.parametrize('record',range(6))
@pytest.mark.parametrize('mutation',['missing','extra-empty','trailing'])
def test_csv_exact_record_arity_at_each_header_and_data_position(record,mutation):
    from benches import x05_reconcile as m
    _,view=table_fixture(); matrix=list(csv.reader(StringIO(csv_fixture(view))))
    positions=range(5) if mutation=='missing' else range(6) if mutation=='extra-empty' else [5]
    for position in positions:
        changed=deepcopy(matrix)
        if mutation=='missing': changed[record].pop(position)
        elif mutation=='extra-empty': changed[record].insert(position,'')
        if mutation=='trailing':
            text=''.join(csv_records_text([row]).rstrip('\n')+(',' if index==record else '')+'\n' for index,row in enumerate(changed))
        else: text=csv_records_text(changed)
        original=deepcopy(view); result=m.compare_csv_view(text,view)
        assert result==dict(verdict='UNREADABLE',reason='csv_headers_unreadable' if record==0 else 'csv_row_unreadable')
        assert view==original


@pytest.mark.parametrize('spelling',['decimal','signed-zero','exponent'])
@pytest.mark.parametrize('tiny',[False,True])
def test_csv_numeric_lexemes_match_direct_rows_or_named_contract_rejection(spelling,tiny):
    from fractions import Fraction
    from benches import x05_reconcile as m
    _,view=table_fixture(); expected=deepcopy(view)
    if tiny:
        row=next(row for row in expected if row['metric']=='kw_dc')
        value=Fraction(row['studio'])+Fraction(1,10**77)
        row['studio']=fraction_fixed_text(value); row['delta']=fraction_fixed_text(Fraction(1,10**77)); row['verdict']='FAIL'
    lexical=deepcopy(expected)
    for row in lexical:
        for field in ('studio','oracle','delta'):
            value=Fraction(row[field]); plain=fraction_fixed_text(value)
            row[field]=plain+'e0' if spelling=='exponent' else '-0.000' if spelling=='signed-zero' and not value else plain+('.000' if '.' not in plain else '')
    text=csv_fixture(lexical); result=m.compare_csv_view(text,expected)
    if spelling=='exponent':
        assert result==dict(verdict='UNREADABLE',reason='csv_view_rows_unreadable')
        assert m._table(lexical) is None  # textual exponent strings are unsupported in both paths
    else:
        assert result['verdict']=='PASS' and m._table(lexical)==m._table(expected)
        parsed,error=m._csv_rows(text); assert error is None
        power=m._table(parsed)['kw_dc']
        assert Fraction(power['delta'])==(Fraction(1,10**77) if tiny else 0)
        if tiny:
            different=m.compare_csv_view(text,view)
            assert different['verdict']=='FAIL'
            assert {'metric':'kw_dc','field':'delta'} in different['mismatches']
            assert Fraction(power['studio'])-Fraction(power['oracle'])==Fraction(1,10**77)


@pytest.mark.parametrize('newline',['\n','\r\n'])
@pytest.mark.parametrize('kind',['embedded-at-limit','extra-record','malformed-last','malformed-overflow'])
def test_csv_logical_record_boundaries_and_malformed_final_records_restore_limits(newline,kind):
    from benches import x05_reconcile as m
    _,view=table_fixture(); matrix=list(csv.reader(StringIO(csv_fixture(view))))
    if kind=='embedded-at-limit':
        matrix[-1][-1]='PA'+newline+'SS'
        text=csv_records_text(matrix,newline)
    elif kind=='extra-record': text=csv_records_text(matrix+[matrix[-1]],newline)
    elif kind=='malformed-last': text=csv_records_text(matrix[:-1],newline)+'"unterminated'
    else: text=csv_records_text(matrix,newline)+'"unterminated'
    original=csv.field_size_limit()
    try:
        csv.field_size_limit(1)
        rows,error=m._csv_rows(text)
        if kind=='embedded-at-limit':
            assert error is None and len(rows)==5
            assert rows[-1]['verdict']=='PA'+newline+'SS'
            assert m.compare_csv_view(text,view)['reason']=='csv_view_rows_unreadable'
        else:
            assert rows is None
            assert error==('csv_excess_rows' if kind=='extra-record' else 'csv_evidence_unreadable')
        assert csv.field_size_limit()==1
    finally: csv.field_size_limit(original)


@pytest.mark.parametrize('size',[20,100,500])
def test_csv_overflow_stops_after_one_logical_lookahead_without_tail_scan(monkeypatch,size):
    from benches import x05_reconcile as m
    original_reader=csv.reader; work=[]
    class CountedReader:
        def __init__(self,reader): self.reader=reader
        def __iter__(self): return self
        def __next__(self):
            work.append(1)
            return next(self.reader)
    monkeypatch.setattr(m.csv,'reader',lambda *args,**kwargs:CountedReader(original_reader(*args,**kwargs)))
    _,view=table_fixture()
    text=','.join(TABLE_COLUMNS)+'\n'+('panels,1,1,0,PASS\n'*size)+'"private-unparsed-tail'
    assert len(text)<m.MAX_CSV_CHARS
    previous=csv.field_size_limit()
    try:
        csv.field_size_limit(2)
        result=m.compare_csv_view(text,view)
        assert result==dict(verdict='UNREADABLE',reason='csv_excess_rows')
        assert len(work)==1+len(METRICS)+1  # header, five accepted records, one overflow
        assert 'private-unparsed-tail' not in serialize_result(result)
        assert csv.field_size_limit()==2
    finally: csv.field_size_limit(previous)


def distinct_csv_binding_rows():
    _,rows=table_fixture()
    values=dict(panels=(25,24,1),optimizers=(13,12,1),inverters=(3,1,2),strings=(5,2,3),kw_dc=('9.6','8.5','1.1'))
    for row in rows:
        row['studio'],row['oracle'],row['delta']=values[row['metric']]
        row['verdict']='FAIL'
    return rows


def test_all_csv_column_permutations_preserve_paired_bindings_detect_cells_only():
    from itertools import permutations
    from benches import x05_reconcile as m
    rows=distinct_csv_binding_rows(); original=deepcopy(rows); outcomes=dict(PASS=0,FAIL=0,UNREADABLE=0)
    baseline=m.compare_csv_view(csv_fixture(rows),rows)
    assert baseline['verdict']=='PASS'
    for columns in permutations(TABLE_COLUMNS):
        assert m.compare_csv_view(csv_fixture(rows,columns),rows)==baseline
        text=csv_records_text([TABLE_COLUMNS]+[[row[key] for key in columns] for row in rows])
        result=m.compare_csv_view(text,rows)
        if columns==TABLE_COLUMNS: expected='PASS'
        elif columns[0]=='metric' and columns[-1]=='verdict': expected='FAIL'
        else: expected='UNREADABLE'
        assert result['verdict']==expected
        outcomes[expected]+=1
    assert outcomes==dict(PASS=1,FAIL=5,UNREADABLE=114) and rows==original


@pytest.mark.parametrize('mismatch',[False,True])
def test_csv_minimal_and_all_quoting_agree_direct_and_bound(mismatch):
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records(); evidence=records[0]['envelope']['assurance']['evidence']
    exported=deepcopy(evidence['view_rows'])
    if mismatch: evidence['view_rows'][0]['studio']+=1
    results=[]
    for quoting in (csv.QUOTE_MINIMAL,csv.QUOTE_ALL):
        output=StringIO(newline=''); writer=csv.writer(output,quoting=quoting,lineterminator='\r\n')
        writer.writerow(TABLE_COLUMNS); writer.writerows([row[key] for key in TABLE_COLUMNS] for row in exported)
        evidence['csv_text']=output.getvalue(); supplied=deepcopy(records)
        direct=m.compare_csv_view(evidence['csv_text'],evidence['view_rows'])
        aggregate=m.aggregate_bound_seven_steps(expected,records)
        assert aggregate['fixtures'][expected[0]]['reconciliation']['steps']['5']['csv_view']==direct
        assert direct['verdict']==('FAIL' if mismatch else 'PASS')
        assert all(aggregate['fixtures'][key]['verdict']=='PASS' for key in expected[1:])
        assert records==supplied
        results.append(aggregate)
    assert results[0]==results[1]


@pytest.mark.parametrize('index',range(5))
@pytest.mark.parametrize('token',['pass','fail','private-verdict-canary','',0])
def test_csv_verdict_tokens_are_strict_sanitized_and_recover(index,token):
    from benches import x05_reconcile as m
    _,view=table_fixture(); exported=deepcopy(view)
    exported[index]['verdict']=token; original=deepcopy(exported)
    result=m.compare_csv_view(csv_fixture(exported),view)
    assert result==dict(verdict='UNREADABLE',reason='csv_view_rows_unreadable')
    assert 'private-verdict-canary' not in serialize_result(result) and exported==original
    exported[index]['verdict']=view[index]['verdict']
    assert m.compare_csv_view(csv_fixture(exported),view)['verdict']=='PASS'


@pytest.mark.parametrize('header',[False,True])
@pytest.mark.parametrize('character',['"',chr(0x1F642)])
@pytest.mark.parametrize('excess',[False,True])
def test_csv_decoded_field_budget_handles_escaping_and_non_bmp_with_restoration(header,character,excess):
    from benches import x05_reconcile as m
    _,view=table_fixture(); matrix=list(csv.reader(StringIO(csv_fixture(view))))
    payload=character*(m.MAX_CSV_CELL+int(excess))
    matrix[0 if header else 1][0]=payload
    text=csv_records_text(matrix)
    if character=='"': assert len(text)>2*len(payload)
    previous=csv.field_size_limit()
    try:
        csv.field_size_limit(3)
        rows,error=m._csv_rows(text)
        if excess: assert rows is None and error=='csv_cell_too_large'
        elif header: assert rows is None and error=='csv_headers_unreadable'
        else:
            assert error is None and len(rows[0]['metric'])==m.MAX_CSV_CELL and rows[0]['metric']==payload
        assert csv.field_size_limit()==3
        result=m.compare_csv_view(text,view)
        assert result['verdict']=='UNREADABLE' and payload not in serialize_result(result)
        assert csv.field_size_limit()==3
    finally: csv.field_size_limit(previous)


@pytest.mark.parametrize('order',[('csv','table'),('table','csv')])
def test_bound_csv_structure_and_independent_table_faults_repair_separately(order):
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records(); source=records[0]['envelope']['assurance']['evidence']
    baseline=m.aggregate_bound_seven_steps(expected,records)
    valid_csv=source['csv_text']; saved_panels=source['saved_values']['panels']
    source['csv_text']='"private-broken-export'; source['saved_values']['panels']+=1
    remaining={'csv','table'}; history=[]; frozen=[]
    for repair in order+(None,):
        supplied=deepcopy(records); result=m.aggregate_bound_seven_steps(expected,records)
        fixture=result['fixtures'][expected[0]]; step=fixture['reconciliation']['steps']['5']
        assert step['csv_view']['verdict']==('UNREADABLE' if 'csv' in remaining else 'PASS')
        assert step['table']['verdict']==('FAIL' if 'table' in remaining else 'PASS')
        assert fixture['verdict']==('UNREADABLE' if 'csv' in remaining else 'FAIL' if 'table' in remaining else 'PASS')
        assert all(result['fixtures'][key]==baseline['fixtures'][key] for key in expected[1:])
        assert 'private-broken-export' not in serialize_result(result) and records==supplied
        history.append(result); frozen.append(deepcopy(result))
        if repair=='csv': source['csv_text']=valid_csv
        elif repair=='table': source['saved_values']['panels']=saved_panels
        if repair is not None: remaining.remove(repair)
    assert history==frozen and history[-1]==baseline


@pytest.mark.parametrize('kind',['semicolon','tab','backslash','single-quote'])
def test_csv_unsupported_dialects_reject_then_comma_and_doubled_quotes_recover(kind):
    from benches import x05_reconcile as m
    _,view=table_fixture()
    if kind in ('semicolon','tab'):
        output=StringIO(newline=''); writer=csv.writer(output,delimiter=';' if kind=='semicolon' else '\t',quoting=csv.QUOTE_MINIMAL)
        writer.writerow(TABLE_COLUMNS); writer.writerows([row[key] for key in TABLE_COLUMNS] for row in view)
        text=output.getvalue(); reason='csv_headers_unreadable'
    elif kind=='single-quote':
        matrix=[TABLE_COLUMNS]+[[str(row[key]) for key in TABLE_COLUMNS] for row in view]
        text='\n'.join(','.join("'"+str(value)+"'" for value in row) for row in matrix)+'\n'; reason='csv_headers_unreadable'
    else:
        text='"private\\"escaped",studio,oracle,delta,verdict\n'; reason='csv_evidence_unreadable'
    result=m.compare_csv_view(text,view)
    assert result==dict(verdict='UNREADABLE',reason=reason) and 'private' not in serialize_result(result)
    assert m.compare_csv_view(csv_fixture(view),view)['verdict']=='PASS'
    structural=deepcopy(view); structural[0]['verdict']='PA"SS'
    decoded,error=m._csv_rows(csv_fixture(structural))
    assert error is None and decoded[0]['verdict']=='PA"SS'
    assert m.compare_csv_view(csv_fixture(structural),view)['verdict']=='UNREADABLE'  # valid quote syntax, foreign verdict
    assert m.compare_csv_view(csv_fixture(view),view)['verdict']=='PASS'


@pytest.mark.parametrize('where',['header','metric'])
@pytest.mark.parametrize('kind',['lookalike','zero-width'])
@pytest.mark.parametrize('index',range(5))
def test_csv_foreign_confusable_labels_never_bind(where,kind,index):
    from benches import x05_reconcile as m
    _,view=table_fixture(); matrix=list(csv.reader(StringIO(csv_fixture(view))))
    row,column=(0,index) if where=='header' else (index+1,0)
    label=matrix[row][column]
    lookalikes={'m':'\u043c','s':'\u0455','o':'\u03bf','d':'\u0501','v':'\u0475','p':'\u0440','i':'\u0456','k':'\u043a'}
    foreign=lookalikes[label[0]]+label[1:] if kind=='lookalike' else label[0]+'\u200b'+label[1:]
    assert foreign!=label
    matrix[row][column]=foreign; original=deepcopy(view)
    result=m.compare_csv_view(csv_records_text(matrix),view)
    assert result==dict(verdict='UNREADABLE',reason='csv_headers_unreadable' if where=='header' else 'csv_view_rows_unreadable')
    assert foreign not in serialize_result(result) and view==original
    assert m.compare_csv_view(csv_fixture(view),view)['verdict']=='PASS'


@pytest.mark.parametrize('control',['\x00','\x1b','\x08'])
@pytest.mark.parametrize('record',range(6))
@pytest.mark.parametrize('column',range(5))
def test_csv_controls_at_every_header_and_data_field_are_sanitized(record,column,control):
    from benches import x05_reconcile as m
    _,view=table_fixture(); matrix=list(csv.reader(StringIO(csv_fixture(view))))
    matrix[record][column]=matrix[record][column]+control+'private-control-canary'
    previous=csv.field_size_limit()
    try:
        csv.field_size_limit(2)
        result=m.compare_csv_view(csv_records_text(matrix),view)
        assert result==dict(verdict='UNREADABLE',reason='csv_headers_unreadable' if record==0 else 'csv_view_rows_unreadable')
        assert 'private-control-canary' not in serialize_result(result) and control not in serialize_result(result)
        assert csv.field_size_limit()==2
        assert m.compare_csv_view(csv_fixture(view),view)['verdict']=='PASS' and csv.field_size_limit()==2
    finally: csv.field_size_limit(previous)


@pytest.mark.parametrize('newline',['\n','\r\n'])
@pytest.mark.parametrize('kind',['empty-physical','quoted-empty','whitespace'])
@pytest.mark.parametrize('position',range(7))
def test_csv_blank_logical_records_are_not_silently_ignored(newline,kind,position):
    from benches import x05_reconcile as m
    _,view=table_fixture(); matrix=list(csv.reader(StringIO(csv_fixture(view))))
    token={'empty-physical':'','quoted-empty':'""','whitespace':' \t '}[kind]
    line=token+newline
    parsed=list(csv.reader(StringIO(line,newline='')))
    assert parsed==[[] if kind=='empty-physical' else [''] if kind=='quoted-empty' else [' \t ']]
    pieces=[csv_records_text([row],newline) for row in matrix]; pieces.insert(position,line)
    result=m.compare_csv_view(''.join(pieces),view)
    reason='csv_headers_unreadable' if position==0 else 'csv_excess_rows' if position==6 else 'csv_row_unreadable'
    assert result==dict(verdict='UNREADABLE',reason=reason)
    assert m.compare_csv_view(csv_records_text(matrix,newline),view)['verdict']=='PASS'


def test_bound_csv_multi_cell_differences_match_fraction_reference_with_opposite_tiny_deltas():
    from fractions import Fraction
    from benches import x05_reconcile as m
    expected,records=bound_seven_fixture_records()
    for record,sign in zip(records[:2],[1,-1]):
        evidence=record['envelope']['assurance']['evidence']; exported=deepcopy(evidence['view_rows'])
        for index,row in enumerate(exported):
            quantum=Fraction(1,10**77) if row['metric']=='kw_dc' else Fraction(index+1)
            row['studio']=fraction_fixed_text(Fraction(row['studio'])+quantum)
            row['oracle']=fraction_fixed_text(Fraction(row['oracle'])+2*quantum)
            row['delta']=fraction_fixed_text(sign*quantum)
            row['verdict']='FAIL'
        evidence['csv_text']=csv_fixture(exported)
    supplied=deepcopy(records); result=m.aggregate_bound_seven_steps(expected,records)
    assert result['verdict']=='FAIL'
    for key,record,sign in zip(expected[:2],records[:2],[1,-1]):
        evidence=record['envelope']['assurance']['evidence']
        exported,error=m._csv_rows(evidence['csv_text']); assert error is None
        original={row['metric']:row for row in evidence['view_rows']}
        reference=[]
        for row in exported:
            for field in ('studio','oracle','delta','verdict'):
                unequal=row[field]!=original[row['metric']][field] if field=='verdict' else Fraction(row[field])!=Fraction(original[row['metric']][field])
                if unequal: reference.append(dict(metric=row['metric'],field=field))
        observed=result['fixtures'][key]['reconciliation']['steps']['5']['csv_view']
        assert observed['verdict']=='FAIL' and observed['mismatches']==reference and len(reference)==20
        power=m._table(exported)['kw_dc']
        assert Fraction(power['delta'])==sign*Fraction(1,10**77)
    assert result['fixtures'][expected[2]]['verdict']=='PASS' and records==supplied



def assert_csv_lexical_bound_parity(text, reason):
    """Keep foreign CSV text out of diagnostics and preserve readable siblings."""
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    source['csv_text'] = text
    supplied = deepcopy(records)
    direct = m.compare_csv_view(text, source['view_rows'])
    assert direct == dict(verdict='UNREADABLE', reason=reason)
    result = m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected, fixtures=records)))
    selected = result['fixtures'][expected[0]]
    assert selected['verdict'] == 'UNREADABLE'
    assert selected['reconciliation']['steps']['5']['csv_view'] == direct
    assert all(result['fixtures'][key]['verdict'] == 'PASS' for key in expected[1:])
    assert records == supplied
    source['csv_text'] = csv_fixture(source['view_rows'])
    recovered = m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected, fixtures=records)))
    assert recovered['verdict'] == 'PASS'
    assert all(recovered['fixtures'][key] == result['fixtures'][key] for key in expected[1:])


@pytest.mark.parametrize('prefix', ['\ufeff', '\ufeff\ufeff', ' \ufeff', '\n\ufeff'])
def test_csv_bom_lexical_prefix_is_exactly_one_at_stream_start(prefix):
    from benches import x05_reconcile as m
    _, records = bound_seven_fixture_records()
    view = records[0]['envelope']['assurance']['evidence']['view_rows']
    text = prefix + csv_fixture(view)
    if prefix == '\ufeff':
        assert m.compare_csv_view(text, view)['verdict'] == 'PASS'
        expected, records = bound_seven_fixture_records()
        records[0]['envelope']['assurance']['evidence']['csv_text'] = text
        assert m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected, fixtures=records)))['verdict'] == 'PASS'
    else:
        assert_csv_lexical_bound_parity(text, 'csv_headers_unreadable')


@pytest.mark.parametrize('record', range(6))
@pytest.mark.parametrize('column', range(5))
@pytest.mark.parametrize('placement', ['prefix', 'middle', 'suffix'])
def test_csv_bom_lexical_inside_every_decoded_field_is_foreign(record, column, placement):
    _, records = bound_seven_fixture_records()
    view = records[0]['envelope']['assurance']['evidence']['view_rows']
    matrix = list(csv.reader(StringIO(csv_fixture(view))))
    value = matrix[record][column]
    position = {'prefix': 0, 'middle': len(value)//2, 'suffix': len(value)}[placement]
    matrix[record][column] = value[:position] + '\ufeff' + value[position:]
    assert_csv_lexical_bound_parity(csv_records_text(matrix),
        'csv_headers_unreadable' if record == 0 else 'csv_view_rows_unreadable')


@pytest.mark.parametrize('whitespace', [' ', '\t', '\u00a0'])
@pytest.mark.parametrize('record', range(6))
@pytest.mark.parametrize('column', range(5))
def test_csv_whitespace_lexical_fields_never_trim_implicitly(whitespace, record, column):
    from benches import x05_reconcile as m
    _, records = bound_seven_fixture_records()
    view = records[0]['envelope']['assurance']['evidence']['view_rows']
    matrix = list(csv.reader(StringIO(csv_fixture(view))))
    value = whitespace + matrix[record][column] + whitespace
    matrix[record][column] = value
    equivalent = deepcopy(view)
    if record:
        equivalent[record-1][TABLE_COLUMNS[column]] = value
    else:
        for row in equivalent:
            row[value] = row.pop(TABLE_COLUMNS[column])
    assert m._table(equivalent) is None
    assert_csv_lexical_bound_parity(csv_records_text(matrix),
        'csv_headers_unreadable' if record == 0 else 'csv_view_rows_unreadable')


@pytest.mark.parametrize('token', ['1,25', '1,000', '1.000,5', '\u0661.\u0662', '\uff11\uff12.\uff15'])
@pytest.mark.parametrize('record', range(1,6))
@pytest.mark.parametrize('column', [1,2,3])
def test_csv_localized_lexical_numeric_tokens_reject_and_canonical_recovers(token, record, column):
    _, records = bound_seven_fixture_records()
    view = records[0]['envelope']['assurance']['evidence']['view_rows']
    matrix = list(csv.reader(StringIO(csv_fixture(view))))
    matrix[record][column] = token
    text = csv_records_text(matrix)
    assert list(csv.reader(StringIO(text)))[record][column] == token
    assert_csv_lexical_bound_parity(text, 'csv_view_rows_unreadable')


@pytest.mark.parametrize('token', ['=1+1', '=SUM(1,2)', '#DIV/0!', '#N/A'])
@pytest.mark.parametrize('record', range(1,6))
@pytest.mark.parametrize('column', [1,2,3,4])
def test_csv_formula_lexical_tokens_reject_without_changing_evidence(token, record, column):
    _, records = bound_seven_fixture_records()
    view = records[0]['envelope']['assurance']['evidence']['view_rows']
    matrix = list(csv.reader(StringIO(csv_fixture(view))))
    matrix[record][column] = token
    assert_csv_lexical_bound_parity(csv_records_text(matrix), 'csv_view_rows_unreadable')


@pytest.mark.parametrize('character', ['x', chr(0x1F642)])
@pytest.mark.parametrize('excess', [False, True])
def test_csv_raw_lexical_budget_precedes_parser_and_restores_global_limit(monkeypatch, character, excess):
    from benches import x05_reconcile as m
    _, view = table_fixture()
    text = character * (m.MAX_CSV_CHARS + int(excess))
    original = m._csv_rows
    calls = []
    def counted(supplied):
        calls.append(supplied)
        return original(supplied)
    monkeypatch.setattr(m, '_csv_rows', counted)
    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(2)
        result = m.compare_csv_view(text, view)
        assert result == dict(verdict='UNREADABLE', reason='csv_input_too_large' if excess else 'csv_cell_too_large')
        assert calls == ([] if excess else [text])
        assert csv.field_size_limit() == 2
        assert text not in serialize_result(result)
    finally:
        csv.field_size_limit(previous)



def assert_csv_record_bound_result(records, expected, text, verdict):
    from benches import x05_reconcile as m
    source = records[0]['envelope']['assurance']['evidence']
    source['csv_text'] = text
    supplied = deepcopy(records)
    direct = m.compare_csv_view(text, source['view_rows'])
    assert direct['verdict'] == verdict
    if verdict == 'UNREADABLE':
        assert direct == dict(verdict='UNREADABLE', reason=direct['reason'])
        assert direct['reason'] in {'csv_headers_unreadable', 'csv_evidence_unreadable',
            'csv_row_unreadable', 'csv_view_rows_unreadable', 'csv_excess_rows', 'csv_cell_too_large'}
    result = m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected, fixtures=records)))
    assert result['fixtures'][expected[0]]['reconciliation']['steps']['5']['csv_view'] == direct
    assert all(result['fixtures'][key]['verdict'] == 'PASS' for key in expected[1:])
    assert records == supplied
    return result


def test_csv_record_prefix_truncation_at_every_character_and_clean_recovery():
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    canonical = csv_fixture(source['view_rows'])
    assert canonical.endswith('"\r\n')
    accepted = []
    for cut in range(len(canonical)+1):
        complete = cut >= len(canonical)-2
        result = assert_csv_record_bound_result(records, expected, canonical[:cut], 'PASS' if complete else 'UNREADABLE')
        assert result['verdict'] == ('PASS' if complete else 'UNREADABLE')
        if complete: accepted.append(cut)
        recovered = assert_csv_record_bound_result(records, expected, canonical, 'PASS')
        assert recovered['verdict'] == 'PASS'
    assert accepted == [len(canonical)-2, len(canonical)-1, len(canonical)]


@pytest.mark.parametrize('record', range(6))
def test_csv_record_mixed_cell_quote_masks_preserve_decoded_rows_and_bound_evidence(record):
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    matrix = list(csv.reader(StringIO(csv_fixture(source['view_rows']))))
    baseline = m.compare_csv_view(csv_records_text(matrix), source['view_rows'])
    for mask in range(32):
        lines = []
        for index, row in enumerate(matrix):
            assert all(not any(c in value for c in ',"\r\n') for value in row)
            lines.append(','.join('"'+value+'"' if index != record or mask & (1<<column) else value
                for column, value in enumerate(row)))
        text = '\n'.join(lines)+'\n'
        assert list(csv.reader(StringIO(text))) == matrix
        result = assert_csv_record_bound_result(records, expected, text, 'PASS')
        assert result['fixtures'][expected[0]]['reconciliation']['steps']['5']['csv_view'] == baseline


@pytest.mark.parametrize('first', ['\n', '\r\n', '\r'])
def test_csv_record_mixed_terminators_exhaustive_with_limit_restoration(first):
    from itertools import product
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    matrix = list(csv.reader(StringIO(csv_fixture(source['view_rows']))))
    content = [csv_records_text([row]).removesuffix('\n') for row in matrix]
    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(2)
        for suffix in product(['\n', '\r\n', '\r'], repeat=5):
            text = ''.join(row+ending for row, ending in zip(content, (first,)+suffix))
            result = assert_csv_record_bound_result(records, expected, text, 'PASS')
            assert result['verdict'] == 'PASS' and csv.field_size_limit() == 2
            decoded, error = m._csv_rows(text)
            assert error is None
            assert [[row[key] for key in TABLE_COLUMNS] for row in decoded] == matrix[1:]
            assert csv.field_size_limit() == 2
    finally:
        csv.field_size_limit(previous)


@pytest.mark.parametrize('separator', ['\u0085', '\u2028', '\u2029'])
@pytest.mark.parametrize('boundary', range(5))
def test_csv_record_unicode_separators_do_not_split_records_and_recover(separator, boundary):
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    matrix = list(csv.reader(StringIO(csv_fixture(source['view_rows']))))
    content = [csv_records_text([row]).removesuffix('\n') for row in matrix]
    text = ''.join(row+(separator if index == boundary else '\n') for index, row in enumerate(content))
    result = assert_csv_record_bound_result(records, expected, text, 'UNREADABLE')
    assert result['verdict'] == 'UNREADABLE'
    assert assert_csv_record_bound_result(records, expected, csv_records_text(matrix), 'PASS')['verdict'] == 'PASS'


@pytest.mark.parametrize('final_newline', [False, True])
@pytest.mark.parametrize('separator', ['', '\n'])
def test_csv_record_concatenated_exports_never_accept_first_table(final_newline, separator):
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    source['saved_values']['panels'] += 1
    canonical = csv_fixture(source['view_rows'])
    first = canonical if final_newline else canonical.removesuffix('\r\n')
    result = assert_csv_record_bound_result(records, expected, first+separator+canonical, 'UNREADABLE')
    assert result['verdict'] == 'UNREADABLE'
    assert result['fixtures'][expected[0]]['reconciliation']['steps']['5']['table']['verdict'] == 'FAIL'
    recovered = assert_csv_record_bound_result(records, expected, canonical, 'PASS')
    assert recovered['verdict'] == 'FAIL'
    assert recovered['fixtures'][expected[0]]['reconciliation']['steps']['5']['table']['verdict'] == 'FAIL'



@pytest.mark.parametrize('mask', range(32))
def test_csv_verdict_masks_have_exact_independent_mismatch_positions(mask):
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    exported = deepcopy(source['view_rows'])
    assert all(row['verdict'] == 'PASS' for row in exported)
    reference = [dict(metric=row['metric'], field='verdict') for index, row in enumerate(exported) if mask & (1<<index)]
    for index, row in enumerate(exported):
        row['verdict'] = 'FAIL' if mask & (1<<index) else 'PASS'
    result = assert_csv_record_bound_result(records, expected, csv_fixture(exported), 'FAIL' if mask else 'PASS')
    comparison = result['fixtures'][expected[0]]['reconciliation']['steps']['5']['csv_view']
    assert comparison['mismatches'] == reference and len(reference) == mask.bit_count()
    assert comparison['reason'] == ('csv_view_mismatch' if mask else 'csv_view_equal')
    assert result['fixtures'][expected[0]]['reconciliation']['steps']['5']['table']['verdict'] == 'PASS'


@pytest.mark.parametrize('target,replacement', [(target, replacement) for target in range(5)
    for replacement in range(5) if target != replacement])
def test_csv_verdict_metric_substitution_names_missing_duplicate_coverage_and_recovers(target, replacement):
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    exported = deepcopy(source['view_rows'])
    exported[target] = deepcopy(exported[replacement])
    assert len(exported) == 5 and len({row['metric'] for row in exported}) == 4
    result = assert_csv_record_bound_result(records, expected, csv_fixture(exported), 'UNREADABLE')
    assert result['fixtures'][expected[0]]['reconciliation']['steps']['5']['csv_view']['reason'] == 'csv_view_rows_unreadable'
    assert assert_csv_record_bound_result(records, expected, csv_fixture(source['view_rows']), 'PASS')['verdict'] == 'PASS'


@pytest.mark.parametrize('metric', range(5))
@pytest.mark.parametrize('field', ['studio', 'oracle', 'delta', 'verdict'])
def test_csv_verdict_comparison_symmetry_for_readable_numeric_and_verdict_changes(metric, field):
    from fractions import Fraction
    from benches import x05_reconcile as m
    _, original = table_fixture()
    changed = deepcopy(original)
    if field == 'verdict':
        changed[metric][field] = 'FAIL' if original[metric][field] == 'PASS' else 'PASS'
    else:
        increment = Fraction(1,10) if metric == 4 else Fraction(1)
        changed[metric][field] = fraction_fixed_text(Fraction(original[metric][field]) + increment)
    supplied = deepcopy((original, changed))
    forward = m.compare_csv_view(csv_fixture(original), changed)
    reverse = m.compare_csv_view(csv_fixture(changed), original)
    reference = [dict(metric=original[metric]['metric'], field=field)]
    assert forward == reverse == dict(verdict='FAIL', reason='csv_view_mismatch', mismatches=reference)
    assert (original, changed) == supplied


@pytest.mark.parametrize('order', list(__import__('itertools').permutations(range(3))))
def test_csv_verdict_fixture_masks_stay_isolated_under_record_permutations(order):
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    baseline = m.aggregate_bound_seven_steps(expected, records)
    masks = [0, 5, 26]
    for record, mask in zip(records, masks):
        source = record['envelope']['assurance']['evidence']
        exported = deepcopy(source['view_rows'])
        for index, row in enumerate(exported):
            row['verdict'] = 'FAIL' if mask & (1<<index) else 'PASS'
        source['csv_text'] = csv_fixture(exported)
    supplied = deepcopy(records)
    result = m.aggregate_bound_seven_steps_json(serialize_result(dict(expected_ids=expected, fixtures=[records[index] for index in order])))
    for key, mask in zip(expected, masks):
        step = result['fixtures'][key]['reconciliation']['steps']['5']
        reference = [dict(metric=metric, field='verdict') for index, metric in enumerate(m.METRICS) if mask & (1<<index)]
        assert step['csv_view']['mismatches'] == reference and len(reference) == mask.bit_count()
        assert step['table'] == baseline['fixtures'][key]['reconciliation']['steps']['5']['table']
        assert result['fixtures'][key]['verdict'] == ('FAIL' if mask else 'PASS')
    assert records == supplied


def test_csv_verdict_transformation_composition_preserves_one_exact_mismatch():
    from itertools import permutations
    from fractions import Fraction
    from benches import x05_reconcile as m
    expected, records = bound_seven_fixture_records()
    source = records[0]['envelope']['assurance']['evidence']
    changed = deepcopy(source['view_rows'])
    changed[2]['verdict'] = 'FAIL'
    reference = [dict(metric=changed[2]['metric'], field='verdict')]
    seen_masks = set()
    for index, columns in enumerate(permutations(TABLE_COLUMNS)):
        mask = index % 32
        seen_masks.add(mask)
        matrix = [list(columns)]
        for row in changed:
            values = []
            for field in columns:
                value = str(row[field])
                if field in ('studio', 'oracle', 'delta'):
                    canonical = fraction_fixed_text(Fraction(value))
                    value = '000'+canonical+('00' if '.' in canonical else '.000')
                    assert Fraction(value) == Fraction(row[field])
                values.append(value)
            matrix.append(values)
        text = '\n'.join(','.join('"'+value+'"' if mask & (1<<column) else value
            for column, value in enumerate(row)) for row in matrix)+'\n'
        result = assert_csv_record_bound_result(records, expected, text, 'FAIL')
        check = result['fixtures'][expected[0]]['reconciliation']['steps']['5']['csv_view']
        assert check == dict(verdict='FAIL', reason='csv_view_mismatch', mismatches=reference)
    assert seen_masks == set(range(32))
