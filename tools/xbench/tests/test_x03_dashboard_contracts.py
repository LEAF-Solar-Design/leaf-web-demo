"""Independent reconciliation of synthetic execution receipts."""
from collections import Counter
import pytest


@pytest.mark.parametrize("extra", ["failed", "complete", "cancelled"])
def test_failure_totals_reconcile_by_drawing_revision(extra):
    executions = [("drawing-a","r1","failed"), ("drawing-a","r2",extra), ("drawing-b","r1","complete")]
    expected = Counter((drawing, revision) for drawing, revision, state in executions if state == "failed")
    reported = {("drawing-a","r1"):1}
    if extra == "failed":
        reported["drawing-a","r2"] = 1
    assert reported == expected
    assert dict(reported, unrelated=1) != expected


@pytest.mark.parametrize("tool", ["catalog", "count", "schedule"])
def test_changed_outputs_pair_only_on_identical_input(tool):
    before = {(tool,"drawing-a:r1"):"hash-a", (tool,"drawing-b:r1"):"hash-b"}
    after = {(tool,"drawing-a:r1"):"hash-c", (tool,"drawing-b:r1"):"hash-b", (tool,"drawing-a:r2"):"hash-d"}
    changes = {(key, "tool:v1", "tool:v2") for key in before.keys() & after.keys() if before[key] != after[key]}
    assert changes == {((tool,"drawing-a:r1"), "tool:v1", "tool:v2")}


@pytest.mark.parametrize("tool", ["catalog", "counts", "schedule"])
def test_reconcile_external_dashboard_rows(tool):
    from benches.x03_dashboard_oracle import dashboard_quality
    first = dict(id="1",drawing="d",version="r",tool=tool,revision="old",output="aa",status="complete",
                 input_sha256="a"*64, parameters={"scale":1})
    next_run = dict(first,id="2",revision="new",output="bb")
    failed = dict(first,id="3",status="failed")
    receipts = [first,next_run,failed]
    totals = [dict(drawing="d",version="r",count=1)]
    delta = [dict(drawing="d",version="r",tool=tool,before="old",after="new")]
    def grade(runs, changes):
        return dashboard_quality(runs,totals,changes,"old","new",expected_run_ids=["1","2","3"])
    assert grade(receipts,delta) == "PASS"
    assert grade(receipts+[failed],delta) == "FAIL"
    assert grade([first,dict(next_run,version=None),failed],delta) == "INCOMPLETE"
    assert grade([first,failed],delta) == "FAIL"
    assert grade(receipts,[]) == "FAIL"
    for mismatch in ({"input_sha256":None}, {"input_sha256":"b"*64}, {"parameters":None}, {"parameters":{"scale":2}}):
        assert grade([first,dict(next_run,**mismatch),failed],delta) == "INCOMPLETE"


@pytest.mark.parametrize("bad", [None, ["failed"], {"status":"failure_typo"}])
def test_dashboard_rejects_unreadable_receipts(bad):
    from benches.x03_dashboard_oracle import dashboard_quality
    valid = dict(id="1",drawing="d",version="v",tool="t",revision="old",status="cancelled")
    row = dict(valid, **bad) if isinstance(bad,dict) else bad
    assert dashboard_quality([row],[],[],"old","new") == "INCOMPLETE"


@pytest.mark.parametrize("drawing", ["roof","shed","office"])
def test_expected_inventory_prevents_omissions(drawing):
    from benches.x03_dashboard_oracle import dashboard_quality
    row = dict(id="one",drawing=drawing,version="v",tool="t",revision="old",status="failed")
    totals = [dict(drawing=drawing,version="v",count=1)]
    def check(rows,report):
        return dashboard_quality(rows,report,[],"old","new",expected_run_ids=["one"])
    assert check([row],totals) == "PASS"
    assert check([],[]) == "FAIL"
    assert check([dict(row,id="unknown")],totals) == "FAIL"
    assert check([row],[]) == "FAIL"

@pytest.mark.parametrize("tool", ["panel", "beam", "duct"])
def test_dashboard_summary_reconciles_to_runs(tool):
    from benches.x03_dashboard_oracle import dashboard_quality
    row = dict(drawing="d", version="v", tool=tool, revision="historic")
    runs = [dict(row, id="1", status="complete", duration_seconds=3),
            dict(row, id="2", status="cancelled", duration_seconds=7)]
    summary = dict(success_rate=0.5, mean_duration_seconds=5, max_duration_seconds=7)
    def check(rows, report):
        return dashboard_quality(rows, [], [], "old", "new",
                                 expected_run_ids=[r["id"] for r in rows], summary=report)
    assert check(runs, summary) == "PASS"
    for field in summary:
        assert check(runs, dict(summary, **{field: 99})) == "FAIL"
    assert check([], summary) == "INCOMPLETE"
    for duration in (-1, None, float("nan")):
        assert check([dict(runs[0], duration_seconds=duration), runs[1]], summary) == "INCOMPLETE"


@pytest.mark.parametrize("left,right,ids", [(True, False, ["s", "m"]), (False, True, ["m", "e"]),
                                         (True, True, ["s", "m", "e"])])
def test_report_window_at_utc_midnight(left, right, ids):
    from benches.x03_dashboard_oracle import window_coverage_quality
    rows = [dict(id="s", timestamp="2026-01-01T18:00:00-06:00"),
            dict(id="m", timestamp="2026-01-02T00:30:00Z"),
            dict(id="e", timestamp="2026-01-02T02:00:00+01:00")]
    def grade(records, reported):
        return window_coverage_quality(records, reported, "2026-01-02T00:00:00Z", "2026-01-02T01:00:00Z",
                                       include_start=left, include_end=right)
    assert grade(rows, ids) == "PASS"
    assert grade(rows, ids + ["outside"]) == "FAIL"
    assert grade([dict(rows[0], timestamp="2026-01-02T00:00:00"), *rows[1:]], ids) == "INCOMPLETE"


@pytest.mark.parametrize("spans,elapsed,worker", [
    ([(1,6),(3,8)],7,10), ([(2,10),(3,4),(12,14)],10,11), ([(0,3),(3,5),(0,3)],5,8),
])
def test_concurrent_run_accounting(spans, elapsed, worker):
    from benches.x03_dashboard_oracle import concurrent_time_quality
    assert concurrent_time_quality(spans[::-1], elapsed, "elapsed") == "PASS"
    assert concurrent_time_quality(spans, worker, "worker") == "PASS"
    assert concurrent_time_quality(spans, worker, "elapsed") == "FAIL"
    assert concurrent_time_quality(spans, elapsed) == "INCOMPLETE"


@pytest.mark.parametrize("samples, percentile, policy, reported, verdict", [
    ([4, 1, 9, 4], 75, "nearest-rank", 4, "PASS"),
    ([10, 2], 25, "linear", 4, "PASS"),
    ([8], 99, "linear", 8, "PASS"),
    ([10, 2], 25, "nearest-rank", 4, "FAIL"),
    ([10, 2], 25, None, 2, "INCOMPLETE"),
], ids=["rank-tied-latencies", "interpolate-two-samples", "report-single-sample",
        "reject-wrong-rule", "require-declared-policy"])
def test_latency_percentiles(samples, percentile, policy, reported, verdict):
    from benches.x03_dashboard_oracle import latency_percentile_quality
    assert latency_percentile_quality(samples, percentile, reported, policy) == verdict


@pytest.mark.parametrize("history, counts, expected", [
    ([("a", 2, "succeeded"), ("a", 1, "failed")], (2, 1, 1, 0), "PASS"),
    ([("b", 1, "failed"), ("c", 1, "succeeded")], (2, 2, 1, 1), "PASS"),
    ([("a", 1, "failed"), ("a", 2, "succeeded")], (2, 2, 2, 0), "FAIL"),
    ([("a", 2, "succeeded")], (1, 1, 1, 0), "INCOMPLETE"),
], ids=["reconcile-retry-success", "split-two-logical-runs", "reject-inflated-dashboard", "missing-first-attempt"])
def test_logical_retry_totals(history, counts, expected):
    from benches.x03_dashboard_oracle import retry_dashboard_quality
    attempts = [dict(run=r, attempt=i, outcome=s) for r, i, s in history]
    assert retry_dashboard_quality(attempts, dict(zip(("attempts", "runs", "succeeded", "failed"), counts))) == expected


@pytest.mark.parametrize("after, verdict", [
    ([("b", "failed"), ("a", "passed")], "PASS"),
    ([("a", "passed"), ("c", "failed")], "FAIL"),
    ([("a", "passed")], "FAIL"),
], ids=["credit-reordered-matched-pairs", "reject-new-drawing-mix", "restore-omitted-failure"])
def test_release_cohort(after, verdict):
    from benches.x03_dashboard_oracle import paired_release_quality
    before = [dict(drawing_version=k, outcome="failed") for k in ("a", "b")]
    after_rows = [dict(drawing_version=k, outcome=v) for k, v in after]
    report = dict(pairs=2, before_passed=0, after_passed=1, improved=True)
    assert paired_release_quality(["a", "b"], before, after_rows, report) == verdict
    assert paired_release_quality(["a", "b"], before, after_rows, dict(report, improved=False)) == "FAIL"


@pytest.mark.parametrize("end,policy,exact,lower,expected", [
    (None, "right-censored", {}, {"a": 8}, "PASS"),
    (11, "right-censored", {"a": 8}, {}, "FAIL"),
    (10, "right-censored", {"a": 8}, {}, "PASS"),
    (None, None, {}, {"a": 8}, "INCOMPLETE"),
], ids=["unfinished-at-cutoff","future-completion-is-censored","completed-at-cutoff","missing-policy"])
def test_cutoff_duration(end, policy, exact, lower, expected):
    from benches.x03_dashboard_oracle import censored_durations_quality
    assert censored_durations_quality({"a": dict(start=2,end=end)}, 10, policy, exact, lower) == expected


@pytest.mark.parametrize("weights,reported,expected", [
    ({"a":1,"b":1}, 0.9, "PASS"), ({"a":1,"b":1}, 0.5, "FAIL"),
    ({"a":1,"b":9}, 0.5, "PASS"), ({"a":0,"b":1}, 0, "PASS"),
    ({"a":1}, 0.9, "INCOMPLETE"),
], ids=["pool-unequal-cohorts", "reject-mean-of-percentages", "weight-exposure", "exclude-zero-weight", "missing-weight"])
def test_dashboard_weighted_exposure(weights, reported, expected):
    from benches.x03_dashboard_oracle import weighted_rate_quality
    rows = {"a":dict(successes=9, exposures=9), "b":dict(successes=0, exposures=1)}
    assert weighted_rate_quality(rows, weights, "weighted-exposures", reported) == expected
    rows["a"].pop("exposures")
    assert weighted_rate_quality(rows, weights, "weighted-exposures", reported) == "INCOMPLETE"


@pytest.mark.parametrize("policy,counts,expected", [
    ("left-closed", [1,2,2], "PASS"), ("right-closed", [2,2,1], "PASS"),
    ("left-closed", [1,2,1], "FAIL"), ("right-closed", [2,3,1], "FAIL"),
], ids=["lower-edges-inclusive", "upper-edges-inclusive", "omitted-overflow", "double-counted-edge"])
def test_duration_histogram_bins(policy, counts, expected):
    from benches.x03_dashboard_oracle import duration_histogram_quality
    runs = dict(enumerate([1, 5, 6, 10, 12]))
    assert duration_histogram_quality(runs, [5,10], policy, counts) == expected
    assert duration_histogram_quality(runs, [5,10], None, counts) == "INCOMPLETE"


@pytest.mark.parametrize("case,path,duration,expected", [
    ("parallel", ["load","long","write"], 9, "PASS"),
    ("parallel", ["load","short","write"], 6, "FAIL"),
    ("parallel", ["load","long","write"], 11, "FAIL"),
    ("cycle", ["load","long","write"], 9, "FAIL"),
    ("missing", ["load","long","write"], 9, "INCOMPLETE"),
], ids=["longest-parallel-branch","shorter-real-path","sum-over-parallel-work","cyclic-dependency","unknown-predecessor"])
def test_task_critical_path(case, path, duration, expected):
    from benches.x03_dashboard_oracle import critical_path_quality
    tasks = {"load":dict(duration=2,predecessors=[]), "short":dict(duration=2,predecessors=["load"]),
             "long":dict(duration=5,predecessors=["load"]), "write":dict(duration=2,predecessors=["short","long"])}
    if case == "cycle":
        tasks["load"]["predecessors"] = ["write"]
    if case == "missing":
        tasks["long"]["predecessors"] = ["other"]
    assert critical_path_quality(tasks, path, duration) == expected
