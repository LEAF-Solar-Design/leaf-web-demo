"""Offline executed pins for leaf-web-demo's CodeBuild merge-queue leg."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mq_review", ROOT / "scripts/ci/mq_review.py")
mq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mq)


def entries():
    return [{"position": i, "headCommit": {"oid": str(i) * 40},
             "baseCommit": {"oid": "0" * 40},
             "pullRequest": {"number": 100 + i, "headRefOid": str(i + 3) * 40}}
            for i in range(1, 4)]


def bash():
    if os.name == "nt":
        for path in ("C:/Program Files/Git/bin/bash.exe",
                     "C:/Program Files (x86)/Git/bin/bash.exe"):
            if Path(path).is_file():
                return path
    return shutil.which("bash") or "bash"


def test_members_prefix():
    queue = entries()
    assert mq.members_for(list(reversed(queue)), "2" * 40) == queue[:2]


def test_members_missing():
    with pytest.raises(mq.NotQueued):
        mq.members_for(entries(), "9" * 40)


def test_verdict_success():
    assert mq.verdict(entries(), {str(i) * 40: "success" for i in (4, 5, 6)})[0]


@pytest.mark.parametrize("state", ["failure", "pending", None])
def test_verdict_failing_member(state):
    states = {str(i) * 40: "success" for i in (4, 5, 6)}
    if state is None:
        del states["5" * 40]
    else:
        states["5" * 40] = state
    ok, reason = mq.verdict(entries(), states)
    assert not ok
    assert "#102" in reason and (state or "absent") in reason


def test_newest_created_at_wins():
    statuses = [
        {"context": mq.REVIEW_CONTEXT, "state": "success", "created_at": "2026-09-01T00:00:00Z"},
        {"context": mq.REVIEW_CONTEXT, "state": "failure", "created_at": "2026-09-02T00:00:00Z"},
        {"context": "unrelated", "state": "success", "created_at": "2026-09-03T00:00:00Z"},
    ]
    assert mq.newest_review(statuses) == "failure"
    assert mq.newest_review(list(reversed(statuses))) == "failure"


def test_drift_reports_changed_head():
    before = entries()
    after = copy.deepcopy(before)
    assert mq.drift(before, after) is None
    after[1]["pullRequest"]["headRefOid"] = "a" * 40
    assert "member 2" in mq.drift(before, after)


def test_shell_ref_guard():
    script = (ROOT / ".codebuild/mq.sh").read_text()
    assert "^refs/heads/gh-readonly-queue/main/" in script
    result = subprocess.run([bash(), ".codebuild/mq.sh"], cwd=ROOT,
                            env={**os.environ, "CODEBUILD_WEBHOOK_HEAD_REF": "refs/heads/main"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "Not a group" in result.stdout


def test_project_dry_run():
    result = subprocess.run([bash(), "scripts/ci/mq-codebuild-project.sh", "--dry-run"],
                            cwd=ROOT, env={**os.environ, "GH_TOKEN": "never-print-this-token"},
                            capture_output=True, text=True, timeout=30, check=True)
    document = json.loads(result.stdout)
    assert "never-print-this-token" not in result.stdout + result.stderr
    project = document["project"]
    assert project["environment"]["environmentVariables"] == [
        {"name": "GH_TOKEN", "type": "SECRETS_MANAGER", "value": "leaf-github-runner-pat"}]
    assert document["webhook"]["filterGroups"] == [[
        {"type": "EVENT", "pattern": "PUSH"},
        {"type": "HEAD_REF", "pattern": "^refs/heads/gh-readonly-queue/main/"}]]
    assert project["source"]["reportBuildStatus"] is True
    assert "bash .codebuild/mq.sh" in project["source"]["buildspec"]
    assert "shell: bash" in project["source"]["buildspec"]
    for name in (".codebuild/mq.sh", "scripts/ci/mq-codebuild-project.sh", "scripts/ci/mq_review.py",
                 "scripts/test_mq_review_codebuild.py", "scripts/run-all-gates.py"):
        assert b"\r" not in (ROOT / name).read_bytes()


def test_queue_pagination(monkeypatch):
    calls = []

    def api(path, payload):
        calls.append(payload["variables"]["cursor"])
        second = len(calls) == 2
        return {"data": {"repository": {"mergeQueue": {"entries": {
            "nodes": entries()[1:] if second else entries()[:1],
            "pageInfo": {"hasNextPage": not second, "endCursor": "next"}}}}}}

    monkeypatch.setattr(mq, "github", api)
    assert mq.read_queue() == entries()
    assert calls == [None, "next"]


def test_status_pagination(monkeypatch):
    calls = []

    def api(path):
        calls.append(path)
        state, date = ("success", "01") if len(calls) == 1 else ("pending", "02")
        row = {"context": mq.REVIEW_CONTEXT, "state": state,
               "created_at": f"2026-09-{date}T00:00:00Z"}
        return [row] * (100 if len(calls) == 1 else 1)

    monkeypatch.setattr(mq, "github", api)
    assert mq.read_review("4" * 40) == "pending"
    assert calls[1].endswith("statuses?per_page=100&page=2")


def test_main_posts_ruleset_context(monkeypatch):
    reads, posts = [], []

    def queue():
        reads.append(True)
        return entries()

    monkeypatch.setattr(mq, "read_queue", queue)
    monkeypatch.setattr(mq, "read_review", lambda head: "success")
    monkeypatch.setattr(mq, "github", lambda path, payload: posts.append((path, payload)))
    monkeypatch.setenv("CODEBUILD_BUILD_URL", "https://example.test/build")
    assert mq.main(["--head-sha", "2" * 40]) == 0
    assert len(reads) == 2
    assert posts == [(f"repos/{mq.REPO}/statuses/" + "2" * 40, {
        "context": "mq-review", "state": "success",
        "description": "All queued members have successful kimi-critic-review",
        "target_url": "https://example.test/build"})]


def test_main_destroyed_group_posts_nothing(monkeypatch):
    queues = iter([entries(), []])
    monkeypatch.setattr(mq, "read_queue", lambda: next(queues))
    monkeypatch.setattr(mq, "read_review", lambda head: "success")
    monkeypatch.setattr(mq, "github", lambda *args: pytest.fail("must not post"))
    assert mq.main(["--head-sha", "2" * 40]) == 2
