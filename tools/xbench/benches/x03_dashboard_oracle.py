"""Reconcile external failure and output-change rows to revision-bound runs."""
from collections import Counter
from math import isclose, isfinite
import json
import re


def dashboard_quality(runs, failures, changes, before, after, *, expected_run_ids=None, summary=None):
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
    if not valid:
        return "FAIL"
    if summary is None:
        return "PASS"
    if not isinstance(summary, dict):
        return "INCOMPLETE"
    durations = [run.get("duration_seconds") for run in runs]
    def finite_nonnegative(value):
        try:
            return type(value) in (int, float) and isfinite(value) and value >= 0
        except OverflowError:
            return False
    if not all(map(finite_nonnegative, durations)):
        return "INCOMPLETE"
    metrics = {
        "success_rate": sum(run["status"] == "complete" for run in runs) / len(runs),
        "mean_duration_seconds": sum(value / len(runs) for value in durations),
        "max_duration_seconds": max(durations),
    }
    if not all(key in summary for key in metrics):
        return "INCOMPLETE"
    return "PASS" if all(finite_nonnegative(summary[k]) and isclose(summary[k], v)
                         for k, v in metrics.items()) else "FAIL"


def window_coverage_quality(runs, reported_ids, start, end, *, include_start, include_end):
    """Recompute report membership from the complete recorded run inventory."""
    from datetime import datetime, timezone

    def utc(value):
        if not isinstance(value, str):
            return None
        try:
            stamp = datetime.fromisoformat(value)
            return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
        except (OverflowError, ValueError):
            return None

    start, end = utc(start), utc(end)
    if start is None or end is None or any(type(flag) is not bool for flag in (include_start, include_end)):
        return "INCOMPLETE"
    if not isinstance(runs, list) or not isinstance(reported_ids, list):
        return "INCOMPLETE"
    if start >= end or not all(isinstance(i, str) and i for i in reported_ids):
        return "FAIL"
    seen, expected = set(), set()
    for row in runs:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            return "INCOMPLETE"
        stamp = utc(row.get("timestamp"))
        if stamp is None:
            return "INCOMPLETE"
        if row["id"] in seen:
            return "FAIL"
        seen.add(row["id"])
        if start < stamp < end or (include_start and stamp == start) or (include_end and stamp == end):
            expected.add(row["id"])
    return "PASS" if len(reported_ids) == len(set(reported_ids)) and expected == set(reported_ids) else "FAIL"


def concurrent_time_quality(intervals, reported_seconds, policy=None):
    """Use one wall clock for all worker intervals measured in seconds."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if policy not in ("elapsed", "worker") or not isinstance(intervals, list) or reported_seconds is None:
        return "INCOMPLETE"
    if not finite_measure(reported_seconds) or reported_seconds < 0:
        return "FAIL"
    spans = []
    for span in intervals:
        if not isinstance(span, (list, tuple)) or len(span) != 2 or None in span:
            return "INCOMPLETE"
        if not all(map(finite_measure, span)) or span[1] < span[0]:
            return "FAIL"
        spans.append(tuple(Fraction(str(x)) for x in span))
    total, last_end = 0, None
    for start, end in sorted(spans):
        if policy == "elapsed" and last_end is not None:
            start = max(start, last_end)
        total += max(0, end-start)
        last_end = end if last_end is None else max(last_end, end)
    return "PASS" if Fraction(str(reported_seconds)) == total else "FAIL"


def latency_percentile_quality(samples, percentile, reported, policy=None):
    """Percentile uses a 0..100 scale and linear interpolation uses rank (n-1)p."""
    from fractions import Fraction
    from math import ceil
    from benches.x03_quality import finite_measure
    if (policy not in ("nearest-rank", "linear") or not isinstance(samples, (list, tuple)) or not samples
            or None in samples or percentile is None or reported is None):
        return "INCOMPLETE"
    if (not finite_measure(percentile) or not 0 <= percentile <= 100 or not finite_measure(reported)
            or not all(finite_measure(v) and v >= 0 for v in samples)):
        return "FAIL"
    values = sorted(Fraction(str(v)) for v in samples)
    p = Fraction(str(percentile)) / 100
    if policy == "nearest-rank":
        expected = values[max(0, ceil(p*len(values))-1)]
    else:
        rank = p * (len(values)-1)
        index = int(rank)
        expected = values[index] + (rank-index) * (values[min(index+1, len(values)-1)]-values[index])
    return "PASS" if Fraction(str(reported)) == expected else "FAIL"


def retry_dashboard_quality(attempts, reported):
    """Grade settled runs from ordered per-run attempt numbers, starting at one."""
    if not isinstance(attempts, list) or not attempts or not isinstance(reported, dict):
        return "INCOMPLETE"
    runs = {}
    for row in attempts:
        if not isinstance(row, dict) or any(row.get(k) is None for k in ("run", "attempt", "outcome")):
            return "INCOMPLETE"
        run, index, outcome = row["run"], row["attempt"], row["outcome"]
        if not isinstance(run, str) or not run or type(index) is not int or index < 1 or outcome not in ("failed", "succeeded"):
            return "FAIL"
        history = runs.setdefault(run, {})
        if index in history:
            return "FAIL"
        history[index] = outcome
    succeeded = 0
    for history in runs.values():
        if max(history) != len(history):
            return "INCOMPLETE"
        if any(history[i] == "succeeded" for i in range(1, len(history))):
            return "FAIL"
        succeeded += history[len(history)] == "succeeded"
    expected = dict(attempts=len(attempts), runs=len(runs), succeeded=succeeded, failed=len(runs)-succeeded)
    if expected.keys() - reported.keys():
        return "INCOMPLETE"
    return "PASS" if all(type(reported[k]) is int and reported[k] == v for k, v in expected.items()) else "FAIL"


def paired_release_quality(cohort, before, after, reported):
    """Compare successful runs on the same frozen drawing-version cohort for each release."""
    if not isinstance(cohort, list) or not cohort or not isinstance(reported, dict):
        return "INCOMPLETE"
    if not all(isinstance(key, str) and key for key in cohort) or len(set(cohort)) != len(cohort):
        return "FAIL"
    successes = []
    for rows in (before, after):
        if not isinstance(rows, list):
            return "INCOMPLETE"
        outcomes = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("drawing_version"), str) or row.get("outcome") is None:
                return "INCOMPLETE"
            key = row["drawing_version"]
            if key in outcomes or row["outcome"] not in ("passed", "failed"):
                return "FAIL"
            outcomes[key] = row["outcome"]
        if outcomes.keys() != set(cohort):
            return "FAIL"
        successes.append(sum(v == "passed" for v in outcomes.values()))
    old, new = successes
    expected = dict(pairs=len(cohort), before_passed=old, after_passed=new, improved=new > old)
    if expected.keys() - reported.keys():
        return "INCOMPLETE"
    return "PASS" if all(type(reported[k]) is type(v) and reported[k] == v for k, v in expected.items()) else "FAIL"


def censored_durations_quality(runs, cutoff, policy, completed, censored):
    """Separate exact completed durations from observed lower bounds at the cutoff."""
    if policy != "right-censored" or type(cutoff) is not int or not isinstance(runs, dict) or not runs:
        return "INCOMPLETE"
    if not isinstance(completed, dict) or not isinstance(censored, dict):
        return "INCOMPLETE"
    exact, lower = {}, {}
    for key, row in runs.items():
        if not isinstance(row, dict) or type(row.get("start")) is not int or "end" not in row:
            return "INCOMPLETE"
        start, end = row["start"], row["end"]
        if end is not None and (type(end) is not int or end < start):
            return "FAIL"
        if start > cutoff:
            continue
        if end is None or end > cutoff:
            lower[key] = cutoff - start
        else:
            exact[key] = end - start
    if not all(type(v) is int for v in (*completed.values(), *censored.values())):
        return "FAIL"
    return "PASS" if (exact, lower) == (completed, censored) else "FAIL"


def weighted_rate_quality(cohorts, weights, policy, reported):
    """Pool weighted successes over weighted exposures. The report is a fraction on [0, 1]."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if policy != "weighted-exposures" or not isinstance(cohorts, dict) or not cohorts or not isinstance(weights, dict):
        return "INCOMPLETE"
    if cohorts.keys() != weights.keys() or reported is None:
        return "INCOMPLETE"
    numerator = denominator = Fraction(0)
    for key, row in cohorts.items():
        if not isinstance(row, dict) or any(row.get(k) is None for k in ("successes", "exposures")) or weights[key] is None:
            return "INCOMPLETE"
        successes, exposures = row["successes"], row["exposures"]
        if (type(successes) is not int or type(exposures) is not int or not 0 <= successes <= exposures
                or not finite_measure(weights[key]) or weights[key] < 0):
            return "FAIL"
        weight = Fraction(str(weights[key]))
        numerator += weight * successes
        denominator += weight * exposures
    if not denominator:
        return "INCOMPLETE"
    if not finite_measure(reported) or not 0 <= reported <= 1:
        return "FAIL"
    return "PASS" if isclose(reported, float(numerator / denominator), abs_tol=1e-12, rel_tol=1e-12) else "FAIL"


def duration_histogram_quality(runs, edges, policy, counts):
    """Count each recorded run in one bin, including underflow and overflow."""
    from bisect import bisect_left, bisect_right
    from benches.x03_quality import finite_measure
    if policy not in ("left-closed", "right-closed") or not isinstance(runs, dict):
        return "INCOMPLETE"
    if not isinstance(edges, list) or not edges or not isinstance(counts, list):
        return "INCOMPLETE"
    if None in runs.values() or None in edges:
        return "INCOMPLETE"
    if not all(finite_measure(v) and v >= 0 for v in (*runs.values(), *edges)) or edges != sorted(set(edges)):
        return "FAIL"
    if len(counts) != len(edges)+1 or not all(type(n) is int and n >= 0 for n in counts):
        return "FAIL"
    expected = [0] * len(counts)
    locate = bisect_right if policy == "left-closed" else bisect_left
    for duration in runs.values():
        expected[locate(edges, duration)] += 1
    return "PASS" if expected == counts else "FAIL"


def critical_path_quality(tasks, reported_path, reported_duration):
    """Grade an actual longest source-to-sink path; ties may use any longest path."""
    from fractions import Fraction
    from graphlib import CycleError, TopologicalSorter
    from benches.x03_quality import finite_measure
    if not isinstance(tasks, dict) or not tasks or not isinstance(reported_path, list) or reported_duration is None:
        return "INCOMPLETE"
    parents, durations = {}, {}
    for task, row in tasks.items():
        if not isinstance(row, dict) or not isinstance(row.get("predecessors"), list) or row.get("duration") is None:
            return "INCOMPLETE"
        deps = row["predecessors"]
        if not all(isinstance(dep, str) and dep in tasks for dep in deps):
            return "INCOMPLETE"
        if not finite_measure(row["duration"]) or row["duration"] < 0:
            return "FAIL"
        parents[task] = set(deps)
        durations[task] = Fraction(str(row["duration"]))
    ends = {}
    try:
        for task in TopologicalSorter(parents).static_order():
            ends[task] = durations[task] + max((ends[p] for p in parents[task]), default=0)
    except CycleError:
        return "FAIL"
    if not reported_path or not all(isinstance(t, str) and t in tasks for t in reported_path):
        return "FAIL"
    if parents[reported_path[0]] or any(reported_path[-1] in predecessors for predecessors in parents.values()):
        return "FAIL"
    if any(a not in parents[b] for a, b in zip(reported_path, reported_path[1:])):
        return "FAIL"
    actual = sum(durations[t] for t in reported_path)
    return "PASS" if finite_measure(reported_duration) and Fraction(str(reported_duration)) == actual == max(ends.values()) else "FAIL"
