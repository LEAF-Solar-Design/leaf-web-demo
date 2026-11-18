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
