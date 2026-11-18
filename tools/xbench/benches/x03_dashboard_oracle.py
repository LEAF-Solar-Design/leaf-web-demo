"""Reconcile external failure and output-change rows to revision-bound runs."""
from collections import Counter
from math import isclose, isfinite
import json
import re


def request_budget_quality(requests, eligible_classes, target, report):
    """Report allowed minus observed eligible failures, in fractional requests."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(requests, list) or not isinstance(eligible_classes, (list, tuple)) or not eligible_classes:
        return "INCOMPLETE"
    if not finite_measure(target) or not 0 < target <= 1:
        return "INCOMPLETE"
    seen = set()
    eligible = failures = 0
    for request in requests:
        if not isinstance(request, dict):
            return "INCOMPLETE"
        identity, category, success = (request.get(k) for k in ("id", "class", "success"))
        if not isinstance(identity, str) or not identity or not category or type(success) is not bool:
            return "INCOMPLETE"
        if identity in seen:
            return "INCOMPLETE"
        seen.add(identity)
        if category in eligible_classes:
            eligible += 1
            failures += int(not success)
    if not eligible or not isinstance(report, dict):
        return "INCOMPLETE"
    remaining = eligible * (1 - Fraction(str(target))) - failures
    for key, expected in (("eligible", eligible), ("failures", failures), ("remaining", remaining)):
        value = report.get(key)
        if value is None:
            return "INCOMPLETE"
        if not finite_measure(value) or Fraction(str(value)) != expected:
            return "FAIL"
    return "PASS"


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


def peak_memory_quality(tasks, reported, reported_unit):
    """Sum constant allocations for tasks active in half-open intervals [start, end)."""
    from collections import defaultdict
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    scales = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    if not isinstance(tasks, dict) or not tasks or reported is None or reported_unit not in scales:
        return "INCOMPLETE"
    if not finite_measure(reported) or reported < 0:
        return "FAIL"
    changes = defaultdict(Fraction)
    for row in tasks.values():
        if not isinstance(row, dict) or any(row.get(k) is None for k in ("start", "end", "allocation", "unit")):
            return "INCOMPLETE"
        if row["unit"] not in scales:
            return "INCOMPLETE"
        start, end, amount = row["start"], row["end"], row["allocation"]
        if not all(map(finite_measure, (start, end, amount))) or end < start or amount < 0:
            return "FAIL"
        size = Fraction(str(amount)) * scales[row["unit"]]
        changes[start] += size
        changes[end] -= size
    active = peak = 0
    for time in sorted(changes):
        active += changes[time]
        peak = max(peak, active)
    return "PASS" if peak == Fraction(str(reported)) * scales[reported_unit] else "FAIL"


def usage_cost_quality(usage, tariff, version, currency, charges, reported_total):
    """Reconcile exact synthetic line charges in a single currency with no intermediate rounding."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(usage, dict) or not usage or not isinstance(charges, list) or not isinstance(tariff, dict):
        return "INCOMPLETE"
    if not all(isinstance(v, str) and v for v in (version, currency, tariff.get("version"), tariff.get("currency"))):
        return "INCOMPLETE"
    if (version, currency) != (tariff["version"], tariff["currency"]):
        return "FAIL"
    if not isinstance(tariff.get("rates"), dict) or reported_total is None:
        return "INCOMPLETE"
    seen = set()
    total = Fraction(0)
    for charge in charges:
        if not isinstance(charge, dict) or not isinstance(charge.get("usage_id"), str):
            return "INCOMPLETE"
        key = charge["usage_id"]
        if key in seen or key not in usage:
            return "FAIL"
        seen.add(key)
        row = usage[key]
        if not isinstance(row, dict) or not isinstance(row.get("meter"), str):
            return "INCOMPLETE"
        rate = tariff["rates"].get(row["meter"])
        if not isinstance(rate, dict) or any(v is None for v in (rate.get("unit"), row.get("unit"), charge.get("currency"))):
            return "INCOMPLETE"
        if row["unit"] != rate["unit"] or charge["currency"] != currency:
            return "FAIL"
        values = (row.get("quantity"), rate.get("unit_price"), charge.get("amount"))
        if None in values:
            return "INCOMPLETE"
        if not all(finite_measure(v) and v >= 0 for v in values):
            return "FAIL"
        quantity, price, amount = (Fraction(str(v)) for v in values)
        if amount != quantity * price:
            return "FAIL"
        total += amount
    if seen != usage.keys() or not finite_measure(reported_total):
        return "FAIL"
    return "PASS" if total == Fraction(str(reported_total)) else "FAIL"


def capacity_utilization_quality(start, end, capacities, busy, reported):
    """Integrate occupied and available slot-time over one half-open integer time window."""
    from collections import defaultdict
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(capacities, list) or not capacities or not isinstance(busy, list) or None in (start, end, reported):
        return "INCOMPLETE"
    if type(start) is not int or type(end) is not int or end <= start or not finite_measure(reported):
        return "FAIL"
    levels = {}
    for row in capacities:
        if not isinstance(row, dict) or row.get("at") is None or row.get("slots") is None:
            return "INCOMPLETE"
        at, slots = row["at"], row["slots"]
        if type(at) is not int or type(slots) is not int or slots < 0 or at in levels:
            return "FAIL"
        levels[at] = slots
    prior = [at for at in levels if at <= start]
    if not prior:
        return "INCOMPLETE"
    capacity = levels[max(prior)]
    changes = defaultdict(int)
    for row in busy:
        if not isinstance(row, dict) or any(row.get(k) is None for k in ("start", "end", "slots")):
            return "INCOMPLETE"
        left, right, slots = row["start"], row["end"], row["slots"]
        if not all(type(v) is int for v in (left, right, slots)) or right < left or slots <= 0:
            return "FAIL"
        left, right = max(left, start), min(right, end)
        if left < right:
            changes[left] += slots
            changes[right] -= slots
    boundaries = sorted({start, end, *changes, *(at for at in levels if start < at < end)})
    occupied = used_time = available_time = 0
    for left, right in zip(boundaries, boundaries[1:]):
        occupied += changes[left]
        capacity = levels.get(left, capacity)
        if occupied > capacity:
            return "FAIL"
        used_time += occupied * (right-left)
        available_time += capacity * (right-left)
    if not available_time:
        return "INCOMPLETE"
    return "PASS" if 0 <= reported <= 1 and isclose(reported, float(Fraction(used_time, available_time)), rel_tol=1e-12, abs_tol=1e-12) else "FAIL"


def queue_service_quality(runs, reported):
    """Reconcile each completed run in one clock domain. Both durations use timestamp units."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(runs, dict) or not runs or not isinstance(reported, dict):
        return "INCOMPLETE"
    if reported.keys() != runs.keys():
        return "FAIL"
    for key, row in runs.items():
        fields = ("enqueued", "started", "finished")
        if not isinstance(row, dict) or any(row.get(k) is None for k in fields):
            return "INCOMPLETE"
        if not all(finite_measure(row[k]) for k in fields):
            return "FAIL"
        queued, started, finished = (Fraction(str(row[k])) for k in fields)
        if not queued <= started <= finished:
            return "FAIL"
        result = reported[key]
        if not isinstance(result, dict) or any(result.get(k) is None for k in ("wait","service")):
            return "INCOMPLETE"
        for k, expected in (("wait",started-queued), ("service",finished-started)):
            if not finite_measure(result[k]) or Fraction(str(result[k])) != expected:
                return "FAIL"
    return "PASS"


def availability_quality(window, outages, maintenance, policy, reported):
    """Fraction of eligible wall-clock time available in a half-open window."""
    from benches.x03_quality import finite_measure
    if policy not in ("exclude", "include") or not isinstance(outages, list) or not isinstance(maintenance, list):
        return "INCOMPLETE"
    def valid(span):
        return isinstance(span, (tuple, list)) and len(span) == 2 and all(map(finite_measure, span)) and span[0] < span[1]
    if not valid(window) or not all(valid(span) for span in outages + maintenance):
        return "INCOMPLETE"
    start, end = window
    events = Counter()
    # Split at transitions. Multiple overlapping records count only once.
    changes = {}
    for kind, spans in enumerate([outages, maintenance if policy == "exclude" else []]):
        for left, right in spans:
            left, right = max(start, left), min(end, right)
            if left < right:
                for point, delta in ((left, 1), (right, -1)):
                    changes.setdefault(point, Counter())[kind] += delta
    eligible = available = 0
    previous = start
    for point in sorted(set(changes) | {end}):
        if events[1] == 0:
            eligible += point - previous
            if events[0] == 0:
                available += point - previous
        events.update(changes.get(point, {}))
        previous = point
    if not eligible or reported is None:
        return "INCOMPLETE"
    return "PASS" if finite_measure(reported) and isclose(reported, available / eligible, abs_tol=1e-12) else "FAIL"


def burn_window_quality(outcomes, coverage, as_of, windows, target, report):
    """All declared windows must meet their inclusive threshold to alert. Times are seconds."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(outcomes, list) or not isinstance(windows, dict) or not windows:
        return "INCOMPLETE"
    if not isinstance(coverage, (list, tuple)) or len(coverage) != 2 or not all(map(finite_measure, coverage)):
        return "INCOMPLETE"
    if not all(map(finite_measure, (as_of, target))) or not 0 < target < 1 or coverage[1] < as_of:
        return "INCOMPLETE"
    for event in outcomes:
        if not isinstance(event, (tuple, list)) or len(event) != 2 or not finite_measure(event[0]) or type(event[1]) is not bool:
            return "INCOMPLETE"
    rates = {}
    alert = True
    for width, threshold in windows.items():
        if not all(map(finite_measure, (width, threshold))) or width <= 0 or threshold < 0 or coverage[0] > as_of - width:
            return "INCOMPLETE"
        samples = [ok for t, ok in outcomes if as_of - width < t <= as_of]
        if not samples:
            return "INCOMPLETE"
        rate = Fraction(samples.count(False), len(samples)) / (1 - Fraction(str(target)))
        rates[width] = rate
        alert = alert and rate >= Fraction(str(threshold))
    if not isinstance(report, dict) or not isinstance(report.get("rates"), dict) or "alert" not in report:
        return "INCOMPLETE"
    if rates.keys() != report["rates"].keys() or report["alert"] is not alert:
        return "FAIL"
    return "PASS" if all(finite_measure(report["rates"][w]) and isclose(report["rates"][w], float(r), rel_tol=1e-12)
                         for w, r in rates.items()) else "FAIL"


def queue_balance_quality(opening, admissions, completions, cancellations, closing):
    """Reconcile unique work IDs across one reporting interval."""
    inventories = []
    for rows in (opening, admissions, completions, cancellations, closing):
        if not isinstance(rows, list) or not all(isinstance(work, str) and work for work in rows):
            return "INCOMPLETE"
        ids = set(rows)
        if len(ids) != len(rows):
            return "FAIL"
        inventories.append(ids)
    start, added, done, cancelled, end = inventories
    if start & added or done & cancelled:
        return "FAIL"
    available = start | added
    removed = done | cancelled
    return "PASS" if removed <= available and end == available - removed else "FAIL"


def mean_queue_quality(start, end, opening_count, events, reported):
    """Integrate occupancy over [start, end), grouping simultaneous changes."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not all(map(finite_measure, (start, end))) or end <= start or type(opening_count) is not int or opening_count < 0:
        return "INCOMPLETE"
    if not isinstance(events, list) or reported is None:
        return "INCOMPLETE"
    changes = Counter()
    for event in events:
        if not isinstance(event, (list, tuple)) or len(event) != 2 or not finite_measure(event[0]):
            return "INCOMPLETE"
        stamp, action = event
        if not start <= stamp < end or action not in ("enqueue", "depart"):
            return "FAIL"
        changes[Fraction(str(stamp))] += 1 if action == "enqueue" else -1
    previous = Fraction(str(start))
    count, area = opening_count, Fraction(0)
    for when in sorted(changes):
        area += count * (when - previous)
        count += changes[when]
        if count < 0:
            return "FAIL"
        previous = when
    area += count * (Fraction(str(end)) - previous)
    mean = area / (Fraction(str(end)) - Fraction(str(start)))
    return "PASS" if finite_measure(reported) and isclose(reported, float(mean), rel_tol=1e-12, abs_tol=1e-12) else "FAIL"


def scheduled_throughput_quality(completions, shifts, reported):
    """Count completed work in the union of scheduled half-open intervals in seconds."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(completions, dict) or not isinstance(shifts, list) or not shifts or reported is None:
        return "INCOMPLETE"
    if not all(map(finite_measure, completions.values())):
        return "INCOMPLETE"
    spans = []
    for shift in shifts:
        if not isinstance(shift, (list, tuple)) or len(shift) != 2 or not all(map(finite_measure, shift)) or shift[0] >= shift[1]:
            return "INCOMPLETE"
        spans.append(tuple(Fraction(str(v)) for v in shift))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    count = sum(any(a <= Fraction(str(t)) < b for a,b in merged) for t in completions.values())
    rate = 3600 * count / sum(b-a for a,b in merged)
    return "PASS" if finite_measure(reported) and isclose(reported, float(rate), rel_tol=1e-12) else "FAIL"


def deadline_miss_quality(inventory, due, completed, as_of, reported_misses):
    """Snapshot missed IDs. An unfinished item becomes overdue strictly after its due time."""
    from benches.x03_quality import finite_measure
    if not isinstance(inventory, list) or not all(isinstance(i,str) and i for i in inventory):
        return "INCOMPLETE"
    if not isinstance(due, dict) or not isinstance(completed, dict) or not finite_measure(as_of):
        return "INCOMPLETE"
    ids = set(inventory)
    if len(ids) != len(inventory) or due.keys() != ids or not completed.keys() <= ids:
        return "INCOMPLETE"
    if not all(map(finite_measure, due.values())) or not all(map(finite_measure, completed.values())):
        return "INCOMPLETE"
    missed = {i for i in ids if min(completed.get(i, as_of), as_of) > due[i]}
    if not isinstance(reported_misses, list) or not all(isinstance(i,str) for i in reported_misses):
        return "INCOMPLETE"
    return "PASS" if len(reported_misses) == len(missed) and set(reported_misses) == missed else "FAIL"


def little_law_quality(metrics, steady_state, tolerance):
    """Compare L to lambda times W, in jobs, jobs/second and seconds respectively."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if steady_state is not True or not finite_measure(tolerance) or tolerance < 0 or not isinstance(metrics, dict):
        return "INCOMPLETE"
    boundaries = []
    values = []
    for metric in ("mean_jobs", "jobs_per_second", "mean_seconds"):
        row = metrics.get(metric)
        if not isinstance(row, dict) or not isinstance(row.get("population"), str) or not row["population"]:
            return "INCOMPLETE"
        start, end, value = (row.get(k) for k in ("start", "end", "value"))
        if not all(map(finite_measure, (start,end,value))) or end <= start:
            return "INCOMPLETE"
        if value < 0:
            return "FAIL"
        boundaries.append((row["population"],start,end))
        values.append(Fraction(str(value)))
    if len(set(boundaries)) != 1:
        return "FAIL"
    jobs, rate, seconds = values
    return "PASS" if abs(jobs-rate*seconds) <= Fraction(str(tolerance)) else "FAIL"


def starvation_quality(ready, dispatches, as_of, aging_limits, reported):
    """Flag any continuous ready spell exceeding its priority's maximum eligible wait.

    Ready rows are (job, priority, start, end), with None for a still-ready job.
    Dispatches are keyed by (job, ready-start) so repeated spells stay distinct.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(ready,list) or not isinstance(dispatches,dict) or not isinstance(aging_limits,dict) or not aging_limits or not finite_measure(as_of):
        return "INCOMPLETE"
    seen, overdue = set(), set()
    for row in ready:
        if not isinstance(row,(tuple,list)) or len(row) != 4:
            return "INCOMPLETE"
        job, priority, start, end = row
        if not all(isinstance(v,str) and v for v in (job,priority)) or not finite_measure(start):
            return "INCOMPLETE"
        limit = aging_limits.get(priority)
        if not finite_measure(limit) or limit < 0 or (end is not None and (not finite_measure(end) or end < start)):
            return "INCOMPLETE"
        key = (job,start)
        if key in seen:
            return "INCOMPLETE"
        seen.add(key)
        stop = as_of if end is None else min(as_of,end)
        if key in dispatches:
            at = dispatches[key]
            if not finite_measure(at) or at < start or (end is not None and at > end):
                return "INCOMPLETE"
            stop = min(stop,at)
        if Fraction(str(stop)) - Fraction(str(start)) > Fraction(str(limit)):
            overdue.add(job)
    if not dispatches.keys() <= seen or not isinstance(reported,list) or not all(isinstance(v,str) for v in reported):
        return "INCOMPLETE"
    return "PASS" if len(reported) == len(overdue) and set(reported) == overdue else "FAIL"
