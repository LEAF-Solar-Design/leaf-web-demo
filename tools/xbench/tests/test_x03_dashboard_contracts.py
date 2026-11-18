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
    first = dict(id="1",drawing="d",version="r",tool=tool,revision="old",output="aa",status="complete")
    next_run = dict(first,id="2",revision="new",output="bb")
    failed = dict(first,id="3",status="failed")
    receipts = [first,next_run,failed]
    totals = [dict(drawing="d",version="r",count=1)]
    delta = [dict(drawing="d",version="r",tool=tool,before="old",after="new")]
    def grade(runs, changes):
        return dashboard_quality(runs,totals,changes,"old","new")
    assert grade(receipts,delta) == "PASS"
    assert grade(receipts+[failed],delta) == "FAIL"
    assert grade([first,dict(next_run,version=None),failed],delta) == "INCOMPLETE"
    assert grade([first,failed],delta) == "FAIL"
    assert grade(receipts,[]) == "FAIL"
