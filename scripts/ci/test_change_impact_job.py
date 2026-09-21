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


@pytest.mark.parametrize("head_ref", ["", "refs/heads/gh-readonly-queue/main/pr-12"])
def test_unresolved_base_skips(sandbox, capsys, head_ref):
    _, _, head, call, receipt_dir, args, _ = sandbox
    assert job.main(args + ["--event", "PUSH", "--head-ref", head_ref]) == 0
    assert f"change-impact: SKIP no base for event=PUSH head={head}" in capsys.readouterr().out
    assert not call.exists()
    assert not receipt_dir.exists()


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
