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
MAX_INPUT_BYTES = 16384
MAX_ENVELOPE_EXAMPLES = 10
MAX_RESULT_CHARS = 262144
MAX_RESULT_NODES = 100000
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


def reconcile_assured(evidence, frozen_manifest, stage_bindings, supplied_inputs,
                      expected_identity, stage_identities):
    """Compose supplied assurance checks without discarding any diagnostics."""
    checks = dict(provenance=reconcile_with_provenance(evidence, frozen_manifest, stage_bindings),
                  input_bytes=verify_input_bytes(frozen_manifest, supplied_inputs),
                  design_identity=validate_saved_design_identity(expected_identity, stage_identities))
    verdicts = {check["verdict"] for check in checks.values()}
    verdict = "UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS"
    return _out(verdict, "offline_assurance", checks=checks)


def reconcile_provenance_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    envelope = decoded["evidence"]
    if set(envelope) != {"evidence", "manifest", "bindings"}:
        return _out("UNREADABLE", "provenance_envelope_unreadable")
    return reconcile_with_provenance(envelope["evidence"], envelope["manifest"], envelope["bindings"])


def aggregate_provenance(expected_ids, fixtures):
    """Validate coverage first, then preserve every fixture's provenance result."""
    coverage = aggregate_fixtures(expected_ids, fixtures)
    if coverage["reason"] != "offline_fixture_reconciliation":
        return _aggregate_envelope_diagnostics(coverage, expected_ids, fixtures, assured=False)
    indexed = {row["fixture_id"]: row for row in fixtures}
    results = {key: reconcile_with_provenance(indexed[key]["evidence"],
                 indexed[key].get("manifest"), indexed[key].get("bindings")) for key in sorted(expected_ids)}
    verdicts = {result["verdict"] for result in results.values()}
    verdict = "UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS"
    return _out(verdict, "offline_provenance_fixtures", fixtures=results)


def _aggregate_envelope_diagnostics(coverage, expected_ids, fixtures, *, assured, seven=False, bound=False):
    """Retain readable unique siblings and bounded index-only envelope errors."""
    if (type(expected_ids) is not list or len(expected_ids) != 3
            or any(type(key) is not str or not key.strip() or len(key) > 128 for key in expected_ids)
            or len(set(expected_ids)) != 3 or type(fixtures) is not list):
        return coverage
    errors, count, indexed, duplicated, seen = [], 0, {}, set(), set()
    for index, row in enumerate(fixtures):
        key = row.get("fixture_id") if type(row) is dict else None
        reason = None
        if type(key) is not str or key not in expected_ids:
            reason = "fixture_envelope_unreadable"
        elif key in seen:
            duplicated.add(key)
            reason = "duplicate_fixture_id"
        else:
            seen.add(key)
            if type(row.get("evidence")) is not dict or row["evidence"].get("fixture_id") != key:
                reason = "fixture_identity_unbound"
            else:
                indexed[key] = row
        if reason:
            count += 1
            if len(errors) < MAX_ENVELOPE_EXAMPLES:
                errors.append(dict(index=index, reason=reason))
    results = {}
    for key in sorted(indexed.keys() - duplicated):
        row = indexed[key]
        if seven:
            results[key] = _bound_seven_step_record(row) if bound else _seven_step_envelope(row.get("envelope"))
        elif assured:
            results[key] = _assurance_envelope(row, fixture_record=True)
        else:
            results[key] = reconcile_with_provenance(row["evidence"], row.get("manifest"), row.get("bindings"))
    return {**coverage, "envelope_error_count": count, "envelope_errors": errors, "fixtures": results}


def aggregate_assurance(expected_ids, fixtures):
    coverage = aggregate_fixtures(expected_ids, fixtures)
    if coverage["reason"] != "offline_fixture_reconciliation":
        return _aggregate_envelope_diagnostics(coverage, expected_ids, fixtures, assured=True)
    indexed = {row["fixture_id"]: row for row in fixtures}
    results = {key: _assurance_envelope(indexed[key], fixture_record=True) for key in sorted(expected_ids)}
    verdicts = {result["verdict"] for result in results.values()}
    verdict = "UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS"
    return _out(verdict, "offline_assurance_fixtures", fixtures=results)


def aggregate_assurance_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    envelope = decoded["evidence"]
    if set(envelope) != {"expected_ids", "fixtures"}:
        return _out("UNREADABLE", "assurance_aggregate_envelope_unreadable")
    return aggregate_assurance(envelope["expected_ids"], envelope["fixtures"])


def aggregate_provenance_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    envelope = decoded["evidence"]
    if set(envelope) != {"expected_ids", "fixtures"}:
        return _out("UNREADABLE", "provenance_aggregate_envelope_unreadable")
    return aggregate_provenance(envelope["expected_ids"], envelope["fixtures"])


def reconcile_assurance_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    return _assurance_envelope(decoded["evidence"])


def _assurance_envelope(envelope, *, fixture_record=False):
    fields = {"evidence", "manifest", "bindings", "inputs_hex", "identity", "stage_identities"}
    if fixture_record:
        fields.add("fixture_id")
    if type(envelope) is not dict or set(envelope) != fields:
        return _out("UNREADABLE", "assurance_envelope_unreadable")
    if not _fixture_binding(envelope["manifest"]):
        return _out("UNREADABLE", "frozen_manifest_unreadable")
    encoded = envelope["inputs_hex"]
    if type(encoded) is dict and set(encoded) - set(envelope["manifest"]["inputs"]):
        return _out("UNREADABLE", "input_hex_roles_unreadable")
    if (type(encoded) is not dict or any(type(value) is not str or len(value) % 2
            or re.fullmatch(r"[0-9a-fA-F]*", value) is None for value in encoded.values())):
        return _out("UNREADABLE", "input_hex_unreadable")
    if any(len(value) > 2 * MAX_INPUT_BYTES for value in encoded.values()):
        return _out("UNREADABLE", "input_hex_too_large")
    supplied = {key: bytes.fromhex(value) for key, value in encoded.items()}
    return reconcile_assured(envelope["evidence"], envelope["manifest"], envelope["bindings"], supplied,
                             envelope["identity"], envelope["stage_identities"])


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
    used, nodes = 0, 0

    def emit(text):
        nonlocal used
        used += len(text)
        if used > MAX_RESULT_CHARS:
            raise ValueError("json_result_too_large")
        return text

    def encode(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_RESULT_NODES:
            raise ValueError("json_result_too_large")
        if depth > MAX_JSON_DEPTH or (type(item) in (dict, list) and depth >= MAX_JSON_DEPTH):
            raise ValueError("json_result_too_deep")
        if type(item) is Decimal:
            if not item.is_finite():
                raise ValueError("json_result_nonfinite")
            raw = str(item)
            # Exponent notation preserves even an integral Decimal's signed
            # zero and exponent when the strict decoder uses parse_float.
            return emit(raw if any(char in raw for char in ".eE") else raw + "E+0")
        if type(item) in (str, int, float, bool) or item is None:
            if type(item) is str and len(item) > MAX_RESULT_CHARS:
                raise ValueError("json_result_too_large")
            return emit(json.dumps(item, ensure_ascii=True, allow_nan=False))
        if type(item) is list:
            emit("[]" + "," * max(0, min(len(item), MAX_RESULT_CHARS + 1) - 1))
            return "[" + ",".join(encode(child, depth + 1) for child in item) + "]"
        if type(item) is dict and all(type(key) is str for key in item):
            if any(len(key) > MAX_RESULT_CHARS for key in item):
                raise ValueError("json_result_too_large")
            emit("{}" + "," * max(0, min(len(item), MAX_RESULT_CHARS + 1) - 1))
            return "{" + ",".join(emit(json.dumps(key, ensure_ascii=True) + ":") + encode(item[key], depth + 1)
                                   for key in sorted(item)) + "}"
        raise ValueError("json_result_type_unreadable")

    return encode(value)


def compare_grid_correspondence(expected_grid_ids, saved_group_ids, rows, *, require_all_matched=False):
    """Compare explicit independent grid/group identities without geometry inference."""
    def identities(values):
        return (type(values) is list and bool(values)
                and all(type(key) is str and bool(key.strip()) and len(key) <= 128 for key in values)
                and len(set(values)) == len(values))

    if (not identities(expected_grid_ids) or not identities(saved_group_ids)
            or type(rows) is not list or type(require_all_matched) is not bool):
        return _out("UNREADABLE", "grid_evidence_unreadable")
    expected_set, group_set = set(expected_grid_ids), set(saved_group_ids)
    indexed, used, error_counts, count = {}, set(), {}, 0
    def issue(reason):
        nonlocal count
        count += 1
        error_counts[reason] = error_counts.get(reason, 0) + 1
    for row in rows:
        if (type(row) is not dict or set(row) != {"grid_id", "matches", "cause"}
                or type(row["grid_id"]) is not str or not row["grid_id"].strip() or len(row["grid_id"]) > 128
                or type(row["matches"]) is not list
                or any(type(key) is not str or not key.strip() or len(key) > 128 for key in row["matches"])):
            return _out("UNREADABLE", "grid_row_unreadable")
        key, matches, cause = row["grid_id"], row["matches"], row["cause"]
        if key not in expected_set:
            issue("foreign_grid")
        if key in indexed:
            issue("duplicate_grid")
        indexed[key] = row
        if matches:
            if len(matches) != 1 or cause is not None:
                issue("grid_match_cardinality")
            for group in matches:
                if group not in group_set:
                    issue("foreign_group")
                if group in used:
                    issue("duplicate_group_match")
                used.add(group)
        elif cause not in ("SOLAREDGE_GRID_UNMATCHED", "SOLAREDGE_GRID_AMBIGUOUS"):
            return _out("UNREADABLE", "grid_cause_unreadable")
        elif require_all_matched:
            issue("required_grid_unmatched")
    for key in expected_set - indexed.keys():
        issue("missing_grid")
    errors = []
    for reason in sorted(error_counts):
        errors.extend([reason] * min(error_counts[reason], MAX_ENVELOPE_EXAMPLES - len(errors)))
    return _out("FAIL" if count else "PASS", "grid_correspondence_mismatch" if count else "grid_correspondence_equal",
                error_count=count, errors=errors)


def _seven_step_inputs(import_evidence, grid_evidence):
    keys = {"normal_user", "dwg_imported", "report_imported"}
    if type(import_evidence) is not dict or set(import_evidence) != keys or any(
            type(value) is not bool for value in import_evidence.values()):
        imported = _out("UNREADABLE", "import_evidence_unreadable")
    else:
        imported = _out("PASS" if all(import_evidence.values()) else "FAIL", "supplied_import_evidence")
    grid_fields = {"expected_grid_ids", "saved_group_ids", "rows", "require_all_matched"}
    if type(grid_evidence) is not dict or set(grid_evidence) - {"before_string_assignments", "string_assignments", "optimizer_totals", "reopened_string_assignments", "panel_totals", "panel_counts", "before_panel_counts", "reopened_panel_counts", "dc_totals", "dc_power", "before_dc_power", "reopened_dc_power", "string_totals", "inverter_totals"} != grid_fields:
        grids = _out("UNREADABLE", "grid_evidence_unreadable")
    else:
        grids = compare_grid_correspondence(**{key: grid_evidence[key] for key in grid_fields})
    return imported, grids


def reconcile_seven_steps(import_evidence, grid_evidence, assurance):
    """Compose explicit supplied evidence only; never a live bench verdict."""
    imported, grids = _seven_step_inputs(import_evidence, grid_evidence)
    fields = {"evidence", "frozen_manifest", "stage_bindings", "supplied_inputs", "expected_identity", "stage_identities"}
    checked = reconcile_assured(**assurance) if type(assurance) is dict and set(assurance) == fields else _out("UNREADABLE", "assurance_evidence_missing")
    ownership = _seven_step_ownership(grid_evidence, assurance.get("evidence") if type(assurance) is dict else None)
    return _seven_step_result(imported, grids, checked, ownership,
                              _seven_step_grid_totals(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_ownership_reopen(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_grid_snapshot(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_panels(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_panel_snapshot(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_dc(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_dc_snapshot(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_strings(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_inverters(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_inverter_snapshot(grid_evidence, assurance.get("evidence")
                                  if type(assurance) is dict else None),
                              _seven_step_assignments(assurance.get("evidence") if type(assurance) is dict else None),
                              _seven_step_assignment_snapshot(assurance.get("evidence") if type(assurance) is dict else None),
                              _seven_step_inverter_loads(grid_evidence, assurance.get("evidence") if type(assurance) is dict else None),
                              _seven_step_inverter_load_reopen(grid_evidence, assurance.get("evidence") if type(assurance) is dict else None),
                              _seven_step_string_metrics(grid_evidence, assurance.get("evidence") if type(assurance) is dict else None),
                              _seven_step_cross_topology(grid_evidence, assurance.get("evidence") if type(assurance) is dict else None),
                              _seven_step_independent_owners(grid_evidence, assurance.get("evidence") if type(assurance) is dict else None),
                              _seven_step_oracle_loads(grid_evidence, assurance.get("evidence") if type(assurance) is dict else None))


def _seven_step_ownership(grid_evidence, evidence):
    if type(grid_evidence) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "ownership_evidence_unreadable")
    saved, error = _index(evidence.get("saved_strings"), "saved")
    if error:
        return _out("UNREADABLE", error)
    return validate_grid_string_ownership(grid_evidence.get("expected_grid_ids"), list(saved),
                                          grid_evidence.get("string_assignments"))


def _seven_step_grid_totals(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_optimizer_evidence_unreadable")
    comparison = compare_grid_optimizer_totals(grid.get("optimizer_totals"), evidence.get("saved_strings"),
                                               grid.get("string_assignments"))
    consistency = compare_grid_oracle_totals(grid.get("expected_grid_ids"), grid.get("optimizer_totals"),
                                             evidence.get("oracle_values"))
    verdicts = {comparison["verdict"], consistency["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_grid_optimizer_evidence", comparison=comparison, oracle_consistency=consistency)


def _seven_step_ownership_reopen(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "ownership_snapshot_evidence_unreadable")
    before, reopened = evidence.get("before_rows"), evidence.get("reopened_rows")
    return compare_bound_ownership_snapshots(grid.get("expected_grid_ids"),
        before.get("strings") if type(before) is dict else None,
        reopened.get("strings") if type(reopened) is dict else None,
        grid.get("string_assignments"), grid.get("reopened_string_assignments"))


def _seven_step_grid_snapshot(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_optimizer_snapshot_evidence_unreadable")
    before, reopened = evidence.get("before_rows"), evidence.get("reopened_rows")
    return compare_grid_optimizer_snapshots(grid.get("expected_grid_ids"),
        before.get("strings") if type(before) is dict else None, grid.get("string_assignments"),
        reopened.get("strings") if type(reopened) is dict else None, grid.get("reopened_string_assignments"))


def _seven_step_panels(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_panel_evidence_unreadable")
    comparison = compare_grid_panel_totals(grid.get("panel_totals"), grid.get("panel_counts"),
                                            grid.get("string_assignments"))
    consistency = compare_grid_panel_oracle_totals(grid.get("expected_grid_ids"), grid.get("panel_totals"),
                                                   evidence.get("oracle_values"))
    binding = compare_panel_string_ids(evidence.get("saved_strings"), grid.get("panel_counts"))
    verdicts = {comparison["verdict"], consistency["verdict"], binding["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_grid_panel_evidence", comparison=comparison, oracle_consistency=consistency,
                string_binding=binding)


def _seven_step_panel_snapshot(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_panel_snapshot_evidence_unreadable")
    before, reopened = evidence.get("before_rows"), evidence.get("reopened_rows")
    snapshot = compare_bound_panel_snapshots(grid.get("expected_grid_ids"),
        before.get("strings") if type(before) is dict else None,
        reopened.get("strings") if type(reopened) is dict else None,
        grid.get("before_panel_counts"), grid.get("string_assignments"),
        grid.get("reopened_panel_counts"), grid.get("reopened_string_assignments"))

    return _with_saved_values(snapshot, compare_before_panel_counts(grid.get("panel_counts"), grid.get("before_panel_counts")))


def _seven_step_dc(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_dc_evidence_unreadable")
    comparison = compare_grid_dc_totals(grid.get("dc_totals"), grid.get("dc_power"),
                                        grid.get("string_assignments"))
    consistency = compare_grid_dc_oracle_totals(grid.get("expected_grid_ids"), grid.get("dc_totals"),
                                               evidence.get("oracle_values"))
    binding = compare_dc_string_ids(evidence.get("saved_strings"), grid.get("dc_power"))
    verdicts = {comparison["verdict"], consistency["verdict"], binding["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_grid_dc_evidence", comparison=comparison, oracle_consistency=consistency,
                string_binding=binding)


def _seven_step_dc_snapshot(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_dc_snapshot_evidence_unreadable")
    before, reopened = evidence.get("before_rows"), evidence.get("reopened_rows")
    snapshot = compare_bound_dc_snapshots(grid.get("expected_grid_ids"),
        before.get("strings") if type(before) is dict else None,
        reopened.get("strings") if type(reopened) is dict else None,
        grid.get("before_dc_power"), grid.get("string_assignments"),
        grid.get("reopened_dc_power"), grid.get("reopened_string_assignments"))

    return _with_saved_values(snapshot, compare_before_dc_values(grid.get("dc_power"), grid.get("before_dc_power")))


def _seven_step_strings(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_string_evidence_unreadable")
    saved, error = _index(evidence.get("saved_strings"), "saved")
    comparison = (_out("UNREADABLE", error) if error else compare_grid_string_totals(
        grid.get("string_totals"), list(saved), grid.get("string_assignments")))
    consistency = compare_grid_string_oracle_totals(grid.get("expected_grid_ids"), grid.get("string_totals"),
                                                   evidence.get("oracle_values"))
    verdicts = {comparison["verdict"], consistency["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_grid_string_evidence", comparison=comparison, oracle_consistency=consistency)


def _inverter_ids(rows):
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        return None
    return [row.get("inverter_id") for row in rows]


def _seven_step_inverters(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "grid_inverter_evidence_unreadable")
    rows = evidence.get("saved_inverters")
    ids = _inverter_ids(rows)
    ownership = validate_grid_inverter_ownership(grid.get("expected_grid_ids"), ids, rows)
    comparison = compare_grid_inverter_totals(grid.get("inverter_totals"), ids, rows)
    consistency = compare_grid_inverter_oracle_totals(grid.get("expected_grid_ids"), grid.get("inverter_totals"),
                                                     evidence.get("oracle_values"))
    verdicts = {ownership["verdict"], comparison["verdict"], consistency["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_grid_inverter_evidence", ownership=ownership, comparison=comparison,
                oracle_consistency=consistency)


def _seven_step_inverter_snapshot(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "inverter_snapshot_evidence_unreadable")
    saved, before, reopened = (evidence.get(key) for key in ("saved_inverters", "before_inverters", "reopened_inverters"))
    snapshot = compare_inverter_snapshots(grid.get("expected_grid_ids"), _inverter_ids(before), before,
                                          _inverter_ids(reopened), reopened)
    binding = compare_inverter_snapshots(grid.get("expected_grid_ids"), _inverter_ids(saved), saved,
                                         _inverter_ids(before), before)
    return _with_saved_values(snapshot, binding)


def _assignment_ids(evidence, stage):
    rows = evidence.get("saved_strings") if stage == "saved" else evidence.get(stage + "_rows", {})
    if stage != "saved":
        rows = rows.get("strings") if type(rows) is dict else None
    indexed, error = _index(rows, stage)
    return (None if error else list(indexed)), _inverter_ids(evidence.get(stage + "_inverters"))


def _seven_step_assignments(evidence):
    if type(evidence) is not dict:
        return _out("UNREADABLE", "assignment_evidence_unreadable")
    strings, inverters = _assignment_ids(evidence, "saved")
    return compare_string_inverter_assignments(inverters, strings,
        evidence.get("oracle_assignments"), evidence.get("saved_assignments"))


def _seven_step_assignment_snapshot(evidence):
    if type(evidence) is not dict:
        return _out("UNREADABLE", "assignment_snapshot_evidence_unreadable")
    states = {}
    ids = {}
    for stage in ("saved", "before", "reopened"):
        strings, inverters = ids[stage] = _assignment_ids(evidence, stage)
        states[stage] = validate_string_inverter_assignments(inverters, strings, evidence.get(stage + "_assignments"))
    strings, inverters = ids["saved"]
    binding = compare_string_inverter_assignments(inverters, strings,
        evidence.get("saved_assignments"), evidence.get("before_assignments"))
    strings, inverters = ids["before"]
    comparison = compare_string_inverter_snapshots(inverters, strings,
        evidence.get("before_assignments"), evidence.get("reopened_assignments"))
    verdicts = {value["verdict"] for value in states.values()} | {binding["verdict"], comparison["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
        "supplied_assignment_snapshots", states=states, saved_binding=binding, comparison=comparison)


def _seven_step_inverter_loads(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "inverter_load_evidence_unreadable")
    supplied = evidence.get("inverter_loads")
    loads = supplied if type(supplied) is dict else {}
    strings, inverters = _assignment_ids(evidence, "saved")
    owners = evidence.get("saved_assignments")
    comparisons = {
        "strings": compare_inverter_string_totals(loads.get("strings"), strings, owners),
        "optimizers": compare_inverter_optimizer_totals(loads.get("optimizers"), evidence.get("saved_strings"), owners),
        "panels": compare_inverter_panel_totals(loads.get("panels"), strings, grid.get("panel_counts"), owners),
        "kw_dc": compare_inverter_dc_totals(loads.get("kw_dc"), strings, grid.get("dc_power"), owners)}
    checks = {metric: dict(comparison=value, oracle_consistency=compare_inverter_metric_oracle_totals(
        inverters, loads.get(metric), evidence.get("oracle_values"), metric)) for metric, value in comparisons.items()}
    verdicts = {value["verdict"] for check in checks.values() for value in check.values()}
    if type(supplied) is not dict or set(supplied) != set(comparisons):
        verdicts.add("UNREADABLE")
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_inverter_loads", metrics=checks)


def _inverter_snapshot_loads(grid, evidence, stage, metric):
    strings, inverters = _assignment_ids(evidence, stage)
    owners = evidence.get(stage + "_assignments")
    ownership = validate_string_inverter_assignments(inverters, strings, owners)
    if ownership["verdict"] != "PASS":
        return None, ownership
    if metric == "strings":
        values = dict.fromkeys(strings, 1)
    else:
        if metric == "optimizers":
            rows = evidence.get("saved_strings") if stage == "saved" else evidence.get(stage + "_rows", {})
            rows = rows.get("strings") if stage != "saved" and type(rows) is dict else rows
        else:
            field = "panel_counts" if metric == "panels" else "dc_power"
            rows = grid.get((stage + "_" if stage != "saved" else "") + field)
        values, error = _index(rows, stage + "_" + metric, metric=metric)
        if error:
            return None, _out("UNREADABLE", error)
        if set(values) != set(strings):
            return None, _out("UNREADABLE", "inverter_snapshot_string_identity_conflict")
    totals = dict.fromkeys(inverters, 0)
    for row in owners:
        totals[row["inverter_id"]] += values[row["string_id"]]
    return totals, _out("PASS", "inverter_snapshot_loads_readable")


def _compare_inverter_load_maps(before, after):
    added, removed = after.keys() - before.keys(), before.keys() - after.keys()
    changed = [key for key in before.keys() & after.keys() if before[key] != after[key]]
    failed = bool(added or removed or changed)
    return _out("FAIL" if failed else "PASS", "inverter_loads_changed" if failed else "inverter_loads_equal",
        counts=dict(added=len(added), removed=len(removed), changed=len(changed)),
        added_ids=nsmallest(MAX_STRING_EXAMPLES, added), removed_ids=nsmallest(MAX_STRING_EXAMPLES, removed),
        examples=[dict(inverter_id=key, before=before[key], reopened=after[key], delta=after[key]-before[key])
                  for key in nsmallest(MAX_STRING_EXAMPLES, changed)])


def _seven_step_inverter_load_reopen(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "inverter_load_snapshot_evidence_unreadable")
    checks = {}
    with localcontext(_CONTEXT):
        for metric in ("strings", "optimizers", "panels", "kw_dc"):
            states, totals = {}, {}
            for stage in ("saved", "before", "reopened"):
                totals[stage], states[stage] = _inverter_snapshot_loads(grid, evidence, stage, metric)
            binding = (_compare_inverter_load_maps(totals["saved"], totals["before"])
                       if totals["saved"] is not None and totals["before"] is not None
                       else _out("UNREADABLE" if "UNREADABLE" in {states["saved"]["verdict"], states["before"]["verdict"]}
                                 else "FAIL", "inverter_load_binding_invalid"))
            comparison = (_compare_inverter_load_maps(totals["before"], totals["reopened"])
                          if totals["before"] is not None and totals["reopened"] is not None
                          else _out("UNREADABLE" if "UNREADABLE" in {states["before"]["verdict"], states["reopened"]["verdict"]}
                                    else "FAIL", "inverter_load_reopen_invalid"))
            checks[metric] = dict(states=states, saved_binding=binding, comparison=comparison)
    verdicts = {value["verdict"] for check in checks.values()
                for value in list(check["states"].values()) + [check["saved_binding"], check["comparison"]]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_inverter_load_snapshots", metrics=checks)


def compare_string_metric_oracle_total(saved_string_ids, oracle_rows, oracle_values, metric):
    """Require explicit oracle string coverage and agreement with the report total."""
    if metric not in ("panels", "kw_dc"):
        return _out("UNREADABLE", "string_metric_unreadable")
    indexed, error = _index(oracle_rows, "oracle", metric=metric)
    if error:
        return _out("UNREADABLE", error)
    with localcontext(_CONTEXT):
        result = _compare_grid_oracle_totals(saved_string_ids, indexed, oracle_values,
                                             metric, "string_" + metric + "_oracle")
    if "grid_total" in result:
        result["string_total"] = result.pop("grid_total")
    return result


def _string_metric_snapshots(grid, evidence, metric, field, compare):
    """Bind and compare explicit per-string metric values across save and reopen."""
    bind = compare_panel_string_ids if metric == "panels" else compare_dc_string_ids
    bindings = {}
    for stage in ("before", "reopened"):
        snapshot = evidence.get(stage + "_rows")
        strings = snapshot.get("strings") if type(snapshot) is dict else None
        bindings[stage] = bind(strings, grid.get(stage + "_" + field))
    saved_binding = compare(grid.get(field), grid.get("before_" + field))
    comparison = compare(grid.get("before_" + field), grid.get("reopened_" + field))
    verdicts = {check["verdict"] for check in bindings.values()} | {saved_binding["verdict"], comparison["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_string_" + metric + "_snapshots", bindings=bindings,
                saved_binding=saved_binding, comparison=comparison)


def _seven_step_string_metrics(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "string_metric_evidence_unreadable")
    saved, error = _index(evidence.get("saved_strings"), "saved")
    ids = None if error else list(saved)
    checks = {}
    for metric, field, oracle_field, compare in (
            ("panels", "panel_counts", "oracle_panels", compare_string_panel_counts),
            ("kw_dc", "dc_power", "oracle_dc_power", compare_string_dc_power)):
        oracle = evidence.get(oracle_field)
        checks[metric] = dict(comparison=compare(oracle, grid.get(field)),
            oracle_consistency=compare_string_metric_oracle_total(ids, oracle, evidence.get("oracle_values"), metric),
            snapshots=_string_metric_snapshots(grid, evidence, metric, field, compare))
    verdicts = {value["verdict"] for check in checks.values() for value in check.values()}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_independent_string_metrics", metrics=checks)


def _seven_step_cross_topology(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "cross_topology_evidence_unreadable")
    checks = {}
    for stage in ("saved", "before", "reopened"):
        strings, inverters = _assignment_ids(evidence, stage)
        field = "string_assignments" if stage == "saved" else stage + "_string_assignments"
        checks[stage] = validate_cross_topology(grid.get("expected_grid_ids"), strings, inverters,
            grid.get(field), evidence.get(stage + "_inverters"), evidence.get(stage + "_assignments"))
    verdicts = {check["verdict"] for check in checks.values()}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_cross_topology", checks=checks)


def _seven_step_independent_owners(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "independent_owner_evidence_unreadable")
    strings, saved_devices = _assignment_ids(evidence, "saved")
    oracle_devices = evidence.get("oracle_inverter_ids")
    grids = grid.get("expected_grid_ids")
    oracle_strings, error = _index(evidence.get("oracle_strings"), "oracle")
    oracle_topology = validate_cross_topology(grids, None if error else list(oracle_strings), oracle_devices,
        evidence.get("oracle_string_owners"), evidence.get("oracle_inverter_owners"), evidence.get("oracle_assignments"))
    checks = dict(oracle_topology=oracle_topology,
                  strings=compare_string_grid_owners(grids, strings, evidence.get("oracle_string_owners"),
                                                    grid.get("string_assignments")),
                  inverters=compare_inverter_grid_owners(grids, oracle_devices, evidence.get("oracle_inverter_owners"),
                                                        evidence.get("saved_inverters")),
                  inverter_ids=compare_inverter_id_sets(oracle_devices, saved_devices))
    verdicts = {check["verdict"] for check in checks.values()}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_independent_owners", checks=checks)


def _seven_step_oracle_loads(grid, evidence):
    if type(grid) is not dict or type(evidence) is not dict:
        return _out("UNREADABLE", "oracle_load_evidence_unreadable")
    strings = evidence.get("oracle_strings")
    indexed, error = _index(strings, "oracle")
    ids = None if error else list(indexed)
    panels, power = evidence.get("oracle_panels"), evidence.get("oracle_dc_power")
    owners, connections = evidence.get("oracle_string_owners"), evidence.get("oracle_assignments")
    supplied = evidence.get("inverter_loads")
    loads = supplied if type(supplied) is dict else {}
    grids, devices, report = grid.get("expected_grid_ids"), evidence.get("oracle_inverter_ids"), evidence.get("oracle_values")
    bindings = dict(owners=validate_grid_string_ownership(grids, ids, owners),
        connections=validate_string_inverter_assignments(devices, ids, connections),
        panels=compare_panel_string_ids(strings, panels), kw_dc=compare_dc_string_ids(strings, power))
    with localcontext(_CONTEXT):
        grid_checks = {
            "strings": compare_grid_string_totals(grid.get("string_totals"), ids, owners),
            "optimizers": compare_grid_optimizer_totals(grid.get("optimizer_totals"), strings, owners),
            "panels": compare_grid_panel_totals(grid.get("panel_totals"), panels, owners),
            "kw_dc": compare_grid_dc_totals(grid.get("dc_totals"), power, owners)}
        inverter_checks = {
            "strings": compare_inverter_string_totals(loads.get("strings"), ids, connections),
            "optimizers": compare_inverter_optimizer_totals(loads.get("optimizers"), strings, connections),
            "panels": compare_inverter_panel_totals(loads.get("panels"), ids, panels, connections),
            "kw_dc": compare_inverter_dc_totals(loads.get("kw_dc"), ids, power, connections)}
        fields = dict(strings="string_totals", optimizers="optimizer_totals", panels="panel_totals", kw_dc="dc_totals")
        metrics = {metric: dict(grid_comparison=grid_checks[metric], inverter_comparison=inverter_checks[metric],
            grid_report=_compare_grid_oracle_totals(grids, grid.get(fields[metric]), report, metric, "oracle_grid_" + metric),
            inverter_report=compare_inverter_metric_oracle_totals(devices, loads.get(metric), report, metric))
            for metric in fields}
    verdicts = {check["verdict"] for check in bindings.values()} | {
        check["verdict"] for checks in metrics.values() for check in checks.values()}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_oracle_load_consistency", bindings=bindings, metrics=metrics)


def _seven_step_result(imported, grids, checked, ownership, grid_optimizers, ownership_reopen, grid_optimizer_reopen, grid_panels, grid_panel_reopen, grid_dc, grid_dc_reopen, grid_strings, grid_inverters, inverter_reopen, assignments, assignment_reopen, inverter_loads, inverter_load_reopen, string_metrics, cross_topology, independent_owners, oracle_loads):
    stages = checked.get("checks", {}).get("provenance", {}).get("reconciliation", {}).get("stages", {})
    absent = _out("UNREADABLE", "stage_evidence_missing")
    steps = {"1": imported, "2": grids, "3": stages.get("strings", absent), "4": stages.get("grouping", absent),
             "5": dict(table=stages.get("table", absent), csv_view=stages.get("csv_view", absent)),
             "6": stages.get("policy", absent), "7": stages.get("reopen", absent)}
    verdicts = {imported["verdict"], grids["verdict"], checked["verdict"], ownership["verdict"], grid_optimizers["verdict"], ownership_reopen["verdict"], grid_optimizer_reopen["verdict"], grid_panels["verdict"], grid_panel_reopen["verdict"], grid_dc["verdict"], grid_dc_reopen["verdict"], grid_strings["verdict"], grid_inverters["verdict"], inverter_reopen["verdict"], assignments["verdict"], assignment_reopen["verdict"], inverter_loads["verdict"], inverter_load_reopen["verdict"], string_metrics["verdict"], cross_topology["verdict"], independent_owners["verdict"], oracle_loads["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "offline_seven_steps", steps=steps, assurance=checked, ownership=ownership, grid_optimizers=grid_optimizers,
                ownership_reopen=ownership_reopen, grid_optimizer_reopen=grid_optimizer_reopen, grid_panels=grid_panels,
                grid_panel_reopen=grid_panel_reopen, grid_dc=grid_dc, grid_dc_reopen=grid_dc_reopen, grid_strings=grid_strings,
                grid_inverters=grid_inverters, inverter_reopen=inverter_reopen,
                assignments=assignments, assignment_reopen=assignment_reopen,
                inverter_loads=inverter_loads, inverter_load_reopen=inverter_load_reopen, string_metrics=string_metrics, cross_topology=cross_topology, independent_owners=independent_owners, oracle_loads=oracle_loads)


def reconcile_seven_steps_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    return _seven_step_envelope(decoded["evidence"])


def _seven_step_envelope(envelope):
    if type(envelope) is not dict or set(envelope) != {"import", "grid", "assurance"}:
        return _out("UNREADABLE", "seven_step_envelope_unreadable")
    imported, grids = _seven_step_inputs(envelope["import"], envelope["grid"])
    return _seven_step_result(imported, grids,
                              _assurance_envelope(envelope["assurance"]),
                              _seven_step_ownership(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_grid_totals(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_ownership_reopen(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_grid_snapshot(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_panels(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_panel_snapshot(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_dc(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_dc_snapshot(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_strings(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_inverters(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_inverter_snapshot(envelope["grid"], envelope["assurance"].get("evidence")
                                  if type(envelope["assurance"]) is dict else None),
                              _seven_step_assignments(envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None),
                              _seven_step_assignment_snapshot(envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None),
                              _seven_step_inverter_loads(envelope["grid"], envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None),
                              _seven_step_inverter_load_reopen(envelope["grid"], envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None),
                              _seven_step_string_metrics(envelope["grid"], envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None),
                              _seven_step_cross_topology(envelope["grid"], envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None),
                              _seven_step_independent_owners(envelope["grid"], envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None),
                              _seven_step_oracle_loads(envelope["grid"], envelope["assurance"].get("evidence") if type(envelope["assurance"]) is dict else None))


def validate_import_grid_provenance(frozen_manifest, import_binding, grid_binding):
    """Compare supplied identity/digest claims only, without acquiring evidence."""
    if not _fixture_binding(frozen_manifest):
        return _out("UNREADABLE", "frozen_manifest_unreadable")
    for stage, binding in (("import", import_binding), ("grid", grid_binding)):
        if not _fixture_binding(binding):
            return _out("UNREADABLE", "import_grid_binding_unreadable", stage=stage)
        if binding != frozen_manifest:
            return _out("UNREADABLE", "import_grid_binding_conflict", stage=stage)
    return _out("PASS", "supplied_import_grid_provenance_equal")


def aggregate_seven_steps(expected_ids, fixtures):
    """Retain seven-step diagnostics for exactly three explicitly bound fixtures."""
    return _aggregate_seven_step_records(expected_ids, fixtures, bound=False)


def _aggregate_seven_step_records(expected_ids, fixtures, *, bound):
    if type(fixtures) is not list:
        return _out("UNREADABLE", "fixture_coverage_unreadable")
    proxies = []
    for row in fixtures:
        key = row.get("fixture_id") if type(row) is dict else None
        envelope = row.get("envelope") if type(row) is dict else None
        assurance = envelope.get("assurance") if type(envelope) is dict else None
        evidence = assurance.get("evidence") if type(assurance) is dict else None
        proxy = dict(row) if type(row) is dict else {}
        proxy.update(fixture_id=key, evidence=evidence, envelope=envelope)
        proxies.append(proxy)
    coverage, _ = _index_fixture_records(expected_ids, proxies)
    if coverage is not None:
        return _aggregate_envelope_diagnostics(coverage, expected_ids, proxies, assured=False, seven=True, bound=bound)
    indexed = {row["fixture_id"]: row for row in fixtures}
    results = {key: _bound_seven_step_record(indexed[key]) if bound else _seven_step_envelope(indexed[key]["envelope"])
               for key in sorted(expected_ids)}
    verdicts = {result["verdict"] for result in results.values()}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "offline_bound_seven_step_fixtures" if bound else "offline_seven_step_fixtures", fixtures=results)


def aggregate_seven_steps_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    envelope = decoded["evidence"]
    if set(envelope) != {"expected_ids", "fixtures"}:
        return _out("UNREADABLE", "seven_step_aggregate_envelope_unreadable")
    return aggregate_seven_steps(envelope["expected_ids"], envelope["fixtures"])


def reconcile_bound_seven_steps(envelope, frozen_manifest, import_binding, grid_binding):
    provenance = validate_import_grid_provenance(frozen_manifest, import_binding, grid_binding)
    reconciliation = _seven_step_envelope(envelope)
    if provenance["verdict"] == "PASS":
        assurance = envelope.get("assurance") if type(envelope) is dict else None
        supplied = assurance.get("manifest") if type(assurance) is dict else None
        if supplied != frozen_manifest:
            provenance = _out("UNREADABLE", "seven_step_manifest_conflict")
    verdict = "UNREADABLE" if provenance["verdict"] == "UNREADABLE" else reconciliation["verdict"]
    return _out(verdict, "offline_bound_seven_steps", provenance=provenance, reconciliation=reconciliation)


def reconcile_bound_seven_steps_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    return _bound_seven_step_envelope(decoded["evidence"])


def _bound_seven_step_envelope(envelope):
    if type(envelope) is not dict or set(envelope) != {"envelope", "manifest", "import_binding", "grid_binding"}:
        evidence = envelope.get("envelope") if type(envelope) is dict else None
        return _out("UNREADABLE", "bound_seven_step_envelope_unreadable", reconciliation=_seven_step_envelope(evidence))
    return reconcile_bound_seven_steps(envelope["envelope"], envelope["manifest"],
                                      envelope["import_binding"], envelope["grid_binding"])


def aggregate_bound_seven_steps(expected_ids, fixtures):
    return _aggregate_seven_step_records(expected_ids, fixtures, bound=True)


def _bound_seven_step_record(row):
    supplied = {name: row.get(name) for name in ("envelope", "manifest", "import_binding", "grid_binding")}
    return _bound_seven_step_envelope(supplied)


def aggregate_bound_seven_steps_json(text):
    decoded = decode_evidence_json(text)
    if decoded["verdict"] != "PASS":
        return decoded
    envelope = decoded["evidence"]
    if set(envelope) != {"expected_ids", "fixtures"}:
        return _out("UNREADABLE", "bound_seven_step_aggregate_envelope_unreadable")
    return aggregate_bound_seven_steps(envelope["expected_ids"], envelope["fixtures"])


def _index_fixture_records(expected_ids, fixtures):
    def identifier(value):
        return type(value) is str and bool(value.strip()) and len(value) <= 128

    if (not isinstance(expected_ids, (list, tuple, set)) or len(expected_ids) != 3
            or any(not identifier(value) for value in expected_ids)
            or len(set(expected_ids)) != 3):
        return _out("UNREADABLE", "expected_fixtures_unreadable"), None
    if not isinstance(fixtures, list) or len(fixtures) != 3:
        return _out("UNREADABLE", "fixture_coverage_unreadable"), None
    indexed = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not identifier(fixture.get("fixture_id")):
            return _out("UNREADABLE", "fixture_id_unreadable"), None
        key = fixture["fixture_id"]
        if key in indexed:
            return _out("UNREADABLE", "duplicate_fixture_id"), None
        if key not in expected_ids:
            return _out("UNREADABLE", "foreign_fixture_id"), None
        evidence = fixture.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("fixture_id") != key:
            return _out("UNREADABLE", "fixture_identity_unbound"), None
        indexed[key] = evidence
    return None, indexed


def aggregate_fixtures(expected_ids, fixtures):
    """Reconcile exactly three explicitly expected fixture evidence records."""
    coverage, indexed = _index_fixture_records(expected_ids, fixtures)
    if coverage is not None:
        return coverage
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


def diff_snapshot_rows(before, reopened):
    """Localize supplied string/count and table changes with bounded examples."""
    parsed = []
    for snapshot in (before, reopened):
        if type(snapshot) is not dict or set(snapshot) != {"strings", "table_rows"}:
            return _out("UNREADABLE", "snapshot_diff_rows_unreadable")
        strings, error = _index(snapshot["strings"], "snapshot")
        table = _table(snapshot["table_rows"])
        if error or table is None:
            return _out("UNREADABLE", "snapshot_diff_rows_unreadable")
        parsed.append((strings, table))
    previous, current = parsed[0][0], parsed[1][0]
    added, removed = current.keys() - previous.keys(), previous.keys() - current.keys()
    changed = {key for key in previous.keys() & current.keys() if previous[key] != current[key]}
    examples = [dict(string_id=key, before=previous.get(key), reopened=current.get(key))
                for key in nsmallest(MAX_STRING_EXAMPLES, added | removed | changed)]
    metrics = [metric for metric in METRICS if parsed[0][1][metric] != parsed[1][1][metric]]
    return _out("FAIL" if examples or metrics else "PASS", "snapshot_rows_changed" if examples or metrics
                else "snapshot_rows_equal", counts=dict(added=len(added), removed=len(removed),
                changed=len(changed), metrics=len(metrics)), string_examples=examples, metrics=metrics)


def validate_grid_string_ownership(expected_grid_ids, saved_string_ids, assignments):
    """Require every supplied saved string ID to have one explicit expected owner."""
    return _validate_grid_ownership(expected_grid_ids, saved_string_ids, assignments, "string_id", "string")


def validate_grid_inverter_ownership(expected_grid_ids, saved_inverter_ids, assignments):
    """Require unique explicit inverter IDs to have exactly one expected grid owner."""
    return _validate_grid_ownership(expected_grid_ids, saved_inverter_ids, assignments, "inverter_id", "inverter")


def validate_string_inverter_assignments(inverter_ids, saved_string_ids, assignments):
    """Require each saved string to name exactly one explicitly existing inverter."""
    result = _validate_grid_ownership(inverter_ids, saved_string_ids, assignments,
                                     "string_id", "string", owner_key="inverter_id")
    reason = {"ownership_evidence_unreadable": "string_inverter_evidence_unreadable",
              "ownership_row_unreadable": "string_inverter_row_unreadable",
              "string_ownership_equal": "string_inverter_assignments_equal",
              "string_ownership_mismatch": "string_inverter_assignments_mismatch"}
    return dict(result, reason=reason[result["reason"]])


def compare_string_inverter_assignments(inverter_ids, saved_string_ids, oracle_assignments, saved_assignments):
    """Compare explicit independent connections, without inferring them from counts."""
    oracle = validate_string_inverter_assignments(inverter_ids, saved_string_ids, oracle_assignments)
    saved = validate_string_inverter_assignments(inverter_ids, saved_string_ids, saved_assignments)
    verdicts = {oracle["verdict"], saved["verdict"]}
    if verdicts != {"PASS"}:
        return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL", "string_inverter_comparison_invalid",
                    oracle=oracle, saved=saved)
    old = {row["string_id"]: row["inverter_id"] for row in oracle_assignments}
    new = {row["string_id"]: row["inverter_id"] for row in saved_assignments}
    changed = [key for key in old if old[key] != new[key]]
    return _out("FAIL" if changed else "PASS",
                "string_inverter_assignments_changed" if changed else "string_inverter_assignments_equal",
                changed_count=len(changed), examples=[dict(string_id=key, oracle=old[key], saved=new[key])
                    for key in nsmallest(MAX_STRING_EXAMPLES, changed)])


def compare_string_inverter_snapshots(inverter_ids, saved_string_ids, before_assignments, reopened_assignments):
    """Compare connections for fixed explicit identities across save and reopen."""
    result = compare_string_inverter_assignments(inverter_ids, saved_string_ids,
                                                 before_assignments, reopened_assignments)
    if "oracle" in result:
        return _out(result["verdict"], "string_inverter_snapshots_invalid",
                    before=result["oracle"], reopened=result["saved"])
    return _out(result["verdict"], "string_inverter_snapshots_changed" if result["changed_count"] else
                "string_inverter_snapshots_equal", changed_count=result["changed_count"],
                examples=[dict(string_id=row["string_id"], before=row["oracle"], reopened=row["saved"])
                          for row in result["examples"]])


def compare_string_grid_owners(expected_grid_ids, string_ids, oracle_owners, saved_owners):
    """Compare independent explicit string grid owners, including balanced swaps."""
    return _compare_independent_grid_owners(expected_grid_ids, string_ids, oracle_owners, saved_owners, "string")


def compare_inverter_grid_owners(expected_grid_ids, inverter_ids, oracle_owners, saved_owners):
    """Compare independent explicit inverter grid owners, including balanced swaps."""
    return _compare_independent_grid_owners(expected_grid_ids, inverter_ids, oracle_owners, saved_owners, "inverter")


def _compare_independent_grid_owners(grids, identities, oracle_rows, saved_rows, label):
    key = label + "_id"
    oracle = _validate_grid_ownership(grids, identities, oracle_rows, key, label)
    saved = _validate_grid_ownership(grids, identities, saved_rows, key, label)
    verdicts = {oracle["verdict"], saved["verdict"]}
    if verdicts != {"PASS"}:
        return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL", label + "_grid_owner_evidence_invalid",
                    oracle=oracle, saved=saved)
    expected = {row[key]: row["grid_id"] for row in oracle_rows}
    actual = {row[key]: row["grid_id"] for row in saved_rows}
    changed = [identity for identity in expected if expected[identity] != actual[identity]]
    return _out("FAIL" if changed else "PASS", label + "_grid_owners_mismatch" if changed else label + "_grid_owners_equal",
                mismatch_count=len(changed), examples=[{key: identity, "oracle": expected[identity], "saved": actual[identity]}
                    for identity in nsmallest(MAX_STRING_EXAMPLES, changed)])


def compare_inverter_id_sets(oracle_ids, saved_ids):
    """Compare explicit independent device identities, including unconnected devices."""
    indexed = []
    for values in (oracle_ids, saved_ids):
        if (type(values) is not list or not values or any(type(key) is not str or not key.strip() or len(key) > 128
                for key in values) or len(set(values)) != len(values)):
            return _out("UNREADABLE", "inverter_id_evidence_unreadable")
        indexed.append(set(values))
    missing, foreign = indexed[0] - indexed[1], indexed[1] - indexed[0]
    failed = bool(missing or foreign)
    return _out("FAIL" if failed else "PASS", "inverter_ids_mismatch" if failed else "inverter_ids_equal",
                counts=dict(missing=len(missing), foreign=len(foreign)),
                missing_ids=nsmallest(MAX_STRING_EXAMPLES, missing), foreign_ids=nsmallest(MAX_STRING_EXAMPLES, foreign))


def validate_cross_topology(expected_grid_ids, saved_string_ids, inverter_ids,
                            string_owners, inverter_owners, assignments):
    """Require every string and its explicitly assigned inverter to share a grid."""
    checks = dict(strings=validate_grid_string_ownership(expected_grid_ids, saved_string_ids, string_owners),
                  inverters=validate_grid_inverter_ownership(expected_grid_ids, inverter_ids, inverter_owners),
                  assignments=validate_string_inverter_assignments(inverter_ids, saved_string_ids, assignments))
    verdicts = {check["verdict"] for check in checks.values()}
    if verdicts != {"PASS"}:
        return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL", "cross_topology_evidence_invalid",
                    checks=checks)
    strings = {row["string_id"]: row["grid_id"] for row in string_owners}
    inverters = {row["inverter_id"]: row["grid_id"] for row in inverter_owners}
    connections = {row["string_id"]: row["inverter_id"] for row in assignments}
    changed = [key for key in strings if strings[key] != inverters[connections[key]]]
    return _out("FAIL" if changed else "PASS", "cross_topology_mismatch" if changed else "cross_topology_equal",
                mismatch_count=len(changed), examples=[dict(string_id=key, inverter_id=connections[key],
                    string_grid=strings[key], inverter_grid=inverters[connections[key]])
                    for key in nsmallest(MAX_STRING_EXAMPLES, changed)])


def compare_inverter_string_totals(expected_totals, saved_string_ids, assignments):
    """Count explicit saved connections against independent inverter string totals."""
    return _compare_inverter_loads(expected_totals, saved_string_ids, assignments, "strings")


def compare_inverter_optimizer_totals(expected_totals, saved_strings, assignments):
    """Sum explicit per-string optimizer counts through supplied connections."""
    indexed, error = _index(saved_strings, "saved")
    if error:
        return _out("UNREADABLE", error)
    return _compare_inverter_loads(expected_totals, list(indexed), assignments, "optimizers", saved_strings)


def compare_inverter_panel_totals(expected_totals, saved_string_ids, panel_rows, assignments):
    """Bind explicit panel counts to saved string IDs before summing inverter loads."""
    return _compare_inverter_loads(expected_totals, saved_string_ids, assignments, "panels", panel_rows)


def compare_inverter_dc_totals(expected_totals, saved_string_ids, power_rows, assignments):
    """Sum explicit kW DC through supplied connections using isolated exact decimals."""
    with localcontext(_CONTEXT):
        return _compare_inverter_loads(expected_totals, saved_string_ids, assignments, "kw_dc", power_rows)


def _compare_inverter_loads(expected_totals, saved_string_ids, assignments, metric, rows=None):
    reason = "inverter_" + metric
    totals = _grid_dc_totals(expected_totals) if metric == "kw_dc" else expected_totals
    if totals is None or (metric != "kw_dc" and not _valid_grid_totals(totals)):
        return _out("UNREADABLE", reason + "_totals_unreadable")
    ownership = validate_string_inverter_assignments(list(totals), saved_string_ids, assignments)
    if ownership["verdict"] != "PASS":
        return _out(ownership["verdict"], reason + "_assignments_invalid", ownership=ownership)
    values = dict.fromkeys(saved_string_ids, 1)
    if metric != "strings":
        values, error = _index(rows, "saved_" + metric, metric=metric)
        if error:
            return _out("UNREADABLE", error)
        if set(values) != set(saved_string_ids):
            return _out("UNREADABLE", reason + "_string_identity_conflict")
    actual = dict.fromkeys(totals, 0)
    for row in assignments:
        actual[row["inverter_id"]] += values[row["string_id"]]
    changed = [key for key in totals if totals[key] != actual[key]]
    return _out("FAIL" if changed else "PASS", reason + "_totals_mismatch" if changed else reason + "_totals_equal",
                mismatch_count=len(changed), examples=[dict(inverter_id=key, oracle=totals[key], saved=actual[key],
                    delta=actual[key] - totals[key]) for key in nsmallest(MAX_STRING_EXAMPLES, changed)])


def compare_inverter_metric_oracle_totals(inverter_ids, expected_totals, oracle_values, metric):
    """Require independent inverter coverage and exact report agreement for one metric."""
    if metric not in ("strings", "optimizers", "panels", "kw_dc"):
        return _out("UNREADABLE", "inverter_metric_unreadable")
    with localcontext(_CONTEXT):
        result = _compare_grid_oracle_totals(inverter_ids, expected_totals, oracle_values,
                                             metric, "inverter_" + metric + "_oracle")
    if "grid_total" in result:
        result["inverter_total"] = result.pop("grid_total")
    return result


def _validate_grid_ownership(expected_grid_ids, saved_ids, assignments, identity_key, label, *, owner_key="grid_id"):
    prefix = "" if label == "string" else label + "_"
    foreign_key = "foreign_" + label
    def valid_ids(values):
        return (type(values) is list and bool(values)
                and all(type(key) is str and bool(key.strip()) and len(key) <= 128 for key in values)
                and len(set(values)) == len(values))
    if not valid_ids(expected_grid_ids) or not valid_ids(saved_ids) or type(assignments) is not list:
        return _out("UNREADABLE", prefix + "ownership_evidence_unreadable")
    expected, saved, seen = set(expected_grid_ids), set(saved_ids), set()
    foreign_owner = "foreign_grid" if owner_key == "grid_id" else "foreign_inverter"
    errors = {"missing": 0, "duplicate": 0, foreign_key: 0, foreign_owner: 0}
    examples = {}
    def issue(reason, key):
        errors[reason] += 1
        candidates = examples.setdefault(reason, set())
        candidates.add(key)
        if len(candidates) > MAX_STRING_EXAMPLES:
            candidates.remove(max(candidates))
    for row in assignments:
        if (type(row) is not dict or set(row) != {owner_key, identity_key}
                or any(type(value) is not str or not value.strip() or len(value) > 128 for value in row.values())):
            return _out("UNREADABLE", prefix + "ownership_row_unreadable")
        key = row[identity_key]
        if key not in saved:
            issue(foreign_key, key)
        if row[owner_key] not in expected:
            issue(foreign_owner, key)
        if key in seen:
            issue("duplicate", key)
        seen.add(key)
    for key in saved - seen:
        issue("missing", key)
    failed = any(errors.values())
    return _out("FAIL" if failed else "PASS", label + "_ownership_mismatch" if failed else label + "_ownership_equal",
                counts=errors, examples={reason: sorted(keys) for reason, keys in sorted(examples.items())})


def _valid_grid_totals(values):
    return (type(values) is dict and bool(values)
            and all(type(key) is str and bool(key.strip()) and len(key) <= 128
                    and type(value) is int and 0 <= value <= 2**53 - 1
                    for key, value in values.items()))


def _grid_dc_totals(values):
    if type(values) is not dict or not values:
        return None
    result = {}
    for key, value in values.items():
        if type(key) is not str or not key.strip() or len(key) > 128:
            return None
        number = _numeric(value)
        if number is None:
            return None
        result[key] = number
    return result


def _with_saved_values(snapshot, binding):
    verdicts = {snapshot["verdict"], binding["verdict"]}
    return dict(snapshot, saved_binding=binding,
                verdict="UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS")


def compare_before_panel_counts(saved_rows, before_rows):
    """Require before-snapshot panels to match saved per-string counts exactly."""
    return _compare_before_values(saved_rows, before_rows, "panels", "panel")


def compare_before_dc_values(saved_rows, before_rows):
    """Bind before-snapshot power to exact saved per-string kW DC values."""
    with localcontext(_CONTEXT):
        return _compare_before_values(saved_rows, before_rows, "kw_dc", "dc")


def _compare_before_values(saved_rows, before_rows, metric, label):
    saved, error = _index(saved_rows, "saved_" + label, metric=metric)
    if error:
        return _out("UNREADABLE", error)
    before, error = _index(before_rows, "before_" + label, metric=metric)
    if error:
        return _out("UNREADABLE", error)
    missing, foreign = saved.keys() - before.keys(), before.keys() - saved.keys()
    changed = [key for key in saved.keys() & before.keys() if saved[key] != before[key]]
    examples = [dict(string_id=key, saved=saved[key], before=before[key], delta=before[key] - saved[key])
                for key in nsmallest(MAX_STRING_EXAMPLES, changed)]
    return _out("UNREADABLE" if missing or foreign else "FAIL" if changed else "PASS",
                label + "_before_saved_values_mismatch" if missing or foreign or changed else label + "_before_saved_values_equal",
                counts=dict(missing=len(missing), foreign=len(foreign), mismatched=len(changed)),
                missing_ids=nsmallest(MAX_STRING_EXAMPLES, missing), foreign_ids=nsmallest(MAX_STRING_EXAMPLES, foreign),
                examples=examples)


def compare_grid_string_totals(expected_totals, saved_string_ids, assignments):
    """Count explicit saved-string ownership against independent per-grid counts."""
    return _compare_grid_identity_totals(expected_totals, saved_string_ids, assignments, "string")


def compare_grid_inverter_totals(expected_totals, saved_inverter_ids, assignments):
    """Count explicit saved inverter owners against independent per-grid counts."""
    return _compare_grid_identity_totals(expected_totals, saved_inverter_ids, assignments, "inverter")


def _compare_grid_identity_totals(expected_totals, saved_ids, assignments, label):
    reason = "grid_" + label
    if not _valid_grid_totals(expected_totals):
        return _out("UNREADABLE", reason + "_totals_unreadable")
    ownership = _validate_grid_ownership(list(expected_totals), saved_ids, assignments, label + "_id", label)
    if ownership["verdict"] != "PASS":
        return _out(ownership["verdict"], reason + "_ownership_invalid", ownership=ownership)
    totals = dict.fromkeys(expected_totals, 0)
    for row in assignments:
        totals[row["grid_id"]] += 1
    changed = [key for key in totals if totals[key] != expected_totals[key]]
    return _out("FAIL" if changed else "PASS", reason + "_totals_mismatch" if changed else reason + "_totals_equal",
                mismatch_count=len(changed), examples=[dict(grid_id=key, oracle=expected_totals[key], saved=totals[key],
                    delta=totals[key] - expected_totals[key]) for key in nsmallest(MAX_STRING_EXAMPLES, changed)])


def compare_grid_string_oracle_totals(expected_grid_ids, expected_totals, oracle_values):
    """Require independent grid string counts to cover and sum to report strings."""
    return _compare_grid_oracle_totals(expected_grid_ids, expected_totals, oracle_values, "strings", "grid_string_oracle")


def compare_grid_inverter_oracle_totals(expected_grid_ids, expected_totals, oracle_values):
    """Require exact grid coverage and independent report inverter-count agreement."""
    return _compare_grid_oracle_totals(expected_grid_ids, expected_totals, oracle_values, "inverters", "grid_inverter_oracle")


def compare_inverter_snapshots(expected_grid_ids, before_ids, before_owners, reopened_ids, reopened_owners):
    """Compare explicit inverter identities and owners, independent of aggregate totals."""
    before = validate_grid_inverter_ownership(expected_grid_ids, before_ids, before_owners)
    reopened = validate_grid_inverter_ownership(expected_grid_ids, reopened_ids, reopened_owners)
    verdicts = {before["verdict"], reopened["verdict"]}
    if verdicts != {"PASS"}:
        return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL", "inverter_snapshots_invalid",
                    before=before, reopened=reopened)
    old = {row["inverter_id"]: row["grid_id"] for row in before_owners}
    new = {row["inverter_id"]: row["grid_id"] for row in reopened_owners}
    added, removed = new.keys() - old.keys(), old.keys() - new.keys()
    changed = [key for key in old.keys() & new.keys() if old[key] != new[key]]
    failed = bool(added or removed or changed)
    return _out("FAIL" if failed else "PASS", "inverter_snapshots_changed" if failed else "inverter_snapshots_equal",
                counts=dict(added=len(added), removed=len(removed), changed=len(changed)),
                added_ids=nsmallest(MAX_STRING_EXAMPLES, added), removed_ids=nsmallest(MAX_STRING_EXAMPLES, removed),
                owner_changes=[dict(inverter_id=key, before=old[key], reopened=new[key])
                               for key in nsmallest(MAX_STRING_EXAMPLES, changed)])


def compare_grid_dc_totals(expected_totals, saved_power, assignments):
    """Compare explicitly supplied per-string kW DC with independent grid totals."""
    with localcontext(_CONTEXT):
        return _compare_grid_totals(expected_totals, saved_power, assignments, "kw_dc", "dc")


def compare_grid_dc_oracle_totals(expected_grid_ids, expected_totals, oracle_values):
    """Require exact coverage and summed agreement with independent report kW DC."""
    with localcontext(_CONTEXT):
        return _compare_grid_oracle_totals(expected_grid_ids, expected_totals, oracle_values, "kw_dc", "grid_dc_oracle")


def compare_grid_dc_snapshots(expected_grid_ids, before_power, before_owners, reopened_power, reopened_owners):
    """Compare explicit kW DC and owners across supplied snapshots with exact sums."""
    with localcontext(_CONTEXT):
        return _compare_grid_snapshots(expected_grid_ids, before_power, before_owners, reopened_power,
                                       reopened_owners, "kw_dc", "grid_dc", "grid_dc_snapshot_strings_unreadable")


def compare_dc_string_ids(saved_strings, power_rows):
    """Bind explicit power-row IDs to the supplied saved design string identities."""
    return _compare_string_metric_ids(saved_strings, power_rows, "kw_dc", "dc")


def compare_bound_dc_snapshots(expected_grid_ids, before_strings, reopened_strings,
                               before_power, before_owners, reopened_power, reopened_owners):
    """Bind each explicit power map to the string IDs of its supplied snapshot."""
    before = compare_dc_string_ids(before_strings, before_power)
    reopened = compare_dc_string_ids(reopened_strings, reopened_power)
    comparison = compare_grid_dc_snapshots(expected_grid_ids, before_power, before_owners,
                                           reopened_power, reopened_owners)
    verdicts = {before["verdict"], reopened["verdict"], comparison["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_grid_dc_snapshot_evidence", before_binding=before, reopened_binding=reopened,
                comparison=comparison)


def compare_grid_oracle_totals(expected_grid_ids, expected_totals, oracle_values):
    """Bind independent grid totals to explicit coverage and the report grand total."""
    return _compare_grid_oracle_totals(expected_grid_ids, expected_totals, oracle_values, "optimizers", "grid_oracle")


def compare_grid_panel_oracle_totals(expected_grid_ids, expected_totals, oracle_values):
    """Require explicit grid panel totals to agree with the independent report total."""
    return _compare_grid_oracle_totals(expected_grid_ids, expected_totals, oracle_values, "panels", "grid_panel_oracle")


def _compare_grid_oracle_totals(expected_grid_ids, expected_totals, oracle_values, metric, reason):
    if metric == "kw_dc":
        expected_totals = _grid_dc_totals(expected_totals)
        valid = expected_totals is not None
    else:
        valid = _valid_grid_totals(expected_totals)
    if (type(expected_grid_ids) is not list or not expected_grid_ids
            or any(type(key) is not str or not key.strip() or len(key) > 128 for key in expected_grid_ids)
            or len(set(expected_grid_ids)) != len(expected_grid_ids)
            or not valid or set(expected_totals) != set(expected_grid_ids)
            or type(oracle_values) is not dict):
        return _out("UNREADABLE", reason + "_evidence_unreadable")
    oracle = _numeric(oracle_values.get(metric), count=metric != "kw_dc")
    if oracle is None:
        return _out("UNREADABLE", reason + "_evidence_unreadable")
    total = sum(expected_totals.values())
    return _out("PASS" if total == oracle else "FAIL", reason + "_total_equal" if total == oracle
                else reason + "_total_mismatch", grid_total=total, oracle_total=oracle)


def compare_ownership_snapshots(expected_grid_ids, saved_string_ids, before_rows, reopened_rows):
    """Compare explicit supplied ownership across save/reopen, including equal-count swaps."""
    before = validate_grid_string_ownership(expected_grid_ids, saved_string_ids, before_rows)
    reopened = validate_grid_string_ownership(expected_grid_ids, saved_string_ids, reopened_rows)
    verdicts = {before["verdict"], reopened["verdict"]}
    if verdicts != {"PASS"}:
        return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL",
                    "ownership_snapshots_invalid", before=before, reopened=reopened)
    old = {row["string_id"]: row["grid_id"] for row in before_rows}
    new = {row["string_id"]: row["grid_id"] for row in reopened_rows}
    changed = [key for key in old if old[key] != new[key]]
    examples = [dict(string_id=key, before=old[key], reopened=new[key])
                for key in nsmallest(MAX_STRING_EXAMPLES, changed)]
    return _out("FAIL" if changed else "PASS", "ownership_changed" if changed else "ownership_equal",
                changed_count=len(changed), examples=examples)


def compare_bound_ownership_snapshots(expected_grid_ids, before_strings, reopened_strings, before_rows, reopened_rows):
    """Bind each ownership map to the string identities in its supplied snapshot."""
    before, before_error = _index(before_strings, "before")
    reopened, reopened_error = _index(reopened_strings, "reopened")
    if before_error or reopened_error:
        return _out("UNREADABLE", "ownership_snapshot_strings_unreadable")
    checks = compare_ownership_snapshots(expected_grid_ids, list(before), before_rows, reopened_rows)
    if set(before) != set(reopened):
        return _out("UNREADABLE", "ownership_snapshot_identity_conflict", comparison=checks)
    if checks["reason"] == "ownership_snapshots_invalid":
        for stage in ("before", "reopened"):
            counts = checks[stage].get("counts", {})
            if counts.get("missing", 0) or counts.get("foreign_string", 0):
                return _out("UNREADABLE", "ownership_snapshot_binding_conflict", comparison=checks)
    return checks


def compare_grid_optimizer_snapshots(expected_grid_ids, before_strings, before_rows, reopened_strings, reopened_rows):
    """Derive each supplied snapshot's per-grid sums without inferring ownership."""
    return _compare_grid_snapshots(expected_grid_ids, before_strings, before_rows, reopened_strings,
                                   reopened_rows, "optimizers", "grid_optimizer", "grid_snapshot_strings_unreadable")


def compare_grid_panel_snapshots(expected_grid_ids, before_strings, before_rows, reopened_strings, reopened_rows):
    """Compare explicit panel-count rows and ownership across both supplied snapshots."""
    return _compare_grid_snapshots(expected_grid_ids, before_strings, before_rows, reopened_strings,
                                   reopened_rows, "panels", "grid_panel", "grid_panel_snapshot_strings_unreadable")


def _compare_grid_snapshots(expected_grid_ids, before_strings, before_rows, reopened_strings, reopened_rows,
                            metric, reason, unreadable):
    totals, checks = [], {}
    for stage, strings, owners in (("before", before_strings, before_rows),
                                    ("reopened", reopened_strings, reopened_rows)):
        indexed, error = _index(strings, stage, metric=metric)
        if error:
            checks[stage] = _out("UNREADABLE", unreadable)
            continue
        ownership = validate_grid_string_ownership(expected_grid_ids, list(indexed), owners)
        checks[stage] = ownership
        if ownership["verdict"] == "PASS":
            sums = dict.fromkeys(expected_grid_ids, 0)
            for row in owners:
                sums[row["grid_id"]] += indexed[row["string_id"]]
            totals.append(sums)
    verdicts = {check["verdict"] for check in checks.values()}
    if verdicts != {"PASS"}:
        return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL",
                    reason + "_snapshots_invalid", checks=checks)
    before, reopened = totals
    changed = [key for key in before if before[key] != reopened[key]]
    examples = [dict(grid_id=key, before=before[key], reopened=reopened[key], delta=reopened[key] - before[key])
                for key in nsmallest(MAX_STRING_EXAMPLES, changed)]
    return _out("FAIL" if changed else "PASS", reason + "_snapshots_changed" if changed
                else reason + "_snapshots_equal", changed_count=len(changed), examples=examples)


def compare_grid_optimizer_totals(expected_totals, saved_strings, assignments):
    """Compare supplied independent per-grid totals using explicit ownership rows."""
    return _compare_grid_totals(expected_totals, saved_strings, assignments, "optimizers", "optimizer")


def compare_panel_string_ids(saved_strings, panel_counts):
    """Bind independently supplied panel rows to the actual saved string identities."""
    return _compare_string_metric_ids(saved_strings, panel_counts, "panels", "panel")


def _compare_string_metric_ids(saved_strings, rows, metric, label):
    saved, error = _index(saved_strings, "saved")
    if error:
        return _out("UNREADABLE", error)
    panels, error = _index(rows, label, metric=metric)
    if error:
        return _out("UNREADABLE", error)
    missing, foreign = saved.keys() - panels.keys(), panels.keys() - saved.keys()
    return _out("UNREADABLE" if missing or foreign else "PASS",
                label + "_string_ids_unbound" if missing or foreign else label + "_string_ids_equal",
                counts=dict(missing=len(missing), foreign=len(foreign)),
                missing_ids=nsmallest(MAX_STRING_EXAMPLES, missing),
                foreign_ids=nsmallest(MAX_STRING_EXAMPLES, foreign))


def compare_bound_panel_snapshots(expected_grid_ids, before_strings, reopened_strings,
                                  before_panels, before_owners, reopened_panels, reopened_owners):
    """Require each explicit snapshot panel map to cover its actual string identities."""
    before = compare_panel_string_ids(before_strings, before_panels)
    reopened = compare_panel_string_ids(reopened_strings, reopened_panels)
    comparison = compare_grid_panel_snapshots(expected_grid_ids, before_panels, before_owners,
                                              reopened_panels, reopened_owners)
    verdicts = {before["verdict"], reopened["verdict"], comparison["verdict"]}
    return _out("UNREADABLE" if "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS",
                "supplied_grid_panel_snapshot_evidence", before_binding=before, reopened_binding=reopened,
                comparison=comparison)


def compare_grid_panel_totals(expected_totals, saved_strings, assignments):
    """Compare independent grid panel totals with explicit saved per-string panel counts."""
    return _compare_grid_totals(expected_totals, saved_strings, assignments, "panels", "panel")


def _compare_grid_totals(expected_totals, saved_strings, assignments, metric, label):
    reason = "grid_" + label
    if metric == "kw_dc":
        expected_totals = _grid_dc_totals(expected_totals)
        valid = expected_totals is not None
    else:
        valid = _valid_grid_totals(expected_totals)
    if not valid:
        return _out("UNREADABLE", reason + "_totals_unreadable")
    saved, error = _index(saved_strings, "saved", metric=metric)
    if error:
        return _out("UNREADABLE", error)
    ownership = validate_grid_string_ownership(list(expected_totals), list(saved), assignments)
    if ownership["verdict"] != "PASS":
        return _out(ownership["verdict"], reason + "_ownership_invalid", ownership=ownership)
    totals = dict.fromkeys(expected_totals, 0)
    for row in assignments:
        totals[row["grid_id"]] += saved[row["string_id"]]
    changed = [key for key in expected_totals if expected_totals[key] != totals[key]]
    examples = [dict(grid_id=key, oracle=expected_totals[key], saved=totals[key],
                     delta=totals[key] - expected_totals[key])
                for key in nsmallest(MAX_STRING_EXAMPLES, changed)]
    return _out("FAIL" if changed else "PASS", reason + "_totals_mismatch" if changed
                else reason + "_totals_equal", mismatch_count=len(changed), examples=examples)


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
    """Read strict quoted logical records with exact header/data arity.

    Accept five metric records and inspect at most one overflow record; never
    consume the remaining tail after a readable overflow. Restore field limits
    on malformed quotes, invalid arity and every early return.
    """
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
        return _out("FAIL", "snapshot_hash_binding_mismatch", unbound_phases=unbound,
                    row_diff=diff_snapshot_rows(before_rows, reopened_rows))
    binding = compare_snapshots(saved, calculated[0])
    if binding["verdict"] != "PASS":
        return _out("FAIL", "snapshot_saved_evidence_mismatch", changed_hashes=binding["changed_hashes"],
                    row_diff=diff_snapshot_rows(dict(strings=saved_strings, table_rows=view_rows), before_rows))
    result = compare_snapshots(*calculated)
    if result["verdict"] == "FAIL":
        result["row_diff"] = diff_snapshot_rows(before_rows, reopened_rows)
    return result


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


def _index(rows, source, *, metric="optimizers"):
    if not isinstance(rows, list) or not rows:
        return None, source + "_rows_unreadable"
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            return None, source + "_row_unreadable"
        identifier, count = row.get("string_id"), row.get(metric)
        if type(identifier) is not str or not identifier.strip() or len(identifier) > 128:
            return None, source + "_row_unreadable"
        if metric == "kw_dc":
            count = _numeric(count)
            if count is None:
                return None, source + "_row_unreadable"
        elif type(count) is not int or not 1 <= count <= 2**53 - 1:
            return None, source + "_row_unreadable"
        if identifier in indexed:
            return None, source + "_duplicate_id"
        indexed[identifier] = count
    return indexed, None


def compare_strings(oracle_rows, saved_rows):
    """Return deterministic, explicit per-string differences, never infer IDs."""
    return _compare_string_metric_rows(oracle_rows, saved_rows, "optimizers")


def compare_string_panel_counts(oracle_rows, saved_rows):
    """Compare explicit independent per-string panel counts, including equal-total swaps."""
    return _compare_string_metric_rows(oracle_rows, saved_rows, "panels")


def compare_string_dc_power(oracle_rows, saved_rows):
    """Compare explicit independent per-string kW DC using isolated exact decimals."""
    with localcontext(_CONTEXT):
        return _compare_string_metric_rows(oracle_rows, saved_rows, "kw_dc")


def _compare_string_metric_rows(oracle_rows, saved_rows, metric):
    oracle, error = _index(oracle_rows, "oracle", metric=metric)
    if error:
        return {"verdict": "UNREADABLE", "reason": error}
    saved, error = _index(saved_rows, "saved", metric=metric)
    if error:
        return {"verdict": "UNREADABLE", "reason": error}
    missing = oracle.keys() - saved.keys()
    foreign = saved.keys() - oracle.keys()
    changed = [key for key in oracle.keys() & saved.keys() if oracle[key] != saved[key]]
    mismatches = [{"string_id": key, "oracle": oracle[key], "saved": saved[key]}
                  for key in nsmallest(MAX_STRING_EXAMPLES, changed)]
    matched = not (missing or foreign or changed)
    reason = ("exact_string_identity" if matched else "string_identity_mismatch") if metric == "optimizers" else (
        "string_" + metric + ("_equal" if matched else "_mismatch"))
    return {"verdict": "PASS" if matched else "FAIL", "reason": reason,
            "missing_ids": nsmallest(MAX_STRING_EXAMPLES, missing),
            "foreign_ids": nsmallest(MAX_STRING_EXAMPLES, foreign), "mismatches": mismatches,
            "counts": {"missing": len(missing), "foreign": len(foreign), "mismatched": len(changed)}}


def _write_all(stream, payload):
    """Deliver one payload without restarting after a partial write."""
    offset = 0
    while offset < len(payload):
        piece = payload[offset:offset + 8192]
        count = stream.write(piece)
        if type(count) is not int or not 0 < count <= len(piece):
            raise ValueError("stdout_write_unreadable")
        offset += count
    stream.flush()


def main(argv=None):
    """CLI:0 PASS,1 FAIL,4 UNREADABLE,5 output failure without recursive reporting."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Offline X05 evidence comparison; no live acceptance.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--aggregate", action="store_true", help="read one three-fixture envelope")
    modes.add_argument("--provenance", action="store_true", help="read one supplied-provenance envelope")
    modes.add_argument("--assurance", action="store_true", help="read one supplied-assurance envelope")
    modes.add_argument("--provenance-aggregate", action="store_true", help="read three provenance envelopes")
    modes.add_argument("--assurance-aggregate", action="store_true", help="read three assurance envelopes")
    modes.add_argument("--seven-step", action="store_true", help="read one supplied seven-step envelope")
    modes.add_argument("--seven-step-aggregate", action="store_true", help="read three supplied seven-step envelopes")
    modes.add_argument("--seven-step-bound", action="store_true", help="read one bound seven-step envelope")
    modes.add_argument("--seven-step-bound-aggregate", action="store_true", help="read three bound seven-step envelopes")
    args = parser.parse_args(argv)
    try:
        chunks, size = [], 0
        limit = 4 * MAX_JSON_CHARS + 1
        while size < limit:
            chunk = sys.stdin.buffer.read(limit - size)
            if type(chunk) is not bytes or len(chunk) > limit - size:
                raise ValueError("stdin_read_unreadable")
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 4 * MAX_JSON_CHARS:
            result = _out("UNREADABLE", "json_input_too_large")
        else:
            text = raw.decode("utf-8", errors="strict")
            decode = (aggregate_bound_seven_steps_json if args.seven_step_bound_aggregate else
                      reconcile_bound_seven_steps_json if args.seven_step_bound else
                      aggregate_seven_steps_json if args.seven_step_aggregate else
                      reconcile_seven_steps_json if args.seven_step else
                      aggregate_assurance_json if args.assurance_aggregate else
                      reconcile_assurance_json if args.assurance else
                      aggregate_provenance_json if args.provenance_aggregate else
                      aggregate_fixtures_json if args.aggregate else
                      reconcile_provenance_json if args.provenance else reconcile_json)
            result = decode(text)
        try:
            output = serialize_result(result)
        except ValueError as error:
            if str(error) != "json_result_too_large":
                raise
            result = _out("UNREADABLE", "json_result_too_large")
            output = serialize_result(result)
    except Exception:
        result = _out("UNREADABLE", "cli_evidence_unreadable")
        output = serialize_result(result)
    try:
        binary_output = getattr(sys.stdout, "buffer", None)
        if binary_output is None:
            _write_all(sys.stdout, output + "\n")
        else:
            _write_all(binary_output, output.encode("ascii") + b"\n")
    except (OSError, UnicodeError, ValueError):
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
