"""Reconcile external failure and output-change rows to revision-bound runs."""
from collections import Counter


def dashboard_quality(runs, failures, changes, before, after):
    if not before or not after or before == after:
        return "INCOMPLETE"
    ids, failed, outputs = set(), Counter(), {}
    for run in runs:
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
            pair = outputs.setdefault(key, {})
            digest = run.get("output")
            if not isinstance(digest, str) or not digest or pair.get(run["revision"], digest) != digest:
                return "INCOMPLETE"
            pair[run["revision"]] = digest
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
