"""Independent reconciliation of synthetic execution receipts."""
from collections import Counter
import pytest


@pytest.mark.parametrize("eligible,failures,target,remaining", [(10, 1, 0.9, 0), (4, 1, 0.95, -0.8), (20, 0, 0.95, 1)])
def test_budget_excludes_probes_from_both_counts(eligible, failures, target, remaining):
    from benches.x03_dashboard_oracle import request_budget_quality
    rows = [{"id": str(i), "class": "interactive", "success": i >= failures} for i in range(eligible)]
    rows.append({"id": "probe", "class": "health", "success": False})
    report = dict(eligible=eligible, failures=failures, remaining=remaining)
    assert request_budget_quality(rows, ["interactive"], target, report) == "PASS"
    assert request_budget_quality(rows, ["interactive"], target, dict(report, failures=failures+1)) == "FAIL"
    assert request_budget_quality(rows, ["interactive"], target, dict(report, remaining=remaining+1)) == "FAIL"
    assert request_budget_quality(rows, None, target, report) == "INCOMPLETE"


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


@pytest.mark.parametrize("start,amount,unit,peak,expected", [
    (1,512,"KiB",1.5,"PASS"), (1,512,"KiB",1,"FAIL"),
    (2,2,"MiB",2,"PASS"), (3,1,"MiB",1,"PASS"), (1,512,None,1.5,"INCOMPLETE"),
], ids=["sum-overlap","understated-peak","equal-time-handoff","disjoint-tasks","missing-unit"])
def test_peak_concurrent_memory(start, amount, unit, peak, expected):
    from benches.x03_dashboard_oracle import peak_memory_quality
    tasks = {"load":dict(start=0,end=2,allocation=1,unit="MiB"),
             "export":dict(start=start,end=4,allocation=amount,unit=unit)}
    assert peak_memory_quality(tasks, peak, "MiB") == expected


@pytest.mark.parametrize("case,expected", [
    ("matched","PASS"), ("duplicate","FAIL"), ("unit","FAIL"), ("currency","FAIL"),
    ("missing-rate","INCOMPLETE"), ("wrong-total","FAIL"),
], ids=["reconcile-two-meters","duplicate-charge","wrong-usage-unit","wrong-currency","absent-tariff","understated-report"])
def test_synthetic_cost_report(case, expected):
    from benches.x03_dashboard_oracle import usage_cost_quality
    rows = {"a":dict(meter="worker",quantity=2.5,unit="hours"), "b":dict(meter="storage",quantity=4,unit="GB-day")}
    rates = {"worker":dict(unit="hours",unit_price=0.2), "storage":dict(unit="GB-day",unit_price=0.05)}
    charges = [dict(usage_id="a",amount=0.5,currency="USD"),dict(usage_id="b",amount=0.2,currency="USD")]
    if case == "duplicate":
        charges.append(charges[0].copy())
    if case == "unit":
        rows["a"]["unit"] = "minutes"
    if case == "currency":
        charges[1]["currency"] = "EUR"
    if case == "missing-rate":
        del rates["storage"]
    tariff = dict(version="v1",currency="USD",rates=rates)
    assert usage_cost_quality(rows,tariff,"v1","USD",charges,0.6 if case=="wrong-total" else 0.7) == expected


@pytest.mark.parametrize("busy,reported,expected", [
    ([(0,5,1)],0.25,"PASS"), ([(0,5,1)],0.5,"FAIL"), ([(0,5,1),(5,10,3)],1,"PASS"),
    ([],0,"PASS"), ([(0,6,2)],0.6,"FAIL"),
], ids=["capacity-weighted-idle-period","reject-mean-of-percentages","exact-changeover","all-idle","early-overcapacity"])
def test_time_integrated_capacity(busy, reported, expected):
    from benches.x03_dashboard_oracle import capacity_utilization_quality
    schedule = [dict(at=0,slots=1),dict(at=5,slots=3)]
    tasks = [dict(start=s,end=e,slots=n) for s,e,n in busy]
    assert capacity_utilization_quality(0,10,schedule,tasks,reported) == expected
    assert capacity_utilization_quality(0,10,schedule[1:],tasks,reported) == "INCOMPLETE"


@pytest.mark.parametrize("times,parts,expected", [
    ((0,4,10),(4,6),"PASS"), ((2,2,5),(0,3),"PASS"), ((0,4,10),(6,4),"FAIL"),
    ((5,3,8),(-2,5),"FAIL"), ((0,3,None),(3,0),"INCOMPLETE"),
], ids=["separate-wait-and-work","immediate-start","swapped-same-total","start-before-enqueue","no-finish"])
def test_queue_service_decomposition(times, parts, expected):
    from benches.x03_dashboard_oracle import queue_service_quality
    runs = {"job":dict(zip(("enqueued","started","finished"),times))}
    assert queue_service_quality(runs,{"job":dict(zip(("wait","service"),parts))}) == expected


@pytest.mark.parametrize("policy,maintenance,expected", [
    ("include", [(3, 7)], 3/10), ("exclude", [(3, 7)], 3/6),
    ("exclude", [(-5, 1), (7, 15)], 0)])
def test_availability_from_eligible_time(policy, maintenance, expected):
    from benches.x03_dashboard_oracle import availability_quality
    outages = [(-2, 4), (3, 7)]
    assert availability_quality((0, 10), outages, maintenance, policy, expected) == "PASS"
    assert availability_quality((0, 10), outages, maintenance, policy, 1) == "FAIL"
    assert availability_quality((0, 10), outages, maintenance, None, expected) == "INCOMPLETE"


@pytest.mark.parametrize("older,recent,thresholds,rates,alert", [
    ([True, True], [True, False], (2, 1), (2, 1), True),
    ([True, True], [True, False], (2, 2), (2, 1), False),
    ([False, False], [True, True], (1, 2), (0, 2), False)])
def test_multiwindow_slo_burn(older, recent, thresholds, rates, alert):
    from benches.x03_dashboard_oracle import burn_window_quality as grade
    events = list(zip([5, 6, 9, 10], older + recent))
    windows = dict(zip([2, 6], thresholds))
    report = dict(rates=dict(zip([2, 6], rates)), alert=alert)
    assert grade(events, (4, 10), 10, windows, 0.75, report) == "PASS"
    assert grade(events, (4, 10), 10, windows, 0.75, dict(report, alert=not alert)) == "FAIL"
    assert grade(events, (5, 10), 10, windows, 0.75, report) == "INCOMPLETE"


@pytest.mark.parametrize("opening,added,done,cancelled,end", [
    (["a"], ["b","c"], ["b"], ["a"], ["c"]), ([], ["x","y"], [], ["y"], ["x"]),
    (["r","s"], [], ["r"], [], ["s"])])
def test_queue_inventory_conservation(opening, added, done, cancelled, end):
    from benches.x03_dashboard_oracle import queue_balance_quality as grade
    assert grade(opening, added, done, cancelled, end) == "PASS"
    assert grade(opening, added, done, cancelled, []) == "FAIL"
    assert grade(opening, added, done+[end[0]], cancelled+[end[0]], []) == "FAIL"
    assert grade(None, added, done, cancelled, end) == "INCOMPLETE"


@pytest.mark.parametrize("opening,events,mean,wrong", [
    (2, [(8,"depart")], 1.8, 1.5),
    (0, [(0,"enqueue"),(1,"enqueue"),(1,"depart"),(3,"depart")], 0.3, 1),
    (3, [(9,"enqueue"),(2,"depart")], 2.3, 2.5)])
def test_time_weighted_queue_length(opening,events,mean,wrong):
    from benches.x03_dashboard_oracle import mean_queue_quality as grade
    assert grade(0,10,opening,events,mean) == "PASS"
    assert grade(0,10,opening,events,wrong) == "FAIL"
    assert grade(None,10,opening,events,mean) == "INCOMPLETE"
    assert grade(0,10,None,events,mean) == "INCOMPLETE"


@pytest.mark.parametrize("shifts,expected", [
    ([(0,1800),(900,2700)],4), ([(0,900),(2700,3600)],4), ([(0,1200),(0,1200)],6)])
def test_completions_per_scheduled_hour(shifts,expected):
    from benches.x03_dashboard_oracle import scheduled_throughput_quality as grade
    completions = dict(a=0,b=900,c=1800,d=3000)
    assert grade(completions,shifts,expected) == "PASS"
    assert grade(completions,shifts,4.8) == "FAIL"
    assert grade(completions,None,expected) == "INCOMPLETE"


@pytest.mark.parametrize("as_of,expected", [(5,[]),(6,["late"]),(10,["late","open"])])
def test_completed_and_unfinished_deadlines(as_of,expected):
    from benches.x03_dashboard_oracle import deadline_miss_quality as grade
    due = dict(late=5,exact=5,open=6)
    done = dict(late=8,exact=5)
    assert grade(list(due),due,done,as_of,expected) == "PASS"
    assert grade(list(due),due,done,as_of,expected+["exact"]) == "FAIL"
    assert grade(list(due),{"late":5},done,as_of,expected) == "INCOMPLETE"


@pytest.mark.parametrize("population,jobs,rate,seconds", [("waiting",2,0.5,4),("in-system",6,2,3),("priority",0.75,0.25,3)])
def test_littles_law_matched_cohort(population,jobs,rate,seconds):
    from benches.x03_dashboard_oracle import little_law_quality as grade
    values = dict(mean_jobs=jobs,jobs_per_second=rate,mean_seconds=seconds)
    rows = {k:dict(population=population,start=0,end=60,value=v) for k,v in values.items()}
    assert grade(rows,True,0) == "PASS"
    for mismatch in (dict(population="other"),dict(end=61),dict(value=seconds+1)):
        assert grade(dict(rows,mean_seconds=dict(rows["mean_seconds"],**mismatch)),True,0) == "FAIL"
    assert grade(rows,None,0) == "INCOMPLETE"
