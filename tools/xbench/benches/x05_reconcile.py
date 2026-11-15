"""X05 pure offline parity helpers, not wired into live grading.

Both callers supply independent rows: {string_id: str, optimizers: int}.
This module does not establish the origin or independence of either input.
"""

import csv
from datetime import datetime
from decimal import Context, Decimal, InvalidOperation, localcontext
from io import StringIO
import re


METRICS = ("panels", "optimizers", "inverters", "strings", "kw_dc")
TABLE_COLUMNS = ("metric", "studio", "oracle", "delta", "verdict")
_CONTEXT = Context(prec=200)


def _out(verdict, reason, **evidence):
    return {"verdict": verdict, "reason": reason, **evidence}


def _numeric(value, *, count=False, signed=False):
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        raw = str(value)
        if len(raw) > 80 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", raw):
            return None
        number = Decimal(raw)
        if not number.is_finite() or (not signed and number < 0):
            return None
        if count and number != number.to_integral_value():
            return None
        return number
    except (InvalidOperation, ValueError):
        return None


def compare_recommendations(report_grid_count, recommendations):
    """Check every supplied shown recommendation against an independent count."""
    if type(report_grid_count) is not int or not 1 <= report_grid_count <= 2**53 - 1:
        return _out("UNREADABLE", "report_grid_count_unreadable")
    if not isinstance(recommendations, list):
        return _out("UNREADABLE", "recommendations_unreadable")
    mismatches = []
    for index, row in enumerate(recommendations):
        count = row.get("group_count") if isinstance(row, dict) else None
        if type(count) is not int or not 1 <= count <= 2**53 - 1:
            return _out("UNREADABLE", "recommendation_count_unreadable")
        if count != report_grid_count:
            mismatches.append(index)
    return _out("FAIL" if mismatches else "PASS", "grouping_count_mismatch" if mismatches
                else "grouping_counts_equal", mismatched_recommendations=mismatches)


def _table(rows):
    if not isinstance(rows, list):
        return None
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(TABLE_COLUMNS):
            return None
        metric = row["metric"]
        if type(metric) is not str or metric not in METRICS or metric in indexed:
            return None
        values = {key: _numeric(row[key], count=metric != "kw_dc", signed=key == "delta")
                  for key in ("studio", "oracle", "delta")}
        if any(value is None for value in values.values()) or row["verdict"] not in ("PASS", "FAIL"):
            return None
        indexed[metric] = {**values, "verdict": row["verdict"]}
    return indexed if set(indexed) == set(METRICS) else None


def _values(values):
    if not isinstance(values, dict) or set(values) != set(METRICS):
        return None
    parsed = {metric: _numeric(value, count=metric != "kw_dc") for metric, value in values.items()}
    return parsed if all(value is not None for value in parsed.values()) else None


def compare_table(rows, saved_values, oracle_values):
    """Validate exported values against two independently supplied value sets.

    A row verdict is PASS for zero delta and FAIL otherwise. Whether a nonzero
    row has a frozen revision discrepancy is a separate policy comparison.
    """
    table, saved, oracle = _table(rows), _values(saved_values), _values(oracle_values)
    if table is None or saved is None or oracle is None:
        return _out("UNREADABLE", "table_evidence_unreadable")
    mismatches = []
    with localcontext(_CONTEXT):
        for metric in METRICS:
            delta = saved[metric] - oracle[metric]
            expected = {"studio": saved[metric], "oracle": oracle[metric], "delta": delta,
                        "verdict": "PASS" if delta == 0 else "FAIL"}
            mismatches.extend({"metric": metric, "field": key} for key in expected
                              if table[metric][key] != expected[key])
    return _out("FAIL" if mismatches else "PASS", "table_value_mismatch" if mismatches
                else "table_values_equal", mismatches=mismatches)


def _instant(value):
    if type(value) is not str:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant if instant.tzinfo is not None and instant.utcoffset() is not None else None
    except ValueError:
        return None


def check_discrepancies(fixture_id, deltas, records, run_started_at):
    """Check supplied frozen policy records; this function grants no approval."""
    started = _instant(run_started_at)
    if (type(fixture_id) is not str or not fixture_id.strip() or started is None
            or not isinstance(deltas, dict) or set(deltas) != set(METRICS)
            or not isinstance(records, list)):
        return _out("UNREADABLE", "discrepancy_evidence_unreadable")
    parsed = {metric: _numeric(value, count=metric != "kw_dc", signed=True)
              for metric, value in deltas.items()}
    if any(value is None for value in parsed.values()):
        return _out("UNREADABLE", "discrepancy_delta_unreadable")
    indexed = {}
    for record in records:
        if not isinstance(record, dict):
            return _out("UNREADABLE", "discrepancy_record_unreadable")
        metric = record.get("metric")
        if record.get("fixture_id") != fixture_id or type(metric) is not str or metric not in METRICS:
            continue
        name, frozen = record.get("name"), _instant(record.get("frozen_at"))
        delta = _numeric(record.get("delta"), count=metric != "kw_dc", signed=True)
        if type(name) is not str or not name.strip() or frozen is None or delta is None:
            return _out("UNREADABLE", "discrepancy_record_unreadable")
        if metric in indexed:
            return _out("UNREADABLE", "duplicate_discrepancy_record")
        indexed[metric] = (delta, frozen)
    rejected = [metric for metric in METRICS if parsed[metric] != 0 and
                (metric not in indexed or indexed[metric][0] != parsed[metric]
                 or indexed[metric][1] >= started)]
    return _out("FAIL" if rejected else "PASS", "unfrozen_nonzero_delta" if rejected
                else "discrepancy_policy_satisfied", rejected_metrics=rejected)


def compare_snapshots(before, reopened):
    """Compare producer-supplied hashes, not their provenance or computation."""
    fields = ("strings_sha256", "table_sha256")
    for snapshot in (before, reopened):
        if (not isinstance(snapshot, dict) or any(type(snapshot.get(key)) is not str
                or re.fullmatch(r"[0-9a-f]{64}", snapshot[key]) is None for key in fields)):
            return _out("UNREADABLE", "snapshot_hash_unreadable")
    changed = [key for key in fields if before[key] != reopened[key]]
    return _out("FAIL" if changed else "PASS", "snapshot_changed" if changed
                else "snapshots_equal", changed_hashes=changed)


def compare_csv_view(csv_text, view_rows):
    """Check normalized CSV/view equality; compare_table checks independent truth."""
    if type(csv_text) is not str:
        return _out("UNREADABLE", "csv_evidence_unreadable")
    try:
        reader = csv.reader(StringIO(csv_text.removeprefix("\ufeff"), newline=""), strict=True)
        headers = next(reader)
        if len(headers) != len(TABLE_COLUMNS) or set(headers) != set(TABLE_COLUMNS):
            return _out("UNREADABLE", "csv_headers_unreadable")
        rows = []
        for cells in reader:
            if len(cells) != len(headers):
                return _out("UNREADABLE", "csv_row_unreadable")
            rows.append(dict(zip(headers, cells)))
    except (csv.Error, StopIteration):
        return _out("UNREADABLE", "csv_evidence_unreadable")
    exported, view = _table(rows), _table(view_rows)
    if exported is None or view is None:
        return _out("UNREADABLE", "csv_view_rows_unreadable")
    mismatches = [{"metric": metric, "field": key} for metric in METRICS
                  for key in ("studio", "oracle", "delta", "verdict")
                  if exported[metric][key] != view[metric][key]]
    return _out("FAIL" if mismatches else "PASS", "csv_view_mismatch" if mismatches
                else "csv_view_equal", mismatches=mismatches)


def _index(rows, source):
    if not isinstance(rows, list) or not rows:
        return None, source + "_rows_unreadable"
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            return None, source + "_row_unreadable"
        identifier, count = row.get("string_id"), row.get("optimizers")
        if (type(identifier) is not str or not identifier.strip() or len(identifier) > 128
                or type(count) is not int or not 1 <= count <= 2**53 - 1):
            return None, source + "_row_unreadable"
        if identifier in indexed:
            return None, source + "_duplicate_id"
        indexed[identifier] = count
    return indexed, None


def compare_strings(oracle_rows, saved_rows):
    """Return deterministic, explicit per-string differences, never infer IDs."""
    oracle, error = _index(oracle_rows, "oracle")
    if error:
        return {"verdict": "UNREADABLE", "reason": error}
    saved, error = _index(saved_rows, "saved")
    if error:
        return {"verdict": "UNREADABLE", "reason": error}
    missing = sorted(oracle.keys() - saved.keys())
    foreign = sorted(saved.keys() - oracle.keys())
    mismatches = [{"string_id": key, "oracle": oracle[key], "saved": saved[key]}
                  for key in sorted(oracle.keys() & saved.keys()) if oracle[key] != saved[key]]
    matched = not (missing or foreign or mismatches)
    return {"verdict": "PASS" if matched else "FAIL",
            "reason": "exact_string_identity" if matched else "string_identity_mismatch",
            "missing_ids": missing, "foreign_ids": foreign, "mismatches": mismatches}
