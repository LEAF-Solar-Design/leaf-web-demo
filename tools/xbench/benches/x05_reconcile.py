"""X05 pure offline parity helpers, not wired into live grading.

Both callers supply independent rows: {string_id: str, optimizers: int}.
This module does not establish the origin or independence of either input.
"""

import csv
from datetime import datetime, timezone
from decimal import Context, Decimal, DecimalException, InvalidOperation, localcontext
from io import StringIO
import re
from hashlib import sha256
import json
from contextlib import contextmanager
from heapq import nsmallest
from threading import RLock


METRICS = ("panels", "optimizers", "inverters", "strings", "kw_dc")
TABLE_COLUMNS = ("metric", "studio", "oracle", "delta", "verdict")
_CONTEXT = Context(prec=200)
MAX_CSV_CHARS = 16384
MAX_CSV_CELL = 256
MAX_STRING_EXAMPLES = 20
MAX_RECOMMENDATION_EXAMPLES = 20
MAX_JSON_CHARS = 262144
MAX_JSON_DEPTH = 32
PROVENANCE_STAGES = ("strings", "grouping", "table", "policy", "reopen", "csv_view")
IDENTITY_STAGES = ("saved_strings", "table", "csv", "reopened")
_CSV_LOCK = RLock()


def _out(verdict, reason, **evidence):
    return {"verdict": verdict, "reason": reason, **evidence}


def _fixture_binding(value):
    if type(value) is not dict or set(value) != {"fixture_id", "inputs"}:
        return False
    identifier, inputs = value["fixture_id"], value["inputs"]
    return (type(identifier) is str and bool(identifier.strip()) and len(identifier) <= 128
            and type(inputs) is dict and set(inputs) in ({"dwg", "report"}, {"dwg", "report", "bom"})
            and all(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest)
                    for digest in inputs.values()))


def validate_fixture_provenance(frozen_manifest, stage_bindings):
    """Compare supplied bindings only; never infer provenance from product output."""
    if not _fixture_binding(frozen_manifest):
        return _out("UNREADABLE", "frozen_manifest_unreadable")
    if type(stage_bindings) is not dict or set(stage_bindings) != set(PROVENANCE_STAGES):
        return _out("UNREADABLE", "provenance_stages_unreadable")
    for stage in PROVENANCE_STAGES:
        supplied = stage_bindings[stage]
        if not _fixture_binding(supplied):
            return _out("UNREADABLE", "stage_binding_unreadable", stage=stage)
        if supplied != frozen_manifest:
            return _out("UNREADABLE", "stage_binding_conflict", stage=stage)
    return _out("PASS", "supplied_provenance_equal", stage_count=len(PROVENANCE_STAGES))


def verify_input_bytes(frozen_manifest, supplied_inputs):
    """Hash caller-supplied bytes only, with no file or network access."""
    if not _fixture_binding(frozen_manifest):
        return _out("UNREADABLE", "frozen_manifest_unreadable")
    expected = frozen_manifest["inputs"]
    if type(supplied_inputs) is not dict or set(supplied_inputs) != set(expected):
        return _out("UNREADABLE", "input_bytes_missing")
    roles = sorted(expected)
    for role in roles:
        if type(supplied_inputs[role]) is not bytes:
            return _out("UNREADABLE", "input_bytes_unreadable", role=role)
    conflicts = [role for role in roles if sha256(supplied_inputs[role]).hexdigest() != expected[role]]
    return _out("UNREADABLE" if conflicts else "PASS",
                "input_digest_conflict" if conflicts else "supplied_input_bytes_equal", roles=conflicts)


def validate_saved_design_identity(expected_identity, stage_identities):
    """Compare explicit project/version claims, never infer identity from totals."""
    def valid(value):
        return (type(value) is dict and set(value) == {"project_id", "version_id"}
                and all(type(item) is str and bool(item.strip()) and len(item) <= 128
                        for item in value.values()))

    if not valid(expected_identity):
        return _out("UNREADABLE", "design_identity_unreadable")
    if type(stage_identities) is not dict or set(stage_identities) != set(IDENTITY_STAGES):
        return _out("UNREADABLE", "design_identity_stages_unreadable")
    for stage in IDENTITY_STAGES:
        if not valid(stage_identities[stage]):
            return _out("UNREADABLE", "stage_design_identity_unreadable", stage=stage)
        if stage_identities[stage] != expected_identity:
            return _out("UNREADABLE", "design_identity_conflict", stage=stage)
    return _out("PASS", "supplied_design_identity_equal")


def reconcile_with_provenance(evidence, frozen_manifest, stage_bindings):
    """Keep all offline diagnostics while requiring supplied provenance agreement."""
    provenance = validate_fixture_provenance(frozen_manifest, stage_bindings)
    if provenance["verdict"] == "PASS":
        if type(evidence) is not dict or evidence.get("fixture_id") != frozen_manifest["fixture_id"]:
            provenance = _out("UNREADABLE", "evidence_fixture_identity_conflict")
    reconciliation = reconcile_offline(evidence)
    verdict = "UNREADABLE" if provenance["verdict"] == "UNREADABLE" else reconciliation["verdict"]
    return _out(verdict, "offline_provenance_reconciliation", provenance=provenance,
                reconciliation=reconciliation)


def _numeric(value, *, count=False, signed=False):
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        with localcontext(_CONTEXT):
            if type(value) in (str, int):
                raw = str(value)
                if len(raw) > 80 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", raw):
                    return None
                number = Decimal(raw)
            else:
                number = value if type(value) is Decimal else Decimal(str(value))
                if not number.is_finite():
                    return None
                sign, digits, exponent = number.as_tuple()
                integer_width = 1 if number.is_zero() else max(1, len(digits) + exponent)
                width = sign + integer_width + (1 - exponent if exponent < 0 else 0)
                if width > 80:
                    return None
            if not number.is_finite() or (not signed and number < 0):
                return None
            if count and number != number.to_integral_value():
                return None
            return number
    except (InvalidOperation, ValueError):
        return None


class _DuplicateJSONKey(ValueError):
    pass


class _NonfiniteJSON(ValueError):
    pass


class _NumericJSON(ValueError):
    pass


def decode_evidence_json(text):
    """Strict JSON preflight; success is decoding proof only, never live grading."""
    if type(text) is not str:
        return _out("UNREADABLE", "json_input_unreadable")
    if len(text) > MAX_JSON_CHARS:
        return _out("UNREADABLE", "json_input_too_large")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return _out("UNREADABLE", "json_nesting_too_deep")
        elif char in "}]":
            depth -= 1

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJSONKey()
            result[key] = value
        return result

    def reject_constant(value):
        raise _NonfiniteJSON()

    def integer_token(raw):
        if len(raw) > 80:
            raise _NumericJSON()
        return int(raw)

    def decimal_token(raw):
        if len(raw) > 128:
            raise _NumericJSON()
        try:
            number = Decimal(raw)
        except DecimalException:
            raise _NumericJSON() from None
        if _numeric(number, signed=True) is None:
            raise _NumericJSON()
        return number

    try:
        with localcontext(_CONTEXT):
            evidence = json.loads(text, object_pairs_hook=unique_object,
                                  parse_constant=reject_constant, parse_float=decimal_token,
                                  parse_int=integer_token)
    except _DuplicateJSONKey:
        return _out("UNREADABLE", "json_duplicate_key")
    except _NonfiniteJSON:
        return _out("UNREADABLE", "json_nonfinite_number")
    except _NumericJSON:
        return _out("UNREADABLE", "json_numeric_unreadable")
    except (ValueError, RecursionError, DecimalException, OverflowError):
        return _out("UNREADABLE", "json_malformed")
    if not isinstance(evidence, dict):
        return _out("UNREADABLE", "json_evidence_unreadable")
    return _out("PASS", "json_decoded", evidence=evidence)


def reconcile_json(text):
    decoded = decode_evidence_json(text)
    return reconcile_offline(decoded["evidence"]) if decoded["verdict"] == "PASS" else decoded


def aggregate_fixtures_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    envelope = decoded["evidence"]
    if set(envelope) != {"expected_ids", "fixtures"}:
        return _out("UNREADABLE", "aggregate_envelope_unreadable")
    return aggregate_fixtures(envelope["expected_ids"], envelope["fixtures"])


def serialize_result(value):
    """Emit exact JSON numbers, preserving Decimal values without float casts."""
    def encode(item, depth=0):
        if depth > MAX_JSON_DEPTH or (type(item) in (dict, list) and depth >= MAX_JSON_DEPTH):
            raise ValueError("json_result_too_deep")
        if type(item) is Decimal:
            if not item.is_finite():
                raise ValueError("json_result_nonfinite")
            raw = str(item)
            # Exponent notation preserves even an integral Decimal's signed
            # zero and exponent when the strict decoder uses parse_float.
            return raw if any(char in raw for char in ".eE") else raw + "E+0"
        if type(item) in (str, int, float, bool) or item is None:
            return json.dumps(item, ensure_ascii=True, allow_nan=False)
        if type(item) is list:
            return "[" + ",".join(encode(child, depth + 1) for child in item) + "]"
        if type(item) is dict and all(type(key) is str for key in item):
            return "{" + ",".join(json.dumps(key, ensure_ascii=True) + ":" + encode(item[key], depth + 1)
                                   for key in sorted(item)) + "}"
        raise ValueError("json_result_type_unreadable")

    return encode(value)


def aggregate_fixtures(expected_ids, fixtures):
    """Reconcile exactly three explicitly expected fixture evidence records."""
    def identifier(value):
        return type(value) is str and bool(value.strip()) and len(value) <= 128

    if (not isinstance(expected_ids, (list, tuple, set)) or len(expected_ids) != 3
            or any(not identifier(value) for value in expected_ids)
            or len(set(expected_ids)) != 3):
        return _out("UNREADABLE", "expected_fixtures_unreadable")
    if not isinstance(fixtures, list) or len(fixtures) != 3:
        return _out("UNREADABLE", "fixture_coverage_unreadable")
    indexed = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not identifier(fixture.get("fixture_id")):
            return _out("UNREADABLE", "fixture_id_unreadable")
        key = fixture["fixture_id"]
        if key in indexed:
            return _out("UNREADABLE", "duplicate_fixture_id")
        if key not in expected_ids:
            return _out("UNREADABLE", "foreign_fixture_id")
        evidence = fixture.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("fixture_id") != key:
            return _out("UNREADABLE", "fixture_identity_unbound")
        indexed[key] = evidence
    results = {key: reconcile_offline(indexed[key]) for key in sorted(expected_ids)}
    verdicts = {result["verdict"] for result in results.values()}
    verdict = "UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS"
    return _out(verdict, "offline_fixture_reconciliation", fixtures=results)


def compare_recommendations(report_grid_count, recommendations, *, no_recommendations_shown=False):
    """Check every supplied shown recommendation against an independent count."""
    if type(report_grid_count) is not int or not 1 <= report_grid_count <= 2**53 - 1:
        return _out("UNREADABLE", "report_grid_count_unreadable")
    if not isinstance(recommendations, list):
        return _out("UNREADABLE", "recommendations_unreadable")
    if type(no_recommendations_shown) is not bool:
        return _out("UNREADABLE", "recommendation_flag_unreadable")
    if not recommendations and no_recommendations_shown is not True:
        return _out("UNREADABLE", "recommendation_absence_unverified")
    if recommendations and no_recommendations_shown is True:
        return _out("UNREADABLE", "recommendation_presence_conflict")
    mismatches, mismatch_count = [], 0
    for index, row in enumerate(recommendations):
        count = row.get("group_count") if isinstance(row, dict) else None
        if type(count) is not int or not 1 <= count <= 2**53 - 1:
            return _out("UNREADABLE", "recommendation_count_unreadable")
        if count != report_grid_count:
            mismatch_count += 1
            if len(mismatches) < MAX_RECOMMENDATION_EXAMPLES:
                mismatches.append(index)
    return _out("FAIL" if mismatch_count else "PASS", "grouping_count_mismatch" if mismatch_count
                else "grouping_counts_equal", mismatched_recommendations=mismatches,
                mismatch_count=mismatch_count)


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
        if instant.tzinfo is None or instant.utcoffset() is None:
            return None
        return instant.astimezone(timezone.utc)
    except (ValueError, OverflowError):
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


@contextmanager
def _csv_limit():
    # Serialize this helper's parsers and restore the caller's process setting
    # even when parsing raises or a validation path returns early.
    with _CSV_LOCK:
        previous = csv.field_size_limit()
        try:
            csv.field_size_limit(MAX_CSV_CHARS)
            yield
        finally:
            csv.field_size_limit(previous)


def _csv_rows(csv_text):
    try:
        with _csv_limit():
            reader = csv.reader(StringIO(csv_text.removeprefix("\ufeff"), newline=""), strict=True)
            headers = next(reader)
            if any(len(cell) > MAX_CSV_CELL for cell in headers):
                return None, "csv_cell_too_large"
            if len(headers) != len(TABLE_COLUMNS) or set(headers) != set(TABLE_COLUMNS):
                return None, "csv_headers_unreadable"
            rows = []
            for cells in reader:
                if len(rows) >= len(METRICS):
                    return None, "csv_excess_rows"
                if any(len(cell) > MAX_CSV_CELL for cell in cells):
                    return None, "csv_cell_too_large"
                if len(cells) != len(headers):
                    return None, "csv_row_unreadable"
                rows.append(dict(zip(headers, cells)))
    except (csv.Error, StopIteration):
        return None, "csv_evidence_unreadable"
    return rows, None


def compare_csv_view(csv_text, view_rows):
    """Check normalized CSV/view equality; compare_table checks independent truth."""
    if type(csv_text) is not str:
        return _out("UNREADABLE", "csv_evidence_unreadable")
    if len(csv_text) > MAX_CSV_CHARS:
        return _out("UNREADABLE", "csv_input_too_large")
    rows, error = _csv_rows(csv_text)
    if error:
        return _out("UNREADABLE", error)
    exported, view = _table(rows), _table(view_rows)
    if exported is None or view is None:
        return _out("UNREADABLE", "csv_view_rows_unreadable")
    mismatches = [{"metric": metric, "field": key} for metric in METRICS
                  for key in ("studio", "oracle", "delta", "verdict")
                  if exported[metric][key] != view[metric][key]]
    return _out("FAIL" if mismatches else "PASS", "csv_view_mismatch" if mismatches
                else "csv_view_equal", mismatches=mismatches)


def canonical_snapshot(strings, table_rows):
    """Hash validated normalized identities and values, independent of row order."""
    indexed, error = _index(strings, "snapshot")
    table = _table(table_rows)
    if error or table is None:
        return _out("UNREADABLE", "snapshot_rows_unreadable")

    def digest(value):
        return sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

    def number(value):
        return "0" if value == 0 else format(value.normalize(), "f")

    with localcontext(_CONTEXT):
        string_rows = [(key, indexed[key]) for key in sorted(indexed)]
        values = [(metric, *(number(table[metric][key]) for key in ("studio", "oracle", "delta")),
                   table[metric]["verdict"]) for metric in METRICS]
        return _out("PASS", "canonical_snapshot", strings_sha256=digest(string_rows),
                    table_sha256=digest(values))


def compare_bound_snapshots(before, reopened, before_rows, reopened_rows, saved_strings=None, view_rows=None):
    calculated = []
    for rows in (before_rows, reopened_rows):
        if not isinstance(rows, dict) or not {"strings", "table_rows"} <= rows.keys():
            return _out("UNREADABLE", "snapshot_rows_missing")
        canonical = canonical_snapshot(rows["strings"], rows["table_rows"])
        if canonical["verdict"] != "PASS":
            return canonical
        calculated.append(canonical)
    supplied = compare_snapshots(before, reopened)
    if supplied["verdict"] == "UNREADABLE":
        return supplied
    saved = canonical_snapshot(saved_strings, view_rows)
    if saved["verdict"] != "PASS":
        return _out("UNREADABLE", "snapshot_saved_evidence_unreadable")
    unbound = [phase for phase, claimed, actual in zip(
        ("before", "reopened"), (before, reopened), calculated)
        if compare_snapshots(claimed, actual)["verdict"] != "PASS"]
    if unbound:
        return _out("FAIL", "snapshot_hash_binding_mismatch", unbound_phases=unbound)
    binding = compare_snapshots(saved, calculated[0])
    if binding["verdict"] != "PASS":
        return _out("FAIL", "snapshot_saved_evidence_mismatch", changed_hashes=binding["changed_hashes"])
    return compare_snapshots(*calculated)


def _compare_string_totals(rows, raw_values, source):
    indexed, error = _index(rows, source)
    values = _values(raw_values)
    if error or values is None:
        return _out("UNREADABLE", source + "_string_totals_unreadable")
    totals = {"strings": len(indexed), "optimizers": sum(indexed.values())}
    mismatches = [metric for metric in totals if totals[metric] != values[metric]]
    return _out("FAIL" if mismatches else "PASS", source + "_string_totals_mismatch" if mismatches
                else source + "_string_totals_equal", mismatched_metrics=mismatches, observed=totals)


def compare_saved_string_totals(saved_rows, saved_values):
    return _compare_string_totals(saved_rows, saved_values, "saved")


def compare_oracle_string_totals(oracle_rows, oracle_values):
    return _compare_string_totals(oracle_rows, oracle_values, "oracle")


def reconcile_offline(evidence):
    """Compose all six offline checks; missing evidence takes precedence over FAIL.

    This tests supplied evidence only and is never a live X05 acceptance result.
    Policy deltas are derived from the independent value maps, not a third claim.
    """
    if not isinstance(evidence, dict):
        return _out("UNREADABLE", "reconciliation_evidence_unreadable", stages={})

    def policy(saved, oracle, fixture, records, started):
        saved, oracle = _values(saved), _values(oracle)
        if saved is None or oracle is None:
            return _out("UNREADABLE", "discrepancy_evidence_unreadable")
        with localcontext(_CONTEXT):
            deltas = {metric: saved[metric] - oracle[metric] for metric in METRICS}
        return check_discrepancies(fixture, deltas, records, started)

    def strings(oracle, saved, values, oracle_values):
        identity = compare_strings(oracle, saved)
        totals = compare_saved_string_totals(saved, values)
        oracle_totals = compare_oracle_string_totals(oracle, oracle_values)
        verdicts = {identity["verdict"], totals["verdict"], oracle_totals["verdict"]}
        verdict = "UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS"
        return _out(verdict, "saved_string_stage", identity=identity, totals=totals,
                    oracle_totals=oracle_totals)

    def grouping(count, rows):
        return compare_recommendations(count, rows,
            no_recommendations_shown=evidence.get("no_recommendations_shown", False))

    checks = (
        ("strings", strings, ("oracle_strings", "saved_strings", "saved_values", "oracle_values")),
        ("grouping", grouping, ("report_grid_count", "recommendations")),
        ("table", compare_table, ("view_rows", "saved_values", "oracle_values")),
        ("policy", policy, ("saved_values", "oracle_values", "fixture_id", "discrepancy_records", "run_started_at")),
        ("reopen", compare_bound_snapshots, ("before", "reopened", "before_rows", "reopened_rows",
                                            "saved_strings", "view_rows")),
        ("csv_view", compare_csv_view, ("csv_text", "view_rows")),
    )
    stages = {name: check(*(evidence[key] for key in keys)) if all(key in evidence for key in keys)
              else _out("UNREADABLE", "stage_evidence_missing") for name, check, keys in checks}
    verdicts = {result["verdict"] for result in stages.values()}
    verdict = "UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS"
    return _out(verdict, "offline_reconciliation", stages=stages)


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
    missing = oracle.keys() - saved.keys()
    foreign = saved.keys() - oracle.keys()
    changed = [key for key in oracle.keys() & saved.keys() if oracle[key] != saved[key]]
    mismatches = [{"string_id": key, "oracle": oracle[key], "saved": saved[key]}
                  for key in nsmallest(MAX_STRING_EXAMPLES, changed)]
    matched = not (missing or foreign or changed)
    return {"verdict": "PASS" if matched else "FAIL",
            "reason": "exact_string_identity" if matched else "string_identity_mismatch",
            "missing_ids": nsmallest(MAX_STRING_EXAMPLES, missing),
            "foreign_ids": nsmallest(MAX_STRING_EXAMPLES, foreign), "mismatches": mismatches,
            "counts": {"missing": len(missing), "foreign": len(foreign), "mismatched": len(changed)}}


def main(argv=None):
    """CLI:0 PASS,1 FAIL,4 UNREADABLE,5 output failure without recursive reporting."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Offline X05 evidence comparison; no live acceptance.")
    parser.add_argument("--aggregate", action="store_true", help="read one three-fixture envelope")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(4 * MAX_JSON_CHARS + 1)
        if len(raw) > 4 * MAX_JSON_CHARS:
            result = _out("UNREADABLE", "json_input_too_large")
        else:
            text = raw.decode("utf-8", errors="strict")
            result = aggregate_fixtures_json(text) if args.aggregate else reconcile_json(text)
        output = serialize_result(result)
    except Exception:
        result = _out("UNREADABLE", "cli_evidence_unreadable")
        output = serialize_result(result)
    try:
        sys.stdout.write(output + "\n")
        sys.stdout.flush()
    except (OSError, UnicodeError):
        # A real pipe may fail only on flush. Redirect this process's stdout
        # descriptor so interpreter shutdown cannot retry the failed pipe.
        try:
            import os
            descriptor = sys.stdout.fileno()
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), descriptor)
        except Exception:
            pass
        return 5
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
