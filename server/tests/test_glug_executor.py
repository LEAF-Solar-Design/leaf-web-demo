import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import glug_adoption
from glug_executor import (
    GlugExecutor,
    GlugExecutorError,
    LocalClaimWorkspaceManager,
    SubprocessCommandRunner,
)


BASE = "205317570ea1a0299a93c694af2480ed3ed4c5b3"
CURRENT = "6" * 40
COMMIT = "3" * 40
TREE = "4" * 40
MUSHY = "c3fdc0869692c804ae69fe00b5b6f0722c80943a"
NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)


def _manifest(tmp_path, *, base=BASE):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    payload = b"trusted built artifact"
    (artifact / "index.js").write_bytes(payload)
    files = [{
        "path": "index.js", "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }]
    aggregate = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw = {
        "version": 1,
        "workspace_id": "glug",
        "repository": {
            "slug_env": "GLUG_REPOSITORY_SLUG",
            "ranglr_upstream": "https://github.com/Evan-Haug/ef26.git",
            "ranglr_base_commit": base,
            "require_clean": True,
            "forbid_linked_refs": True,
            "forbid_submodules": True,
            "forbid_symlinks": True,
        },
        "sources": {
            "mushy_source_commit": MUSHY,
            "package_lock_sha256": "1" * 64,
        },
        "artifact": {
            "component": "mushy-author", "entrypoint": "index.js", "files": files,
            "byte_count": len(payload), "aggregate_sha256": aggregate,
        },
        "limits": {
            "max_changed_files": 20, "max_diff_bytes": 120000,
            "author_timeout_seconds": 240, "wrapper_timeout_seconds": 280,
            "reclaim_timeout_seconds": 300,
        },
        "powers": {
            "allowed": [
                "code_question", "announcement_draft", "schedule_draft",
                "stage_change", "create_review_branch", "create_pull_request",
            ],
            "denied": [
                "raw_member_query", "raw_finance_query", "membership_mutation",
                "treasury_action", "merge", "deploy", "app_store_publish",
            ],
        },
        "checks": [
            "server-tests", "migrations", "ios-build", "authorization", "pin-integrity",
        ],
        "targets": ["staging", "review_branch", "pull_request", "testflight"],
        "rollback": {
            "source_commit": base, "backend_deployment": "prior-proven-deployment",
            "ios_source_commit": base,
        },
    }
    manifest = tmp_path / "adoption.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    return manifest, artifact


class FakeCommands:
    def __init__(self, root):
        self.root = Path(root)
        self.head = CURRENT
        self.tree = "2" * 40
        self.dirty = False
        self.replacements = []
        self.index_lines = []
        self.changed_files = []
        self.diff = b""
        self.calls = []
        self.merge_bases = {}

    def run(self, argv, *, cwd, timeout_seconds, env=None):
        assert Path(cwd) == self.root
        assert timeout_seconds == 30
        self.calls.append((tuple(argv), dict(env or {})))
        args = tuple(argv[1:])
        if args == ("rev-parse", "--show-toplevel"):
            return str(self.root.resolve()).encode()
        if args == ("rev-parse", "HEAD"):
            return self.head.encode()
        if len(args) == 3 and args[0] == "merge-base":
            ancestor, descendant = args[1:]
            override = self.merge_bases.get((ancestor, descendant))
            if override is not None:
                return override.encode()
            if ancestor == BASE and descendant in {BASE, CURRENT, COMMIT}:
                return BASE.encode()
            if ancestor == CURRENT and descendant in {CURRENT, COMMIT}:
                return CURRENT.encode()
            if ancestor == descendant:
                return ancestor.encode()
            return ("f" * 40).encode()
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return b" M dirty.py\n" if self.dirty else b""
        if args == ("replace", "-l"):
            return ("\n".join(self.replacements)).encode()
        if args == ("ls-files", "--stage"):
            return ("\n".join(self.index_lines)).encode()
        if len(args) == 2 and args[0] == "rev-parse" and args[1].endswith("^{tree}"):
            return self.tree.encode()
        if args[:4] == ("diff", "--name-only", "-z", "--diff-filter=ACMRTUXB"):
            return ("\0".join(self.changed_files) + ("\0" if self.changed_files else "")).encode()
        if args[:4] == ("diff", "--binary", "--full-index", "--no-ext-diff"):
            return self.diff
        raise AssertionError(f"unexpected git command: {args}")


class FakeAuthor:
    def __init__(self, commands, *, read_text=None, mutate=True, result=None):
        self.commands = commands
        self.read_text = read_text
        self.mutate = mutate
        self.result = result
        self.calls = []

    def run(self, payload, **kwargs):
        self.calls.append((dict(payload), kwargs))
        if self.mutate:
            self.commands.head = COMMIT
            self.commands.tree = TREE
            self.commands.changed_files = ["README.md", "server/change.py"]
            self.commands.diff = b"diff --git a/README.md b/README.md\n+Glug\n"
        if self.result is not None:
            return self.result
        return {"text": self.read_text} if self.read_text is not None else {}


class FakeApprovals:
    def __init__(self, approved):
        self.approved = approved
        self.calls = []

    def verify(self, **kwargs):
        self.calls.append(kwargs)
        return {"approved": self.approved, "approval_id": kwargs["approval_id"]}


class FakeProvider:
    def __init__(self):
        self.branches = []
        self.pull_requests = []

    def create_review_branch(self, **kwargs):
        self.branches.append(kwargs)
        return {"branch": kwargs["branch_name"]}

    def create_pull_request(self, **kwargs):
        self.pull_requests.append(kwargs)
        return {"number": 17, "url": "https://example.test/pull/17"}


def _git(repository, *args):
    completed = subprocess.run(
        ["git", *args], cwd=str(repository), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        env=dict(os.environ),
    )
    return completed.stdout.decode("utf-8").strip()


def _canonical_source(tmp_path):
    source = tmp_path / "canonical"
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.name", "Glug Test")
    _git(source, "config", "user.email", "glug-test@example.test")
    _git(source, "config", "core.autocrlf", "false")
    (source / "README.md").write_text("Glug base\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "--quiet", "-m", "Glug base")
    return source, _git(source, "rev-parse", "HEAD")


class FilesystemAuthor:
    def __init__(self, *, mutate=True, text="Glug source answer", started=None, release=None):
        self.mutate = mutate
        self.text = text
        self.started = started
        self.release = release
        self.calls = []
        self.repository_checks = []

    def run(self, payload, **kwargs):
        repository = Path(kwargs["repository"])
        self.calls.append((dict(payload), kwargs))
        self.repository_checks.append({
            "git_dir": (repository / ".git").is_dir(),
            "alternates": (repository / ".git" / "objects" / "info" / "alternates").exists(),
            "remotes": _git(repository, "remote"),
        })
        if self.started is not None:
            self.started.set()
        if self.release is not None and not self.release.wait(timeout=5):
            raise RuntimeError("test author was not released")
        if self.mutate:
            (repository / "glug-change.txt").write_text(
                "board-authored change\n", encoding="utf-8")
            _git(repository, "config", "user.name", "Glug Author")
            _git(repository, "config", "user.email", "glug-author@example.test")
            _git(repository, "add", "glug-change.txt")
            _git(repository, "commit", "--quiet", "-m", "Glug staged change")
            return {}
        return {"text": self.text}


class InspectingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.repository_exists_at_call = []

    def create_review_branch(self, **kwargs):
        self.repository_exists_at_call.append(Path(kwargs["repository"]).is_dir())
        return super().create_review_branch(**kwargs)

    def create_pull_request(self, **kwargs):
        self.repository_exists_at_call.append(Path(kwargs["repository"]).is_dir())
        return super().create_pull_request(**kwargs)


def _local_executor(tmp_path, *, author=None, approvals=None, provider=None):
    source, base = _canonical_source(tmp_path)
    manifest, artifact = _manifest(tmp_path, base=base)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    commands = SubprocessCommandRunner()
    secret = "test-signing-key-with-at-least-32-bytes"
    env = {
        **dict(os.environ),
        "GLUG_REPOSITORY_SLUG": "biting-fogies/glug",
        "GLUG_MUSHY_CLAIM_SIGNING_SECRET": secret,
    }
    manager = LocalClaimWorkspaceManager(
        canonical_source=source,
        workspace_root=workspace_root,
        state_key=secret.encode("utf-8"),
        commands=commands,
        env=env,
    )
    author = author or FilesystemAuthor()
    clock = [NOW]
    claim_number = 0

    def next_claim_id():
        nonlocal claim_number
        claim_number += 1
        return f"{claim_number:032x}"

    executor = GlugExecutor(
        repository=None,
        artifact_root=artifact,
        env=env,
        adoption_path=manifest,
        commands=commands,
        author=author,
        approvals=approvals,
        provider=provider,
        clock=lambda: clock[0],
        claim_id_factory=next_claim_id,
        workspace_manager=manager,
    )
    return executor, manager, author, source, workspace_root, clock, base


def _request(executor, power="stage_change", *, actor_id="board-admin", **overrides):
    claim = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": power}, actor_id=actor_id)
    value = {
        "workspace_id": "glug",
        "requested_power": power,
        "instruction": "Change the Glug weekend welcome copy.",
        "claim": claim,
    }
    value.update(overrides)
    return value


def _request_for_claim(claim, power):
    return {
        "workspace_id": "glug",
        "requested_power": power,
        "instruction": "Change the Glug weekend welcome copy.",
        "claim": claim,
    }


def _workspace_in_state(workspace_root, state):
    matches = [
        path for path in workspace_root.iterdir()
        if path.is_dir() and (path / f".{state}").is_dir()
    ]
    assert len(matches) == 1
    return matches[0]


def _executor(tmp_path, *, author=None, commands=None, approvals=None, provider=None,
              monotonic=None):
    manifest, artifact = _manifest(tmp_path)
    repository = tmp_path / "clone"
    repository.mkdir()
    (repository / ".git").mkdir()
    commands = commands or FakeCommands(repository)
    author = author or FakeAuthor(commands)
    claim_number = 0

    def next_claim_id():
        nonlocal claim_number
        claim_number += 1
        return f"claim-{claim_number}"

    executor = GlugExecutor(
        repository=repository, artifact_root=artifact,
        env={
            "GLUG_REPOSITORY_SLUG": "biting-fogies/glug",
            "GLUG_MUSHY_CLAIM_SIGNING_SECRET": "test-signing-key-with-at-least-32-bytes",
            "PATH": "safe-path", "GITHUB_TOKEN": "must-not-flow",
            "STRIPE_SECRET_KEY": "must-not-flow",
        },
        adoption_path=manifest, commands=commands, author=author,
        approvals=approvals, provider=provider, clock=lambda: NOW,
        monotonic=monotonic,
        claim_id_factory=next_claim_id,
    )
    return executor, commands, author


def test_stage_derives_git_receipt_and_filters_author_environment(tmp_path):
    executor, commands, author = _executor(tmp_path)
    result = executor.execute(_request(executor), actor_id="board-admin")
    receipt = result["receipt"]
    assert receipt["commit"] == COMMIT
    assert receipt["base_commit"] == CURRENT
    assert receipt["tree"] == TREE
    assert len(receipt["signature"]) == 64
    assert receipt["changed_files"] == ["README.md", "server/change.py"]
    assert receipt["diff_bytes"] == len(commands.diff)
    assert receipt["diff_sha256"] == hashlib.sha256(commands.diff).hexdigest()
    assert receipt["limits"] == {
        "max_changed_files": 20, "max_diff_bytes": 120000,
        "author_timeout_seconds": 240, "wrapper_timeout_seconds": 280,
        "reclaim_timeout_seconds": 300,
    }
    payload, call = author.calls[0]
    assert payload["base_commit"] == CURRENT
    assert call["author_timeout_seconds"] == 240
    assert call["wrapper_timeout_seconds"] == 280
    assert call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "GLUG_MUSHY_CLAIM_SIGNING_SECRET" not in call["env"]
    assert "GITHUB_TOKEN" not in call["env"]
    assert "STRIPE_SECRET_KEY" not in call["env"]
    assert not any("push" in command for command, _ in commands.calls)


def test_rejects_extra_input_unknown_workspace_and_unavailable_power(tmp_path):
    executor, _, author = _executor(tmp_path)
    with pytest.raises(GlugExecutorError, match="fields"):
        executor.execute(_request(executor, actor_id="admin", extra=True), actor_id="admin")
    with pytest.raises(GlugExecutorError, match="fields"):
        executor.execute(
            _request(executor, actor_id="admin", base_commit=CURRENT), actor_id="admin")
    with pytest.raises(glug_adoption.GlugAdoptionError, match="unknown workspace"):
        executor.execute(
            _request(executor, actor_id="admin", workspace_id="ranglr"), actor_id="admin")
    valid = _request(executor, actor_id="admin")
    for power in ("treasury_action", "membership_mutation", "merge", "deploy", "app_store_publish"):
        with pytest.raises(GlugExecutorError, match="unavailable"):
            executor.execute({**valid, "requested_power": power}, actor_id="admin")
    assert author.calls == []


def test_claim_is_server_issued_for_current_descendant_head(tmp_path):
    executor, _, _ = _executor(tmp_path)
    claim = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": "stage_change"},
        actor_id="board-admin",
    )
    assert claim["contract"] == "glug.mushy-claim.v1"
    assert claim["base_commit"] == CURRENT
    assert claim["workspace"] == "glug"
    assert claim["power"] == "stage_change"
    assert claim["actor_digest"] != hashlib.sha256(b"board-admin").hexdigest()
    assert len(claim["signature"]) == 64
    assert set(claim) == {
        "contract", "id", "workspace", "actor_digest", "power", "base_commit",
        "issued_at", "expires_at", "signature",
    }
    with pytest.raises(GlugExecutorError, match="fields"):
        executor.issue_claim({
            "workspace_id": "glug", "requested_power": "stage_change",
            "base_commit": CURRENT,
        }, actor_id="board-admin")


@pytest.mark.parametrize(
    ("mutation", "actor_id"),
    [
        (lambda claim: claim.__setitem__("signature", "0" * 64), "board-admin"),
        (lambda claim: claim.__setitem__("workspace", "ranglr"), "board-admin"),
        (lambda claim: None, "another-admin"),
    ],
    ids=["forged", "wrong-workspace", "wrong-actor"],
)
def test_rejects_forged_or_wrongly_scoped_claim(tmp_path, mutation, actor_id):
    executor, _, author = _executor(tmp_path)
    request = _request(executor)
    mutation(request["claim"])
    with pytest.raises(GlugExecutorError) as caught:
        executor.execute(request, actor_id=actor_id)
    assert caught.value.code == "claim_invalid"
    assert author.calls == []


def test_rejects_stale_and_wrong_power_claim(tmp_path):
    executor, _, author = _executor(tmp_path)
    stale = _request(executor)
    executor.clock = lambda: NOW + dt.timedelta(seconds=301)
    with pytest.raises(GlugExecutorError, match="stale claim"):
        executor.execute(stale, actor_id="board-admin")

    executor.clock = lambda: NOW
    wrong_power = _request(executor, "code_question")
    wrong_power["requested_power"] = "stage_change"
    with pytest.raises(GlugExecutorError) as caught:
        executor.execute(wrong_power, actor_id="board-admin")
    assert caught.value.code == "claim_invalid"
    assert author.calls == []


def test_rejects_unrelated_head_and_head_drift_after_claim(tmp_path):
    executor, commands, author = _executor(tmp_path)
    commands.merge_bases[(BASE, CURRENT)] = "f" * 40
    with pytest.raises(GlugExecutorError) as caught:
        executor.issue_claim(
            {"workspace_id": "glug", "requested_power": "stage_change"},
            actor_id="admin",
        )
    assert caught.value.code == "lineage_invalid"

    commands.merge_bases.clear()
    request = _request(executor, actor_id="admin")
    commands.head = "7" * 40
    commands.merge_bases[(BASE, commands.head)] = BASE
    with pytest.raises(GlugExecutorError) as caught:
        executor.execute(request, actor_id="admin")
    assert caught.value.code == "base_drift"
    assert author.calls == []


def test_rejects_non_descendant_author_result(tmp_path):
    executor, commands, author = _executor(tmp_path)
    commands.merge_bases[(BASE, COMMIT)] = "f" * 40
    with pytest.raises(GlugExecutorError) as caught:
        executor.execute(_request(executor), actor_id="board-admin")
    assert caught.value.code == "lineage_invalid"
    assert len(author.calls) == 1


@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    [
        ("dirty", True, "dirty clone"),
        ("replacements", ["replace-ref"], "linked_refs"),
        ("index_lines", ["160000 " + "1" * 40 + " 0\tvendor/submodule"], "submodules"),
        ("index_lines", ["120000 " + "1" * 40 + " 0\tconfig/current"], "symlinks"),
    ],
)
def test_unsafe_clone_state_never_reaches_author(tmp_path, attribute, value, reason):
    executor, commands, author = _executor(tmp_path)
    setattr(commands, attribute, value)
    with pytest.raises(GlugExecutorError, match=reason):
        executor.execute(_request(executor, actor_id="admin"), actor_id="admin")
    assert author.calls == []


@pytest.mark.parametrize(("files", "diff", "code"), [
    ([f"file-{index:02}.txt" for index in range(21)], b"diff", "file_limit"),
    (["one.txt"], b"x" * 120001, "diff_limit"),
], ids=["file-limit", "diff-limit"])
def test_enforces_file_and_diff_limits(tmp_path, files, diff, code):
    executor, commands, author = _executor(tmp_path)

    def run(payload, **kwargs):
        author.calls.append((payload, kwargs))
        commands.head = COMMIT
        commands.tree = TREE
        commands.changed_files = files
        commands.diff = diff
        return {}

    author.run = run
    with pytest.raises(GlugExecutorError) as caught:
        executor.execute(_request(executor, actor_id="admin"), actor_id="admin")
    assert caught.value.code == code


def test_read_only_power_must_leave_git_unchanged_and_returns_safe_text(tmp_path):
    manifest, artifact = _manifest(tmp_path)
    repository = tmp_path / "clone"
    repository.mkdir()
    (repository / ".git").mkdir()
    commands = FakeCommands(repository)
    author = FakeAuthor(commands, read_text="The welcome lives in app/home.ts.", mutate=False)
    executor = GlugExecutor(
        repository=repository, artifact_root=artifact,
        env={
            "GLUG_REPOSITORY_SLUG": "biting-fogies/glug",
            "GLUG_MUSHY_CLAIM_SIGNING_SECRET": "test-signing-key-with-at-least-32-bytes",
        },
        adoption_path=manifest, commands=commands, author=author, clock=lambda: NOW,
        claim_id_factory=lambda: "claim-1",
    )
    result = executor.execute(
        _request(executor, "code_question", actor_id="admin"), actor_id="admin")
    assert result["text"] == "The welcome lives in app/home.ts."
    assert result["receipt"]["changed_files"] == []
    assert result["receipt"]["commit"] == CURRENT


def test_wrapper_timeout_is_frozen_at_280_seconds(tmp_path):
    ticks = iter((10.0, 291.0))
    executor, _, _ = _executor(tmp_path, monotonic=lambda: next(ticks))
    with pytest.raises(GlugExecutorError) as caught:
        executor.execute(_request(executor, actor_id="admin"), actor_id="admin")
    assert caught.value.code == "wrapper_timeout"


def test_review_branch_and_pull_request_require_verified_explicit_approval(tmp_path):
    approvals = FakeApprovals(False)
    provider = FakeProvider()
    executor, _, _ = _executor(
        tmp_path, approvals=approvals, provider=provider)
    stage = executor.execute(
        _request(executor, actor_id="admin"), actor_id="admin")["receipt"]
    publish = {
        "workspace_id": "glug", "requested_power": "create_review_branch",
        "approval_id": "approval-1", "stage_receipt": stage,
    }
    with pytest.raises(GlugExecutorError) as caught:
        executor.publish(publish, actor_id="board-admin")
    assert caught.value.code == "approval_required"
    assert provider.branches == []

    approvals.approved = True
    branch = executor.publish(publish, actor_id="board-admin")
    assert branch["branch"] == f"glug/mushy/{COMMIT[:12]}"
    assert provider.branches[0]["commit"] == COMMIT
    with pytest.raises(GlugExecutorError) as replayed:
        executor.publish(publish, actor_id="board-admin")
    assert replayed.value.code == "workspace_unavailable"

    pr_root = tmp_path / "pull-request"
    pr_root.mkdir()
    pr_executor, _, _ = _executor(
        pr_root, approvals=approvals, provider=provider)
    pr_stage = pr_executor.execute(
        _request(pr_executor, actor_id="admin"), actor_id="admin")["receipt"]
    pr = pr_executor.publish({
        **publish,
        "requested_power": "create_pull_request",
        "stage_receipt": pr_stage,
    }, actor_id="board-admin")
    assert pr["provider_result"]["number"] == 17
    assert provider.pull_requests[0]["base_branch"] == "main"

    with pytest.raises(GlugExecutorError, match="unavailable"):
        executor.publish({**publish, "requested_power": "merge"}, actor_id="board-admin")


@pytest.mark.parametrize("field", ["commit", "diff_sha256", "base_commit"])
def test_publish_rejects_tampered_signed_stage_receipt(tmp_path, field):
    approvals = FakeApprovals(True)
    provider = FakeProvider()
    executor, _, _ = _executor(
        tmp_path, approvals=approvals, provider=provider)
    stage = dict(executor.execute(
        _request(executor, actor_id="admin"), actor_id="admin")["receipt"])
    stage[field] = ("a" * 40) if field in {"commit", "base_commit"} else ("b" * 64)
    with pytest.raises(GlugExecutorError) as caught:
        executor.publish({
            "workspace_id": "glug",
            "requested_power": "create_review_branch",
            "approval_id": "approval-1",
            "stage_receipt": stage,
        }, actor_id="board-admin")
    assert caught.value.code == "receipt_invalid"
    assert approvals.calls == []
    assert provider.branches == []


def test_local_manager_gives_each_claim_a_fresh_clone_and_cleans_read_only(tmp_path):
    author = FilesystemAuthor(mutate=False)
    executor, _, _, source, workspace_root, _, base = _local_executor(
        tmp_path, author=author)
    first = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": "code_question"},
        actor_id="admin",
    )
    (source / "NEXT.md").write_text("new canonical source\n", encoding="utf-8")
    _git(source, "add", "NEXT.md")
    _git(source, "commit", "--quiet", "-m", "Advance canonical source")
    advanced = _git(source, "rev-parse", "HEAD")
    second = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": "code_question"},
        actor_id="admin",
    )
    assert first["base_commit"] == base
    assert second["base_commit"] == advanced
    assert first["id"] != second["id"]
    assert str(workspace_root) not in json.dumps(first)
    assert str(source) not in json.dumps(first)

    first_result = executor.execute(
        _request_for_claim(first, "code_question"), actor_id="admin")
    second_result = executor.execute(
        _request_for_claim(second, "code_question"), actor_id="admin")
    repositories = [Path(call[1]["repository"]) for call in author.calls]
    assert repositories[0] != repositories[1]
    assert all(check == {
        "git_dir": True, "alternates": False, "remotes": "",
    } for check in author.repository_checks)
    assert not repositories[0].exists()
    assert not repositories[1].exists()
    assert list(workspace_root.iterdir()) == []
    encoded = json.dumps([first_result, second_result])
    assert str(workspace_root) not in encoded
    assert str(source) not in encoded
    assert "test-signing-key" not in encoded


def test_atomic_execution_lease_refuses_concurrent_and_replayed_claim(tmp_path):
    started = threading.Event()
    release = threading.Event()
    author = FilesystemAuthor(
        mutate=False, started=started, release=release)
    executor, _, _, _, _, _, _ = _local_executor(tmp_path, author=author)
    claim = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": "code_question"},
        actor_id="admin",
    )
    request = _request_for_claim(claim, "code_question")
    outcome = {}

    def run_first():
        try:
            outcome["result"] = executor.execute(request, actor_id="admin")
        except Exception as exc:  # pragma: no cover - asserted below
            outcome["error"] = exc

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(timeout=5)
    with pytest.raises(GlugExecutorError) as concurrent:
        executor.execute(request, actor_id="admin")
    assert concurrent.value.code == "claim_busy"
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in outcome
    assert outcome["result"]["text"] == "Glug source answer"
    with pytest.raises(GlugExecutorError) as replay:
        executor.execute(request, actor_id="admin")
    assert replay.value.code == "workspace_unavailable"


def test_stale_claimed_and_running_workspaces_are_reclaimed(tmp_path):
    author = FilesystemAuthor(mutate=False)
    executor, manager, _, _, workspace_root, clock, _ = _local_executor(
        tmp_path, author=author)
    stale = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": "code_question"},
        actor_id="admin",
    )
    clock[0] = NOW + dt.timedelta(seconds=301)
    with pytest.raises(GlugExecutorError) as expired:
        executor.execute(
            _request_for_claim(stale, "code_question"), actor_id="admin")
    assert expired.value.code == "claim_stale"
    assert list(workspace_root.iterdir()) == []

    running = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": "code_question"},
        actor_id="admin",
    )
    lease = manager.acquire_execution(
        running["id"], expected_base=running["base_commit"],
        now=clock[0], reclaim_seconds=300)
    clock[0] = clock[0] + dt.timedelta(seconds=301)
    assert manager.reclaim_claim_if_stale(
        running["id"], now=clock[0], reclaim_seconds=300)
    assert not lease.repository.exists()
    assert list(workspace_root.iterdir()) == []


def test_failed_author_workspace_is_removed(tmp_path):
    author = FilesystemAuthor(mutate=False)
    executor, _, _, _, workspace_root, _, _ = _local_executor(
        tmp_path, author=author)

    def fail_author(payload, **kwargs):
        author.calls.append((dict(payload), kwargs))
        raise RuntimeError("author failed")

    author.run = fail_author
    with pytest.raises(RuntimeError, match="author failed"):
        executor.execute(
            _request(executor, "code_question", actor_id="admin"),
            actor_id="admin",
        )
    assert len(author.calls) == 1
    assert list(workspace_root.iterdir()) == []


def test_stage_persists_until_approved_provider_publication_then_cleans(tmp_path):
    approvals = FakeApprovals(False)
    provider = InspectingProvider()
    executor, _, _, source, workspace_root, _, _ = _local_executor(
        tmp_path, approvals=approvals, provider=provider)
    stage = executor.execute(
        _request(executor, actor_id="admin"), actor_id="admin")["receipt"]
    staged = _workspace_in_state(workspace_root, "staged")
    publish = {
        "workspace_id": "glug", "requested_power": "create_review_branch",
        "approval_id": "approval-1", "stage_receipt": stage,
    }
    with pytest.raises(GlugExecutorError) as denied:
        executor.publish(publish, actor_id="board-admin")
    assert denied.value.code == "approval_required"
    assert _workspace_in_state(workspace_root, "staged") == staged

    approvals.approved = True
    publication = executor.publish(publish, actor_id="board-admin")
    assert publication["commit"] == stage["commit"]
    assert provider.repository_exists_at_call == [True]
    assert list(workspace_root.iterdir()) == []
    encoded = json.dumps({"stage": stage, "publication": publication})
    assert str(workspace_root) not in encoded
    assert str(source) not in encoded
    assert "test-signing-key" not in encoded


def test_publish_rederives_git_and_refuses_dirty_staged_workspace(tmp_path):
    approvals = FakeApprovals(True)
    provider = InspectingProvider()
    executor, _, _, _, workspace_root, _, _ = _local_executor(
        tmp_path, approvals=approvals, provider=provider)
    stage = executor.execute(
        _request(executor, actor_id="admin"), actor_id="admin")["receipt"]
    staged = _workspace_in_state(workspace_root, "staged")
    (staged / "repository" / "unreceipted.txt").write_text(
        "tamper\n", encoding="utf-8")
    with pytest.raises(GlugExecutorError) as caught:
        executor.publish({
            "workspace_id": "glug",
            "requested_power": "create_review_branch",
            "approval_id": "approval-1",
            "stage_receipt": stage,
        }, actor_id="board-admin")
    assert caught.value.code == "dirty_result"
    assert provider.branches == []
    assert _workspace_in_state(workspace_root, "staged") == staged


def test_signed_workspace_state_tampering_never_reaches_author(tmp_path):
    author = FilesystemAuthor(mutate=False)
    executor, _, _, _, workspace_root, _, _ = _local_executor(
        tmp_path, author=author)
    claim = executor.issue_claim(
        {"workspace_id": "glug", "requested_power": "code_question"},
        actor_id="admin",
    )
    claimed = _workspace_in_state(workspace_root, "claimed")
    state_path = claimed / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["base_commit"] = "f" * 40
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(GlugExecutorError) as caught:
        executor.execute(
            _request_for_claim(claim, "code_question"), actor_id="admin")
    assert caught.value.code == "workspace_invalid"
    assert author.calls == []
