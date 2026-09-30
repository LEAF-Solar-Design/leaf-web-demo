"""Fixture-backed decisions and the scheduled reaper's mutation boundary."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig

# Same stdlib preload the convergence suite uses, and for the same reason: the
# repo root carries a `platform/` package that shadows the stdlib module, and
# pytest loads plugins that import `platform` before this file runs. Pin the
# real module first, widen sys.path only afterwards.
_platform_spec = importlib.util.spec_from_file_location(
    "platform", Path(sysconfig.get_path("stdlib")) / "platform.py"
)
assert _platform_spec and _platform_spec.loader
_stdlib_platform = importlib.util.module_from_spec(_platform_spec)
sys.modules["platform"] = _stdlib_platform
_platform_spec.loader.exec_module(_stdlib_platform)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from scripts import reap_stalled_staging_runs as reaper  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts" / "fixtures" / "reap_stalled"
NOW = datetime(2026, 9, 30, 13, 55, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def cancelled_run():
    return fixture("run_36711671971_after_cancel.json")


def stalled_variant():
    # Reconstruct the live 13:5xZ pre-cancel observation of 36711671971.
    # All job objects come from the real after-cancel fixture; only the
    # observed queued/failure fields specified by the incident are restored.
    run = cancelled_run()
    jobs = fixture("run_36711671971_jobs_after_cancel.json")["jobs"]
    run.update(status="queued", conclusion=None)
    for job in jobs:
        if job["name"] in {"build (canonical-worker)", "build (broker)"}:
            job.update(status="queued", conclusion=None, runner_name="",
                       created_at="2026-09-30T12:16:17Z")
        elif job["name"] in {"build (web)", "build (harness)"}:
            job.update(status="completed", conclusion="failure", runner_name="")
    return run, jobs


def plan(run, jobs, head=None, others=(), workflow=reaper.BUILD_WORKFLOW):
    return reaper.plan_actions({workflow: [run, *others]}, {run["id"]: jobs},
                               head or run["head_sha"], NOW)


def test_incident_stalls_and_has_exactly_one_cancel():
    run, jobs = stalled_variant()
    assert reaper.is_stalled(run, jobs, NOW)[0]
    actions = plan(run, jobs)
    assert len(actions) == 1
    assert actions[0]["action"] == "cancel"
    assert actions[0]["run_id"] == 36711671971


def test_real_after_cancel_is_not_stalled():
    jobs = fixture("run_36711671971_jobs_after_cancel.json")["jobs"]
    assert not reaper.is_stalled(cancelled_run(), jobs, NOW)[0]


def test_healthy_success_jobs_are_not_stalled_even_with_active_run():
    run, _ = stalled_variant()
    jobs = fixture("run_36708796106_jobs_success.json")["jobs"]
    assert not reaper.is_stalled(run, jobs, NOW)[0]


def test_nineteen_minutes_is_too_soon():
    run, jobs = stalled_variant()
    for job in jobs:
        if job["status"] == "queued":
            job["created_at"] = (NOW - timedelta(minutes=19)).isoformat()
    assert not reaper.is_stalled(run, jobs, NOW)[0]


def test_threshold_is_inclusive_and_null_runner_counts():
    run, jobs = stalled_variant()
    for job in jobs:
        if job["status"] == "queued":
            job.update(created_at=(NOW - timedelta(seconds=1200)).isoformat(), runner_name=None)
    assert reaper.is_stalled(run, jobs, NOW)[0]


def test_running_job_prevents_cancel():
    run, jobs = stalled_variant()
    jobs[0]["status"] = "in_progress"
    assert not reaper.is_stalled(run, jobs, NOW)[0]
    assert plan(run, jobs) == []


@pytest.mark.parametrize("status", ["waiting", "pending"])
def test_approval_or_pending_does_not_stall(status):
    run, jobs = stalled_variant()
    for job in jobs:
        if job["status"] == "queued":
            job["status"] = status
    assert not reaper.is_stalled(run, jobs, NOW)[0]


def test_assigned_runner_is_not_stalled():
    run, jobs = stalled_variant()
    for job in jobs:
        if job["status"] == "queued":
            job["runner_name"] = "assigned-runner"
    assert not reaper.is_stalled(run, jobs, NOW)[0]


@pytest.mark.parametrize("bad", ["broken", None, "2026-09-30T12:16:17"])
def test_malformed_timestamp_fails_closed(bad):
    run, jobs = stalled_variant()
    next(j for j in jobs if j["status"] == "queued")["created_at"] = bad
    stalled, reason = reaper.is_stalled(run, jobs, NOW)
    assert not stalled
    assert reason.startswith("unreadable")
    assert plan(run, jobs) == []


@pytest.mark.parametrize("bad_jobs", [None, {}, [None], [{}]])
def test_malformed_shapes_fail_closed(bad_jobs):
    run, _ = stalled_variant()
    assert reaper.is_stalled(run, bad_jobs, NOW)[1].startswith("unreadable")


def test_rerun_cancelled_main_head():
    actions = plan(cancelled_run(), [])
    assert len(actions) == 1
    assert actions[0]["action"] == "rerun"
    assert actions[0]["run_id"] == 36711671971


@pytest.mark.parametrize("case", ["attempt-three", "other-head", "queued", "in_progress", "pending", "success"])
def test_rerun_guards(case):
    run = cancelled_run()
    others = []
    head = run["head_sha"]
    if case == "attempt-three":
        run["run_attempt"] = 3
    elif case == "other-head":
        head = "a" * 40
    elif case == "success":
        run["conclusion"] = "success"
    else:
        other = deepcopy(run)
        other.update(id=run["id"] + 1, status=case, conclusion=None)
        others.append(other)
    assert plan(run, [], head=head, others=others) == []


def test_only_newest_main_head_push_is_rerun():
    run = cancelled_run()
    newer = deepcopy(run)
    newer.update(id=run["id"] + 1, created_at="2026-09-30T13:00:00Z", conclusion="success")
    assert plan(run, [], others=[newer]) == []
    newer["conclusion"] = "cancelled"
    actions = plan(run, [], others=[newer])
    assert len(actions) == 1 and actions[0]["run_id"] == newer["id"]


@pytest.mark.parametrize("event,branch", [("workflow_dispatch", "main"), ("push", "feature")])
def test_build_cancel_scope(event, branch):
    run, jobs = stalled_variant()
    run.update(event=event, head_branch=branch)
    assert plan(run, jobs) == []


def test_relay_cancel_scope_and_no_relay_rerun():
    run, jobs = stalled_variant()
    run["event"] = "workflow_run"
    assert plan(run, jobs, workflow=reaper.RELAY_WORKFLOW)[0]["action"] == "cancel"
    run.update(status="completed", conclusion="cancelled")
    assert plan(run, jobs, workflow=reaper.RELAY_WORKFLOW) == []
    run.update(status="queued", event="workflow_dispatch")
    assert plan(run, jobs, workflow=reaper.RELAY_WORKFLOW) == []


def test_two_read_union_retains_stalled_run_missing_from_first():
    run, jobs = stalled_variant()
    merged = reaper.union_runs([], [run])
    actions = reaper.plan_actions({reaper.BUILD_WORKFLOW: merged}, {run["id"]: jobs}, run["head_sha"], NOW)
    assert len(actions) == 1 and actions[0]["action"] == "cancel"
    assert reaper.union_runs([run], [run]) == [run]


def fake_client(monkeypatch, runs=None, read_failure=False, write_failure=False):
    run, jobs = stalled_variant()
    writes = []
    reads = []

    class Client:
        def __init__(self, repo, token):
            pass

        def list_runs(self, workflow):
            reads.append(workflow)
            if read_failure and len(reads) == 4:
                raise OSError("listing unavailable")
            return (runs if runs is not None else [run]) if workflow == reaper.BUILD_WORKFLOW else []

        def list_jobs(self, run_id):
            return deepcopy(jobs)

        def main_head(self):
            return run["head_sha"]

        def write(self, action):
            if write_failure:
                raise OSError("403 refused")
            writes.append(action)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 30, 13, 55, tzinfo=timezone.utc)

    monkeypatch.setattr(reaper, "GitHubClient", Client)
    monkeypatch.setattr(reaper, "datetime", Clock)
    return writes, reads


def test_dry_run_has_zero_writes(monkeypatch, capsys):
    writes, reads = fake_client(monkeypatch)
    assert reaper.main(["--dry-run"]) == 0
    assert writes == []
    assert reads.count(reaper.BUILD_WORKFLOW) == reads.count(reaper.RELAY_WORKFLOW) == 2
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["actions"][0]["action"] == "cancel"
    assert summary["writes"] == 0


def test_failed_read_makes_whole_tick_noop(monkeypatch, capsys):
    writes, _ = fake_client(monkeypatch, read_failure=True)
    assert reaper.main([]) == 0
    assert writes == []
    assert "::warning::" in capsys.readouterr().out


def test_refused_write_exits_one(monkeypatch):
    fake_client(monkeypatch, write_failure=True)
    assert reaper.main([]) == 1


def test_at_most_four_writes(monkeypatch, capsys):
    run, _ = stalled_variant()
    runs = [dict(run, id=run["id"] + i) for i in range(6)]
    writes, _ = fake_client(monkeypatch, runs=runs)
    assert reaper.main([]) == 0
    assert len(writes) == 4
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["deferred"] == 2


def test_client_rest_paths_and_bounded_jobs(monkeypatch):
    client = reaper.GitHubClient("owner/repo", "test-token")
    run, jobs = stalled_variant()
    calls = []

    def request(path, method="GET"):
        calls.append((path, method))
        if "/workflows/" in path:
            return {"workflow_runs": [run]}
        if "/jobs?" in path:
            return {"jobs": jobs, "total_count": len(jobs)}
        if path == "/branches/main":
            return {"commit": {"sha": run["head_sha"]}}
        return None

    monkeypatch.setattr(client, "request", request)
    assert client.list_runs(reaper.BUILD_WORKFLOW) == [run]
    assert client.list_runs(reaper.RELAY_WORKFLOW) == [run]
    assert "branch=main" in calls[0][0] and "per_page=20" in calls[0][0]
    assert "event=push" in calls[0][0] and "event=" not in calls[1][0]
    assert client.list_jobs(run["id"]) == jobs
    assert "per_page=100" in calls[-1][0]
    assert client.main_head() == run["head_sha"]
    for action in ("cancel", "rerun"):
        client.write({"run_id": run["id"], "action": action})
        assert calls[-1] == (f"/actions/runs/{run['id']}/{action}", "POST")
    monkeypatch.setattr(client, "request", lambda path: {"jobs": jobs, "total_count": 301})
    with pytest.raises(ValueError, match="unreadable"):
        client.list_jobs(run["id"])


def test_client_requests_have_explicit_timeout(monkeypatch):
    observed = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        observed.append((request, timeout))
        return Response()

    monkeypatch.setattr(reaper.urllib.request, "urlopen", urlopen)
    client = reaper.GitHubClient("owner/repo", "test-token")
    client.request("/branches/main")
    client.request("/actions/runs/1/cancel", "POST")
    assert [timeout for _, timeout in observed] == [15, 15]
    assert [request.get_method() for request, _ in observed] == ["GET", "POST"]


def test_workflow_shape():
    text = (ROOT / ".github" / "workflows" / "reap-stalled-staging-runs.yml").read_text(encoding="utf-8")
    # BaseLoader preserves `on`, following test_prewarm_staging_cutover_workflow.
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow["on"]["schedule"] == [{"cron": "*/10 * * * *"}]
    dry = workflow["on"]["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry["type"] == "boolean" and dry["default"] == "false"
    assert workflow["permissions"] == {"actions": "write", "contents": "read"}
    assert workflow["concurrency"] == {"group": "reap-stalled-staging-runs", "cancel-in-progress": "true"}
    assert len(workflow["jobs"]) == 1
    job = workflow["jobs"]["reap"]
    assert job["runs-on"] == "codebuild-leaf-gha-runner-web-demo-${{ github.run_id }}-${{ github.run_attempt }}-small"
    assert job["timeout-minutes"] == "5"
    assert job["if"] == "${{ vars.REAP_STALLED_STAGING_RUNS != 'off' }}"
    assert job["steps"][0]["uses"].startswith("actions/checkout@")
    step = job["steps"][1]
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert '"$DRY_RUN" = "true"' in step["run"] and "--dry-run" in step["run"]
    assert "python3 scripts/reap_stalled_staging_runs.py" in step["run"]
    assert "secrets." not in text
    assert "36711671971" in text and "#1598" in text
