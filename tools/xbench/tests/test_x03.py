import importlib.util
import copy
import csv
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("x03", Path(__file__).parents[1] / "benches/x03.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
SHA = "a" * 40


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


def staged_tool():
    return {"tool": {"name": "generated"}, "model_id": "synthetic-model",
            "receipt": {"state": "staged", "staged_commit": "b" * 40,
                        "change_set_id": "synthetic-change"}}


def synthetic_prompt():
    return {"id": "X03-01", "trade": "solar", "drawing": "synthetic",
            "prompt": "Count synthetic entities", "oracle": {"value": 3},
            "compare": {"mode": "exact"}}


def prompt_manifest(tmp_path, *, invalid_id=None):
    rows = []
    for i, trade in enumerate(["solar"] * 30 + ["structural"] * 10 + ["mechanical"] * 10):
        rows.append({**synthetic_prompt(), "id": f"X03-{i+1:02d}", "trade": trade,
                     "drawing": trade, "oracle_method": {"query": {"operation": "count"}}})
    if invalid_id is not None:
        rows[0]["id"] = invalid_id
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"schema": "leaf.xbench.x03-prompts.v1", "rows": rows}))
    review = tmp_path / "review.json"
    review.write_text("{}")
    fixtures = [{"id": "x03-prompts", "path": str(path), "bytes": path.stat().st_size,
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
                {"id": "review", "path": str(review)}]
    for trade in bench.TRADES:
        drawing = tmp_path / (trade + ".dxf")
        drawing.write_bytes(b"synthetic drawing")
        fixtures.append({"id": trade, "role": "drawing", "path": str(drawing)})
    return {"fixtures": fixtures, "oracle_review": {"evidence": str(review),
            "row_ids": [row["id"] for row in rows[:10]]},
            "baseline_path": str(tmp_path / "baseline.json")}


@pytest.mark.parametrize("invalid_id", ["../escaped", r"..\escaped", "/absolute",
    "C:/absolute", r"C:\absolute", "sub/name", r"sub\name", "X03-00", "X03-51", ""])
def test_unsafe_prompt_ids_are_rejected_before_requests_or_evidence(tmp_path, invalid_id):
    manifest = prompt_manifest(tmp_path, invalid_id=invalid_id)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="prompt ID"):
        bench.load_prompts(manifest)
    row = {**synthetic_prompt(), "id": invalid_id}
    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe ID reached transport")
    with pytest.raises(ValueError, match="prompt ID"):
        bench.evaluate_prompt(row, transport=forbidden, client=None, project={}, drawing={},
            source_sha=SHA, evidence_dir=tmp_path, run_id="unsafe")
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (tmp_path.parent / "escaped.json").exists()


def test_changed_prompt_bytes_fail_before_session_or_author_request(tmp_path, monkeypatch):
    manifest = prompt_manifest(tmp_path)
    rows, digest = bench.load_prompts(manifest)
    assert len(rows) == 50
    assert digest == manifest["fixtures"][0]["sha256"]
    path = Path(manifest["fixtures"][0]["path"])
    path.write_bytes(path.read_bytes() + b" ")  # Still valid JSON, different bytes.
    def forbidden(*args, **kwargs):
        raise AssertionError("changed prompts reached browser or transport")
    monkeypatch.setattr(bench.xbench_session, "signed_in", forbidden)
    monkeypatch.setattr(bench, "transport_for", forbidden)
    monkeypatch.setitem(sys.modules, "xbench_studio", SimpleNamespace(
        StudioClient=forbidden, disposable_project=forbidden))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        bench.run(SimpleNamespace(manifest=manifest, out_dir=tmp_path / "out"))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("case", ["complete", "upload-failure", "drift", "existing-baseline", "baseline-race", "project-cleanup-failure", "authority-refusal", "authority-timeout", "nan-output", "inf-output", "negative-inf-output"])
def test_mocked_run_lifecycle_dispatches_sequentially_and_cleans_up(tmp_path, monkeypatch, case):
    manifest = prompt_manifest(tmp_path)
    fail_upload = case == "upload-failure"
    out = tmp_path / "out"
    events, dispatches, snapshots = [], [], []
    uploads = 0
    real_evaluate = bench.evaluate_prompt
    baseline_path = Path(manifest["baseline_path"])
    baseline_bytes = None
    if case == "existing-baseline":
        baseline_bytes = (json.dumps({"schema": "leaf.xbench.x03-scores.v1", "complete": True,
            "source_sha": SHA, "prompt_sha256": manifest["fixtures"][0]["sha256"],
            "overall": 0.6, "trades": {trade: 0.6 for trade in bench.TRADES}}) + "\n ").encode()
        baseline_path.write_bytes(baseline_bytes)
    if case == "baseline-race":
        original_write = bench.write_json_atomic
        def race_write(path, value):
            result = original_write(path, value)
            if Path(path).name == "summary.json":
                baseline_path.write_bytes(b"competing baseline must survive")
            return result
        monkeypatch.setattr(bench, "write_json_atomic", race_write)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline lifecycle reached browser or network")

    @contextmanager
    def signed_in(*args, **kwargs):
        events.append("session-open")
        try:
            yield object(), {"source": SHA}
        finally:
            events.append("session-close")

    class Client:
        def __init__(self, transport):
            pass

        def create_project(self, name):
            trade = name.split()[1]
            events.append("create-" + trade)
            return {"project_id": trade, "org_id": "synthetic-org"}

        def delete_project(self, project_id):
            events.append("delete-" + project_id)
            if case == "project-cleanup-failure" and project_id == "structural":
                raise RuntimeError("SECRET SENTINEL cleanup detail")

        def upload_drawing(self, name, content):
            nonlocal uploads
            uploads += 1
            assert content == b"synthetic drawing"
            if fail_upload and uploads == 2:
                raise RuntimeError("synthetic upload failure")
            return name

        def wait_upload(self, *args, **kwargs):
            pass

        def adopt_drawing(self, project_id, uploaded, name):
            return {"drawing_id": project_id + "-drawing", "version_id": project_id + "-version"}

    @contextmanager
    def disposable_project(client, name):
        project = client.create_project(name)
        try:
            yield project
        finally:
            client.delete_project(project["project_id"])

    def evaluate(row, **kwargs):
        previous = json.loads((out / "records.json").read_text()) if dispatches else []
        assert len(previous) == len(dispatches), "each result must persist before the next dispatch"
        assert kwargs["project"]["project_id"] == row["trade"]
        assert kwargs["drawing"]["version_id"] == row["trade"] + "-version"
        assert kwargs["source_sha"] == SHA
        snapshots.append(copy.deepcopy(previous))
        dispatches.append(row["id"])
        if case in ("nan-output", "inf-output", "negative-inf-output"):
            value = {"nan-output": float("nan"), "inf-output": float("inf"), "negative-inf-output": -float("inf")}[case]
            result, _ = run_prompt(out, value=[{"nested": value}], evaluator=real_evaluate)
            return {**result, "trade": row["trade"], "drawing": row["drawing"]}
        if case == "drift":
            health_reads = 0
            def transport(method, path, **kw):
                nonlocal health_reads
                if path == "/api/health":
                    health_reads += 1
                    return 200, {"source_sha": SHA if health_reads == 1 else "e" * 40}
                if path == "/api/author":
                    return 200, staged_tool()
                if path == "/api/author/publication-requests":
                    return 200, {"status": "published"}
                if path == "/api/tools":
                    return 200, {"tools": [{"name": "generated", "catalog_digest": "f" * 64}]}
                if path == "/api/run":
                    return 200, {"ok": True, "result": row["oracle"]}
                raise AssertionError(path)
            return real_evaluate(row, **{**kwargs, "transport": transport})
        bench.write_json_atomic(out / (row["id"] + "-output.json"), row["oracle"])
        return {**records()[0], "id": row["id"], "trade": row["trade"], "drawing": row["drawing"]}

    monkeypatch.setitem(sys.modules, "xbench_studio", SimpleNamespace(
        StudioClient=Client, disposable_project=disposable_project))
    monkeypatch.setattr(bench.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(bench.xbench_session, "api", forbidden)
    monkeypatch.setattr(bench, "transport_for", lambda *args: forbidden)
    @contextmanager
    def fake_authority(*args, **kwargs):
        yield {"X-Authority-Session-Id": "owned-session", "X-Authority-Turn-Id": "owned-turn"}
    if case.startswith("authority-"):
        authority_calls = []
        def authority_transport(method, path, **kw):
            authority_calls.append(path)
            if path == "/api/sessions":
                if len(dispatches) == 1 and case == "authority-timeout":
                    raise TimeoutError("synthetic timeout")
                return 200, {"session_id": "owned-session"}
            if path.endswith("/messages"):
                if len(dispatches) == 1:
                    return 403, {"reason_code": "tenant_role_denied"}
                return 202, {"turn_id": "owned-turn", "status": "started"}
            assert path == "/api/sessions/owned-session/turns/owned-turn/cancel"
            events.append("cancel-owned-turn")
            return 200, {}
        monkeypatch.setattr(bench, "transport_for", lambda *args: authority_transport)
    else:
        monkeypatch.setattr(bench, "author_authority", fake_authority)
    monkeypatch.setattr(bench, "evaluate_prompt", evaluate)
    ctx = SimpleNamespace(manifest=manifest, source_sha=SHA,
                          base="https://synthetic.invalid", out_dir=out)
    if fail_upload:
        with pytest.raises(RuntimeError, match="synthetic upload failure"):
            bench.run(ctx)
        assert not dispatches
        assert not (out / "records.json").exists()
        assert not Path(manifest["baseline_path"]).exists()
    elif case == "project-cleanup-failure":
        steps = bench.run(ctx)
        assert all(step.verdict == "UNREADABLE" for step in steps)
        assert len(dispatches) == 50
        assert len(json.loads((out / "records.json").read_text())) == 50
        assert json.loads((out / "summary.json").read_text())["complete"] is False
        assert json.loads((out / "cleanup.json").read_text()) == [{"stage": "owned_resource_cleanup", "failure": "RuntimeError"}]
        assert "SECRET SENTINEL" not in (out / "cleanup.json").read_text()
        assert not baseline_path.exists()
    elif case == "baseline-race":
        with pytest.raises(FileExistsError):
            bench.run(ctx)
        assert len(dispatches) == 50
        assert baseline_path.read_bytes() == b"competing baseline must survive"
    elif case.startswith("authority-") or case.endswith("-output"):
        steps = bench.run(ctx)
        assert dispatches == ["X03-01"]
        assert all(step.verdict == "UNREADABLE" for step in steps)
        persisted = json.loads((out / "records.json").read_text())
        if case.startswith("authority-"):
            assert [r["id"] for r in persisted] == ["X03-01", "X03-02"]
            assert persisted[0]["score"] == 1
            assert persisted[1]["error_code"] == "AUTHOR_AUTHORITY_SETUP_FAILED"
            assert persisted[1]["author_submitted"] is False
            assert (out / "X03-02-authority.json").is_file()
            assert authority_calls.count("/api/sessions/owned-session/turns/owned-turn/cancel") == 1
            events.remove("cancel-owned-turn")
        else:
            assert len(persisted) == 1
            assert persisted[0]["output_hash"] is None
            assert persisted[0]["error_code"] == "INVALID_EXECUTION_OUTPUT"
            assert not (out / "X03-01-output.json").exists()
        assert persisted[-1]["score"] == 0 and persisted[-1]["unreadable"] is True
        assert json.loads((out / "summary.json").read_text())["complete"] is False
        assert not baseline_path.exists()
    elif case == "drift":
        steps = bench.run(ctx)
        assert dispatches == ["X03-01"]
        assert all(step.verdict == "UNREADABLE" for step in steps)
        evidence = json.loads((out / "X03-01.json").read_text())
        assert evidence["record"]["score"] == 0
        assert evidence["record"]["failing_stage"] == "source_drift"
        assert json.loads((out / "X03-01-output.json").read_text()) == {"value": 3}
        assert len(json.loads((out / "records.json").read_text())) == 1
        assert not baseline_path.exists()
    else:
        steps = bench.run(ctx)
        assert all(step.verdict == "PASS" for step in steps)
        assert dispatches == [f"X03-{i:02d}" for i in range(1, 51)]
        assert [len(snapshot) for snapshot in snapshots] == list(range(50))
        assert len(json.loads((out / "records.json").read_text())) == 50
        assert json.loads(Path(manifest["baseline_path"]).read_text())["complete"] is True
        if baseline_bytes is not None:
            assert baseline_path.read_bytes() == baseline_bytes
    assert events == ["session-open", "create-solar", "create-structural", "create-mechanical",
                      "delete-mechanical", "delete-structural", "delete-solar", "session-close"]


def test_csv_and_summary_rates_agree_for_mixed_models_and_order(tmp_path):
    source = records()
    for i, row in enumerate(source):
        row["model_id"] = "model-a" if i < 20 else "model-b" if i < 40 else None
        if i % 4 == 0 or row["model_id"] is None:
            row.update(score=0, failing_stage="comparison" if row["model_id"] else "model_provenance")
    reports = []
    for name, rows in (("forward", source), ("reverse", list(reversed(source)))):
        out = tmp_path / name
        out.mkdir()
        persist_outputs(out)
        _, report = bench.grade_records(rows, frozen_rows=records(), baseline=None, prompt_sha256="d" * 64,
                                        source_sha=SHA, out_dir=out)
        summary = json.loads((out / "summary.json").read_text())
        assert summary == report
        with (out / "scores.csv").open(newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
        def rate(group):
            return sum(int(row["score"]) for row in group) / len(group) if group else None
        assert summary["overall"] == rate(csv_rows)
        for trade in bench.TRADES:
            assert summary["trades"][trade] == rate([r for r in csv_rows if r["trade"] == trade])
        for model, values in summary["models"].items():
            group = [r for r in csv_rows if (r["model_id"] or "unreported") == model]
            assert values["overall"] == rate(group)
            for trade in bench.TRADES:
                assert values["trades"][trade] == rate([r for r in group if r["trade"] == trade])
        assert summary["models"]["model-a"]["trades"]["mechanical"] is None
        assert summary["models"]["unreported"]["overall"] == 0
        reports.append(summary)
    assert reports[0] == reports[1]


@pytest.mark.parametrize("phase", ["author", "execution"])
def test_polling_deadline_stops_requests_at_600_seconds_without_credit(tmp_path, phase):
    clock = FakeClock()
    calls = []
    poll = "/api/author/stages/change" if phase == "author" else "/api/jobs/job"

    def transport(method, path, **kwargs):
        assert clock.now < 600
        assert 0 < kwargs["timeout_s"] <= 600 - clock.now
        calls.append((path, clock.now))
        if path == "/api/health":
            return 200, {"source_sha": SHA}
        if path == "/api/author":
            if phase == "author":
                clock.now = 590
                return 202, {"contract": "leaf.customization-stage-job.v1",
                             "poll_url": poll, "change_set_id": "change"}
            return 200, staged_tool()
        if path == "/api/author/publication-requests":
            return 200, {"status": "published"}
        if path == "/api/tools":
            return 200, {"tools": [{"name": "generated", "catalog_digest": "f" * 64}]}
        if path == "/api/run":
            clock.now = 590
            return 202, {"job_id": "job", "status": "pending"}
        if path == poll:
            clock.now += 7
            return (200, {"contract": "leaf.customization-stage-job.v1",
                          "poll_url": poll, "change_set_id": "change"}) if phase == "author" else (200, {"status": "pending"})
        raise AssertionError(path)

    record = bench.evaluate_prompt(synthetic_prompt(), transport=transport, client=None,
        project={"project_id": "p", "org_id": "o"}, drawing={"version_id": "v"},
        source_sha=SHA, evidence_dir=tmp_path, run_id="timeout",
        clock=clock, sleep=clock.sleep)
    assert clock.now == 600
    assert sum(path == poll for path, _ in calls) == 1
    assert all(at < 600 for _, at in calls)
    assert record["score"] == 0
    assert record["failing_stage"] == phase
    assert "600 second ceiling" in record["failure"]
    assert record["output_hash"] is None
    evidence = json.loads((tmp_path / "X03-01.json").read_text())
    assert len(evidence["routes"]) == len(calls)


@pytest.mark.parametrize("cancel_on_submit", [True, False])
def test_cancelled_execution_stops_immediately_with_execution_evidence(tmp_path, cancel_on_submit):
    clock = FakeClock()
    polls = []

    def transport(method, path, **kwargs):
        if path == "/api/health":
            return 200, {"source_sha": SHA}
        if path == "/api/author":
            return 200, staged_tool()
        if path == "/api/author/publication-requests":
            return 200, {"status": "published"}
        if path == "/api/tools":
            return 200, {"tools": [{"name": "generated", "catalog_digest": "f" * 64}]}
        if path == "/api/run":
            return 202, {"job_id": "job", "status": "cancelled" if cancel_on_submit else "pending"}
        if path == "/api/jobs/job":
            polls.append(path)
            assert len(polls) == 1, "no request may follow cancellation"
            return 200, {"status": "cancelled"}
        raise AssertionError(path)

    result = bench.evaluate_prompt(synthetic_prompt(), transport=transport, client=None,
        project={"project_id": "p", "org_id": "o"}, drawing={"version_id": "v"},
        source_sha=SHA, evidence_dir=tmp_path, run_id="cancel",
        clock=clock, sleep=clock.sleep)
    assert len(polls) == (0 if cancel_on_submit else 1)
    assert result["score"] == 0
    assert result["failing_stage"] == "execution"
    assert result["error_code"] == "JOB_CANCELLED"
    assert result["failure"] == "tool execution cancelled"
    assert result["output_hash"] is None
    assert not result.get("unreadable")


def records():
    return [{"id": f"X03-{i+1:02d}", "trade": trade, "drawing": "synthetic", "source_sha": SHA,
             "author_submitted": True, "oracle": {"value": 3}, "compare": {"mode": "exact"},
             "model_id": "synthetic-model", "score": 1,
             "failing_stage": None, "tool_name": "synthetic-tool", "version": "1",
             "tool_source_hash": "b" * 40, "output_hash": bench.content_hash({"value": 3})}
            for i, trade in enumerate(["solar"] * 30 + ["structural"] * 10 + ["mechanical"] * 10)]


@pytest.mark.parametrize("change", [
    {"id": "X03-51"}, {"id": "../bad"}, {"id": "X03-02"},
    {"score": 2}, {"score": -1}, {"score": 0.5}, {"score": True},
    {"score": 1, "failing_stage": "author"},
    {"score": 0, "failing_stage": None}, {"score": 0, "failing_stage": ""},
    {"score": 1, "unreadable": True},
])
def test_invalid_records_cannot_emit_acceptance_evidence(tmp_path, change):
    rows = records()
    rows[0].update(change)
    with pytest.raises(ValueError):
        bench.grade_records(rows, frozen_rows=records(), baseline=None, prompt_sha256="d" * 64,
                            source_sha=SHA, out_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_comparison_rejects_extra_keys_boolean_nan_and_obeys_absolute_tolerance():
    exact = {"mode": "exact"}
    assert bench.matches({"value": [1, "a"]}, {"value": [1, "a"]}, exact)
    assert not bench.matches({"value": 1, "extra": 1}, {"value": 1}, exact)
    assert not bench.matches(True, 1, exact)
    assert not bench.matches(float("nan"), 1, exact)
    assert bench.matches(2.001, 2, {"mode": "absolute", "tolerance": 0.0011})
    assert not bench.matches(2.002, 2, {"mode": "absolute", "tolerance": 0.001})


def test_regression_boundary_and_first_baseline(tmp_path):
    persist_outputs(tmp_path)
    first = records()
    _, baseline = bench.grade_records(first, frozen_rows=records(), baseline=None, prompt_sha256="d" * 64,
                                      source_sha=SHA, out_dir=tmp_path)
    # Exactly five points overall and ten points in one trade is allowed.
    for index in (0, 1, 2):
        first[index].update(score=0, failing_stage="comparison")
    _, report = bench.grade_records(first, frozen_rows=records(), baseline=baseline, prompt_sha256="d" * 64,
                                    source_sha=SHA, out_dir=tmp_path)
    assert report["REGRESSION"]  # Three of fifty is six points overall.
    first[2].update(score=1, failing_stage=None)
    _, report = bench.grade_records(first, frozen_rows=records(), baseline=baseline, prompt_sha256="d" * 64,
                                    source_sha=SHA, out_dir=tmp_path)
    assert not report["REGRESSION"]
    with pytest.raises(ValueError, match="different frozen"):
        bench.grade_records(first, frozen_rows=records(), baseline=baseline, prompt_sha256="e" * 64,
                            source_sha=SHA, out_dir=tmp_path)


def test_trade_floor_and_incomplete_run_never_pass(tmp_path):
    persist_outputs(tmp_path)
    first = records()
    for index in range(40, 46):
        first[index].update(score=0, failing_stage="comparison")
    steps, report = bench.grade_records(first, frozen_rows=records(), baseline=None, prompt_sha256="d" * 64,
                                       source_sha=SHA, out_dir=tmp_path)
    assert report["overall"] == 0.88
    assert steps[-1].verdict == "FAIL"
    steps, report = bench.grade_records(first[:-1], frozen_rows=records(), baseline=None, prompt_sha256="d" * 64,
                                       source_sha=SHA, out_dir=tmp_path)
    assert not report["complete"]
    assert all(step.verdict == "UNREADABLE" for step in steps)


def test_unreadable_evidence_is_not_a_scored_failure_or_baseline(tmp_path):
    persist_outputs(tmp_path)
    first = records()
    first[0].update(score=0, failing_stage="author", unreadable=True)
    steps, report = bench.grade_records(first, frozen_rows=records(), baseline=None, prompt_sha256="d" * 64,
                                       source_sha=SHA, out_dir=tmp_path)
    assert not report["complete"]
    assert all(step.verdict == "UNREADABLE" for step in steps)


@pytest.mark.parametrize("change", [
    {"schema": "wrong"}, {"complete": False}, {"complete": 1},
    {"source_sha": "invalid"}, {"overall": None}, {"overall": True},
    {"overall": -0.1}, {"overall": 1.1}, {"overall": float("nan")},
    {"overall": float("inf")}, {"trades": {}}, {"trades": []},
    {"trades": {"solar": 1, "structural": 1}},
    {"trades": {"solar": 1, "structural": 1, "mechanical": -0.01}},
    {"trades": {"solar": 1, "structural": 1, "mechanical": float("inf")}},
])
def test_malformed_baseline_is_rejected_before_comparison_or_receipt(tmp_path, monkeypatch, change):
    baseline = {"schema": "leaf.xbench.x03-scores.v1", "complete": True,
                "source_sha": SHA, "prompt_sha256": "d" * 64, "overall": 1,
                "trades": {trade: 1 for trade in bench.TRADES}}
    baseline.update(change)
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid baseline reached comparison or receipt")
    monkeypatch.setattr(bench, "rates", forbidden)
    monkeypatch.setattr(bench, "write_json_atomic", forbidden)
    with pytest.raises(ValueError, match="baseline"):
        bench.grade_records(records(), frozen_rows=records(), baseline=baseline, prompt_sha256="d" * 64,
                            source_sha=SHA, out_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def run_prompt(tmp_path, *, fallback=False, model=True, publication="published", value=3, drift=False,
               catalog_change=None, authority_headers=None, staged_envelope=False, evaluator=None, transport_wrapper=None):
    calls = []
    def transport(method, path, **kw):
        calls.append((method, path, kw))
        if path == "/api/health":
            return 200, {"source_sha": "e" * 40 if drift else SHA}
        if path == "/api/author":
            if staged_envelope:
                return 202, {"contract": "leaf.customization-stage-job.v1",
                    "status": "queued", "change_set_id": "change", "poll_url": "/api/author/stages/change"}
            body = {"tool": {"name": "generated", "version": "1", "source_sha256": "c" * 64}, "receipt": {"state": "staged",
                    "staged_commit": "b" * 40, "change_set_id": "synthetic-change"}}
            if fallback:
                body["preview"] = "templated fallback"
            if model:
                body["model_id"] = "synthetic-model"
            return 200, body
        if path == "/api/author/stages/change":
            body = staged_tool()
            body["receipt"]["change_set_id"] = "change"
            return 200, {"contract": "leaf.customization-stage-job.v1", "status": "staged",
                "change_set_id": "change", "receipt": body["receipt"],
                "result": {"tool": body["tool"]}}
        if path == "/api/author/publication-requests":
            return 200, {"status": publication}
        if path == "/api/tools":
            return 200, {"tools": [{"name": "generated", "catalog_digest": "f" * 64,
                                    "version": "1", "source_sha256": "c" * 64, **(catalog_change or {})}]}
        if path == "/api/run":
            return 200, {"ok": True, "result": {"value": value}}
        raise AssertionError(path)
    class Client:
        def run(self, *args, **kwargs):
            calls.append(("POST", "/api/run", kwargs))
            return 200, {"ok": True, "result": {"value": value}}
    row = {"id": "X03-01", "trade": "solar", "drawing": "synthetic",
           "prompt": "Count synthetic entities", "oracle": {"value": 3}, "compare": {"mode": "exact"}}
    if transport_wrapper is not None:
        transport = transport_wrapper(transport)
    result = (evaluator or bench.evaluate_prompt)(row, transport=transport, client=Client(),
        project={"project_id": "p", "org_id": "o"}, drawing={"version_id": "v"},
        source_sha=SHA, evidence_dir=tmp_path, run_id="test", authority_headers=authority_headers)
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


def test_new_tool_authoring_never_requests_revision_of_a_nonexistent_target(tmp_path):
    result, calls = run_prompt(tmp_path)
    author = next(kw for _, path, kw in calls if path == "/api/author")
    # AuthorLoop.stage treats target_tool_name as an existing-tool revision.
    assert author["json_body"] == {"description": synthetic_prompt()["prompt"], "mode": "build"}
    assert "target_tool_name" not in author["json_body"]
    assert author["headers"]["Idempotency-Key"].startswith("x03-")
    assert result["tool_name"] == "generated"
    assert result["score"] == 1


def test_real_staged_envelope_finishes_polling_and_reaches_bound_execution(tmp_path):
    result, calls = run_prompt(tmp_path, staged_envelope=True)
    assert sum(path == "/api/author/stages/change" for _, path, _ in calls) == 1
    assert sum(path == "/api/run" for _, path, _ in calls) == 1
    assert result["output_hash"] == bench.content_hash({"value": 3})
    # The current producer envelope does not report a model. Do not invent one.
    assert result["score"] == 0
    assert result["failing_stage"] == "model_provenance"


@pytest.mark.parametrize("conflict", [{"version": "2"}, {"source_sha256": "e" * 64},
    {"catalog_commit": "e" * 40}, {"effective_catalog_digest": "e" * 64}])
def test_catalog_conflict_never_executes_or_scores(tmp_path, conflict):
    result, calls = run_prompt(tmp_path, catalog_change=conflict)
    assert result["score"] == 0
    assert result["error_code"] == "CATALOG_PROVENANCE_MISMATCH"
    assert not any(path == "/api/run" for _, path, _ in calls)


@pytest.mark.parametrize("poll", ["/api/author/stages/../other", r"/api/author/stages/\change",
    "/api/author/stages/change?x=1", "/api/author/stages/change#fragment",
    "/api/author/stages/foreign", "/api/author/stages/%63hange", "https://foreign.invalid/change"])
def test_poll_address_is_bound_to_accepted_change_set(tmp_path, poll):
    calls = []
    def transport(method, path, **kwargs):
        calls.append(path)
        if path == "/api/health":
            return 200, {"source_sha": SHA}
        if path == "/api/author":
            return 202, {"contract": "leaf.customization-stage-job.v1",
                         "change_set_id": "change", "poll_url": poll}
        raise AssertionError("invalid poll request escaped validation")
    result = bench.evaluate_prompt(synthetic_prompt(), transport=transport, client=None,
        project={}, drawing={}, source_sha=SHA, evidence_dir=tmp_path, run_id="poll")
    assert calls == ["/api/health", "/api/author"]
    assert result["score"] == 0 and result["failing_stage"] == "author"
    assert result["failure"] == "invalid stage polling address"


@pytest.mark.parametrize("refuse", [False, True])
def test_normal_author_uses_server_issued_turn_and_settles_only_owned_authority(tmp_path, refuse):
    calls = []
    def transport(method, path, **kw):
        calls.append((path, kw))
        if path == "/api/sessions":
            assert kw["json_body"] == {"drawing_id": "d", "project_id": "p"}
            return 200, {"session_id": "owned-session"}
        if path == "/api/sessions/owned-session/messages":
            assert kw["json_body"]["classifier_hint"] == {"source": "author_panel", "purpose": "stage_authority"}
            assert kw["json_body"]["project_context"] == {"project_id": "p", "input_version_id": "v"}
            assert kw["json_body"]["text"] == synthetic_prompt()["prompt"]
            return (403, {"reason_code": "tenant_role_denied"}) if refuse else (202, {"turn_id": "owned-turn", "status": "started"})
        assert path == "/api/sessions/owned-session/turns/owned-turn/cancel"
        return 200, {"cancelled": True}
    def invoke():
        with bench.author_authority(transport, synthetic_prompt(), {"project_id": "p"},
                {"drawing_id": "d", "version_id": "v"}, tmp_path) as authority:
            assert not refuse, "rejected mint must not reach author submission"
            result, authored = run_prompt(tmp_path, authority_headers=authority)
            assert result["score"] == 1
            headers = next(kw["headers"] for _, path, kw in authored if path == "/api/author")
            assert headers["X-Authority-Session-Id"] == "owned-session"
            assert headers["X-Authority-Turn-Id"] == "owned-turn"
    if refuse:
        with pytest.raises(bench.PromptFailure, match="HTTP 403"):
            invoke()
        assert len(calls) == 2
    else:
        invoke()
        assert len(calls) == 3
    assert (tmp_path / "X03-01-authority.json").is_file()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_initial_terminal_author_envelope_never_polls_or_publishes(tmp_path, status):
    calls = []
    def transport(method, path, **kwargs):
        calls.append(path)
        if path == "/api/health":
            return 200, {"source_sha": SHA}
        assert path == "/api/author"
        return 202, {"contract": "leaf.customization-stage-job.v1", "change_set_id": "change",
                     "status": status, "error": {"reason_code": "SYNTHETIC_AUTHOR_TERMINAL"}}
    result = bench.evaluate_prompt(synthetic_prompt(), transport=transport, client=None,
        project={}, drawing={}, source_sha=SHA, evidence_dir=tmp_path, run_id="terminal")
    assert calls == ["/api/health", "/api/author"]
    assert result["score"] == 0 and result["failing_stage"] == "author"
    assert result["error_code"] == "SYNTHETIC_AUTHOR_TERMINAL"
    assert json.loads((tmp_path / "X03-01.json").read_text())["record"] == result


@pytest.mark.parametrize("job_id", ["../other", "part/name", r"part\name", "job?x=1",
    "job#fragment", "/absolute", "", None, 123, ["job"], {"id": "job"}])
def test_execution_identity_rejected_before_job_poll(tmp_path, job_id):
    calls = []
    def transport(method, path, **kwargs):
        calls.append(path)
        if path == "/api/health":
            return 200, {"source_sha": SHA}
        if path == "/api/author":
            return 200, staged_tool()
        if path == "/api/author/publication-requests":
            return 200, {"status": "published"}
        if path == "/api/tools":
            return 200, {"tools": [{"name": "generated", "catalog_digest": "f" * 64}]}
        assert path == "/api/run", "invalid job identity reached a poll"
        return 202, {"job_id": job_id, "status": "pending"}
    result = bench.evaluate_prompt(synthetic_prompt(), transport=transport, client=None,
        project={"org_id": "o", "project_id": "p"}, drawing={"version_id": "v"},
        source_sha=SHA, evidence_dir=tmp_path, run_id="job-id")
    assert not any(path.startswith("/api/jobs/") for path in calls)
    assert result["score"] == 0 and result["failing_stage"] == "execution"
    assert result["error_code"] == "INVALID_JOB_ID"
    assert result["output_hash"] is None


@pytest.mark.parametrize("field", ["trade", "drawing"])
def test_identity_swaps_are_rejected_before_reports(tmp_path, field):
    original = records()
    original[30]["drawing"] = "second-synthetic"
    swapped = copy.deepcopy(original)
    swapped[0][field], swapped[30][field] = swapped[30][field], swapped[0][field]
    with pytest.raises(ValueError, match="frozen prompt identity"):
        bench.grade_records(swapped, baseline=None, prompt_sha256="d" * 64,
                            source_sha=SHA, out_dir=tmp_path, frozen_rows=original)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status,content_type,body,code", [
    (200, "application/json", "{broken SECRET SENTINEL", "INVALID_JSON_RESPONSE"),
    (429, "application/json", "{broken SECRET SENTINEL", "INVALID_JSON_RESPONSE"),
    (503, "application/json", "[1,2]", "INVALID_JSON_RESPONSE"),
    (429, "text/html", "<html>SECRET SENTINEL</html>", "NON_JSON_RESPONSE"),
    (500, "text/html", "<html>SECRET SENTINEL</html>", "NON_JSON_RESPONSE"),
    (503, "text/html", "<html>SECRET SENTINEL</html>", "NON_JSON_RESPONSE"),
])
def test_transport_invalid_bodies_keep_status_without_raw_bytes(tmp_path, monkeypatch, status, content_type, body, code):
    def api(page, method, path, **kwargs):
        if path == "/api/health":
            return 200, {"content-type": "application/json"}, json.dumps({"source_sha": SHA})
        assert path == "/api/author"
        return status, {"content-type": content_type, "Authorization": "SECRET SENTINEL"}, body
    monkeypatch.setattr(bench.xbench_session, "api", api)
    result = bench.evaluate_prompt(synthetic_prompt(), transport=bench.transport_for(object(), "https://synthetic.invalid"),
        client=None, project={}, drawing={}, source_sha=SHA, evidence_dir=tmp_path, run_id="bad-response")
    assert result["score"] == 0 and result["unreadable"] is True
    assert result["error_code"] == code
    receipt = json.loads((tmp_path / "X03-01.json").read_text())
    assert receipt["routes"][-1]["status"] == status
    assert "SECRET SENTINEL" not in (tmp_path / "X03-01.json").read_text()


@pytest.mark.parametrize("foreign_id", ["foreign", None, 42])
def test_async_poll_cannot_complete_another_job(tmp_path, foreign_id):
    polls = []
    def wrapper(original):
        def transport(method, path, **kwargs):
            if path == "/api/run":
                return 202, {"job_id": "owned", "status": "pending"}
            if path == "/api/jobs/owned":
                polls.append(path)
                return 200, {"job_id": foreign_id, "status": "complete", "result": {"value": 3}}
            return original(method, path, **kwargs)
        return transport
    monkey_sleep = bench.time.sleep
    try:
        bench.time.sleep = lambda seconds: None
        result, _ = run_prompt(tmp_path, transport_wrapper=wrapper)
    finally:
        bench.time.sleep = monkey_sleep
    assert polls == ["/api/jobs/owned"]
    assert result["score"] == 0 and result["failing_stage"] == "execution"
    assert result["error_code"] == "JOB_IDENTITY_MISMATCH"
    assert result["output_hash"] is None
    assert (tmp_path / "X03-01.json").exists()


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_catalog_names_never_execute(tmp_path, reverse):
    def wrapper(original):
        def transport(method, path, **kwargs):
            if path == "/api/tools":
                rows = [{"name": "generated", "catalog_digest": "f" * 64, "version": version} for version in ("1", "2")]
                return 200, {"tools": rows[::-1] if reverse else rows}
            assert path != "/api/run", "duplicate catalog reached execution"
            return original(method, path, **kwargs)
        return transport
    result, _ = run_prompt(tmp_path, transport_wrapper=wrapper)
    assert result["score"] == 0 and result["failing_stage"] == "publication"
    assert result["error_code"] == "DUPLICATE_CATALOG_TOOL"


@pytest.mark.parametrize("field", ["tool_name", "version", "tool_source_hash", "output_hash", "model_id"])
@pytest.mark.parametrize("missing", [None, ""])
def test_credited_record_missing_provenance_cannot_write_reports(tmp_path, field, missing):
    rows = records()
    rows[0][field] = missing
    with pytest.raises(ValueError, match="complete tool and model provenance"):
        bench.grade_records(rows, frozen_rows=records(), baseline=None, prompt_sha256="d" * 64,
                            source_sha=SHA, out_dir=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cleanup", ["http-failure", "exception"])
def test_owned_turn_cleanup_failure_keeps_result_and_bounded_diagnostics(tmp_path, cleanup):
    calls = []
    def transport(method, path, **kwargs):
        calls.append(path)
        if path == "/api/sessions":
            return 200, {"session_id": "owned-session"}
        if path.endswith("/messages"):
            return 202, {"turn_id": "owned-turn", "status": "started"}
        assert path == "/api/sessions/owned-session/turns/owned-turn/cancel"
        if cleanup == "exception":
            raise RuntimeError("SECRET SENTINEL provider message")
        return 503, {"message": "SECRET SENTINEL provider message"}
    with bench.author_authority(transport, synthetic_prompt(), {"project_id": "p"},
                               {"drawing_id": "d", "version_id": "v"}, tmp_path):
        result, _ = run_prompt(tmp_path)
    assert result["score"] == 1
    assert calls.count("/api/sessions/owned-session/turns/owned-turn/cancel") == 1
    text = (tmp_path / "X03-01-authority.json").read_text()
    assert "SECRET SENTINEL" not in text
    last = json.loads(text)[-1]
    if cleanup == "exception":
        assert last["cleanup_error"] == "RuntimeError"
    else:
        assert last["status"] == 503 and last["owned_turn_cleanup"] is True


@pytest.mark.parametrize("case", ["provider-exception", "nested-error", "invalid-code"])
def test_secret_sentinel_never_enters_prompt_receipt(tmp_path, case):
    secret = "SECRET SENTINEL bearer sk-test-token"
    def wrapper(original):
        def transport(method, path, **kwargs):
            if path == "/api/author":
                if case == "provider-exception":
                    raise RuntimeError(secret)
                code = {"token": secret} if case == "invalid-code" else "PUBLIC_AUTHOR_FAILURE"
                return 503, {"error": {"reason_code": code, "message": secret,
                    "headers": {"Authorization": secret}, "provider": {"token": secret}}, "raw": secret}
            return original(method, path, **kwargs)
        return transport
    result, _ = run_prompt(tmp_path, transport_wrapper=wrapper)
    assert result["score"] == 0
    if case == "nested-error":
        assert result["error_code"] == "PUBLIC_AUTHOR_FAILURE"
    if case == "invalid-code":
        assert result["error_code"] is None
    if case == "provider-exception":
        assert result["failure"] == "RuntimeError"
    receipt = (tmp_path / "X03-01.json").read_text()
    assert "SECRET SENTINEL" not in receipt and "Authorization" not in receipt


def persist_outputs(out):
    for row in records():
        bench.write_json_atomic(out / (row["id"] + "-output.json"), {"value": 3})


@pytest.mark.parametrize("publication", [
    {"change_set_id": "foreign"}, {"tool_name": "foreign"}, {"tool": "foreign"},
    {"tool": {"name": "foreign"}}, {"tool": {"name": "generated", "version": "foreign"}},
])
def test_publication_identity_conflicts_stop_before_catalog(tmp_path, publication):
    def wrapper(original):
        def transport(method, path, **kwargs):
            if path == "/api/author/publication-requests":
                return 200, {"status": "published", **publication}
            assert path not in ("/api/tools", "/api/run")
            return original(method, path, **kwargs)
        return transport
    result, _ = run_prompt(tmp_path, transport_wrapper=wrapper)
    assert result["score"] == 0 and result["failing_stage"] == "publication"
    assert result["error_code"] == "PUBLICATION_IDENTITY_MISMATCH"


@pytest.mark.parametrize("status", ["queued", "running", "unknown", None])
def test_nonterminal_http200_output_never_scores(tmp_path, status):
    def wrapper(original):
        def transport(method, path, **kwargs):
            if path == "/api/run":
                return 200, {"status": status, "result": {"value": 3}}
            return original(method, path, **kwargs)
        return transport
    result, _ = run_prompt(tmp_path, transport_wrapper=wrapper)
    assert result["score"] == 0 and result["failing_stage"] == "execution"
    assert result["output_hash"] is None
    assert result["error_code"] == "EXECUTION_NOT_TERMINAL_SUCCESS"


@pytest.mark.parametrize("status", ["failed", "cancelled", None, "unknown"])
def test_authority_requires_producer_started_status_and_never_cancels_other_turns(tmp_path, status):
    paths = []
    def transport(method, path, **kwargs):
        paths.append(path)
        if path == "/api/sessions":
            return 200, {"session_id": "owned-session"}
        assert path == "/api/sessions/owned-session/messages"
        return 202, {"turn_id": "returned-turn", "status": status}
    with pytest.raises(bench.PromptFailure, match="active turn"):
        with bench.author_authority(transport, synthetic_prompt(), {"project_id": "p"},
                                   {"drawing_id": "d", "version_id": "v"}, tmp_path):
            pytest.fail("invalid status reached author submission")
    assert len(paths) == 2
    assert (tmp_path / "X03-01-authority.json").exists()


@pytest.mark.parametrize("delta,valid", [(0, True), (1e-13, True), (1e-5, False), (0.1, False)])
def test_baseline_overall_matches_frozen_weighted_trades(tmp_path, delta, valid):
    baseline = {"schema": "leaf.xbench.x03-scores.v1", "complete": True,
        "source_sha": SHA, "prompt_sha256": "d" * 64,
        "overall": 0.7 + delta, "trades": {"solar": 0.8, "structural": 0.6, "mechanical": 0.5}}
    if valid:
        bench.validate_baseline(baseline, "d" * 64)
    else:
        with pytest.raises(ValueError, match="weighted trade rates"):
            bench.grade_records([], frozen_rows=records(), baseline=baseline,
                prompt_sha256="d" * 64, source_sha=SHA, out_dir=tmp_path)
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("artifact", [None, '{"value":4}', '{"value":NaN}', '{"value":Infinity}', '{broken'])
def test_credited_output_artifact_must_exist_and_match_finite_digest(tmp_path, artifact):
    if artifact is not None:
        (tmp_path / "X03-01-output.json").write_text(artifact)
    with pytest.raises(ValueError, match="credited output artifact"):
        bench.grade_records(records(), frozen_rows=records(), baseline=None,
            prompt_sha256="d" * 64, source_sha=SHA, out_dir=tmp_path)
    assert not (tmp_path / "summary.json").exists()
    assert not (tmp_path / "scores.csv").exists()
    assert not (tmp_path / "baseline.json").exists()


@pytest.mark.parametrize("field,value", [("source_sha", "bad"), ("tool_source_hash", "b"*39),
    ("tool_source_hash", "g"*40), ("output_hash", "c"*63), ("output_hash", "z"*64)])
def test_malformed_provenance_digest_cannot_emit_acceptance(tmp_path, field, value):
    rows = records()
    rows[0][field] = value
    with pytest.raises(ValueError, match="provenance digest"):
        bench.grade_records(rows, frozen_rows=records(), baseline=None,
            prompt_sha256="d" * 64, source_sha=SHA, out_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_hash_matched_wrong_output_is_rejected_against_frozen_oracle(tmp_path):
    rows = records()
    wrong = {"value": 4}
    rows[0]["output_hash"] = bench.content_hash(wrong)
    bench.write_json_atomic(tmp_path / "X03-01-output.json", wrong)
    with pytest.raises(ValueError, match="frozen oracle"):
        bench.grade_records(rows, frozen_rows=records(), baseline=None,
            prompt_sha256="d" * 64, source_sha=SHA, out_dir=tmp_path)
    assert not (tmp_path / "summary.json").exists()
    assert not (tmp_path / "scores.csv").exists()


@pytest.mark.parametrize("submitted", ["true", "false", 1, 0, [], {}, [True]])
def test_submission_evidence_must_be_literal_boolean(tmp_path, submitted):
    rows = records()
    rows[0]["author_submitted"] = submitted
    with pytest.raises(ValueError, match="literal boolean"):
        bench.grade_records(rows, frozen_rows=records(), baseline=None,
            prompt_sha256="d" * 64, source_sha=SHA, out_dir=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("rates,valid", [
    ((0.61, 0.61, 0.61), False), ((0.5, 0.55, 0.45), False),
    ((17/30, 0.6, 0.7), True), ((17/30+1e-13, 0.6, 0.7), True),
])
def test_baseline_rates_must_represent_realizable_counts(tmp_path, rates, valid):
    trades = dict(zip(bench.TRADES, rates))
    baseline = {"schema": "leaf.xbench.x03-scores.v1", "complete": True,
        "source_sha": SHA, "prompt_sha256": "d" * 64,
        "overall": sum(trades[t]*n for t,n in bench.TRADES.items())/50, "trades": trades}
    if valid:
        bench.validate_baseline(baseline, "d" * 64)
    else:
        with pytest.raises(ValueError, match="integer success count"):
            bench.grade_records([], frozen_rows=records(), baseline=baseline,
                prompt_sha256="d" * 64, source_sha=SHA, out_dir=tmp_path)
        assert not list(tmp_path.iterdir())


def test_authority_and_execution_share_one_600_second_budget(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(bench.time, "monotonic", clock)
    calls = []
    def transport(method, path, **kwargs):
        calls.append((path, clock.now, kwargs["timeout_s"]))
        if path.endswith("/cancel"):
            return 200, {}
        assert clock.now < 600
        assert 0 < kwargs["timeout_s"] <= 600-clock.now
        if path == "/api/sessions":
            clock.now = 540
            return 200, {"session_id": "owned-session"}
        if path.endswith("/messages"):
            clock.now = 580
            return 202, {"turn_id": "owned-turn", "status": "started"}
        if path == "/api/health":
            return 200, {"source_sha": SHA}
        if path == "/api/author":
            clock.now = 585
            return 200, staged_tool()
        if path == "/api/author/publication-requests":
            return 200, {"status": "published"}
        if path == "/api/tools":
            return 200, {"tools": [{"name": "generated", "catalog_digest": "f"*64}]}
        assert path == "/api/run"
        clock.now = 600
        return 200, {"ok": True, "result": {"value": 3}}
    with bench.author_authority(transport, synthetic_prompt(), {"project_id": "p"},
            {"drawing_id": "d", "version_id": "v"}, tmp_path, deadline=600) as headers:
        result = bench.evaluate_prompt(synthetic_prompt(), transport=transport, client=None,
            project={"project_id": "p", "org_id": "o"}, drawing={"version_id": "v"},
            source_sha=SHA, evidence_dir=tmp_path, run_id="shared-budget",
            authority_headers=headers, deadline_at=600, clock=clock, sleep=clock.sleep)
    assert clock.now == 600 and result["score"] == 0
    assert "600 second ceiling" in result["failure"]
    assert sum(path == "/api/health" for path, _, _ in calls) == 1
    assert next(timeout for path, _, timeout in calls if path == "/api/author") == 20
    assert next(timeout for path, _, timeout in calls if path == "/api/run") == 15


@pytest.mark.parametrize("route,stage", [("/api/author", "author"),
    ("/api/author/publication-requests", "publication"), ("/api/tools", "publication"),
    ("/api/run", "execution")])
@pytest.mark.parametrize("payload", [None, [], "scalar", 42])
def test_success_payload_must_be_object_before_later_requests(tmp_path, route, stage, payload):
    after = []
    def wrapper(original):
        def transport(method, path, **kwargs):
            assert not after, "request dispatched after malformed successful payload"
            if path == route:
                after.append(path)
                return 200, payload
            return original(method, path, **kwargs)
        return transport
    result, _ = run_prompt(tmp_path, transport_wrapper=wrapper)
    assert result["score"] == 0 and result["failing_stage"] == stage
    assert result["unreadable"] is True and result["error_code"] == "INVALID_RESPONSE_SHAPE"


@pytest.mark.parametrize("route,payload,stage", [
    ("/api/author", {"receipt": []}, "author"),
    ("/api/author", {"contract": "leaf.customization-stage-job.v1", "change_set_id": "change", "status": "staged", "result": [], "receipt": {}}, "author"),
    ("/api/author/publication-requests", {"status": "published", "tool": []}, "publication"),
    ("/api/tools", {"tools": None}, "publication"),
    ("/api/tools", {"tools": [None]}, "publication"),
])
def test_malformed_nested_success_envelope_has_bounded_receipt(tmp_path, route, payload, stage):
    def wrapper(original):
        def transport(method, path, **kwargs):
            if path == route:
                return 200, payload
            return original(method, path, **kwargs)
        return transport
    result, _ = run_prompt(tmp_path, transport_wrapper=wrapper)
    assert result["score"] == 0 and result["failing_stage"] == stage
    assert result["unreadable"] is True and result["error_code"] == "INVALID_RESPONSE_SHAPE"
    assert (tmp_path / "X03-01.json").exists()


@pytest.mark.parametrize("case", ["complete", "output-write", "receipt-write"])
def test_fifty_prompt_real_functions_lifecycle_without_network(tmp_path, monkeypatch, case):
    manifest = prompt_manifest(tmp_path)
    rows, _ = bench.load_prompts(manifest)
    out = tmp_path / "out"
    events, authored = [], []
    active = None
    phase = None
    @contextmanager
    def signed_in(*args, **kwargs):
        events.append("sign-in")
        try:
            yield object(), {"synthetic": True}
        finally:
            events.append("sign-out")
    class Client:
        def __init__(self, transport):
            pass
        def upload_drawing(self, name, content):
            assert content == b"synthetic drawing"
            return name
        def wait_upload(self, *args, **kwargs):
            pass
        def adopt_drawing(self, project_id, uploaded, name):
            return {"drawing_id": project_id+"-drawing", "version_id": project_id+"-version"}
    @contextmanager
    def project(client, name):
        trade = name.split()[1]
        events.append("create-"+trade)
        try:
            yield {"org_id": "o", "project_id": trade}
        finally:
            events.append("delete-"+trade)
    def transport(method, path, **kw):
        nonlocal active, phase
        if path == "/api/sessions":
            assert active is None
            prior = json.loads((out/"records.json").read_text()) if authored else []
            assert len(prior) == len(authored)
            active = rows[len(authored)]
            phase = "session"
            return 200, {"session_id": "session-"+active["id"]}
        assert active is not None
        row = active
        if path.endswith("/messages"):
            assert kw["json_body"]["text"] == row["prompt"]
            phase = "authority"
            return 202, {"turn_id": "turn-"+row["id"], "status": "started"}
        if path.endswith("/cancel"):
            assert path == "/api/sessions/session-"+row["id"]+"/turns/turn-"+row["id"]+"/cancel"
            events.append("cancel-"+row["id"])
            active = None
            return 200, {}
        if path == "/api/health":
            return 200, {"source_sha": SHA}
        if path == "/api/author":
            assert phase == "authority"
            assert kw["json_body"] == {"description": row["prompt"], "mode": "build"}
            assert kw["headers"]["X-Authority-Turn-Id"] == "turn-"+row["id"]
            authored.append(row["id"])
            events.append("author-"+row["id"])
            phase = "author"
            return 200, {"tool": {"name": "generated-"+row["id"]}, "model_id": "synthetic-model",
                "receipt": {"state": "staged", "staged_commit": "b"*40, "change_set_id": "change-"+row["id"]}}
        if path == "/api/author/publication-requests":
            assert phase == "author"
            phase = "published"
            assert kw["json_body"]["change_set_id"] == "change-"+row["id"]
            events.append("publish-"+row["id"])
            return 200, {"status": "published", "change_set_id": "change-"+row["id"], "tool_name": "generated-"+row["id"]}
        if path == "/api/tools":
            assert phase == "published"
            return 200, {"tools": [{"name": "generated-"+row["id"], "catalog_digest": "f"*64}]}
        assert path == "/api/run" and phase == "published"
        assert kw["json_body"]["dwg"] == row["trade"]+"-version"
        events.append("execute-"+row["id"])
        phase = "executed"
        return 200, {"ok": True, "result": row["oracle"]}
    original_write = bench.write_json_atomic
    injected = False
    def write(path, value):
        nonlocal injected
        target = "X03-02-output.json" if case == "output-write" else "X03-02.json"
        if case != "complete" and Path(path).name == target and not injected:
            injected = True
            raise OSError("SECRET SENTINEL injected write failure")
        return original_write(path, value)
    monkeypatch.setattr(bench, "write_json_atomic", write)
    monkeypatch.setattr(bench.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(bench, "transport_for", lambda *args: transport)
    monkeypatch.setitem(sys.modules, "xbench_studio", SimpleNamespace(StudioClient=Client, disposable_project=project))
    steps = bench.run(SimpleNamespace(manifest=manifest, out_dir=out, source_sha=SHA, base="https://synthetic.invalid"))
    collected = json.loads((out/"records.json").read_text())
    assert active is None
    assert events[-4:] == ["delete-mechanical", "delete-structural", "delete-solar", "sign-out"]
    if case == "complete":
        assert authored == [r["id"] for r in rows]
        assert all(step.verdict == "PASS" for step in steps)
        assert len(collected) == 50 and all(r["score"] == 1 for r in collected)
        assert json.loads(Path(manifest["baseline_path"]).read_text())["overall"] == 1
        assert len(list(out.glob("*-authority.json"))) == 50
        assert len(list(out.glob("*-output.json"))) == 50
        for row in rows:
            sequence = ["author-", "publish-", "execute-", "cancel-"]
            indices = [events.index(prefix+row["id"]) for prefix in sequence]
            assert indices == sorted(indices)
    else:
        assert injected and authored == ["X03-01", "X03-02"]
        assert len(collected) == 2 and collected[0]["score"] == 1
        assert collected[1]["score"] == 0 and collected[1]["unreadable"] is True
        assert all(step.verdict == "UNREADABLE" for step in steps)
        assert json.loads((out/"summary.json").read_text())["complete"] is False
        assert not Path(manifest["baseline_path"]).exists()
        assert "SECRET SENTINEL" not in (out/"records.json").read_text()


@pytest.mark.parametrize("text", ["café façade", "太阳 光伏", "Δοκιμή مرحبا"])
def test_unicode_output_reload_is_explicit_utf8_and_hash_stable(tmp_path, monkeypatch, text):
    rows = records()
    frozen = records()
    value = {"text": text}
    rows[0]["output_hash"] = bench.content_hash(value)
    frozen[0]["oracle"] = value
    persist_outputs(tmp_path)
    (tmp_path/"X03-01-output.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if path.name.endswith("-output.json"):
            assert kwargs.get("encoding") == "utf-8"
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    steps, report = bench.grade_records(rows, frozen_rows=frozen, baseline=None,
        prompt_sha256="d"*64, source_sha=SHA, out_dir=tmp_path)
    assert report["complete"] and all(step.verdict == "PASS" for step in steps)


@pytest.mark.parametrize("invalid", ["multiple-trades", "nan", "inf", "negative-inf"])
def test_invalid_frozen_oracle_or_adoption_fails_before_session(tmp_path, monkeypatch, invalid):
    manifest = prompt_manifest(tmp_path)
    fixture = manifest["fixtures"][0]
    path = Path(fixture["path"])
    payload = json.loads(path.read_text())
    if invalid == "multiple-trades":
        payload["rows"][30]["drawing"] = "solar"
    else:
        value = {"nan": float("nan"), "inf": float("inf"), "negative-inf": -float("inf")}[invalid]
        payload["rows"][0]["oracle"] = {"nested": [value]}
    path.write_text(json.dumps(payload))
    fixture["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    def forbidden(*args, **kwargs):
        pytest.fail("invalid manifest reached sign-in or transport")
    monkeypatch.setattr(bench.xbench_session, "signed_in", forbidden)
    monkeypatch.setattr(bench, "transport_for", forbidden)
    with pytest.raises(ValueError, match="multiple trades|finite JSON"):
        bench.run(SimpleNamespace(manifest=manifest, out_dir=tmp_path/"out", source_sha=SHA, base="https://synthetic.invalid"))
    assert not (tmp_path/"out").exists()


@pytest.mark.parametrize("counts,expected", [((25,5,5), "PASS"), ((24,5,5), "FAIL"), ((26,4,5), "FAIL"), ((26,5,4), "FAIL")])
def test_realizable_acceptance_boundary_matrix(tmp_path, counts, expected):
    rows = records()
    persist_outputs(tmp_path)
    for trade, count in zip(bench.TRADES, counts):
        group = [r for r in rows if r["trade"] == trade]
        for row in group[count:]:
            row.update(score=0, failing_stage="comparison")
    steps, _ = bench.grade_records(rows, frozen_rows=records(), baseline=None,
        prompt_sha256="d"*64, source_sha=SHA, out_dir=tmp_path)
    assert steps[-1].verdict == expected


@pytest.mark.parametrize("mechanical,regression", [(7,False), (6,True)])
def test_isolated_trade_regression_ten_vs_twenty_points(tmp_path, mechanical, regression):
    rows = records()
    persist_outputs(tmp_path)
    for row in rows[40+mechanical:]:
        row.update(score=0, failing_stage="comparison")
    baseline = {"schema": "leaf.xbench.x03-scores.v1", "complete": True,
        "source_sha": SHA, "prompt_sha256": "d"*64,
        "overall": 48/50, "trades": {"solar": 1, "structural": 1, "mechanical": .8}}
    _, report = bench.grade_records(rows, frozen_rows=records(), baseline=baseline,
        prompt_sha256="d"*64, source_sha=SHA, out_dir=tmp_path)
    assert report["REGRESSION"] is regression
