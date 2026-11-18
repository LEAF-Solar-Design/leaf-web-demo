"""Reconcile external failure and output-change rows to revision-bound runs."""
from collections import Counter
import json
import re


def dashboard_quality(runs, failures, changes, before, after, *, expected_run_ids=None):
    if not before or not after or before == after:
        return "INCOMPLETE"
    ids, failed, outputs = set(), Counter(), {}
    inputs = {}
    if not isinstance(runs, list):
        return "INCOMPLETE"
    for run in runs:
        if not isinstance(run, dict) or run.get("status") not in ("complete", "failed", "cancelled"):
            return "INCOMPLETE"
        keys = ("id", "drawing", "version", "tool", "revision", "status")
        if not all(isinstance(run.get(k), str) and run[k] for k in keys):
            return "INCOMPLETE"
        if run["id"] in ids:
            return "FAIL"
        ids.add(run["id"])
        drawing = (run["drawing"], run["version"])
        if run["status"] == "failed":
            failed[drawing] += 1
        elif run["status"] == "complete" and run["revision"] in (before, after):
            key = (*drawing, run["tool"])
            input_digest = run.get("input_sha256")
            params = run.get("parameters")
            if not isinstance(input_digest, str) or not re.fullmatch("[0-9a-f]{64}", input_digest) or not isinstance(params, dict):
                return "INCOMPLETE"
            try:
                identity = input_digest, json.dumps(params, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError):
                return "INCOMPLETE"
            if inputs.setdefault(key, identity) != identity:
                return "INCOMPLETE"
            pair = outputs.setdefault(key, {})
            digest = run.get("output")
            if not isinstance(digest, str) or not digest or pair.get(run["revision"], digest) != digest:
                return "INCOMPLETE"
            pair[run["revision"]] = digest
    if expected_run_ids is None:
        return "INCOMPLETE"
    if (not isinstance(expected_run_ids, (list, tuple)) or not expected_run_ids
            or not all(isinstance(i, str) and i for i in expected_run_ids)):
        return "INCOMPLETE"
    if len(set(expected_run_ids)) != len(expected_run_ids) or ids != set(expected_run_ids):
        return "FAIL"
    expected = [(*key, before, after) for key, pair in outputs.items()
                if before in pair and after in pair and pair[before] != pair[after]]
    try:
        totals = {(r["drawing"], r["version"]): r["count"] for r in failures}
        claimed = [tuple(r[k] for k in ("drawing","version","tool","before","after")) for r in changes]
        if any(type(n) is not int or n < 1 for n in totals.values()):
            return "FAIL"
        valid = len(totals) == len(failures) and totals == dict(failed) and Counter(claimed) == Counter(expected)
    except (KeyError, TypeError):
        return "INCOMPLETE"
    return "PASS" if valid else "FAIL"
