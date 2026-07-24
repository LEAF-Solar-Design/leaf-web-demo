from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from customization_authority import PublishRequest, TenantBinding
from customization_models import ChangeState
import customization_service
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore


WORKSPACE = "fc5fdcb63704127f1c70a430632699e878f79bcea4d7fecdc60782fc210e6865"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def test_publish_recovers_after_git_succeeds_before_sqlite_pointer_flip(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "registry.json").write_text(
        json.dumps({"tools": []}, indent=2) + "\n", encoding="utf-8"
    )
    git(source, "add", "registry.json")
    git(source, "commit", "-m", "catalog")
    commit = git(source, "rev-parse", "HEAD")
    bare_base = tmp_path / "bare"
    bare_base.mkdir()
    git(bare_base, "clone", "--bare", str(source), "tenant-a.git")
    registry = subprocess.run(
        ["git", "show", f"{commit}:registry.json"],
        cwd=bare_base / "tenant-a.git", check=True, stdout=subprocess.PIPE,
    ).stdout
    digest = hashlib.sha256(registry).hexdigest()

    database = tmp_path / "customization.db"
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(bare_base))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "test-confirmation-key")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT", "staff|reviewer")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(
        customization_service,
        "_binding",
        lambda tenant: TenantBinding(
            str(tenant), "auth0|author", "owner", True
        ),
    )

    store = SQLiteCustomizationStore(database)
    created = store.create_change_set(
        tenant_id="tenant-a", idempotency_key="create", base_commit=commit,
        desired_platform_release="leaf-platform-2026.07.23",
        workspace_contract_digest=WORKSPACE, author_subject="auth0|author",
    )
    staging = store.transition(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        next_state=ChangeState.STAGING, expected_version=created.version,
        idempotency_key="staging",
    )
    staged = store.record_staged(
        tenant_id="tenant-a", change_set_id=created.change_set_id,
        expected_version=staging.version, idempotency_key="staged",
        staged_commit=commit, catalog_digest=digest,
        platform_release="leaf-platform-2026.07.23",
        workspace_contract_digest=WORKSPACE,
    )
    service = CustomizationService(store)
    confirmation = service.confirm(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    )
    request = PublishRequest(
        staged.change_set_id, commit, digest,
        "leaf-platform-2026.07.23", WORKSPACE,
    )

    def interrupted(_change):
        raise CustomizationServiceError("simulated_crash", 503)

    monkeypatch.setattr(service, "_harness_publish", interrupted)
    with pytest.raises(CustomizationServiceError, match="simulated_crash"):
        service.publish(
            tenant="tenant-a", request=request,
            confirmation_id=confirmation["confirmation_id"],
            idempotency_key="publish",
        )
    stranded = store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    )
    assert stranded.state is ChangeState.PUBLISHING

    monkeypatch.setenv(
        "LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "rotated-confirmation-key"
    )
    monkeypatch.setattr(service, "_harness_publish", lambda _change: commit)
    with pytest.raises(
        CustomizationServiceError, match="publish_recovery_authority_invalid"
    ):
        service.publish(
            tenant="tenant-a", request=request,
            confirmation_id=confirmation["confirmation_id"],
            idempotency_key="publish",
        )

    monkeypatch.setenv(
        "LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "test-confirmation-key"
    )
    result = service.publish(
        tenant="tenant-a", request=request,
        confirmation_id=confirmation["confirmation_id"],
        idempotency_key="publish",
    )
    assert result["catalog_commit"] == commit
    assert store.get_effective_catalog(tenant_id="tenant-a").catalog_commit == commit
