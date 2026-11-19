"""Reconcile external failure and output-change rows to revision-bound runs."""
from collections import Counter
from math import isclose, isfinite
import json
import re


def pending_age_quality(start, cutoff, opening, admissions, departures, percentile, reported, policy):
    """Age all work still pending at the inclusive cutoff using original enqueue times."""
    from benches.x03_quality import finite_measure
    if not all(map(finite_measure, (start,cutoff))) or start > cutoff:
        return "INCOMPLETE"
    if not all(isinstance(table,dict) for table in (opening,admissions,departures)):
        return "INCOMPLETE"
    if not all(finite_measure(t) for table in (opening,admissions,departures) for t in table.values()):
        return "INCOMPLETE"
    if opening.keys() & admissions.keys() or any(t > start for t in opening.values()) or any(t < start for t in admissions.values()):
        return "FAIL"
    entered = {**opening, **admissions}
    if not departures.keys() <= entered.keys() or any(t < max(start,entered[k]) for k,t in departures.items()):
        return "FAIL"
    ages = [cutoff-t for k,t in entered.items() if t <= cutoff and (k not in departures or departures[k] > cutoff)]
    return latency_percentile_quality(ages, percentile, reported, policy)


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


def abandonment_quality(admissions, starts, abandoned, reported_served):
    """Reconcile a closed queue cohort at service start using per-request event times."""
    from benches.x03_quality import finite_measure
    if not all(isinstance(events, dict) for events in (admissions, starts, abandoned)):
        return "INCOMPLETE"
    if not isinstance(reported_served, set):
        return "INCOMPLETE"
    for events in (admissions, starts, abandoned):
        if not all(map(finite_measure, events.values())):
            return "INCOMPLETE"
    if starts.keys() & abandoned.keys():
        return "FAIL"
    terminal = {**starts, **abandoned}
    if terminal.keys() - admissions.keys():
        return "FAIL"
    if admissions.keys() - terminal.keys():
        return "INCOMPLETE"
    if any(time < admissions[key] for key, time in terminal.items()):
        return "FAIL"
    return "PASS" if reported_served == starts.keys() else "FAIL"


def histogram_merge_quality(shards, reported):
    """Merge interval counts only for identical units and edges and disjoint sample IDs."""
    from benches.x03_quality import finite_measure
    if not isinstance(shards, list) or not shards or not isinstance(reported, list):
        return "INCOMPLETE"
    schema = None
    seen = set()
    for shard in shards:
        if not isinstance(shard, dict):
            return "INCOMPLETE"
        unit, edges, ids, counts = (shard.get(k) for k in ("unit", "edges", "sample_ids", "counts"))
        if not isinstance(unit, str) or not unit or not isinstance(edges, list) or len(edges) < 2:
            return "INCOMPLETE"
        if not all(map(finite_measure, edges)) or any(a >= b for a, b in zip(edges, edges[1:])):
            return "INCOMPLETE"
        if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            return "INCOMPLETE"
        if not isinstance(counts, list) or len(counts) != len(edges)-1 or not all(type(n) is int and n >= 0 for n in counts):
            return "INCOMPLETE"
        if len(set(ids)) != len(ids) or seen.intersection(ids) or sum(counts) != len(ids):
            return "FAIL"
        if schema is None:
            schema = (unit, edges)
            totals = [0] * len(counts)
        elif (unit, edges) != schema:
            return "FAIL"
        seen.update(ids)
        totals = [a+b for a, b in zip(totals, counts)]
    return "PASS" if all(type(n) is int for n in reported) and reported == totals else "FAIL"


def failure_streak_quality(rows, reset_outcomes, reported_longest):
    """Count the longest failure run in a complete sequence, ignoring declared neutral outcomes."""
    if not isinstance(rows, list) or not rows or not isinstance(reset_outcomes, set) or "failure" in reset_outcomes:
        return "INCOMPLETE"
    if type(reported_longest) is not int:
        return "INCOMPLETE"
    ordered = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            return "INCOMPLETE"
        sequence, outcome = row
        if type(sequence) is not int or sequence in ordered or not isinstance(outcome, str):
            return "INCOMPLETE"
        if outcome not in reset_outcomes and outcome not in ("failure", "skipped"):
            return "INCOMPLETE"
        ordered[sequence] = outcome
    if max(ordered) - min(ordered) + 1 != len(ordered):
        return "INCOMPLETE"
    longest = streak = 0
    for sequence in sorted(ordered):
        outcome = ordered[sequence]
        if outcome in reset_outcomes:
            streak = 0
        elif outcome == "failure":
            streak += 1
            longest = max(longest, streak)
    return "PASS" if longest == reported_longest else "FAIL"


def counter_throughput_quality(epochs, modulus, reported):
    """Sum within-epoch changes, using explicit wrap counts and simultaneous reset snapshots."""
    if type(modulus) is not int or modulus <= 0 or type(reported) is not int:
        return "INCOMPLETE"
    if not isinstance(epochs, list) or not epochs:
        return "INCOMPLETE"
    last_end = None
    seen = set()
    total = 0
    for epoch in epochs:
        if not isinstance(epoch, dict):
            return "INCOMPLETE"
        identity, samples = epoch.get("epoch"), epoch.get("samples")
        if not isinstance(identity, str) or not identity or identity in seen or not isinstance(samples, list) or len(samples) < 2:
            return "INCOMPLETE"
        seen.add(identity)
        previous = None
        for row in samples:
            if not isinstance(row, (tuple, list)) or len(row) != 3 or any(type(n) is not int for n in row):
                return "INCOMPLETE"
            stamp, count, wraps = row
            if not 0 <= count < modulus or wraps < 0:
                return "INCOMPLETE"
            value = wraps * modulus + count
            if previous is None:
                if last_end is not None and (stamp != last_end or value != 0):
                    return "INCOMPLETE"
            else:
                if stamp <= previous[0] or value < previous[1]:
                    return "INCOMPLETE"
                total += value - previous[1]
            previous = stamp, value
        last_end = previous[0]
    return "PASS" if reported == total else "FAIL"


def admission_limit_quality(required_ids, starts, terminals, limit_changes):
    """Closed cohort. Releases and new limits take effect before starts at the same instant."""
    from collections import Counter
    from benches.x03_quality import finite_measure
    if not isinstance(required_ids, set) or not all(isinstance(v, dict) for v in (starts, terminals, limit_changes)):
        return "INCOMPLETE"
    if starts.keys() != required_ids or terminals.keys() != required_ids or not limit_changes:
        return "INCOMPLETE"
    if not all(map(finite_measure, (*starts.values(), *terminals.values(), *limit_changes.keys()))):
        return "INCOMPLETE"
    if any(type(limit) is not int or limit < 0 for limit in limit_changes.values()):
        return "INCOMPLETE"
    if any(terminals[k] <= starts[k] for k in required_ids):
        return "INCOMPLETE"
    arrivals, releases = Counter(starts.values()), Counter(terminals.values())
    active = 0
    limit = None
    for stamp in sorted(arrivals.keys() | releases.keys() | limit_changes.keys()):
        active -= releases[stamp]
        limit = limit_changes.get(stamp, limit)
        if arrivals[stamp]:
            if limit is None:
                return "INCOMPLETE"
            active += arrivals[stamp]
            if active > limit:
                return "FAIL"
    return "PASS"


def cancellation_latency_quality(requests, acknowledgements, stops, reported_latencies, max_seconds):
    """Verify per-worker request-to-stop latency, including the interval after acknowledgement."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    records = (requests, acknowledgements, stops, reported_latencies)
    if not all(isinstance(record, dict) for record in records) or not requests:
        return "INCOMPLETE"
    if not finite_measure(max_seconds) or max_seconds < 0:
        return "INCOMPLETE"
    if any(record.keys() != requests.keys() for record in records[1:]):
        return "INCOMPLETE"
    if not all(finite_measure(v) for record in records for v in record.values()):
        return "INCOMPLETE"
    for worker in requests:
        request, ack, stop, reported = (Fraction(str(record[worker])) for record in records)
        if not request <= ack <= stop:
            return "FAIL"
        latency = stop - request
        if reported != latency or latency > Fraction(str(max_seconds)):
            return "FAIL"
    return "PASS"


def scheduler_fairness_quality(service, entitlements, allowed_share_error):
    """Continuously backlogged queues. Compare service fractions to normalized entitlement fractions."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(service, dict) or not service or not isinstance(entitlements, dict) or service.keys() != entitlements.keys():
        return "INCOMPLETE"
    if not finite_measure(allowed_share_error) or not 0 <= allowed_share_error <= 1:
        return "INCOMPLETE"
    if not all(finite_measure(v) and v >= 0 for v in service.values()) or not all(finite_measure(v) and v > 0 for v in entitlements.values()):
        return "INCOMPLETE"
    amounts = {k: Fraction(str(v)) for k,v in service.items()}
    weights = {k: Fraction(str(v)) for k,v in entitlements.items()}
    total, weight = sum(amounts.values()), sum(weights.values())
    if not total:
        return "INCOMPLETE"
    for queue in service:
        if abs(amounts[queue]/total - weights[queue]/weight) > Fraction(str(allowed_share_error)):
            return "FAIL"
    return "PASS"




def latency_bounds_quality(send_s, receive_s, send_offset, receive_offset, reported):
    """Clock calibration gives local minus reference time as a closed interval."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not all(isinstance(pair, (list, tuple)) and len(pair) == 2 for pair in (send_offset, receive_offset, reported)):
        return "INCOMPLETE"
    values = (send_s, receive_s, *send_offset, *receive_offset, *reported)
    if not all(map(finite_measure, values)):
        return "INCOMPLETE"
    send, receive, s_low, s_high, r_low, r_high, lower, upper = (Fraction(str(v)) for v in values)
    if s_low > s_high or r_low > r_high:
        return "INCOMPLETE"
    earliest = max(0, receive-send+s_low-r_high)
    latest = receive-send+s_high-r_low
    return "PASS" if latest >= earliest and (lower, upper) == (earliest, latest) else "FAIL"


def preemption_quality(events, reported_active):
    """Sum active service from start through completion for one recorded job."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(events, list) or not events or not finite_measure(reported_active):
        return "INCOMPLETE"
    state = "initial"
    previous = None
    active = Fraction(0)
    transitions = {("initial","start"):"running", ("running","suspend"):"paused",
                   ("paused","resume"):"running", ("running","complete"):"done"}
    for row in events:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return "INCOMPLETE"
        stamp, event = row
        if not finite_measure(stamp) or not isinstance(event, str):
            return "INCOMPLETE"
        stamp = Fraction(str(stamp))
        if previous is not None and stamp < previous:
            return "FAIL"
        if state == "initial" and event != "start":
            return "INCOMPLETE"
        if (state, event) not in transitions:
            return "FAIL"
        if state == "running":
            active += stamp - previous
        state = transitions[state, event]
        previous = stamp
    if state != "done":
        return "INCOMPLETE"
    return "PASS" if active == Fraction(str(reported_active)) else "FAIL"


def autoscaler_quality(revision, policy, ticks, opening, prior_change_tick):
    """One decision per tick. Downscale to the highest demand in the pinned trailing window."""
    if not revision or not isinstance(policy, dict) or policy.get("revision") != revision:
        return "INCOMPLETE"
    window, cooldown = policy.get("down_window_ticks"), policy.get("cooldown_ticks")
    if any(type(v) is not int for v in (window,cooldown,opening,prior_change_tick)):
        return "INCOMPLETE"
    if window < 1 or min(cooldown,opening) < 0 or not isinstance(ticks, list) or not ticks:
        return "INCOMPLETE"
    history = []
    previous = None
    replicas = opening
    for row in ticks:
        if not isinstance(row, (tuple,list)) or len(row) != 3 or any(type(v) is not int for v in row):
            return "INCOMPLETE"
        stamp, demand, observed = row
        if min(demand,observed) < 0 or (previous is not None and stamp != previous+1):
            return "INCOMPLETE"
        if previous is None and prior_change_tick >= stamp:
            return "INCOMPLETE"
        history.append(demand)
        expected = replicas
        if stamp-prior_change_tick >= cooldown:
            if demand > replicas:
                expected = demand
            elif demand < replicas:
                recent_peak = max(history[-window:])
                if len(history) < window and recent_peak < replicas:
                    return "INCOMPLETE"
                expected = min(replicas, recent_peak)
        if observed != expected:
            return "FAIL"
        if observed != replicas:
            prior_change_tick = stamp
        replicas, previous = observed, stamp
    return "PASS"


def work_steal_quality(opening, events, closing, *, complete):
    """Ownership advances one epoch at ack. An unacknowledged offer leaves the donor in charge."""
    if complete is not True or not all(isinstance(x, list) for x in (opening, events, closing)):
        return "INCOMPLETE"
    for snapshot in (opening, closing):
        for row in snapshot:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                return "INCOMPLETE"
            task, owner, epoch = row
            if not all(isinstance(x, str) and x for x in (task, owner)) or type(epoch) is not int or epoch < 0:
                return "INCOMPLETE"
    owners = {task:(owner, epoch) for task, owner, epoch in opening}
    if len(owners) != len(opening):
        return "FAIL"
    pending = {}
    for event in events:
        if not isinstance(event, (list, tuple)) or len(event) != 5:
            return "INCOMPLETE"
        kind, task, donor, recipient, epoch = event
        if not all(isinstance(x, str) and x for x in (task, donor, recipient)) or type(epoch) is not int:
            return "INCOMPLETE"
        if kind not in ("offer", "ack"):
            return "INCOMPLETE"
        if owners.get(task) != (donor, epoch) or donor == recipient:
            return "FAIL"
        if kind == "offer":
            if task in pending:
                return "FAIL"
            pending[task] = recipient
        else:
            if pending.pop(task, None) != recipient:
                return "FAIL"
            owners[task] = recipient, epoch + 1
    actual = {task:(owner, epoch) for task, owner, epoch in closing}
    return "PASS" if len(actual) == len(closing) and actual == owners else "FAIL"


def checkpoint_work_quality(attempts, reported_units, *, complete):
    """Count the durable prefix plus final completion for a sequential range task.

    Each attempt records its parent checkpoint, executed half-open range, and an
    optional new durable checkpoint (identity and exclusive offset). A retry
    starts at the most recent durable offset, so replay adds no units.
    """
    if complete is not True or not isinstance(attempts, list) or not attempts or type(reported_units) is not int:
        return "INCOMPLETE"
    parent, offset = None, 0
    seen = set()
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict) or not {"parent", "range", "checkpoint", "finished"} <= attempt.keys():
            return "INCOMPLETE"
        bounds = attempt["range"]
        if not isinstance(bounds, (tuple,list)) or len(bounds) != 2 or not all(type(x) is int for x in bounds):
            return "INCOMPLETE"
        start, end = bounds
        if attempt["parent"] != parent:
            return "INCOMPLETE"
        if start != offset or end < start or type(attempt["finished"]) is not bool:
            return "INCOMPLETE"
        checkpoint = attempt["checkpoint"]
        if checkpoint is not None:
            if not isinstance(checkpoint, (tuple,list)) or len(checkpoint) != 2:
                return "INCOMPLETE"
            name, saved = checkpoint
            if not isinstance(name,str) or not name or name in seen or type(saved) is not int or not start <= saved <= end:
                return "INCOMPLETE"
            seen.add(name)
            parent, offset = name, saved
        if attempt["finished"] != (index == len(attempts)-1):
            return "INCOMPLETE"
    return "PASS" if reported_units == end else "FAIL"


def placement_feasibility_quality(workers, demands, allowed, reported_feasible):
    """Exact placement for small recorded snapshots, with indivisible tasks and worker affinity."""
    def resource_pair(pair):
        return isinstance(pair, (list,tuple)) and len(pair) == 2 and all(type(n) is int and n >= 0 for n in pair)
    if type(reported_feasible) is not bool or not all(isinstance(x,dict) for x in (workers,demands,allowed)):
        return "INCOMPLETE"
    if allowed.keys() != demands.keys() or not all(map(resource_pair, workers.values())) or not all(map(resource_pair, demands.values())):
        return "INCOMPLETE"
    for candidates in allowed.values():
        if not isinstance(candidates, list) or not all(isinstance(w,str) and w in workers for w in candidates):
            return "INCOMPLETE"
    tasks = sorted(demands, key=lambda t: len(allowed[t]))
    free = dict(workers)
    def fits(index):
        if index == len(tasks):
            return True
        task = tasks[index]
        cpu, memory = demands[task]
        for worker in allowed[task]:
            cores, ram = free[worker]
            if cpu <= cores and memory <= ram:
                free[worker] = (cores-cpu, ram-memory)
                placed = fits(index+1)
                free[worker] = (cores,ram)
                if placed:
                    return True
        return False
    return "PASS" if fits(0) == reported_feasible else "FAIL"


def cross_worker_latency_quality(start, finish, offsets, reported_bounds):
    """Check tight latency bounds in integer ticks for distinct worker clocks.

    An offset interval bounds local time minus reference time at the event.
    Each event consists of its worker ID and its local timestamp.
    """
    if not isinstance(offsets, dict):
        return "INCOMPLETE"
    reference = []
    for event in (start, finish):
        if not isinstance(event, (tuple, list)) or len(event) != 2:
            return "INCOMPLETE"
        worker, local_tick = event
        if not isinstance(worker, str) or not worker or type(local_tick) is not int:
            return "INCOMPLETE"
        bounds = offsets.get(worker)
        if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
            return "INCOMPLETE"
        if not all(type(x) is int for x in bounds) or bounds[0] > bounds[1]:
            return "INCOMPLETE"
        reference.append((local_tick - bounds[1], local_tick - bounds[0]))
    if start[0] == finish[0]:
        return "INCOMPLETE"
    earliest = reference[1][0] - reference[0][1]
    latest = reference[1][1] - reference[0][0]
    if latest < 0:
        return "FAIL"
    if earliest <= 0 <= latest and earliest != latest:
        return "INCOMPLETE"
    if not isinstance(reported_bounds, (list, tuple)) or len(reported_bounds) != 2:
        return "INCOMPLETE"
    if not all(type(x) is int for x in reported_bounds):
        return "INCOMPLETE"
    return "PASS" if tuple(reported_bounds) == (earliest, latest) else "FAIL"


def priority_inversion_quality(rule, waiter, priorities, wait, segments, *, acquired):
    """Grade one high-priority wait for a single occupied lock.

    Higher integer priorities run first. The trace covers the full wait with
    contiguous half-open intervals of owner ID and effective owner priority.
    The lock is handed to the waiter at acquisition. An open wait supplies a
    lower bound on blocking time only.
    """
    if not isinstance(rule, dict) or rule.get("policy") != "single-lock-immediate-inheritance-v1":
        return "INCOMPLETE"
    limit = rule.get("max_wait_ticks")
    if type(limit) is not int or limit < 0 or type(acquired) is not bool:
        return "INCOMPLETE"
    if not isinstance(priorities, dict) or not isinstance(waiter, str) or not waiter:
        return "INCOMPLETE"
    waiter_priority = priorities.get(waiter)
    if type(waiter_priority) is not int:
        return "INCOMPLETE"
    if not isinstance(wait, (tuple, list)) or len(wait) != 2 or not all(type(t) is int for t in wait):
        return "INCOMPLETE"
    start, end = wait
    if end <= start or not isinstance(segments, list):
        return "INCOMPLETE"
    cursor = start
    inherited = True
    for row in segments:
        if not isinstance(row, (tuple, list)) or len(row) != 4:
            return "INCOMPLETE"
        left, right, owner, effective = row
        if type(left) is not int or type(right) is not int or left != cursor or right <= left:
            return "INCOMPLETE"
        if not isinstance(owner, str) or not owner or owner == waiter or type(effective) is not int:
            return "INCOMPLETE"
        base = priorities.get(owner)
        if type(base) is not int:
            return "INCOMPLETE"
        if effective < max(base, waiter_priority):
            inherited = False
        cursor = right
    if cursor != end:
        return "INCOMPLETE"
    if not inherited or end - start > limit:
        return "FAIL"
    return "PASS" if acquired else "INCOMPLETE"


def head_of_line_quality(policy, queue, workers, demand, free, eligible, assignments):
    """Replay one dispatch per idle worker in recorded order.

    Demands and free capacities use one common resource unit. Eligibility gives
    separate placement constraints for every queued task and worker pair.
    """
    if policy not in ("strict-fifo-v1", "first-compatible-bypass-v1"):
        return "INCOMPLETE"
    if not all(isinstance(ids, list) for ids in (queue, workers)):
        return "INCOMPLETE"
    for ids in (queue, workers):
        if not all(isinstance(x, str) and x for x in ids) or len(ids) != len(set(ids)):
            return "INCOMPLETE"
    if not all(isinstance(mapping, dict) for mapping in (demand, free, eligible, assignments)):
        return "INCOMPLETE"
    if set(assignments) != set(workers):
        return "INCOMPLETE"
    for worker in workers:
        if type(free.get(worker)) is not int or free[worker] < 0:
            return "INCOMPLETE"
    for task in queue:
        if type(demand.get(task)) is not int or demand[task] < 0:
            return "INCOMPLETE"
        choices = eligible.get(task)
        if not isinstance(choices, dict) or any(type(choices.get(w)) is not bool for w in workers):
            return "INCOMPLETE"
    pending = list(queue)
    for worker in workers:
        candidates = pending[:1] if policy == "strict-fifo-v1" else pending
        selected = next((task for task in candidates
                         if eligible[task][worker] and demand[task] <= free[worker]), None)
        if assignments[worker] != selected:
            return "FAIL"
        if selected is not None:
            pending.remove(selected)
    return "PASS"


def resource_deadlock_quality(policy, owners, waits, release):
    """Check exclusive resources when each worker requires every listed resource.

    A held resource is released on worker completion or by a guaranteed timer
    independent of worker progress. None explicitly records an unowned resource.
    """
    from collections import deque

    if policy != "exclusive-wait-all-v1" or not all(isinstance(x, dict) for x in (owners, waits, release)):
        return "INCOMPLETE"
    if owners.keys() != release.keys() or not all(isinstance(w, str) and w for w in waits):
        return "INCOMPLETE"
    for resource, owner in owners.items():
        if not isinstance(resource, str) or not resource:
            return "INCOMPLETE"
        if owner is not None and (not isinstance(owner, str) or owner not in waits):
            return "INCOMPLETE"
        if release[resource] not in ("on-completion", "independent-timer"):
            return "INCOMPLETE"
    dependents = {w: [] for w in waits}
    remaining = {}
    for worker, requests in waits.items():
        if not isinstance(requests, list):
            return "INCOMPLETE"
        dependencies = set()
        for resource in requests:
            if not isinstance(resource, str) or resource not in owners:
                return "INCOMPLETE"
            owner = owners[resource]
            if owner is not None and release[resource] == "on-completion":
                dependencies.add(owner)
        remaining[worker] = len(dependencies)
        for owner in dependencies:
            dependents[owner].append(worker)
    ready = deque(w for w in waits if remaining[w] == 0)
    completed = 0
    while ready:
        owner = ready.popleft()
        completed += 1
        for worker in dependents[owner]:
            remaining[worker] -= 1
            if remaining[worker] == 0:
                ready.append(worker)
    return "PASS" if completed == len(waits) else "FAIL"


def backpressure_hysteresis_quality(low, high, window, segments, *, initially_paused):
    """Check each recorded intake decision after sampling buffer occupancy.

    The trace covers every decision in a contiguous interval. Occupancy at
    each segment start controls intake for that segment.
    """
    if type(low) is not int or type(high) is not int or not 0 <= low < high:
        return "INCOMPLETE"
    if type(initially_paused) is not bool or not isinstance(segments, list):
        return "INCOMPLETE"
    if not isinstance(window, (list, tuple)) or len(window) != 2 or not all(type(x) is int for x in window):
        return "INCOMPLETE"
    cursor, end = window
    if end <= cursor:
        return "INCOMPLETE"
    paused = initially_paused
    valid = True
    for segment in segments:
        if not isinstance(segment, (list, tuple)) or len(segment) != 5:
            return "INCOMPLETE"
        start, stop, occupancy, event, accepted = segment
        if not all(type(x) is int for x in (start, stop, occupancy, accepted)):
            return "INCOMPLETE"
        if start != cursor or stop <= start or min(occupancy, accepted) < 0:
            return "INCOMPLETE"
        if event not in ("pause", "resume", "none"):
            return "INCOMPLETE"
        expected = "none"
        if not paused and occupancy >= high:
            paused = True
            expected = "pause"
        elif paused and occupancy <= low:
            paused = False
            expected = "resume"
        if event != expected or (paused and accepted > 0):
            valid = False
        cursor = stop
    if cursor != end:
        return "INCOMPLETE"
    return "PASS" if valid else "FAIL"


def token_bucket_quality(capacity, refill_per_second, initial, arrivals):
    """Replay greedy whole-request admissions in arrival order, with a capped balance.

    Initial state is (reference time, available tokens). Each arrival records
    its time, token cost, and whether it was admitted. Rejected requests cost zero.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(initial, (tuple, list)) or len(initial) != 2:
        return "INCOMPLETE"
    values = (capacity, refill_per_second, *initial)
    if not all(map(finite_measure, values)) or not isinstance(arrivals, list):
        return "INCOMPLETE"
    cap, rate, previous, available = (Fraction(str(x)) for x in values)
    if cap <= 0 or rate < 0 or not 0 <= available <= cap:
        return "INCOMPLETE"
    valid = True
    for event in arrivals:
        if not isinstance(event, (tuple, list)) or len(event) != 3:
            return "INCOMPLETE"
        timestamp, cost, admitted = event
        if type(admitted) is not bool or not all(map(finite_measure, (timestamp, cost))):
            return "INCOMPLETE"
        timestamp, cost = Fraction(str(timestamp)), Fraction(str(cost))
        if timestamp < previous or cost <= 0:
            return "INCOMPLETE"
        available = min(cap, available + (timestamp - previous) * rate)
        affordable = available >= cost
        if admitted != affordable:
            valid = False
        if affordable:
            available -= cost
        previous = timestamp
    return "PASS" if valid else "FAIL"


def gang_start_quality(policy, cohorts, reservations, starts, *, complete):
    """Check a complete launch ledger at integer scheduler ticks.

    Each named cohort may remain pending, or start every member at one tick
    on distinct workers with unique, cohort-bound reservations.
    """
    if policy != "all-members-one-tick-v1" or complete is not True:
        return "INCOMPLETE"
    if not isinstance(cohorts, dict) or not cohorts or not isinstance(reservations, dict) or not isinstance(starts, list):
        return "INCOMPLETE"
    for cohort, members in cohorts.items():
        if not isinstance(cohort, str) or not cohort or not isinstance(members, list) or not members:
            return "INCOMPLETE"
        if not all(isinstance(m, str) and m for m in members) or len(set(members)) != len(members):
            return "INCOMPLETE"
    for ticket, reservation in reservations.items():
        if not isinstance(ticket, str) or not ticket or not isinstance(reservation, (list, tuple)) or len(reservation) != 4:
            return "INCOMPLETE"
        cohort, worker, begin, end = reservation
        if not isinstance(cohort, str) or cohort not in cohorts or not isinstance(worker, str) or not worker:
            return "INCOMPLETE"
        if type(begin) is not int or type(end) is not int or begin >= end:
            return "INCOMPLETE"
    batches = {}
    consumed = set()
    occupied_ticks = set()
    for event in starts:
        if not isinstance(event, (list, tuple)) or len(event) != 4:
            return "INCOMPLETE"
        cohort, member, ticket, tick = event
        if not all(isinstance(x, str) and x for x in (cohort, member, ticket)) or type(tick) is not int:
            return "INCOMPLETE"
        if cohort not in cohorts:
            return "INCOMPLETE"
        if ticket not in reservations or ticket in consumed:
            return "FAIL"
        owner, worker, begin, end = reservations[ticket]
        if owner != cohort or not begin <= tick < end or (worker, tick) in occupied_ticks:
            return "FAIL"
        batch_tick, launched = batches.setdefault(cohort, (tick, set()))
        if batch_tick != tick or member not in cohorts[cohort] or member in launched:
            return "FAIL"
        launched.add(member)
        consumed.add(ticket)
        occupied_ticks.add((worker, tick))
    return "PASS" if all(launched == set(cohorts[c]) for c, (_, launched) in batches.items()) else "FAIL"


def service_circuit_breaker_quality(rule, events, *, expected_events):
    """Replay a consecutive-failure breaker starting closed with zero failures.

    Normal calls finish immediately. After cooldown, the next request starts
    one probe. Other requests must be rejected until that probe completes.
    """
    if not isinstance(rule, dict) or rule.get("policy") != "consecutive-single-probe-v1":
        return "INCOMPLETE"
    threshold, cooldown = rule.get("failures"), rule.get("cooldown_ticks")
    if type(threshold) is not int or threshold < 1 or type(cooldown) is not int or cooldown <= 0:
        return "INCOMPLETE"
    if type(expected_events) is not int or not isinstance(events, list) or expected_events != len(events):
        return "INCOMPLETE"
    previous_tick = None
    for serial, event in enumerate(events):
        if not isinstance(event, (tuple, list)) or len(event) != 4:
            return "INCOMPLETE"
        sequence, tick, action, request = event
        if type(sequence) is not int or sequence != serial or type(tick) is not int:
            return "INCOMPLETE"
        if previous_tick is not None and tick < previous_tick:
            return "INCOMPLETE"
        if action not in ("ok", "fail", "reject", "probe", "probe-ok", "probe-fail") or not isinstance(request, str) or not request:
            return "INCOMPLETE"
        previous_tick = tick

    failures = 0
    retry_at = None
    probe = None
    seen = set()
    for _, tick, action, request in events:
        if action in ("probe-ok", "probe-fail"):
            if request != probe:
                return "FAIL"
            failures = 0
            retry_at = None if action == "probe-ok" else tick + cooldown
            probe = None
            continue
        if request in seen:
            return "FAIL"
        seen.add(request)
        if retry_at is None:
            if action not in ("ok", "fail"):
                return "FAIL"
            failures = failures + 1 if action == "fail" else 0
            if failures == threshold:
                retry_at = tick + cooldown
        elif tick < retry_at or probe is not None:
            if action != "reject":
                return "FAIL"
        else:
            if action != "probe":
                return "FAIL"
            probe = request
    return "PASS"


def quorum_transition_quality(rule, epochs, commits, *, complete):
    """Check observed certificates for one log slot in one voting round.

    Each epoch names one active configuration or both sides of a joint epoch.
    A certificate needs a majority in every named configuration. Concurrent
    certificates for this slot must intersect, including across epoch names.
    """
    if rule != "majority-each-config-v1" or complete is not True:
        return "INCOMPLETE"
    if not isinstance(epochs, dict) or not epochs or not isinstance(commits, list) or not commits:
        return "INCOMPLETE"
    membership = {}
    for epoch, configurations in epochs.items():
        if not isinstance(epoch, str) or not epoch or not isinstance(configurations, list) or not 1 <= len(configurations) <= 2:
            return "INCOMPLETE"
        groups = []
        for group in configurations:
            if not isinstance(group, list) or not group or not all(isinstance(v, str) and v for v in group):
                return "INCOMPLETE"
            if len(group) != len(set(group)):
                return "INCOMPLETE"
            groups.append(set(group))
        membership[epoch] = groups
    certificates = []
    for commit in commits:
        if not isinstance(commit, dict):
            return "INCOMPLETE"
        epoch, voters, value = commit.get("epoch"), commit.get("voters"), commit.get("value")
        if not isinstance(epoch, str) or epoch not in membership or not isinstance(value, str) or not value:
            return "INCOMPLETE"
        if not isinstance(voters, list) or not all(isinstance(v, str) and v for v in voters):
            return "INCOMPLETE"
        certificates.append((membership[epoch], voters, value))
    accepted = []
    for groups, voters, value in certificates:
        support = set(voters)
        if len(support) != len(voters) or not support <= set.union(*groups):
            return "FAIL"
        if any(2 * len(support & group) <= len(group) for group in groups):
            return "FAIL"
        if any(value != prior_value or support.isdisjoint(prior) for prior, prior_value in accepted):
            return "FAIL"
        accepted.append((support, value))
    return "PASS"


def retry_amplification_quality(policy, layers, attempts, *, complete):
    """Check one original request with both per-parent and per-request layer caps.

    Every attempt names an ID, its parent ID (None only for the original),
    and its zero-based layer. Layer caps count all attempts including first tries.
    """
    from collections import Counter

    if policy != "layer-attempt-caps-v1" or complete is not True:
        return "INCOMPLETE"
    if not isinstance(layers, list) or not layers or not isinstance(attempts, list) or not attempts:
        return "INCOMPLETE"
    caps = []
    for layer in layers:
        if not isinstance(layer, dict):
            return "INCOMPLETE"
        local, total = layer.get("per_parent"), layer.get("per_original")
        if type(local) is not int or type(total) is not int or min(local, total) < 0:
            return "INCOMPLETE"
        caps.append((local, total))
    lineage = {}
    for attempt in attempts:
        if not isinstance(attempt, (list, tuple)) or len(attempt) != 3:
            return "INCOMPLETE"
        identity, parent, level = attempt
        if not isinstance(identity, str) or not identity or identity in lineage:
            return "INCOMPLETE"
        if type(level) is not int or not 0 <= level <= len(caps):
            return "INCOMPLETE"
        if parent is not None and (not isinstance(parent, str) or not parent):
            return "INCOMPLETE"
        lineage[identity] = (parent, level)
    if sum(level == 0 for _, level in lineage.values()) != 1:
        return "INCOMPLETE"
    children = Counter()
    at_layer = Counter()
    for parent, level in lineage.values():
        if level == 0:
            if parent is not None:
                return "INCOMPLETE"
            continue
        if parent not in lineage or lineage[parent][1] != level - 1:
            return "INCOMPLETE"
        children[parent] += 1
        at_layer[level] += 1
    for parent, count in children.items():
        parent_level = lineage[parent][1]
        if count > caps[parent_level][0]:
            return "FAIL"
    return "PASS" if all(count <= caps[level - 1][1] for level, count in at_layer.items()) else "FAIL"


def speculative_winner_quality(attempts, committed, reported, *, complete):
    """Reconcile a closed execution ledger against one atomic publish per task.

    Attempts bind attempt IDs to parent task and candidate output ID. Commit
    receipts and report rows are (task, winning attempt, published output).
    """
    if complete is not True or not all(isinstance(x, list) for x in (attempts, committed, reported)):
        return "INCOMPLETE"
    for rows in (attempts, committed, reported):
        for row in rows:
            if not isinstance(row, (tuple, list)) or len(row) != 3:
                return "INCOMPLETE"
            if not all(isinstance(x, str) and x for x in row):
                return "INCOMPLETE"
    candidates = {}
    for attempt, task, output in attempts:
        if attempt in candidates:
            return "INCOMPLETE"
        candidates[attempt] = (task, output)
    winners, outputs = set(), set()
    for task, attempt, output in committed:
        if attempt not in candidates:
            return "INCOMPLETE"
        if candidates[attempt] != (task, output) or task in winners or output in outputs:
            return "FAIL"
        winners.add(task)
        outputs.add(output)
    receipts = {tuple(row) for row in committed}
    credits = [tuple(row) for row in reported]
    return "PASS" if len(credits) == len(receipts) and set(credits) == receipts else "FAIL"


def cache_invalidation_quality(manifest, initial_digests, events, *, complete):
    """Reconcile cache builds and hits against the full source-dependency manifest.

    A build receipt pins an output and its input digest map. An explicit eviction
    discards that build even if the current inputs still match.
    """
    if complete is not True or not isinstance(manifest, dict) or not isinstance(initial_digests, dict):
        return "INCOMPLETE"
    if not isinstance(events, list):
        return "INCOMPLETE"
    def named(value):
        return isinstance(value, str) and bool(value)

    if not all(named(k) and named(v) for k, v in initial_digests.items()):
        return "INCOMPLETE"
    for key, dependencies in manifest.items():
        if not named(key) or not isinstance(dependencies, list) or not dependencies:
            return "INCOMPLETE"
        if not all(named(dep) and dep in initial_digests for dep in dependencies):
            return "INCOMPLETE"
        if len(set(dependencies)) != len(dependencies):
            return "INCOMPLETE"
    current = dict(initial_digests)
    builds = {}
    for event in events:
        if not isinstance(event, dict):
            return "INCOMPLETE"
        op, key = event.get("op"), event.get("key")
        if not named(key):
            return "INCOMPLETE"
        if op == "change":
            digest = event.get("digest")
            if key not in current or not named(digest):
                return "INCOMPLETE"
            current[key] = digest
            continue
        if key not in manifest:
            return "INCOMPLETE"
        if op == "invalidate":
            builds.pop(key, None)
            continue
        if op not in ("build", "reuse"):
            return "INCOMPLETE"
        receipt, output = event.get("dependencies"), event.get("output")
        if not isinstance(receipt, dict) or not named(output) or set(receipt) != set(manifest[key]):
            return "INCOMPLETE"
        if not all(named(digest) for digest in receipt.values()):
            return "INCOMPLETE"
        if any(current[dep] != value for dep, value in receipt.items()):
            return "FAIL"
        if op == "reuse" and builds.get(key) != (output, receipt):
            return "FAIL"
        if op == "build":
            builds[key] = (output, dict(receipt))
    return "PASS"


def reusable_barrier_quality(rule, generations, events, *, complete):
    """Grade a complete sequence of zero-based all-member barriers.

    Each generation records one arrival per member and a single release.
    A release resets arrivals before the next cohort can open.
    """
    if rule != "all-members-per-generation-v1" or complete is not True:
        return "INCOMPLETE"
    if not isinstance(generations, list) or not generations or not isinstance(events, list):
        return "INCOMPLETE"
    cohorts = []
    for members in generations:
        if not isinstance(members, list) or not members or not all(isinstance(m, str) and m for m in members):
            return "INCOMPLETE"
        cohort = set(members)
        if len(cohort) != len(members):
            return "INCOMPLETE"
        cohorts.append(cohort)
    current = 0
    arrived = set()
    for event in events:
        if not isinstance(event, (tuple, list)) or len(event) != 3:
            return "INCOMPLETE"
        kind, generation, member = event
        if type(generation) is not int or not 0 <= generation < len(cohorts):
            return "INCOMPLETE"
        if kind not in ("arrive", "release"):
            return "INCOMPLETE"
        if generation != current:
            return "FAIL"
        if kind == "arrive":
            if not isinstance(member, str) or not member:
                return "INCOMPLETE"
            if member not in cohorts[current] or member in arrived:
                return "FAIL"
            arrived.add(member)
        else:
            if member is not None:
                return "INCOMPLETE"
            if arrived != cohorts[current]:
                return "FAIL"
            arrived.clear()
            current += 1
    return "PASS" if current == len(cohorts) else "FAIL"


def semaphore_permit_quality(policy, capacity, opening, events, reported_free, *, complete):
    """Audit weighted acquire/release with unique grant IDs and owner-only returns.

    Resizing is atomic and cannot revoke outstanding grants. A grant is always
    released in full, and its ID cannot be reused within the observed epoch.
    """
    if policy != "weighted-owner-grants-v1" or complete is not True or type(capacity) is not int or capacity < 0:
        return "INCOMPLETE"
    if not isinstance(opening, list) or not isinstance(events, list) or type(reported_free) is not int:
        return "INCOMPLETE"
    def named(x):
        return isinstance(x, str) and bool(x)
    held = {}
    for row in opening:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            return "INCOMPLETE"
        grant, owner, count = row
        if not named(grant) or not named(owner) or type(count) is not int or count <= 0 or grant in held:
            return "INCOMPLETE"
        held[grant] = (owner, count)
    used = sum(count for _, count in held.values())
    if used > capacity:
        return "FAIL"
    seen = set(held)
    for event in events:
        if not isinstance(event, dict):
            return "INCOMPLETE"
        op = event.get("op")
        if op == "resize":
            capacity = event.get("capacity")
            if type(capacity) is not int or capacity < 0:
                return "INCOMPLETE"
            if used > capacity:
                return "FAIL"
            continue
        grant, owner = event.get("grant"), event.get("owner")
        if op not in ("acquire", "release") or not named(grant) or not named(owner):
            return "INCOMPLETE"
        if op == "acquire":
            count = event.get("permits")
            if type(count) is not int or count <= 0:
                return "INCOMPLETE"
            if grant in seen or used + count > capacity:
                return "FAIL"
            held[grant] = (owner, count)
            seen.add(grant)
            used += count
        else:
            if grant not in held or held[grant][0] != owner:
                return "FAIL"
            used -= held.pop(grant)[1]
    return "PASS" if reported_free == capacity - used else "FAIL"
