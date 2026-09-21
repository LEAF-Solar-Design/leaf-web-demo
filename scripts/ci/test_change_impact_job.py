"""Hermetic coverage of the advisory native-CI change-impact job."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).with_name("change_impact_job.py")
SPEC = importlib.util.spec_from_file_location("change_impact_job", HELPER)
job = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "no-gitconfig"))
    for name in ("CHANGE_IMPACT_DISABLE", "CHANGE_IMPACT_CHECKER", "GH_TOKEN",
                 "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(home / "no-hooks"))
    (repo / "ASPECTS.yaml").write_text("schema_version: 1\nproject_id: fixture\n", encoding="utf-8")
    git("add", "ASPECTS.yaml")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "feature")
    (repo / "change.txt").write_text("changed\n", encoding="utf-8")
    git("add", "change.txt")
    git("commit", "-m", "head")
    head = git("rev-parse", "HEAD")
    # Fetch only from this temp repository, never a network or developer remote.
    git("remote", "add", "origin", str(repo))
    checker = tmp_path / "checker.py"
    checker.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "Path(os.environ['CHECKER_CALL']).write_text(json.dumps(args), encoding='utf-8')\n"
        "print(json.dumps(args))\n"
        "base = args[args.index('--base') + 1]\n"
        "head = args[args.index('--head') + 1]\n"
        "print(f'assessment executed base={base} head={head}')\n"
        "print('IMPACT: ' + os.environ.get('CHECKER_VERDICT', 'COMPLETE'), file=sys.stderr)\n"
        "sys.exit(int(os.environ.get('CHECKER_EXIT', '0')))\n",
        encoding="utf-8",
    )
    call = tmp_path / "call.json"
    monkeypatch.setenv("CHANGE_IMPACT_CHECKER", str(checker))
    monkeypatch.setenv("CHECKER_CALL", str(call))
    monkeypatch.delenv("CHECKER_VERDICT", raising=False)
    monkeypatch.delenv("CHECKER_EXIT", raising=False)
    receipt_dir = tmp_path / "impact"
    args = ["--repo", str(repo), "--head", head, "--receipt-dir", str(receipt_dir)]
    return repo, base, head, call, receipt_dir, args, git


@pytest.mark.parametrize("base_ref", ["main", "refs/heads/main"])
def test_pr_base_ref(sandbox, tmp_path, capsys, base_ref):
    repo, base, head, call, receipt_dir, args, _ = sandbox
    gate_result = tmp_path / "gate-result.json"
    gate_result.write_text("{}", encoding="utf-8")
    assert job.main(args + ["--base-ref", base_ref, "--event", "PULL_REQUEST_UPDATED",
                            "--gate-result", str(gate_result)]) == 0
    command = json.loads(call.read_text(encoding="utf-8"))
    assert command == ["check", "--workdir", str(repo.resolve()), "--change-id", "ci-" + head[:12],
                       "--base", base, "--head", head, "--record", str(receipt_dir / "record.yaml"),
                       "--receipt", str(receipt_dir / "receipt.json"), "--json"]
    context = json.loads((receipt_dir / "ci.json").read_text(encoding="utf-8"))
    assert context == {"event": "PULL_REQUEST_UPDATED", "base_rule": "pr-base-ref",
                       "base": base, "head": head, "gate_result_present": True,
                       "elapsed_s": context["elapsed_s"]}
    assert context["elapsed_s"] >= 0
    output = capsys.readouterr().out
    assert f"assessment executed base={base} head={head}" in output
    assert "IMPACT: COMPLETE" in output


def test_pr_base_is_the_merge_base_not_the_tip(sandbox, capsys):
    """main advances after the branch point; the base must stay at the fork point."""
    repo, base, head, call, _, args, git = sandbox
    git("checkout", "main")
    (repo / "landed-later.txt").write_text("on main after the fork\n", encoding="utf-8")
    git("add", "landed-later.txt")
    git("commit", "-m", "main moves on")
    tip = git("rev-parse", "HEAD")
    git("checkout", "feature")
    assert tip != base
    assert job.main(args + ["--base-ref", "main"]) == 0
    command = json.loads(call.read_text(encoding="utf-8"))
    assert command[command.index("--base") + 1] == base
    assert f"base={base}" in capsys.readouterr().out


def test_pr_local_fallback(sandbox):
    _, base, _, call, _, args, git = sandbox
    git("remote", "remove", "origin")
    assert job.main(args + ["--base-ref", "main"]) == 0
    command = json.loads(call.read_text(encoding="utf-8"))
    assert command[command.index("--base") + 1] == base


def test_merge_group_embedded_base(sandbox, monkeypatch):
    _, base, head, call, receipt_dir, args, _ = sandbox
    def no_network(*args, **kwargs):
        pytest.fail("Embedded base must not query GitHub")
    monkeypatch.setattr(job.urllib.request, "urlopen", no_network)
    assert job.main(args + ["--event", "MERGE_GROUP", "--head-ref",
                            f"refs/heads/gh-readonly-queue/main/pr-12-{base}"]) == 0
    command = json.loads(call.read_text(encoding="utf-8"))
    assert command[command.index("--base") + 1] == base
    context = json.loads((receipt_dir / "ci.json").read_text(encoding="utf-8"))
    assert context["base"] == base
    assert context["head"] == head
    assert context["base_rule"] == "merge-group-ref"
    assert context["gate_result_present"] is False


def test_merge_queue_fallback_matches_head(sandbox, monkeypatch):
    _, base, head, call, receipt_dir, args, _ = sandbox
    monkeypatch.setenv("GH_TOKEN", "fixture-token")
    requests = []
    def urlopen(request, timeout):
        requests.append(json.loads(request.data))
        assert timeout == 60
        cursor = requests[-1]["variables"]["cursor"]
        entry = {"headCommit": {"oid": "f" * 40 if cursor is None else head},
                 "baseCommit": {"oid": "e" * 40 if cursor is None else base}}
        return io.StringIO(json.dumps({"data": {"repository": {"mergeQueue": {"entries": {
            "nodes": [entry], "pageInfo": {"hasNextPage": cursor is None, "endCursor": "next"},
        }}}}}))
    monkeypatch.setattr(job.urllib.request, "urlopen", urlopen)
    assert job.main(args + ["--head-ref", "refs/heads/gh-readonly-queue/main/pr-12"]) == 0
    assert len(requests) == 2
    assert requests[0]["variables"]["branch"] == "main"
    context = json.loads((receipt_dir / "ci.json").read_text(encoding="utf-8"))
    assert context["base"] == base
    assert context["base_rule"] == "merge-queue-entry"
    assert call.exists()


def test_queue_ref_without_entry_skips(sandbox, capsys):
    """A queue ref with no embedded sha and no readable queue entry never guesses a base."""
    _, _, head, call, receipt_dir, args, _ = sandbox
    head_ref = "refs/heads/gh-readonly-queue/main/pr-12"
    assert job.main(args + ["--event", "PUSH", "--head-ref", head_ref]) == 0
    assert f"change-impact: SKIP no base for event=PUSH head={head}" in capsys.readouterr().out
    assert not call.exists()
    assert not receipt_dir.exists()


def test_push_without_base_ref_uses_first_parent(sandbox, capsys):
    """A merge landing on main, or a manual build: the base is the head's first parent."""
    _, base, head, call, _, args, _ = sandbox
    assert job.main(args + ["--event", "PUSH", "--head-ref", ""]) == 0
    command = json.loads(call.read_text(encoding="utf-8"))
    assert command[command.index("--base") + 1] == base
    out = capsys.readouterr().out
    assert "base_rule=push-first-parent" in out
    assert f"assessment executed base={base} head={head}" in out


def test_root_commit_push_skips(sandbox, capsys):
    """A head with no parent has nothing to diff against and SKIPs."""
    repo, _, _, call, receipt_dir, args, git = sandbox
    root = git("rev-list", "--max-parents=0", "HEAD").splitlines()[0]
    root_args = [a for a in args]
    root_args[root_args.index("--head") + 1] = root
    assert job.main(root_args + ["--event", "PUSH", "--head-ref", ""]) == 0
    assert f"change-impact: SKIP no base for event=PUSH head={root}" in capsys.readouterr().out
    assert not call.exists()


def test_kill_switch(sandbox, monkeypatch, capsys):
    _, _, _, call, _, args, _ = sandbox
    monkeypatch.setenv("CHANGE_IMPACT_DISABLE", "1")
    assert job.main(args) == 0
    assert capsys.readouterr().out.strip() == "change-impact: disabled"
    assert not call.exists()


def test_checker_absent(sandbox, monkeypatch, capsys):
    _, _, _, call, _, args, _ = sandbox
    monkeypatch.delenv("CHANGE_IMPACT_CHECKER")
    assert job.main(args) == 0
    assert "checker not installed on this runner, skipped" in capsys.readouterr().out
    assert not call.exists()


@pytest.mark.parametrize("strict, expected", [(False, 0), (True, 1)])
def test_incomplete_is_advisory_unless_strict(sandbox, monkeypatch, strict, expected):
    _, _, _, _, _, args, _ = sandbox
    monkeypatch.setenv("CHECKER_VERDICT", "INCOMPLETE")
    monkeypatch.setenv("CHECKER_EXIT", "1")
    assert job.main(args + ["--base-ref", "main"] + (["--strict"] if strict else [])) == expected


def test_checker_timeout_is_advisory(sandbox, monkeypatch, capsys):
    _, _, _, call, receipt_dir, args, _ = sandbox
    original_run = job.subprocess.run
    def run(command, **kwargs):
        if command[0] == sys.executable:
            assert kwargs["timeout"] == 300
            raise subprocess.TimeoutExpired(command, 300, output=b"partial assessment\n")
        return original_run(command, **kwargs)
    monkeypatch.setattr(job.subprocess, "run", run)
    assert job.main(args + ["--base-ref", "main"]) == 0
    assert "checker timed out (advisory)" in capsys.readouterr().out
    assert (receipt_dir / "ci.json").is_file()
    assert not call.exists()


def test_vendored_checker_used_when_no_override(sandbox, monkeypatch, capsys):
    repo, _, _, call, _, args, git = sandbox
    monkeypatch.delenv("CHANGE_IMPACT_CHECKER")
    vendored = repo / "scripts" / "ci" / "vendor" / "impact" / "impact.py"
    vendored.parent.mkdir(parents=True)
    # the same fake checker the env override uses, now found through the repo
    vendored.write_text((Path(os.environ["CHECKER_CALL"]).parent / "checker.py").read_text(encoding="utf-8"),
                        encoding="utf-8")
    assert job.main(args + ["--base-ref", "main"]) == 0
    out = capsys.readouterr().out
    assert "checker=vendored" in out
    assert call.exists()


def test_vendored_pin_matches_tree():
    """VENDORED.json is the pin: every listed file's sha256 matches, no extra files."""
    import hashlib
    root = HELPER.parent / "vendor" / "impact"
    pin = json.loads((root / "VENDORED.json").read_text(encoding="utf-8"))
    assert len(pin["source_revision"]) == 40
    listed = {e["path"]: e["sha256"] for e in pin["entries"]}
    assert listed, "pin lists no files"
    for rel, digest in listed.items():
        data = (root / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, rel
    on_disk = {
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file() and p.name != "VENDORED.json" and "__pycache__" not in p.parts
    }
    assert on_disk == set(listed), sorted(on_disk ^ set(listed))


def test_ci_job_shape_and_shell_syntax():
    script = ROOT / ".codebuild" / "ci.sh"
    lines = script.read_text(encoding="utf-8").splitlines()
    marker = 'echo "=== job change-impact ==="'
    assert lines.count(marker) == 1
    assert lines.index('echo "=== job test-gate ==="') < lines.index(marker) < len(lines) - 1
    assert lines[-1] == 'exit "$gate_status"'
    assert lines[lines.index(marker) - 1] == "fi"
    result = subprocess.run(["bash", "-n", ".codebuild/ci.sh"], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def comment_receipt():
    return {
        "checker_version": "0.1.0", "manifest_digest": "d5a01e293a79" + "0" * 52,
        "verdict": "INCOMPLETE",
        "summary": {"required": 3, "unresolved": 1, "changed": 1,
                    "unchanged-compatible": 1},
        "rows": [
            {"required": True, "concern": "security", "subject": "ci",
             "outcome": "changed", "evidence": "review:77"},
            {"required": True, "concern": "tests", "subject": "ci",
             "outcome": "unchanged-compatible", "evidence": "run:77"},
            {"required": True, "concern": "alarms", "subject": "monitor",
             "outcome": "unresolved", "evidence": None},
            {"required": False, "concern": "docs", "subject": "optional-subject",
             "outcome": "unresolved", "evidence": None},
        ],
    }


@pytest.fixture
def comment_run(sandbox, monkeypatch, comment_receipt):
    _, base, _, _, receipt_dir, args, _ = sandbox
    monkeypatch.delenv("CHANGE_IMPACT_NO_COMMENT", raising=False)
    checker = Path(os.environ["CHANGE_IMPACT_CHECKER"])
    source = checker.read_text(encoding="utf-8")
    source = source.replace(
        "sys.exit(",
        "Path(args[args.index('--receipt') + 1]).write_text("
        + repr(json.dumps(comment_receipt)) + ", encoding='utf-8')\nsys.exit(",
    )
    checker.write_text(source, encoding="utf-8")
    return args + ["--event", "PUSH", "--head-ref",
                   f"refs/heads/gh-readonly-queue/main/pr-77-{base}"], receipt_dir


@pytest.fixture
def fake_comments(monkeypatch):
    requests, responses = [], []

    def urlopen(request, timeout):
        assert timeout == 60
        assert request.get_header("User-agent") == "leaf-change-impact-ci"
        assert request.get_header("Authorization") == "Bearer fixture-comment-token"
        requests.append((request.get_method(), request.selector,
                         json.loads(request.data) if request.data else None))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.StringIO(json.dumps(response))

    monkeypatch.setattr(job.urllib.request, "urlopen", urlopen)
    return requests, responses


def test_render_comment_required_rows(comment_receipt, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-token-must-not-appear")
    context = {"base": "a" * 40, "head": "b" * 40, "base_rule": "merge-group-ref"}
    body = job.render_comment(comment_receipt, context)
    assert body.splitlines()[0] == "<!-- change-impact -->"
    assert "### Change impact (advisory)" in body
    assert ("base aaaaaaaaaaaa head bbbbbbbbbbbb rule merge-group-ref "
            "checker 0.1.0 manifest d5a01e293a79") in body
    assert ("verdict INCOMPLETE, required 3, unresolved 1, changed 1, "
            "unchanged-compatible 1, deferred 0, not-applicable 0") in body
    table = [line for line in body.splitlines() if line.startswith("| ")]
    assert len(table[2:]) == 3
    assert "| alarms | monitor | unresolved | - |" in table
    assert "optional-subject" not in body
    assert "secret-token-must-not-appear" not in body
    assert body.endswith("Dispositions: impact.py resolve|defer|dismiss --record <record> "
                         "--row <id>; kill switch C:/tmp/gates/CHANGE_IMPACT_OFF.")


@pytest.mark.parametrize("evidence, shown", [("run:77", 40), ("x" * 60000, 0)])
def test_render_comment_bounds_table(comment_receipt, evidence, shown):
    row = dict(comment_receipt["rows"][0], evidence=evidence)
    comment_receipt["rows"] = [row] * 45
    body = job.render_comment(comment_receipt, {
        "base": "a" * 40, "head": "b" * 40, "base_rule": "merge-group-ref",
    })
    assert len(body) <= 60000
    assert len([line for line in body.splitlines() if line.startswith("| ")]) == shown + 2
    assert f"... and {45 - shown} more" in body
    assert body.endswith("kill switch C:/tmp/gates/CHANGE_IMPACT_OFF.")


@pytest.mark.parametrize("existing, method, path, result", [
    ([{"id": 42, "body": "<!-- change-impact -->\nold"}], "PATCH",
     "/repos/LEAF-Solar-Design/leaf-web-demo/issues/comments/42", "updated"),
    ([{"id": 41, "body": "unrelated comment"}], "POST",
     "/repos/LEAF-Solar-Design/leaf-web-demo/issues/77/comments", "created"),
])
def test_merge_group_comment_upsert(comment_run, fake_comments, monkeypatch, capsys,
                                   existing, method, path, result):
    args, receipt_dir = comment_run
    requests, responses = fake_comments
    responses.extend([existing, {}])
    monkeypatch.setenv("GH_TOKEN", "fixture-comment-token")
    assert job.main(args) == 0
    assert requests[0] == ("GET", "/repos/LEAF-Solar-Design/leaf-web-demo/issues/77/"
                           "comments?per_page=100", None)
    assert len(requests) == 2
    assert requests[1][:2] == (method, path)
    receipt = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    context = json.loads((receipt_dir / "ci.json").read_text(encoding="utf-8"))
    assert requests[1][2] == {"body": job.render_comment(receipt, context)}
    output = capsys.readouterr().out
    assert f"change-impact: comment {result} pr=77" in output
    assert "fixture-comment-token" not in output


@pytest.mark.parametrize("token", [None, "bad\rtoken", "bad\ntoken"])
def test_merge_group_comment_no_valid_token(comment_run, fake_comments, monkeypatch,
                                          capsys, token):
    args, receipt_dir = comment_run
    requests, _ = fake_comments
    if token is not None:
        monkeypatch.setenv("GH_TOKEN", token)
    assert job.main(args) == 0
    assert requests == []
    assert "change-impact: comment skipped pr=77" in capsys.readouterr().out
    assert (receipt_dir / "receipt.json").is_file()


def test_pr_comment_not_applicable(comment_run, fake_comments, monkeypatch, capsys):
    args, _ = comment_run
    requests, _ = fake_comments
    monkeypatch.setenv("GH_TOKEN", "fixture-comment-token")
    args[args.index("--event") + 1] = "PULL_REQUEST"
    assert job.main(args) == 0
    assert requests == []
    assert "change-impact: comment not applicable (event=PULL_REQUEST)" in capsys.readouterr().out


def test_merge_group_comment_disabled(comment_run, fake_comments, monkeypatch):
    args, receipt_dir = comment_run
    requests, _ = fake_comments
    monkeypatch.setenv("GH_TOKEN", "fixture-comment-token")
    monkeypatch.setenv("CHANGE_IMPACT_NO_COMMENT", "1")
    assert job.main(args) == 0
    assert requests == []
    assert (receipt_dir / "receipt.json").is_file()


@pytest.mark.parametrize("step", ["list", "create", "update"])
def test_comment_http_failure_is_advisory(comment_run, fake_comments, monkeypatch,
                                         capsys, step):
    args, receipt_dir = comment_run
    requests, responses = fake_comments
    monkeypatch.setenv("GH_TOKEN", "fixture-comment-token")
    if step != "list":
        responses.append([{"id": 42, "body": "<!-- change-impact -->"}]
                         if step == "update" else [])
    responses.append(job.urllib.error.HTTPError(
        "https://api.github.com/", 403, "sensitive-error-text", {},
        io.BytesIO(b"sensitive-response-body"),
    ))
    assert job.main(args) == 0
    output = capsys.readouterr().out
    assert [line for line in output.splitlines() if line.startswith("change-impact: comment")] == [
        f"change-impact: comment skipped ({step} http=403)",
    ]
    assert "sensitive" not in output
    assert "fixture-comment-token" not in output
    assert len(requests) == (1 if step == "list" else 2)
    assert (receipt_dir / "receipt.json").is_file()
    assert (receipt_dir / "ci.json").is_file()
