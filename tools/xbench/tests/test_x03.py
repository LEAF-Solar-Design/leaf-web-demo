import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("x03", Path(__file__).parents[1] / "benches/x03.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
SHA = "a" * 40


def records():
    return [{"id": str(i), "trade": trade, "drawing": "synthetic", "source_sha": SHA,
             "author_submitted": True, "model_id": "synthetic-model", "score": 1,
             "failing_stage": None, "tool_name": "synthetic-tool", "version": "1",
             "tool_source_hash": "b" * 40, "output_hash": "c" * 64}
            for i, trade in enumerate(["solar"] * 30 + ["structural"] * 10 + ["mechanical"] * 10)]


def test_comparison_rejects_extra_keys_boolean_nan_and_obeys_absolute_tolerance():
    exact = {"mode": "exact"}
    assert bench.matches({"value": [1, "a"]}, {"value": [1, "a"]}, exact)
    assert not bench.matches({"value": 1, "extra": 1}, {"value": 1}, exact)
    assert not bench.matches(True, 1, exact)
    assert not bench.matches(float("nan"), 1, exact)
    assert bench.matches(2.001, 2, {"mode": "absolute", "tolerance": 0.0011})
    assert not bench.matches(2.002, 2, {"mode": "absolute", "tolerance": 0.001})


def test_regression_boundary_and_first_baseline(tmp_path):
    first = records()
    _, baseline = bench.grade_records(first, baseline=None, prompt_sha256="d" * 64,
                                      source_sha=SHA, out_dir=tmp_path)
    # Exactly five points overall and ten points in one trade is allowed.
    for index in (0, 1, 2):
        first[index].update(score=0, failing_stage="comparison")
    _, report = bench.grade_records(first, baseline=baseline, prompt_sha256="d" * 64,
                                    source_sha=SHA, out_dir=tmp_path)
    assert report["REGRESSION"]  # Three of fifty is six points overall.
    first[2].update(score=1, failing_stage=None)
    _, report = bench.grade_records(first, baseline=baseline, prompt_sha256="d" * 64,
                                    source_sha=SHA, out_dir=tmp_path)
    assert not report["REGRESSION"]
    with pytest.raises(ValueError, match="different frozen"):
        bench.grade_records(first, baseline=baseline, prompt_sha256="e" * 64,
                            source_sha=SHA, out_dir=tmp_path)


def test_trade_floor_and_incomplete_run_never_pass(tmp_path):
    first = records()
    for index in range(40, 46):
        first[index].update(score=0, failing_stage="comparison")
    steps, report = bench.grade_records(first, baseline=None, prompt_sha256="d" * 64,
                                       source_sha=SHA, out_dir=tmp_path)
    assert report["overall"] == 0.88
    assert steps[-1].verdict == "FAIL"
    steps, report = bench.grade_records(first[:-1], baseline=None, prompt_sha256="d" * 64,
                                       source_sha=SHA, out_dir=tmp_path)
    assert not report["complete"]
    assert all(step.verdict == "UNREADABLE" for step in steps)


def test_unreadable_evidence_is_not_a_scored_failure_or_baseline(tmp_path):
    first = records()
    first[0].update(score=0, failing_stage="author", unreadable=True)
    steps, report = bench.grade_records(first, baseline=None, prompt_sha256="d" * 64,
                                       source_sha=SHA, out_dir=tmp_path)
    assert not report["complete"]
    assert all(step.verdict == "UNREADABLE" for step in steps)


def run_prompt(tmp_path, *, fallback=False, model=True, publication="published", value=3, drift=False):
    calls = []
    def transport(method, path, **kw):
        calls.append((method, path, kw))
        if path == "/api/health":
            return 200, {"source_sha": "e" * 40 if drift else SHA}
        if path == "/api/author":
            body = {"tool": {"name": "generated"}, "receipt": {"state": "staged",
                    "staged_commit": "b" * 40, "change_set_id": "synthetic-change"}}
            if fallback:
                body["preview"] = "templated fallback"
            if model:
                body["model_id"] = "synthetic-model"
            return 200, body
        if path == "/api/author/publication-requests":
            return 200, {"status": publication}
        if path == "/api/tools":
            return 200, {"tools": [{"name": "generated", "catalog_digest": "f" * 64}]}
        if path == "/api/run":
            return 200, {"result": {"value": value}}
        raise AssertionError(path)
    class Client:
        def run(self, *args, **kwargs):
            calls.append(("POST", "/api/run", kwargs))
            return 200, {"result": {"value": value}}
    row = {"id": "X03-01", "trade": "solar", "drawing": "synthetic",
           "prompt": "Count synthetic entities", "oracle": {"value": 3}, "compare": {"mode": "exact"}}
    result = bench.evaluate_prompt(row, transport=transport, client=Client(),
        project={"project_id": "p", "org_id": "o"}, drawing={"version_id": "v"},
        source_sha=SHA, evidence_dir=tmp_path, run_id="test")
    return result, calls


@pytest.mark.parametrize("options,stage", [({"fallback": True}, "author"),
    ({"model": False}, "model_provenance"), ({"publication": "awaiting_approval"}, "publication"),
    ({"value": 4}, "comparison"), ({"drift": True}, "source_drift")])
def test_no_credit_for_fallback_unpublished_wrong_output_or_missing_provenance(tmp_path, options, stage):
    result, calls = run_prompt(tmp_path, **options)
    assert result["score"] == 0
    assert result["failing_stage"] == stage
    if stage in ("author", "publication", "source_drift"):
        assert not any(path == "/api/run" for _, path, _ in calls)


def test_success_uses_normal_author_publication_and_bound_run(tmp_path):
    result, calls = run_prompt(tmp_path)
    assert result["score"] == 1
    assert result["output_hash"] == bench.content_hash({"value": 3})
    assert [path for method, path, _ in calls if method == "POST"] == [
        "/api/author", "/api/author/publication-requests", "/api/run"]
    assert next(kw for _, path, kw in calls if path == "/api/run")["json_body"]["dwg"] == "v"
