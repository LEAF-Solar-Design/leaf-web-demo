"""Offline executed pins for leaf-web-demo's CodeBuild merge-queue leg."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

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
    result = subprocess.run([bash(), ".codebuild/mq.sh"], cwd=ROOT,
                            env={**os.environ, "CODEBUILD_WEBHOOK_HEAD_REF": "refs/heads/main"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "Not a group" in result.stdout


def test_mq_sh_accept_path_execs_python3(tmp_path):
    assert "MQ_REVIEW_PY" in (ROOT / ".codebuild/mq.sh").read_text()
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "argv.txt"
    stub = stub_dir / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > \"{record.as_posix()}\"\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    head_sha = "a" * 40
    env = {**os.environ, "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
           "CODEBUILD_WEBHOOK_HEAD_REF": "refs/heads/gh-readonly-queue/main/pr-1-abcdef",
           "CODEBUILD_RESOLVED_SOURCE_VERSION": head_sha,
           "MQ_REVIEW_PY": "/tmp/mq_review.py"}
    result = subprocess.run([bash(), ".codebuild/mq.sh"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    argv = record.read_text().splitlines()
    assert argv == ["/tmp/mq_review.py", "--head-sha", head_sha]


def test_loader_is_one_parsed_block():
    result = subprocess.run([bash(), "scripts/ci/mq-codebuild-project.sh", "--dry-run"],
                            cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
    document = json.loads(result.stdout)
    spec = yaml.safe_load(document["project"]["source"]["buildspec"])
    assert spec["version"] == 0.2
    assert spec["env"]["shell"] == "bash"
    commands = spec["phases"]["build"]["commands"]
    assert isinstance(commands, list)
    assert all(isinstance(command, str) for command in commands)
    assert len(commands) == 1
    block = commands[0]
    previous = -1
    for substring in (
        "set -euo pipefail",
        "git show-ref --verify -s refs/remotes/origin/main",
        "git show-ref --verify -s refs/heads/main",
        'git cat-file -e "$REF:.codebuild/mq.sh"',
        'git cat-file -e "$REF:scripts/ci/mq_review.py"',
        'git show "$REF:.codebuild/mq.sh" > /tmp/mq.sh',
        'git show "$REF:scripts/ci/mq_review.py" > /tmp/mq_review.py',
        "MQ_REVIEW_PY=/tmp/mq_review.py bash /tmp/mq.sh",
    ):
        index = block.index(substring)
        assert index > previous
        previous = index
    assert "git rev-parse --verify -q origin/main" not in block
    assert "bash .codebuild/mq.sh" not in block


def test_connection_is_read_once():
    script = (ROOT / "scripts/ci/mq-codebuild-project.sh").read_text()
    assert script.count("list-connections") == 1


def test_project_dry_run():
    result = subprocess.run([bash(), "scripts/ci/mq-codebuild-project.sh", "--dry-run"],
                            cwd=ROOT, env={**os.environ, "GH_TOKEN": "never-print-this-token"},
                            capture_output=True, text=True, timeout=30, check=True)
    document = json.loads(result.stdout)
    assert "never-print-this-token" not in result.stdout + result.stderr

    role = document["role"]
    assert role["name"] == "leaf-mq-codebuild"
    assert len(role["trustPolicy"]["Statement"]) == 1
    trust_statement = role["trustPolicy"]["Statement"][0]
    assert trust_statement["Principal"] == {"Service": "codebuild.amazonaws.com"}
    assert trust_statement["Action"] == "sts:AssumeRole"
    assert trust_statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "807034087062"},
        "ArnEquals": {"aws:SourceArn": "arn:aws:codebuild:us-east-1:807034087062:project/leaf-mq-leaf-web-demo"}}
    statements = role["policy"]["Statement"]
    assert len(statements) == 3
    for statement in statements:
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
        assert all(action != "*" for action in actions)
        assert all(resource != "*" for resource in resources)
    logs_statement, secret_statement, conn_statement = statements
    assert logs_statement["Action"] == ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    assert logs_statement["Resource"] == [
        "arn:aws:logs:us-east-1:807034087062:log-group:/codebuild/leaf-mq-leaf-web-demo",
        "arn:aws:logs:us-east-1:807034087062:log-group:/codebuild/leaf-mq-leaf-web-demo:*"]
    assert secret_statement["Action"] == "secretsmanager:GetSecretValue"
    assert set(conn_statement["Action"]) == {
        "codeconnections:GetConnection", "codeconnections:GetConnectionToken",
        "codeconnections:UseConnection", "codestar-connections:GetConnection",
        "codestar-connections:GetConnectionToken", "codestar-connections:UseConnection"}
    assert len(conn_statement["Resource"]) == 2

    project = document["project"]
    assert project["environment"]["environmentVariables"] == [
        {"name": "GH_TOKEN", "type": "SECRETS_MANAGER", "value": "leaf-github-runner-pat"}]
    assert document["webhook"]["filterGroups"] == [[
        {"type": "EVENT", "pattern": "PUSH"},
        {"type": "HEAD_REF", "pattern": "^refs/heads/gh-readonly-queue/main/"}]]
    assert project["source"]["reportBuildStatus"] is True
    assert project["source"]["gitCloneDepth"] == 0
    assert project["serviceRole"] == "arn:aws:iam::807034087062:role/leaf-mq-codebuild"
    assert project["timeoutInMinutes"] == 40
    assert project["logsConfig"]["cloudWatchLogs"]["groupName"] == "/codebuild/leaf-mq-leaf-web-demo"
    buildspec = project["source"]["buildspec"]
    assert "shell: bash" in buildspec
    assert "bash .codebuild/mq.sh" not in buildspec
    assert "origin/main" in buildspec
    assert 'git show "$REF:.codebuild/mq.sh"' in buildspec
    assert 'git show "$REF:scripts/ci/mq_review.py"' in buildspec
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


def test_head_sha_arg_rejects_non_hex():
    with pytest.raises(SystemExit):
        mq.main(["--head-sha", "not-a-sha"])


def test_headrefoid_guard_rejects_bad_sha(monkeypatch):
    bad_entries = [{"position": 1, "headCommit": {"oid": "1" * 40},
                    "pullRequest": {"number": 101, "headRefOid": "not-a-sha"}}]
    monkeypatch.setattr(mq, "read_queue", lambda: bad_entries)
    monkeypatch.setattr(mq, "github", lambda *a, **k: pytest.fail("must not call GitHub"))
    assert mq.main(["--head-sha", "1" * 40]) == 1


def test_github_token_crosses_on_stdin_only(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, input=None, text=None, capture_output=None):
        calls.append((command, input))
        return FakeResult()

    monkeypatch.setattr(mq.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    mq.github("some/path")
    assert len(calls) == 1
    command, stdin = calls[0]
    assert "--max-time" in command
    assert not any("secret-token-value" in str(arg) for arg in command)
    assert "secret-token-value" in stdin
