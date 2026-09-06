"""Focused evidence parser checks. These do not rerun historical application tests."""
import copy
from pathlib import Path
from types import SimpleNamespace
import urllib.request

import pytest
from ci import recover_gate_log_evidence as recovery


def fixture():
    suite = SimpleNamespace(id="bounded-suite", db_gated=False, opt_in_env=None)
    job = {"id": 123, "name": "test / gate-shard-0", "run_id": recovery.RUN,
        "head_sha": recovery.TESTED, "status": "completed", "conclusion": "failure", "steps": [
        {"name": "Run gate shard 0", "conclusion": "success",
         "started_at": "2026-09-06T20:29:59Z", "completed_at": "2026-09-06T20:31:43Z"},
        {"name": "Upload shard result and logs", "conclusion": "failure"}]}
    raw = (f"2026-09-06T20:29:00.000Z {recovery.TESTED}\n"
        "2026-09-06T20:30:01.000Z ... bounded-suite PASS 12 1.0s\n"
        "2026-09-06T20:30:02.000Z suites: 1 PASS 0 FAIL 0 SKIP 0 UNAVAILABLE test cases passed: 12 skipped: 0\n"
        "2026-09-06T20:31:44.000Z Failed to CreateArtifact: Artifact storage quota has been hit\n").encode()
    return job, raw, {suite.id: suite}


def test_upload_only_fixture():
    row = recovery.inspect_shard(*fixture())
    assert row["test_step"] == "success"
    assert row["job_conclusion"] == "failure"
    assert row["reported_passed_cases"] == 12


@pytest.mark.parametrize("field,value", [("run_id", 1), ("head_sha", "f" * 40),
    ("status", "in_progress"), ("conclusion", "success")])
def test_foreign_or_unfinished_job(field, value):
    job, raw, expected = fixture()
    job[field] = value
    with pytest.raises(ValueError):
        recovery.inspect_shard(job, raw, expected)


@pytest.mark.parametrize("mutation", ["test-failed", "duplicate-step", "other-failure", "missing-suite",
    "duplicate-suite", "failed-suite", "foreign-suite", "ambiguous-summary", "no-quota", "wrong-checkout", "unexpected-skip"])
def test_evidenced_rejections(mutation):
    job, raw, expected = fixture()
    if mutation == "test-failed": job["steps"][0]["conclusion"] = "failure"
    if mutation == "duplicate-step": job["steps"].insert(0, copy.deepcopy(job["steps"][0]))
    if mutation == "other-failure": job["steps"].append({"name": "checkout", "conclusion": "failure"})
    if mutation == "missing-suite": raw = raw.replace(b"... bounded-suite PASS", b"not-a-row bounded-suite PASS")
    if mutation == "duplicate-suite": raw += b"2026-09-06T20:30:03.000Z ... bounded-suite PASS 12 1.0s\n"
    if mutation == "failed-suite": raw = raw.replace(b"bounded-suite PASS", b"bounded-suite FAIL")
    if mutation == "foreign-suite": raw = raw.replace(b"bounded-suite", b"foreign-suite")
    if mutation == "ambiguous-summary": raw += raw.splitlines()[2] + b"\n"
    if mutation == "no-quota": raw = raw.replace(b"storage quota", b"transport issue")
    if mutation == "wrong-checkout": raw = raw.replace(recovery.TESTED.encode(), b"0" * 40)
    if mutation == "unexpected-skip": raw = raw.replace(b"bounded-suite PASS", b"bounded-suite SKIP")
    with pytest.raises(ValueError):
        recovery.inspect_shard(job, raw, expected)


def test_redirect_never_forwards_authorization():
    req = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer test-fixture"})
    redirected = recovery.LogRedirect().redirect_request(req, None, 302, "", {},
        "https://fixture.blob.core.windows.net/log?signed=fixture")
    assert redirected.get_header("Authorization") is None
    with pytest.raises(ValueError):
        recovery.LogRedirect().redirect_request(req, None, 302, "", {}, "https://foreign.example/log")


def test_no_caller_document_transport():
    with pytest.raises(ValueError): recovery.GitHub("")
    client = recovery.GitHub("fixture")
    with pytest.raises(ValueError): client.get("repos/foreign/repo/actions/runs/1")


def test_projection_rejects_application_drift(monkeypatch):
    def git(root, *args):
        if args[0] == "diff": return "platform/api.py"
        return "a" * 40
    monkeypatch.setattr(recovery, "git", git)
    with pytest.raises(ValueError, match="outside recovery projection"):
        recovery.projection(Path("."))


def test_bound_rejects_historical_tests_relabelled(monkeypatch):
    monkeypatch.setattr(recovery, "projection", lambda root: {"tested_source": recovery.TESTED})
    with pytest.raises(ValueError, match="projection"):
        recovery.verify_bound({"schema": recovery.SCHEMA, "binding": {"tested_source": "f" * 40}}, Path("."))


def provider_fixture():
    run = {"id": recovery.RUN, "run_attempt": 1, "head_sha": recovery.TESTED,
        "event": "push", "status": "completed", "conclusion": "failure",
        "path": ".github/workflows/build-platform-images.yml",
        "repository": {"id": recovery.REPO_ID, "full_name": recovery.REPO},
        "head_repository": {"id": recovery.REPO_ID, "full_name": recovery.REPO}}
    jobs, logs = [], {}
    for index in range(8):
        job, raw, expected = fixture()
        job["id"] = 100 + index
        job["name"] = f"test / gate-shard-{index}"
        job["steps"][0]["name"] = f"Run gate shard {index}"
        jobs.append(job)
        logs[job["id"]] = raw
    jobs.append({"id": 200, "name": "test / run-all-gates", "run_id": recovery.RUN,
        "head_sha": recovery.TESTED, "conclusion": "failure",
        "steps": [{"name": "Verify the complete gate", "conclusion": "failure"}]})
    logs[200] = b"no shard result files (schema 1) found\ngate proof NOT emitted: the fan-in did not prove the gate\nSHARD_JOB_RESULT: failure"
    class Client:
        def get(self, path, raw=False):
            if raw: return logs[int(path.split("/")[-2])]
            if "/jobs?" in path: return {"jobs": jobs, "total_count": len(jobs)}
            return run
    return Client(), run, jobs, [list(expected.values()) for _ in range(8)]


def test_provider_topology_positive_preserves_failure():
    client, run, jobs, partitions = provider_fixture()
    receipt = recovery.recover(client, "catalog-fixture", partitions)
    assert receipt["schema"] == recovery.SCHEMA
    assert receipt["aggregate_conclusion"] == "failure"
    assert receipt["canonical_proof_emitted"] is False


@pytest.mark.parametrize("mutation", ["attempt", "repository", "missing", "duplicate", "foreign-job", "aggregate"])
def test_provider_identity_failures(mutation):
    client, run, jobs, partitions = provider_fixture()
    if mutation == "attempt": run["run_attempt"] = 2
    if mutation == "repository": run["repository"]["id"] = 1
    if mutation == "missing": jobs.pop(0)
    if mutation == "duplicate": jobs[1] = copy.deepcopy(jobs[0])
    if mutation == "foreign-job": jobs[0]["head_sha"] = "f" * 40
    if mutation == "aggregate": jobs[-1]["steps"][0]["name"] = "checkout"
    with pytest.raises(ValueError):
        recovery.recover(client, "catalog-fixture", partitions)
