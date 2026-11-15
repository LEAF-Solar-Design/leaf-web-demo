"""X05 offline string identity comparison, not wired into live grading.

Both callers supply independent rows: {string_id: str, optimizers: int}.
This module does not establish the origin or independence of either input.
"""


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
