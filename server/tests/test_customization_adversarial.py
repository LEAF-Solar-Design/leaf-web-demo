from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from customization_models import ChangeSet, ChangeState
from customization_service import (
    CustomizationService,
    CustomizationServiceError,
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


def test_enabled_customization_uses_base_catalog_when_pin_is_absent(
    tmp_path, monkeypatch
):
    database = tmp_path / "customization.db"
    SQLiteCustomizationStore(database).initialize()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")

    assert effective_catalog_dir("tenant-a") is None


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
        assert effective_catalog_dir("tenant-a") is None

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
