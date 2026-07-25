from __future__ import annotations

import errno
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import customization_service
from customization_models import ChangeSet, ChangeState
from customization_service import (
    CustomizationService,
    CustomizationServiceError,
    _exclusive_materialize,
    _materialize_worktree,
    effective_catalog_dir,
)
from customization_store import SQLiteCustomizationStore


RELEASE = "leaf-platform-2026.07.23"
WORKSPACE = "fc5fdcb63704127f1c70a430632699e878f79bcea4d7fecdc60782fc210e6865"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def repository(tmp_path: Path, tenant_id: str = "tenant-a"):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    base_tool = {
        "name": "base-tool",
        "entry": "tools/base-tool/tool.py",
    }
    (source / "tools" / "base-tool").mkdir(parents=True)
    (source / "tools" / "base-tool" / "tool.py").write_text("def run(): return 1\n")
    (source / "registry.json").write_text(
        json.dumps({"tools": [base_tool]}, indent=2) + "\n", encoding="utf-8"
    )
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    bare_base = tmp_path / "bare"
    bare_base.mkdir()
    git(bare_base, "clone", "--bare", str(source), f"{tenant_id}.git")
    return source, bare_base, base, base_tool


def staged_change(source: Path, bare_base: Path, base: str, base_tool: dict):
    tool = {
        "name": "new-tool",
        "entry": "tools/new-tool/tool.py",
    }
    (source / "tools" / "new-tool").mkdir(parents=True)
    (source / "tools" / "new-tool" / "tool.py").write_text("def run(): return 2\n")
    (source / "registry.json").write_text(
        json.dumps({"tools": [base_tool, tool]}, indent=2) + "\n", encoding="utf-8"
    )
    git(source, "add", ".")
    git(source, "commit", "-m", "stage")
    staged = git(source, "rev-parse", "HEAD")
    git(source, "push", str(bare_base / "tenant-a.git"), f"{staged}:refs/leaf/changes/test")
    registry = subprocess.run(
        ["git", "show", f"{staged}:registry.json"],
        cwd=bare_base / "tenant-a.git", check=True, stdout=subprocess.PIPE,
    ).stdout
    digest = hashlib.sha256(registry).hexdigest()
    change = ChangeSet(
        change_set_id="11111111-1111-4111-8111-111111111111",
        tenant_id="tenant-a",
        idempotency_key="stage",
        state=ChangeState.STAGED,
        version=2,
        base_commit=base,
        staged_commit=staged,
        catalog_digest=digest,
        desired_platform_release=RELEASE,
        workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author",
        approver_subject=None,
        created_at="",
        updated_at="",
    )
    return change, tool


def test_policy_accepts_one_trusted_tool_delta_and_rejects_frozen_path(
    tmp_path, monkeypatch
):
    source, bare_base, base, base_tool = repository(tmp_path)
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(bare_base))
    change, tool = staged_change(source, bare_base, base, base_tool)
    CustomizationService._verify_stage_policy(change, {"tool": tool})

    (source / "requirements.txt").write_text("attacker-package\n", encoding="utf-8")
    git(source, "add", "requirements.txt")
    git(source, "commit", "-m", "frozen attack")
    attacked = git(source, "rev-parse", "HEAD")
    git(source, "push", str(bare_base / "tenant-a.git"), f"{attacked}:refs/leaf/changes/attack")
    attacked_change = ChangeSet(
        **{**change.__dict__, "staged_commit": attacked}
    )
    with pytest.raises(CustomizationServiceError, match="frozen_path_changed"):
        CustomizationService._verify_stage_policy(attacked_change)


def test_git_symlink_is_rejected_without_creating_an_os_symlink(tmp_path, monkeypatch):
    source, bare_base, base, base_tool = repository(tmp_path)
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(bare_base))
    change, tool = staged_change(source, bare_base, base, base_tool)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=source, check=True, text=True, input="../../credentials",
        stdout=subprocess.PIPE,
    ).stdout.strip()
    git(source, "update-index", "--add", "--cacheinfo", f"120000,{blob},tools/new-tool/link")
    git(source, "commit", "-m", "symlink attack")
    attacked = git(source, "rev-parse", "HEAD")
    git(source, "push", str(bare_base / "tenant-a.git"), f"{attacked}:refs/leaf/changes/symlink")
    attacked_change = ChangeSet(
        **{**change.__dict__, "staged_commit": attacked}
    )
    with pytest.raises(CustomizationServiceError, match="staged_symlink_denied"):
        CustomizationService._verify_stage_policy(attacked_change, {"tool": tool})


def test_gitlink_mode_is_rejected(tmp_path, monkeypatch):
    source, bare_base, base, base_tool = repository(tmp_path)
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(bare_base))
    change, tool = staged_change(source, bare_base, base, base_tool)
    git(
        source, "update-index", "--add", "--cacheinfo",
        f"160000,{base},tools/new-tool/gitlink",
    )
    git(source, "commit", "-m", "gitlink attack")
    attacked = git(source, "rev-parse", "HEAD")
    git(
        source, "push", str(bare_base / "tenant-a.git"),
        f"{attacked}:refs/leaf/changes/gitlink",
    )
    attacked_change = ChangeSet(**{**change.__dict__, "staged_commit": attacked})

    with pytest.raises(CustomizationServiceError, match="staged_file_mode_denied"):
        CustomizationService._verify_stage_policy(attacked_change, {"tool": tool})


def test_materialized_runtime_rejects_tampering(tmp_path, monkeypatch):
    source, bare_base, base, _ = repository(tmp_path)
    registry = subprocess.run(
        ["git", "show", f"{base}:registry.json"],
        cwd=bare_base / "tenant-a.git", check=True, stdout=subprocess.PIPE,
    ).stdout
    digest = hashlib.sha256(registry).hexdigest()
    database = tmp_path / "customization.db"
    effective = tmp_path / "effective"
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(bare_base))
    monkeypatch.setenv("LEAF_EFFECTIVE_TENANTS_DIR", str(effective))
    store = SQLiteCustomizationStore(database)
    store.initialize()
    created = store.create_change_set(
        tenant_id="tenant-a", idempotency_key="create", base_commit=base,
        desired_platform_release=RELEASE, workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author",
        change_set_id="11111111-1111-4111-8111-111111111111",
    )
    staging = store.transition(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        next_state=ChangeState.STAGING, expected_version=created.version,
        idempotency_key="staging",
    )
    staged = store.record_staged(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        expected_version=staging.version, idempotency_key="staged",
        staged_commit=base, catalog_digest=digest, platform_release=RELEASE,
        workspace_contract_digest=WORKSPACE,
    )
    awaiting = store.transition(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        next_state=ChangeState.AWAITING_APPROVAL, expected_version=staged.version,
        idempotency_key="awaiting",
    )
    approved = store.transition(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        next_state=ChangeState.APPROVED, expected_version=awaiting.version,
        idempotency_key="approved", approver_subject="auth0|approver",
    )
    publishing = store.transition(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        next_state=ChangeState.PUBLISHING, expected_version=approved.version,
        idempotency_key="publishing",
    )
    store.publish(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        expected_version=publishing.version, idempotency_key="published",
        approver_subject="auth0|approver",
    )
    barrier = threading.Barrier(2)

    def materialize():
        barrier.wait()
        return effective_catalog_dir("tenant-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _: materialize(), range(2)))
    path = paths[0]
    assert path is not None
    assert paths == [path, path]
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    assert effective_catalog_dir("tenant-a") == path
    (path / "registry.json").write_text('{"tools":[]}\n', encoding="utf-8")
    with pytest.raises(CustomizationServiceError, match="effective_catalog_digest_mismatch"):
        effective_catalog_dir("tenant-a")


def test_enabled_customization_never_falls_back_when_database_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")

    with pytest.raises(
        CustomizationServiceError, match="effective_catalog_authority_unavailable"
    ):
        effective_catalog_dir("tenant-a")


def test_shared_sqlite_is_inert_while_customization_is_disabled(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", "/data/state/customization.db")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(
        CustomizationService,
        "configured",
        classmethod(lambda cls: pytest.fail("shared SQLite must not be opened")),
    )

    assert effective_catalog_dir("tenant-a") is None


def test_shared_sqlite_fails_closed_when_customization_is_enabled(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", "/data/state/customization.db")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")

    with pytest.raises(
        CustomizationServiceError,
        match="customization_shared_sqlite_unsupported",
    ):
        effective_catalog_dir("tenant-a")


def test_shared_sqlite_fails_closed_for_r6_only_configuration(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", "/data/state/customization.db")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")

    with pytest.raises(
        CustomizationServiceError,
        match="customization_shared_sqlite_unsupported",
    ):
        effective_catalog_dir("tenant-a")


def test_enabled_customization_never_falls_back_when_pin_is_absent(
    tmp_path, monkeypatch
):
    database = tmp_path / "customization.db"
    SQLiteCustomizationStore(database).initialize()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")

    with pytest.raises(CustomizationServiceError, match="effective_catalog_unavailable"):
        effective_catalog_dir("tenant-a")


def test_effective_catalog_reuses_initialized_store(tmp_path, monkeypatch):
    database = tmp_path / "customization.db"
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    calls = []
    original = SQLiteCustomizationStore.__init__

    def construct_once(store, path):
        calls.append(str(path))
        original(store, path)

    monkeypatch.setattr(SQLiteCustomizationStore, "__init__", construct_once)
    CustomizationService.configured()

    for _ in range(2):
        with pytest.raises(
            CustomizationServiceError, match="effective_catalog_unavailable"
        ):
            effective_catalog_dir("tenant-a")

    assert calls == [str(database)]


def test_effective_catalog_wraps_sqlite_failure(tmp_path, monkeypatch):
    database = tmp_path / "customization.db"
    database.touch()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))

    def fail(*, tenant_id):
        raise sqlite3.OperationalError("database is locked")

    service = SimpleNamespace(
        store=SimpleNamespace(get_effective_catalog=fail)
    )
    monkeypatch.setattr(
        CustomizationService,
        "configured",
        classmethod(lambda cls: service),
    )

    with pytest.raises(CustomizationServiceError) as caught:
        effective_catalog_dir("tenant-a")

    assert caught.value.code == "effective_catalog_unavailable"
    assert caught.value.status_code == 503


def _published_pin(tmp_path, monkeypatch):
    """Publish one catalog pin and return (bare repo, effective root)."""
    _source, bare_base, base, _tool = repository(tmp_path)
    registry = subprocess.run(
        ["git", "show", f"{base}:registry.json"],
        cwd=bare_base / "tenant-a.git", check=True, stdout=subprocess.PIPE,
    ).stdout
    digest = hashlib.sha256(registry).hexdigest()
    database = tmp_path / "customization.db"
    effective = tmp_path / "effective"
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(bare_base))
    monkeypatch.setenv("LEAF_EFFECTIVE_TENANTS_DIR", str(effective))
    store = SQLiteCustomizationStore(database)
    store.initialize()
    change_set_id = "11111111-1111-4111-8111-111111111111"
    state = store.create_change_set(
        tenant_id="tenant-a", idempotency_key="create", base_commit=base,
        desired_platform_release=RELEASE, workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author", change_set_id=change_set_id,
    )
    state = store.transition(
        tenant_id="tenant-a", change_set_id=change_set_id,
        next_state=ChangeState.STAGING, expected_version=state.version,
        idempotency_key="staging",
    )
    state = store.record_staged(
        tenant_id="tenant-a", change_set_id=change_set_id,
        expected_version=state.version, idempotency_key="staged",
        staged_commit=base, catalog_digest=digest, platform_release=RELEASE,
        workspace_contract_digest=WORKSPACE,
    )
    for next_state, key in (
        (ChangeState.AWAITING_APPROVAL, "awaiting"),
        (ChangeState.APPROVED, "approved"),
        (ChangeState.PUBLISHING, "publishing"),
    ):
        extra = {"approver_subject": "auth0|approver"} if key == "approved" else {}
        state = store.transition(
            tenant_id="tenant-a", change_set_id=change_set_id,
            next_state=next_state, expected_version=state.version,
            idempotency_key=key, **extra,
        )
    store.publish(
        tenant_id="tenant-a", change_set_id=change_set_id,
        expected_version=state.version, idempotency_key="published",
        approver_subject="auth0|approver",
    )
    return bare_base / "tenant-a.git", effective, base


def test_worktree_add_failure_surfaces_git_stderr(tmp_path, monkeypatch):
    """git's own words must reach the operator.

    These subprocesses used to run with stderr=DEVNULL, so a failure in CI
    produced a bare 503 and nothing to diagnose it with.
    """
    bare, effective, _base = _published_pin(tmp_path, monkeypatch)
    missing = "0" * 40

    with pytest.raises(CustomizationServiceError) as caught:
        _materialize_worktree(bare, effective / "tenant-a" / missing, missing)

    # The client still learns only the generic code...
    assert caught.value.code == "effective_catalog_unavailable"
    assert caught.value.status_code == 503
    # ...while the operator gets what git actually said.
    assert "invalid reference" in caught.value.detail
    assert missing in caught.value.detail


def test_materialization_recovers_from_a_deleted_worktree(tmp_path, monkeypatch):
    """A worktree deleted underneath us must not wedge the tenant forever.

    `git worktree add` removes the target on its own failure path, and the
    registration it leaves behind makes every later add fail with "missing but
    already registered worktree" until something prunes it.
    """
    _bare, _effective, _base = _published_pin(tmp_path, monkeypatch)
    path = effective_catalog_dir("tenant-a")
    assert path is not None

    shutil.rmtree(path)  # exactly what the losing racer used to do

    assert effective_catalog_dir("tenant-a") == path
    assert (path / "registry.json").exists()


def test_concurrent_materialization_never_loses_the_worktree(tmp_path, monkeypatch):
    """Many callers racing one pin all get the same materialized path.

    Two concurrent `git worktree add` calls on one path let the loser delete
    the winner's directory; this failed ~6% of contended attempts before the
    materialization was serialized.
    """
    _bare, _effective, _base = _published_pin(tmp_path, monkeypatch)
    workers = 8
    barrier = threading.Barrier(workers)

    def materialize(_):
        barrier.wait()
        return effective_catalog_dir("tenant-a")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        paths = list(pool.map(materialize, range(workers)))

    assert paths[0] is not None
    assert paths == [paths[0]] * workers
    assert (paths[0] / "registry.json").exists()


# A holder in a genuinely separate PROCESS, using the same OS primitive the
# service uses. It takes the lock, announces it, then waits to be killed.
_HOLD_LOCK = """
import fcntl, sys, time
handle = open(sys.argv[1], "a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(300)
"""


def _spawn_lock_holder(lock_path):
    """Start a separate process holding the lock; return it once it confirms."""
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(lock_path)],
        stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline().strip() == "held", "holder never took the lock"
    return child


@pytest.mark.skipif(
    customization_service.fcntl is None, reason="POSIX advisory locking only"
)
def test_materialize_lock_excludes_another_process(tmp_path, monkeypatch):
    """A lock held by another PROCESS must block, not be walked straight past.

    The in-process `threading.Lock` cannot cover this, so the holder here is a
    real second process taking the same OS lock the service takes.
    """
    monkeypatch.setattr(customization_service, "_MATERIALIZE_LOCK_TIMEOUT", 0.5)
    lock_path = tmp_path / ".materialize.lock"
    child = _spawn_lock_holder(lock_path)
    try:
        with pytest.raises(CustomizationServiceError) as caught:
            with _exclusive_materialize(tmp_path / "tenant-a.git", lock_path):
                pytest.fail("acquired a lock another process already holds")
        assert caught.value.code == "effective_catalog_unavailable"
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.mark.skipif(
    customization_service.fcntl is None, reason="POSIX advisory locking only"
)
def test_killed_holder_never_wedges_the_tenant(tmp_path):
    """A worker killed mid-add must not wedge the tenant, and must not need a
    staleness timeout to recover.

    The kernel drops an advisory lock when the holding process dies, so the
    next caller proceeds immediately. Recovery here is not "eventually, after a
    stale threshold" -- it is at once, with nobody having to judge whether a
    live holder was dead.
    """
    lock_path = tmp_path / ".materialize.lock"
    child = _spawn_lock_holder(lock_path)
    child.kill()
    child.wait(timeout=10)

    entered = False
    with _exclusive_materialize(tmp_path / "tenant-a.git", lock_path):
        entered = True

    assert entered, "a dead holder's lock was never reclaimed"


class _FakeFcntl:
    """Stand-in for the `fcntl` MODULE whose `flock` fails on command.

    Injected in place of the module rather than in place of `_try_os_lock`, so
    that the errno classification inside `_try_os_lock` is the thing under
    test. Replacing `_try_os_lock` itself would leave that classification
    unexercised and let a regression back to "every OSError is contention"
    pass.

    Counting calls is what replaces reading a stopwatch: the property being
    asserted is how many attempts the service made, which is exact and does
    not depend on how fast the machine is.
    """

    LOCK_EX = 2
    LOCK_NB = 4

    def __init__(self, code: int, *, forever: bool = False) -> None:
        self._code = code
        self._forever = forever
        self.calls = 0

    def flock(self, fileno: int, flags: int) -> None:
        self.calls += 1
        if self._forever or self.calls == 1:
            raise OSError(self._code, "injected lock failure")


def test_unlockable_file_reports_its_real_cause_not_a_phantom_holder(
    tmp_path, monkeypatch
):
    """An error waiting cannot clear must not be retried as contention.

    Reporting "held by another worker" for a bad descriptor or a filesystem
    without locking would stall for the whole timeout and then name a holder
    that does not exist -- the same misreporting this change set removes.

    The fake keeps failing, because ENOLCK does not heal on a retry. One call
    is therefore the whole claim: the service was handed an error it cannot
    wait out and did not wait. The timeout below bounds only a BROKEN run --
    a correct one never consults it, so no wall clock is ever read.
    """
    monkeypatch.setattr(customization_service, "_MATERIALIZE_LOCK_TIMEOUT", 0.5)
    unlockable = _FakeFcntl(errno.ENOLCK, forever=True)
    monkeypatch.setattr(customization_service, "fcntl", unlockable)

    with pytest.raises(OSError) as caught:
        with _exclusive_materialize(tmp_path / "tenant-a.git", tmp_path / "lock"):
            pytest.fail("acquired a lock that cannot be taken")

    assert caught.value.errno == errno.ENOLCK
    assert unlockable.calls == 1, "a hard lock error was retried as contention"


def test_contended_lock_is_waited_out_and_then_taken(tmp_path, monkeypatch):
    """The other side of the same classification: EAGAIN IS contention.

    Pinning only the hard-error side would let the classification collapse the
    other way -- every OSError raised straight out -- which would turn an
    ordinary busy holder into a hard failure and drop the retry the lock
    depends on.

    The fake fails once and then succeeds, so this case needs no deadline and
    no sleep budget of its own: the loop ends because the second attempt wins,
    not because a clock ran out.
    """
    contended = _FakeFcntl(errno.EAGAIN)
    monkeypatch.setattr(customization_service, "fcntl", contended)

    entered = False
    with _exclusive_materialize(tmp_path / "tenant-a.git", tmp_path / "lock"):
        entered = True

    assert entered, "contention was reported as a hard error instead of retried"
    assert contended.calls == 2, "a busy holder was not retried exactly once"
