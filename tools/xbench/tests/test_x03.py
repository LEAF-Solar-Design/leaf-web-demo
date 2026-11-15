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


@pytest.mark.parametrize("case", ["complete", "upload-failure", "drift", "existing-baseline", "baseline-race"])
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
                    return 200, {"result": row["oracle"]}
                raise AssertionError(path)
            return real_evaluate(row, **{**kwargs, "transport": transport})
        return {**records()[0], "id": row["id"], "trade": row["trade"], "drawing": row["drawing"]}

    monkeypatch.setitem(sys.modules, "xbench_studio", SimpleNamespace(
        StudioClient=Client, disposable_project=disposable_project))
    monkeypatch.setattr(bench.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(bench.xbench_session, "api", forbidden)
    monkeypatch.setattr(bench, "transport_for", lambda *args: forbidden)
    @contextmanager
    def fake_authority(*args, **kwargs):
        yield {"X-Authority-Session-Id": "owned-session", "X-Authority-Turn-Id": "owned-turn"}
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
    elif case == "baseline-race":
        with pytest.raises(FileExistsError):
            bench.run(ctx)
        assert len(dispatches) == 50
        assert baseline_path.read_bytes() == b"competing baseline must survive"
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
        _, report = bench.grade_records(rows, baseline=None, prompt_sha256="d" * 64,
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
             "author_submitted": True, "model_id": "synthetic-model", "score": 1,
             "failing_stage": None, "tool_name": "synthetic-tool", "version": "1",
             "tool_source_hash": "b" * 40, "output_hash": "c" * 64}
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
        bench.grade_records(rows, baseline=None, prompt_sha256="d" * 64,
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
        bench.grade_records(records(), baseline=baseline, prompt_sha256="d" * 64,
                            source_sha=SHA, out_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def run_prompt(tmp_path, *, fallback=False, model=True, publication="published", value=3, drift=False,
               catalog_change=None, authority_headers=None):
    calls = []
    def transport(method, path, **kw):
        calls.append((method, path, kw))
        if path == "/api/health":
            return 200, {"source_sha": "e" * 40 if drift else SHA}
        if path == "/api/author":
            body = {"tool": {"name": "generated", "version": "1", "source_sha256": "c" * 64}, "receipt": {"state": "staged",
                    "staged_commit": "b" * 40, "change_set_id": "synthetic-change"}}
            if fallback:
                body["preview"] = "templated fallback"
            if model:
                body["model_id"] = "synthetic-model"
            return 200, body
        if path == "/api/author/publication-requests":
            return 200, {"status": publication}
        if path == "/api/tools":
            return 200, {"tools": [{"name": "generated", "catalog_digest": "f" * 64,
                                    "version": "1", "source_sha256": "c" * 64, **(catalog_change or {})}]}
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
            return (403, {"reason_code": "tenant_role_denied"}) if refuse else (202, {"turn_id": "owned-turn", "status": "running"})
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
