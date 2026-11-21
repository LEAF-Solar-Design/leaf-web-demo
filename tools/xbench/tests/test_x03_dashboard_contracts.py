"""Independent reconciliation of synthetic execution receipts."""
from collections import Counter
import pytest


@pytest.mark.parametrize("p,method,expected", [(50,"nearest-rank",5),(100,"nearest-rank",12),(75,"linear",8.5)])
def test_cutoff_percentile_uses_pending_population(p,method,expected):
    from benches.x03_dashboard_oracle import pending_age_quality as grade
    opening = {"old":-2}
    admissions = {"slow":5,"new":9,"done":7,"future":12}
    departures = {"done":10,"slow":11}
    # At cutoff 10 the ages are 1, 5 and 12. Completed-only age is 3.
    assert grade(0,10,opening,admissions,departures,p,expected,method) == "PASS"
    assert grade(0,10,opening,admissions,departures,p,3,method) == "FAIL"
    assert grade(0,10,{"old":None},admissions,departures,p,expected,method) == "INCOMPLETE"


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


@pytest.mark.parametrize("limit,dispatch,expected", [(5,None,["low"]), (4,4,[]), (3,4,["low"])])
def test_aging_of_eligible_work_amid_fast_dispatches(limit,dispatch,expected):
    from benches.x03_dashboard_oracle import starvation_quality as grade
    ready = [("low","batch",0,None)] + [(str(i),"urgent",i,i+1) for i in range(5)]
    events = {(str(i),i):i for i in range(5)}
    if dispatch is not None:
        events["low",0] = dispatch
    policy = {"batch":limit,"urgent":1}
    assert grade(ready,events,6,policy,expected) == "PASS"
    assert grade(ready,events,6,policy,[] if expected else ["low"]) == "FAIL"
    assert grade(ready,events,6,None,expected) == "INCOMPLETE"


@pytest.mark.parametrize("served,left", [(["a"], ["b"]), ([], ["a", "b"]), (["a", "b"], ["c", "d"])])
def test_queue_report_excludes_abandoned_requests(served, left):
    from benches.x03_dashboard_oracle import abandonment_quality
    arrivals = dict.fromkeys(served + left, 1)
    starts = dict.fromkeys(served, 2)
    exits = dict.fromkeys(left, 3)
    assert abandonment_quality(arrivals, starts, exits, set(served)) == "PASS"
    assert abandonment_quality(arrivals, starts, exits, set(arrivals)) == "FAIL"
    del exits[left[0]]
    assert abandonment_quality(arrivals, starts, exits, set(served)) == "INCOMPLETE"


@pytest.mark.parametrize("unit,edges", [("seconds",[0,2,4]), ("watts",[10,20,50]), ("metres",[-2,0,3])])
def test_merge_disjoint_recorded_histograms(unit, edges):
    from benches.x03_dashboard_oracle import histogram_merge_quality as grade
    a = dict(unit=unit,edges=edges,sample_ids=["a","b"],counts=[2,0])
    b = dict(unit=unit,edges=edges,sample_ids=["c"],counts=[0,1])
    assert grade([a,b], [2,1]) == "PASS"
    assert grade([a,b], [1,2]) == "FAIL"
    for change in (dict(edges=[edges[0],edges[1]+1,edges[2]]), dict(unit="other"), dict(sample_ids=["a"])):
        assert grade([a,dict(b,**change)], [2,1]) == "FAIL"
    assert grade([a,dict(b,sample_ids=None)], [2,1]) == "INCOMPLETE"


@pytest.mark.parametrize("outcomes,resets,longest", [
    (["pass","failure","failure","pass","failure"], {"pass"}, 2),
    (["failure","skipped","failure","recovered","failure"], {"recovered"}, 2),
    (["failure","skipped","failure","pass"], {"skipped","pass"}, 1),
])
def test_dashboard_longest_outage(outcomes,resets,longest):
    from benches.x03_dashboard_oracle import failure_streak_quality as grade
    rows = list(reversed(list(enumerate(outcomes))))
    assert grade(rows,resets,longest) == "PASS"
    assert grade(rows,resets,0) == "FAIL"
    assert grade(rows+[rows[0]],resets,longest) == "INCOMPLETE"


@pytest.mark.parametrize("before,after,delta", [
    ([(0,95,0),(1,2,1)],[(1,0,0),(2,4,0)],11),
    ([(0,40,0),(2,50,0)],[(2,0,0),(8,1,2)],211),
    ([(3,8,4),(7,12,4)],[(7,0,0),(9,0,0)],4),
])
def test_counter_throughput_across_resets_and_wraps(before, after, delta):
    from benches.x03_dashboard_oracle import counter_throughput_quality as grade
    epochs = [dict(epoch="boot1",samples=before),dict(epoch="boot2",samples=after)]
    assert grade(epochs,100,delta) == "PASS"
    for wrong in (-1,delta+100):
        assert grade(epochs,100,wrong) == "FAIL"
    epochs[1]["samples"] = [(after[0][0],0,None),after[1]]
    assert grade(epochs,100,delta) == "INCOMPLETE"


@pytest.mark.parametrize("end,begin,limits", [(2,2,{0:1}), (4,2,{0:1,2:2}), (3,3,{0:2,3:1})])
def test_admission_limit_at_release_or_limit_change(end,begin,limits):
    from benches.x03_dashboard_oracle import admission_limit_quality as grade
    ids = {"a","b"}
    starts = {"a":0,"b":begin}
    terminals = {"a":end,"b":6}
    assert grade(ids,starts,terminals,limits) == "PASS"
    assert grade(ids,{"a":0,"b":1},terminals,{0:1,2:3}) == "FAIL"
    assert grade(ids,starts,{"a":end},limits) == "INCOMPLETE"


@pytest.mark.parametrize("worker,start,ack,stop,elapsed", [
    ("export",10,11,15,5), ("analysis",3,3,4,1), ("publish",2.1,2.2,2.6,0.5),
])
def test_cancellation_ends_at_worker_stop(worker,start,ack,stop,elapsed):
    from benches.x03_dashboard_oracle import cancellation_latency_quality as grade
    def check(stops,reported,limit):
        return grade({worker:start},{worker:ack},stops,{worker:reported},limit)
    terminal = {worker:stop}
    assert check(terminal,elapsed,elapsed) == "PASS"
    assert check(terminal,ack-start,elapsed) == "FAIL"
    assert check(terminal,elapsed,elapsed/2) == "FAIL"
    assert check({},elapsed,elapsed) == "INCOMPLETE"


@pytest.mark.parametrize("weights,balanced,skewed", [
    ([1,1],[50,50],[60,40]), ([1,3],[25,75],[50,50]), ([2,3,5],[20,30,50],[20,40,40]),
])
def test_queue_service_relative_to_declared_entitlements(weights,balanced,skewed):
    from benches.x03_dashboard_oracle import scheduler_fairness_quality as grade
    entitlements = dict(enumerate(weights))
    assert sum(balanced) == sum(skewed)
    assert grade(dict(enumerate(balanced)),entitlements,0) == "PASS"
    assert grade(dict(enumerate(skewed)),entitlements,0) == "FAIL"
    assert grade(dict(enumerate(balanced)),{},0) == "INCOMPLETE"




@pytest.mark.parametrize("receive,send_cal,recv_cal,bounds", [
    (15,(1,2),(3,4),(2,4)),(10,(-1,1),(-2,0),(0,3)),(8,(4,5),(0,1),(1,3)),
])
def test_latency_respects_both_host_clocks(receive,send_cal,recv_cal,bounds):
    from benches.x03_dashboard_oracle import latency_bounds_quality as grade
    assert grade(10,receive,send_cal,recv_cal,bounds) == "PASS"
    assert grade(10,receive,send_cal,recv_cal,(bounds[1],bounds[1])) == "FAIL"
    assert grade(10,receive,None,recv_cal,bounds) == "INCOMPLETE"


@pytest.mark.parametrize("events,active", [
    ([(0,"start"),(3,"suspend"),(8,"resume"),(10,"complete")],5),
    ([(1,"start"),(2,"suspend"),(4,"resume"),(7,"suspend"),(9,"resume"),(12,"complete")],7),
    ([(0.5,"start"),(1.5,"suspend"),(2.5,"resume"),(3,"complete")],1.5),
])
def test_preempted_jobs_exclude_suspended_time(events,active):
    from benches.x03_dashboard_oracle import preemption_quality as grade
    assert grade(events,active) == "PASS"
    assert grade(events,events[-1][0]-events[0][0]) == "FAIL"
    assert grade(events[:-1]+[events[-2],events[-1]],active) == "FAIL"
    assert grade(events[:-1],active) == "INCOMPLETE"


@pytest.mark.parametrize("window,cooldown,rows,wrong", [
    (2,0,[(1,4,4),(2,1,4),(3,1,1)],[(1,4,4),(2,1,1)]),
    (1,3,[(1,5,5),(2,1,5),(3,1,5),(4,1,1)],[(1,5,5),(2,1,1)]),
    (3,2,[(1,4,4),(2,2,4),(3,1,4),(4,1,2)],[(1,4,4),(2,2,4),(3,1,1)]),
])
def test_autoscaler_uses_every_decision_tick(window,cooldown,rows,wrong):
    from benches.x03_dashboard_oracle import autoscaler_quality as grade
    policy = dict(revision="scale",down_window_ticks=window,cooldown_ticks=cooldown)
    def check(trace):
        return grade("scale",policy,trace,4,-10)
    assert check(rows) == "PASS"
    assert check(wrong) == "FAIL"
    assert check(rows[:1]+rows[2:]) == "INCOMPLETE"


@pytest.mark.parametrize("chain,ack_last,expected", [
    (["beta"],True,("beta",5)),(["beta","gamma"],True,("gamma",6)),(["beta","gamma"],False,("beta",5)),
])
def test_work_steal_conserves_owner_until_ack(chain,ack_last,expected):
    from benches.x03_dashboard_oracle import work_steal_quality as grade
    initial = [("task","alpha",4)]
    donor = "alpha"
    events = []
    for epoch, recipient in enumerate(chain,4):
        transfer = ("task",donor,recipient,epoch)
        events.append(("offer",*transfer))
        if ack_last or recipient != chain[-1]:
            events.append(("ack",*transfer))
        donor = recipient
    final = [("task",*expected)]
    assert grade(initial,events,final,complete=True) == "PASS"
    assert grade(initial,events,[],complete=True) == "FAIL"
    assert grade(initial,events,final+initial,complete=True) == "FAIL"
    assert grade(initial,events,final,complete=False) == "INCOMPLETE"


@pytest.mark.parametrize("first_end,saved,final_end", [(80,50,100),(20,0,40),(200,190,220)])
def test_retry_discards_non_durable_tail(first_end,saved,final_end):
    from benches.x03_dashboard_oracle import checkpoint_work_quality as grade
    first = dict(parent=None,range=(0,first_end),checkpoint=("c1",saved),finished=False)
    retry = dict(parent="c1",range=(saved,final_end),checkpoint=None,finished=True)
    assert grade([first,retry],final_end,complete=True) == "PASS"
    assert grade([first,retry],final_end+first_end-saved,complete=True) == "FAIL"
    assert grade([first,dict(retry,parent=None)],final_end,complete=True) == "INCOMPLETE"


@pytest.mark.parametrize("workers,tasks,allowed", [
    ({"a":(4,1),"b":(1,4)},{"t":(2,2)},{"t":["a","b"]}),
    ({"a":(2,2),"b":(2,2)},{"x":(2,1),"y":(1,2)},{"x":["a"],"y":["a"]}),
    ({"a":(3,3),"b":(2,2)},{"x":(2,2),"y":(3,3)},{"x":["a","b"],"y":["a","b"]}),
])
def test_worker_fit_matches_cartesian_enumeration(workers,tasks,allowed):
    from itertools import product
    from benches.x03_dashboard_oracle import placement_feasibility_quality as grade
    def feasible(choices):
        for w, capacity in workers.items():
            assigned = [tasks[t] for t,target in zip(tasks,choices) if target == w]
            if any(sum(d[i] for d in assigned) > capacity[i] for i in (0,1)):
                return False
        return True
    expected = any(feasible(p) for p in product(*(allowed[t] for t in tasks)))
    assert grade(workers,tasks,allowed,expected) == "PASS"
    assert grade(workers,tasks,allowed,not expected) == "FAIL"
    assert grade(workers,tasks,{},expected) == "INCOMPLETE"


@pytest.mark.parametrize("start,end,offsets,bounds", [
    pytest.param(100, 130, {"a": (0, 2), "b": (4, 6)}, (24, 28), id="bound-handoff-delay"),
    pytest.param(50, 47, {"a": (5, 7), "b": (-3, -1)}, (3, 7), id="time-an-acknowledgement"),
    pytest.param(0, 4, {"a": (-10, -8), "b": (-12, -10)}, (4, 8), id="reconcile-retry-latency"),
])
def test_cross_worker_offsets_bound_reports(start, end, offsets, bounds):
    from benches.x03_dashboard_oracle import cross_worker_latency_quality as grade

    events = (("a", start), ("b", end))
    assert grade(*events, offsets, bounds) == "PASS"
    midpoint = sum(bounds) // 2
    assert grade(*events, offsets, (midpoint, midpoint)) == "FAIL"
    assert grade(*events, {"a": offsets["a"]}, bounds) == "INCOMPLETE"
    # Shift the end clock back until the supported delay straddles zero.
    assert grade(events[0], ("b", end - midpoint), offsets, bounds) == "INCOMPLETE"


@pytest.mark.parametrize("waiter,owner,priority,base,window,bound", [
    pytest.param("render", "sync", 7, 2, (11, 15), 4, id="check-render-lock-wait"),
    pytest.param("alarm", "trace", 9, 1, (31, 40), 12, id="audit-urgent-alarm-delay"),
    pytest.param("commit", "upload", 5, 4, (100, 106), 6, id="verify-commit-blocking-budget"),
])
def test_priority_inversion_wait_requires_coverage_and_boost(waiter, owner, priority, base, window, bound):
    from benches.x03_dashboard_oracle import priority_inversion_quality as grade

    start, end = window
    priorities = {waiter: priority, owner: base}
    rule = {"policy": "single-lock-immediate-inheritance-v1", "max_wait_ticks": bound}
    def report(rows, *, acquired=True, stop=end):
        return grade(rule, waiter, priorities, (start, stop), rows, acquired=acquired)

    boosted = [(start, end, owner, priority)]
    assert report(boosted) == "PASS"
    assert report(boosted, acquired=False) == "INCOMPLETE"
    assert report([(start, end, owner, base)]) == "FAIL"
    assert report([(start + 1, end, owner, priority)]) == "INCOMPLETE"
    late = start + bound + 1
    assert report([(start, late, owner, priority)], acquired=False, stop=late) == "FAIL"


@pytest.mark.parametrize("queue,needs,space,allowed,expected,fifo", [
    pytest.param(["large", "small"], [9, 3], [4], [[True], [True]], ["small"], [None],
                 id="serve-a-small-job-behind-an-oversize-task"),
    pytest.param(["gpu", "cpu"], [2, 2], [3, 4], [[False, True], [True, True]],
                 ["cpu", "gpu"], [None, "gpu"], id="drain-the-cpu-queue-during-a-gpu-wait"),
    pytest.param(["bulk", "urgent", "light"], [8, 3, 1], [2, 4],
                 [[True, True], [True, True], [True, True]], ["light", "urgent"], [None, None],
                 id="assign-two-workers-around-a-blocked-head"),
])
def test_head_of_line_bypass_reconciles_idle_workers(queue, needs, space, allowed, expected, fifo):
    from benches.x03_dashboard_oracle import head_of_line_quality as grade

    workers = [f"w{i}" for i in range(len(space))]
    demand = dict(zip(queue, needs))
    free = dict(zip(workers, space))
    eligible = {task: dict(zip(workers, row)) for task, row in zip(queue, allowed)}
    args = (queue, workers, demand, free, eligible)
    bypass = dict(zip(workers, expected))
    strict = dict(zip(workers, fifo))
    assert grade("strict-fifo-v1", *args, strict) == "PASS"
    assert grade("first-compatible-bypass-v1", *args, bypass) == "PASS"
    assert grade("first-compatible-bypass-v1", *args, strict) == "FAIL"
    del eligible[queue[0]][workers[0]]
    assert grade("first-compatible-bypass-v1", *args, bypass) == "INCOMPLETE"


@pytest.mark.parametrize("owners,waits,releasable", [
    pytest.param({"tile": "draw", "cache": "save"}, {"draw": ["cache"], "save": ["tile"]},
                 "tile", id="check-two-worker-lock-exchange"),
    pytest.param({"a": "one", "b": "two", "c": "three", "d": "log"},
                 {"one": ["b", "d"], "two": ["c"], "three": ["a"], "log": []},
                 "b", id="find-a-three-stage-pipeline-cycle"),
    pytest.param({"lease": "retry", "quota": "submit"}, {"retry": ["lease"], "submit": ["lease"]},
                 "lease", id="detect-a-nonreentrant-self-wait"),
])
def test_deadlock_respects_release_dependencies(owners, waits, releasable):
    from benches.x03_dashboard_oracle import resource_deadlock_quality as grade

    policy = "exclusive-wait-all-v1"
    release = dict.fromkeys(owners, "on-completion")
    assert grade(policy, owners, waits, release) == "FAIL"
    release[releasable] = "independent-timer"
    assert grade(policy, owners, waits, release) == "PASS"
    without_owner = {k: v for k, v in owners.items() if k != releasable}
    without_rule = {k: v for k, v in release.items() if k != releasable}
    assert grade(policy, without_owner, waits, without_rule) == "INCOMPLETE"


@pytest.mark.parametrize("low,high,occupancies", [
    pytest.param(2, 8, [8, 4, 2], id="check-a-queue-draining-to-its-resume-watermark"),
    pytest.param(5, 12, [16, 11, 4], id="audit-an-ingest-burst-above-the-stop-limit"),
    pytest.param(0, 3, [3, 1, 0], id="verify-intake-waits-for-an-empty-buffer"),
])
def test_buffer_watermarks_require_matching_intake_events(low, high, occupancies):
    from benches.x03_dashboard_oracle import backpressure_hysteresis_quality as grade

    start, middle, last = occupancies
    rows = [(0, 1, start, "pause", 0), (1, 2, middle, "none", 0), (2, 3, last, "resume", 1)]
    def check(trace):
        return grade(low, high, (0, 3), trace, initially_paused=False)

    assert check(rows) == "PASS"
    assert check([rows[0], (1, 2, middle, "resume", 1), rows[2]]) == "FAIL"
    assert check([(0, 1, start, "pause", 1), *rows[1:]]) == "FAIL"
    assert check([rows[0], rows[2]]) == "INCOMPLETE"


@pytest.mark.parametrize("capacity,rate,opening,arrivals,rejected", [
    pytest.param(4, 1, (0, 4), [(0, 3, True), (0, 2, False), (12, 4, True)], 1,
                 id="audit-a-simultaneous-api-burst"),
    pytest.param(3, 0.5, (0, 0), [(2, 2, False), (4, 2, True), (20, 3, True)], 0,
                 id="audit-a-cold-start-with-fractional-refill"),
    pytest.param(2, 2, (10, 1), [(20, 2, True), (20, 1, False), (21, 2, True)], 1,
                 id="cap-idle-credit-before-a-weighted-burst"),
])
def test_token_bucket_accounts_for_bursts(capacity, rate, opening, arrivals, rejected):
    from benches.x03_dashboard_oracle import token_bucket_quality as grade

    assert grade(capacity, rate, opening, arrivals) == "PASS"
    altered = list(arrivals)
    time, cost, _ = altered[rejected]
    altered[rejected] = time, cost, True
    assert sum(c for _, c, _ in arrivals) <= rate * (arrivals[-1][0] - opening[0])
    assert grade(capacity, rate, opening, altered) == "FAIL"
    assert grade(capacity, rate, None, arrivals) == "INCOMPLETE"


@pytest.mark.parametrize("cohorts,ticks", [
    pytest.param({"render": ["left", "right"]}, [4], id="start-stereo-render-workers-together"),
    pytest.param({"solve": ["a", "b", "c"]}, [0], id="reconcile-a-three-shard-launch"),
    pytest.param({"fit": ["a", "b"], "plot": ["a", "b"]}, [1, 8], id="check-separate-cohort-barriers"),
])
def test_gang_start_requires_distinct_reservations_and_all_members(cohorts, ticks):
    from benches.x03_dashboard_oracle import gang_start_quality as grade

    reservations, events = {}, []
    for (cohort, members), tick in zip(cohorts.items(), ticks):
        for index, member in enumerate(members):
            ticket = f"{cohort}/{index}"
            reservations[ticket] = cohort, f"worker-{index}", tick, tick + 2
            events.append((cohort, member, ticket, tick))

    def evaluate(ledger):
        return grade("all-members-one-tick-v1", cohorts, reservations, ledger, complete=True)

    assert evaluate(events) == "PASS"
    assert evaluate(events[1:]) == "FAIL"
    cohort, member, ticket, tick = events[1]
    assert evaluate([events[0], (cohort, member, events[0][2], tick), *events[2:]]) == "FAIL"
    assert evaluate([events[0], (cohort, member, ticket, tick + 1), *events[2:]]) == "FAIL"
    assert grade("all-members-one-tick-v1", {}, reservations, events, complete=True) == "INCOMPLETE"


@pytest.mark.parametrize("threshold,cooldown,history,probe_result", [
    pytest.param(1, 4, ["fail"], "probe-ok", id="recover-from-a-single-failed-service-call"),
    pytest.param(2, 2, ["fail", "ok", "fail", "fail"], "probe-fail", id="restart-cooldown-after-a-failed-probe"),
    pytest.param(3, 6, ["ok", "fail", "fail", "fail"], "probe-ok", id="enforce-a-three-strike-breaker"),
])
def test_service_breaker_replays_cooldown_and_probe(threshold, cooldown, history, probe_result):
    from benches.x03_dashboard_oracle import service_circuit_breaker_quality as grade

    rule = dict(policy="consecutive-single-probe-v1", failures=threshold, cooldown_ticks=cooldown)
    opening = len(history) - 1
    eligible = opening + cooldown
    stream = [(tick, outcome, f"call-{tick}") for tick, outcome in enumerate(history)]
    stream.extend([(eligible - 1, "reject", "early"), (eligible, "probe", "probe-1"),
                   (eligible, "reject", "parallel"), (eligible + 1, probe_result, "probe-1")])
    stream.append((eligible + 2, "ok" if probe_result == "probe-ok" else "reject", "later"))
    def replay(items):
        return grade(rule, [(i, *e) for i, e in enumerate(items)], expected_events=len(stream))

    assert replay(stream) == "PASS"
    late = list(stream)
    late[len(history)] = (eligible - 1, "ok", "early")
    assert replay(late) == "FAIL"
    duplicate = list(stream)
    duplicate[len(history) + 2] = (eligible, "probe", "parallel")
    assert replay(duplicate) == "FAIL"
    assert replay(stream[:-1]) == "INCOMPLETE"


@pytest.mark.parametrize("old,new,old_votes,joint_votes,new_votes", [
    pytest.param(list("abc"), list("cde"), list("ab"), list("abcd"), list("de"),
                 id="verify-a-two-node-replacement"),
    pytest.param(list("abcd"), list("defg"), list("abc"), list("abcdef"), list("efg"),
                 id="reconcile-overlapping-four-voter-configurations"),
    pytest.param(list("abcde"), list("efghi"), list("abe"), list("abefg"), list("fgi"),
                 id="audit-majority-certificates-across-a-five-voter-handoff"),
])
def test_quorum_transition_certificates_intersect(old, new, old_votes, joint_votes, new_votes):
    from benches.x03_dashboard_oracle import quorum_transition_quality as grade

    epochs = {"old": [old], "joint": [old, new], "new": [new]}
    def cert(epoch, voters):
        return {"epoch": epoch, "value": "same-log-entry", "voters": voters}
    def check(membership, receipts):
        return grade("majority-each-config-v1", membership, receipts, complete=True)

    before, handoff, after = cert("old", old_votes), cert("joint", joint_votes), cert("new", new_votes)
    assert check(epochs, [before, handoff]) == "PASS"
    assert check(epochs, [handoff, after]) == "PASS"
    assert check(epochs, [before, after]) == "FAIL"
    assert check(epochs, [cert("joint", old_votes)]) == "FAIL"
    assert check({"old": [old]}, [before, handoff]) == "INCOMPLETE"


@pytest.mark.parametrize("widths,layer_totals", [
    pytest.param([2, 2], [2, 4], id="account-for-gateway-and-service-retries"),
    pytest.param([3, 2], [3, 6], id="bound-database-attempts-across-three-workers"),
    pytest.param([2, 2, 2], [2, 4, 8], id="measure-a-three-tier-retry-cascade"),
])
def test_retry_amplification_counts_all_parent_branches(widths, layer_totals):
    from benches.x03_dashboard_oracle import retry_amplification_quality as grade

    events = [("request", None, 0)]
    parents = ["request"]
    for level, width in enumerate(widths, 1):
        batch = [(f"{p}-{i}", p, level) for p in parents for i in range(width)]
        events.extend(batch)
        parents = [identity for identity, _, _ in batch]
    limits = [dict(per_parent=w, per_original=n) for w, n in zip(widths, layer_totals)]
    def check(caps, rows):
        return grade("layer-attempt-caps-v1", caps, rows, complete=True)

    assert check(limits, list(reversed(events))) == "PASS"
    assert check([*limits[:-1], dict(limits[-1], per_original=layer_totals[-1] - 1)], events) == "FAIL"
    assert check([dict(limits[0], per_parent=widths[0] - 1), *limits[1:]], events) == "FAIL"
    assert check(limits, [events[0], *events[2:]]) == "INCOMPLETE"


@pytest.mark.parametrize("attempts,receipts,losing", [
    pytest.param([("primary", "map", "o1"), ("backup", "map", "o2")],
                 [("map", "backup", "o2")], ("map", "primary", "o1"), id="credit-the-faster-backup"),
    pytest.param([("a", "plot", "p"), ("b", "plot", "q"), ("c", "plot", "r")],
                 [("plot", "a", "p")], ("plot", "c", "r"), id="publish-once-after-three-siblings"),
    pytest.param([("x", "fit", "f"), ("y", "fit", "g"), ("z", "draw", "h")],
                 [("fit", "y", "g"), ("draw", "z", "h")], ("fit", "x", "f"),
                 id="reconcile-two-tasks-with-one-speculative-retry"),
])
def test_speculative_accounting_follows_atomic_winner(attempts, receipts, losing):
    from benches.x03_dashboard_oracle import speculative_winner_quality as grade

    assert grade(attempts, receipts, list(reversed(receipts)), complete=True) == "PASS"
    assert grade(attempts, receipts, [losing, *receipts[1:]], complete=True) == "FAIL"
    assert grade(attempts, [*receipts, losing], receipts, complete=True) == "FAIL"
    assert grade(attempts, receipts, [*receipts, receipts[0]], complete=True) == "FAIL"
    missing = [row for row in attempts if row[0] != receipts[0][1]]
    assert grade(missing, receipts, receipts, complete=True) == "INCOMPLETE"


@pytest.mark.parametrize("key,dependencies,changed", [
    pytest.param("preview", ["drawing"], "drawing", id="refresh-a-changed-drawing-preview"),
    pytest.param("layout", ["drawing", "rules"], "rules", id="evict-a-layout-after-a-rule-update"),
    pytest.param("report", ["model", "weather", "tariff"], "weather", id="refresh-a-dependent-energy-report"),
])
def test_cache_invalidation_reconciles_dependency_digests(key, dependencies, changed):
    from benches.x03_dashboard_oracle import cache_invalidation_quality as grade

    initial = {dep: f"{dep}-v1" for dep in dependencies}
    updated = dict(initial, **{changed: f"{changed}-v2"})
    first = dict(op="build", key=key, dependencies=initial, output="out-1")
    hit = dict(first, op="reuse")
    change = dict(op="change", key=changed, digest=updated[changed])
    evict = dict(op="invalidate", key=key)
    second = dict(first, dependencies=updated, output="out-2")
    def check(events):
        return grade({key: dependencies}, initial, events, complete=True)

    assert check([first, hit, change, evict, second, dict(second, op="reuse")]) == "PASS"
    assert check([first, change, hit]) == "FAIL"
    assert check([first, evict, hit]) == "FAIL"
    assert check([first, change, dict(hit, dependencies=updated)]) == "FAIL"
    assert check([first, dict(hit, dependencies={d: initial[d] for d in dependencies if d != changed})]) == "INCOMPLETE"


@pytest.mark.parametrize("cohorts", [
    pytest.param([["tile-a", "tile-b"], ["tile-a", "tile-b"]], id="release-two-consecutive-render-frames"),
    pytest.param([["a", "b"], ["b", "c"]], id="replace-a-participant-between-epochs"),
    pytest.param([["one"], ["one", "two"], ["two"]], id="resize-an-iterative-worker-cohort"),
])
def test_reusable_barrier_arrivals_expire_at_release(cohorts):
    from benches.x03_dashboard_oracle import reusable_barrier_quality as grade

    rule = "all-members-per-generation-v1"
    events = []
    for generation, members in enumerate(cohorts):
        events.extend(("arrive", generation, member) for member in members)
        events.append(("release", generation, None))
    assert grade(rule, cohorts, events, complete=True) == "PASS"
    assert grade(rule, cohorts, events[1:], complete=True) == "FAIL"
    # Drop one returning member's new arrival. Its last arrival cannot release generation one.
    returning = next(member for member in cohorts[1] if member in cohorts[0])
    missing = [e for e in events if e != ("arrive", 1, returning)]
    assert grade(rule, cohorts, missing, complete=True) == "FAIL"
    stale = [("arrive", 0, returning) if e == ("arrive", 1, returning) else e for e in events]
    assert grade(rule, cohorts, stale, complete=True) == "FAIL"
    assert grade(rule, [cohorts[0], None], events, complete=True) == "INCOMPLETE"


@pytest.mark.parametrize("capacity,opening_grant,acquired,resized,free", [
    pytest.param(3, 1, 2, 4, 2, id="grow-a-full-worker-pool"),
    pytest.param(8, 3, 2, 2, 0, id="shrink-to-the-outstanding-permit-count"),
    pytest.param(5, 2, 1, 3, 2, id="return-a-weighted-reservation-before-resize"),
])
def test_semaphore_conserves_owned_permits(capacity, opening_grant, acquired, resized, free):
    from benches.x03_dashboard_oracle import semaphore_permit_quality as grade

    opening = [("old", "a", opening_grant)]
    acquire = dict(op="acquire", grant="new", owner="b", permits=acquired)
    release = dict(op="release", grant="old", owner="a")
    resize = dict(op="resize", capacity=resized)
    def check(events, reported, snapshot=opening):
        return grade("weighted-owner-grants-v1", capacity, snapshot, events, reported, complete=True)

    assert check([acquire, release, resize], free) == "PASS"
    assert check([acquire, release, release, resize], free) == "FAIL"
    assert check([acquire, release, resize], free + 1) == "FAIL"
    assert check([dict(acquire, permits=capacity)], free) == "FAIL"
    assert check([dict(release, owner="b")], free) == "FAIL"
    assert check([dict(release, owner=None)], free) == "INCOMPLETE"
    assert check([], free, snapshot=None) == "INCOMPLETE"


@pytest.mark.parametrize("workers,early_signal", [
    pytest.param(["draw"], False, id="wake-a-render-worker-after-input-arrives"),
    pytest.param(["merge", "save"], False, id="broadcast-readiness-to-two-waiters"),
    pytest.param(["fit", "plot", "export"], True, id="discard-a-broadcast-before-workers-park"),
])
def test_condition_predicate_does_not_strand_waiters(workers, early_signal):
    from benches.x03_dashboard_oracle import condition_wait_quality as grade

    prefix = [("source", "lock"), ("source", "broadcast"), ("source", "unlock")] if early_signal else []
    for worker in workers:
        prefix.extend([(worker, "lock"), (worker, "wait-until")])
    prefix.extend([("source", "lock"), ("source", "set-true")])
    def encode(steps):
        return [(i, actor, op) for i, (actor, op) in enumerate(steps)]
    def check(rows):
        return grade("locked-predicate-broadcast-v1", rows, initial_predicate=False, expected_count=len(rows))

    valid = encode(prefix + [("source", "broadcast"), ("source", "unlock")])
    assert check(valid) == "PASS"
    assert check(encode(prefix + [("source", "unlock")])) == "FAIL"
    assert check(encode(prefix + [("source", "unlock"), ("source", "broadcast")])) == "FAIL"
    assert check(valid[1:]) == "INCOMPLETE"


@pytest.mark.parametrize("quantum,cost,small", [
    pytest.param(3, 5, 1, id="carry-render-credit-to-a-second-visit"),
    pytest.param(4, 8, 2, id="dispatch-at-the-exact-budget-of-two-rounds"),
    pytest.param(6, 7, 5, id="discard-unused-credit-after-a-large-export"),
])
def test_deficit_round_robin_reconciles_dispatch_and_idle_reset(quantum, cost, small):
    from benches.x03_dashboard_oracle import deficit_round_robin_quality as grade

    order, quanta = ["batch", "short"], {"batch": quantum, "short": small + 1}
    first = dict(arrivals=[("batch", "large", cost), ("short", "quick", small)],
                 dispatch=["quick"], closing={"batch": quantum, "short": 0})
    second = dict(arrivals=[], dispatch=["large"], closing={"batch": 0, "short": 0})
    model = "fifo-deficit-reset-empty-v1"
    assert grade(model, order, quanta, [first, second]) == "PASS"
    assert grade(model, order, quanta, [dict(first, dispatch=["large", "quick"]), second]) == "FAIL"
    assert grade(model, order, quanta, [dict(first, closing={"batch": quantum, "short": 1})]) == "FAIL"
    assert grade(None, order, quanta, [first, second]) == "INCOMPLETE"


@pytest.mark.parametrize("readers,writer", [
    pytest.param(["preview"], "publish", id="serialize-a-preview-and-a-publish"),
    pytest.param(["left", "right"], "rebuild", id="wait-for-both-concurrent-viewport-reads"),
    pytest.param(["audit", "export", "compare"], "replace", id="hand-off-after-three-auditors"),
])
def test_reader_writer_lock_excludes_conflicting_owners(readers, writer):
    from benches.x03_dashboard_oracle import reader_writer_lock_quality as grade

    rule = "shared-read-exclusive-write-v1"
    opened = {readers[0]: "read"}
    extend = [("acquire", r, "read") for r in readers[1:]]
    release = [("release", r, "read") for r in readers]
    write = ("acquire", writer, "write")
    valid = extend + release + [write]
    assert grade(rule, opened, valid, complete=True) == "PASS"
    assert grade(rule, opened, extend + release[:-1] + [write], complete=True) == "FAIL"
    assert grade(rule, opened, valid + [("acquire", "other", "write")], complete=True) == "FAIL"
    assert grade(rule, opened, valid + [("acquire", "other", "read")], complete=True) == "FAIL"
    assert grade(rule, None, valid, complete=True) == "INCOMPLETE"
