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
