"""Focused runtime gates for the durable customization integration."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import customization_service
from customization_flags import enabled
from customization_models import ChangeSetConflictError, ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from customization_authority import TenantBinding
from routers import author as author_router


BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64


def staged_change(store, tenant_id="tenant-a", suffix="a"):
    created = store.create_change_set(
        tenant_id=tenant_id,
        idempotency_key=f"create-{suffix}",
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject=f"auth0|author-{suffix}",
    )
    staging = store.transition(
        tenant_id=tenant_id,
        change_set_id=created.change_set_id,
        next_state=ChangeState.STAGING,
        expected_version=created.version,
        idempotency_key=f"staging-{suffix}",
    )
    return store.record_staged(
        tenant_id=tenant_id,
        change_set_id=created.change_set_id,
        expected_version=staging.version,
        idempotency_key=f"staged-{suffix}",
        staged_commit=STAGED,
        catalog_digest=DIGEST,
        platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
    )


def publish_change(store, tenant_id="tenant-a", suffix="a"):
    staged = staged_change(store, tenant_id, suffix)
    store.put_confirmation(
        confirmation_id=f"confirmation-{suffix}",
        payload={"tenant_id": tenant_id, "change_set_id": staged.change_set_id},
        signature=f"signature-{suffix}",
    )
    publishing = store.prepare_publish(
        tenant_id=tenant_id,
        change_set_id=staged.change_set_id,
        confirmation_id=f"confirmation-{suffix}",
        confirmation_signature=f"signature-{suffix}",
        approver_subject=f"auth0|approver-{suffix}",
        idempotency_key=f"publish-{suffix}",
    )
    store.publish(
        tenant_id=tenant_id,
        change_set_id=publishing.change_set_id,
        expected_version=publishing.version,
        idempotency_key=f"published-{suffix}",
        approver_subject=f"auth0|approver-{suffix}",
    )
    return staged.change_set_id


def test_rollout_flags_are_strict_and_r6_depends_on_r5(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "wat")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    assert enabled(5, "tenant-a") is False
    assert enabled(6, "tenant-a") is False

    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-a")
    assert enabled(5, "tenant-a") is True
    assert enabled(6, "tenant-a") is True
    assert enabled(6, "tenant-b") is False
    assert enabled(7, "tenant-a") is False


def test_durable_confirmation_is_single_use(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.put_confirmation(confirmation_id="confirmation", payload={"bound": True}, signature="sig")
    assert store.get_confirmation(confirmation_id="confirmation") == {
        "payload": {"bound": True}, "signature": "sig", "consumed": False,
    }
    assert store.consume_confirmation(confirmation_id="confirmation", signature="sig") is True
    assert store.consume_confirmation(confirmation_id="confirmation", signature="sig") is False


def test_confirmation_consume_and_approval_transitions_are_atomic_and_idempotent(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    staged = staged_change(store)
    store.put_confirmation(
        confirmation_id="confirmation",
        payload={"tenant_id": "tenant-a", "change_set_id": staged.change_set_id},
        signature="signature",
    )
    publishing = store.prepare_publish(
        tenant_id="tenant-a",
        change_set_id=staged.change_set_id,
        confirmation_id="confirmation",
        confirmation_signature="signature",
        approver_subject="auth0|approver",
        idempotency_key="publish",
    )
    assert publishing.state is ChangeState.PUBLISHING
    assert store.get_confirmation(confirmation_id="confirmation")["consumed"] is True
    replay = store.prepare_publish(
        tenant_id="tenant-a",
        change_set_id=staged.change_set_id,
        confirmation_id="confirmation",
        confirmation_signature="signature",
        approver_subject="auth0|approver",
        idempotency_key="publish",
    )
    assert replay == publishing
    with pytest.raises(ChangeSetConflictError):
        store.prepare_publish(
            tenant_id="tenant-a",
            change_set_id=staged.change_set_id,
            confirmation_id="confirmation",
            confirmation_signature="signature",
            approver_subject="auth0|approver",
            idempotency_key="different-operation",
        )


def test_only_one_publish_per_tenant_can_remain_in_recovery(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    first = staged_change(store, suffix="a")
    second = staged_change(store, suffix="b")
    for suffix, change in (("a", first), ("b", second)):
        store.put_confirmation(
            confirmation_id=f"confirmation-{suffix}",
            payload={"tenant_id": "tenant-a", "change_set_id": change.change_set_id},
            signature=f"signature-{suffix}",
        )
    store.prepare_publish(
        tenant_id="tenant-a",
        change_set_id=first.change_set_id,
        confirmation_id="confirmation-a",
        confirmation_signature="signature-a",
        approver_subject="auth0|approver-a",
        idempotency_key="publish-a",
    )

    with pytest.raises(
        ChangeSetConflictError, match="requires recovery first"
    ):
        store.prepare_publish(
            tenant_id="tenant-a",
            change_set_id=second.change_set_id,
            confirmation_id="confirmation-b",
            confirmation_signature="signature-b",
            approver_subject="auth0|approver-b",
            idempotency_key="publish-b",
        )


def test_deployment_snapshot_restores_all_effective_catalogs_and_audits(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    first = publish_change(store, "tenant-a", "a")
    second = publish_change(store, "tenant-b", "b")
    snapshot = store.capture_deployment_snapshot(
        platform_release="prod-old", idempotency_key="snapshot"
    )
    payload = json.loads(snapshot["payload_json"])
    assert [row["tenant_id"] for row in payload] == ["tenant-a", "tenant-b"]

    with store._transaction() as conn:
        conn.execute("DELETE FROM effective_catalogs WHERE tenant_id = ?", ("tenant-b",))
    broken = store.verify_deployment_snapshot(
        snapshot_id=snapshot["snapshot_id"],
        action="verify",
        idempotency_key="verify-broken",
    )
    assert broken["verified"] is False

    restored = store.restore_deployment_snapshot(
        snapshot_id=snapshot["snapshot_id"], idempotency_key="restore"
    )
    assert restored["platform_release"] == "prod-old"
    assert store.get_effective_catalog(tenant_id="tenant-a").change_set_id == first
    assert store.get_effective_catalog(tenant_id="tenant-b").change_set_id == second
    verified = store.verify_deployment_snapshot(
        snapshot_id=snapshot["snapshot_id"],
        action="restore_verify",
        idempotency_key="verify-restored",
    )
    assert verified["verified"] is True


def test_independent_confirmation_rejects_the_harness_dispatch_secret(
    monkeypatch,
):
    calls = []
    fake = SimpleNamespace(
        confirm=lambda **kwargs: calls.append(kwargs) or {"confirmation_id": "ok"}
    )
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "harness-secret")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "approval-secret")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: fake),
    )
    request = author_router.InternalConfirmRequest(change_set_id="change")

    denied = author_router.confirm(
        request,
        x_tenant_id="tenant-a",
        x_approval_secret="harness-secret",
    )
    approved = author_router.confirm(
        request,
        x_tenant_id="tenant-a",
        x_approval_secret="approval-secret",
    )

    assert denied.status_code == 403
    assert approved == {"confirmation_id": "ok"}
    assert calls == [{"tenant_id": "tenant-a", "change_set_id": "change"}]


def test_live_author_fails_closed_when_r5_is_disabled(monkeypatch):
    legacy_calls = []
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: False)
    monkeypatch.setattr(
        author_router,
        "_legacy_author",
        lambda *_: legacy_calls.append(True),
    )

    response = author_router.author(
        author_router.AuthorRequest(description="make a tool"),
        tenant="tenant-a",
        idempotency_key="request-a",
    )

    assert response.status_code == 404
    assert json.loads(response.body)["reason_code"] == "customization_stage_disabled"
    assert legacy_calls == []


def test_stage_retry_returns_callback_recorded_receipt_in_one_call(
    tmp_path, monkeypatch
):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    service = CustomizationService(store)
    release = SimpleNamespace(
        release_id="release-a", workspace_contract_sha256=WORKSPACE
    )
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setattr(
        customization_service,
        "_binding",
        lambda tenant: TenantBinding(str(tenant), "auth0|author", "owner", True),
    )
    monkeypatch.setattr(
        customization_service.entitlements, "resolve_tier", lambda tenant: "pro"
    )
    monkeypatch.setattr(
        customization_service.entitlements,
        "entitlements_for",
        lambda tier: {"build": True},
    )
    monkeypatch.setattr(customization_service, "_bare_repo", lambda tenant: Path("."))
    monkeypatch.setattr(customization_service, "_git", lambda *args: BASE)
    monkeypatch.setattr(service, "_release", lambda: release)
    monkeypatch.setattr(
        service,
        "_authority",
        lambda: SimpleNamespace(authorize_stage=lambda **kwargs: None),
    )
    monkeypatch.setattr(service, "_verify_catalog", lambda *args: None)
    policy_calls = []
    monkeypatch.setattr(
        service,
        "_verify_stage_policy",
        lambda change, body=None: policy_calls.append((change.state, body)),
    )

    def callback_completed(change_tenant, description, change):
        receipt = {
            "contract": "leaf.customization.v1",
            "tenant_id": change_tenant,
            "change_set_id": change.change_set_id,
            "state": "staged",
            "base_commit": change.base_commit,
            "staged_commit": STAGED,
            "catalog_digest": DIGEST,
            "platform_release": change.desired_platform_release,
            "workspace_contract_digest": change.workspace_contract_digest,
            "idempotency_key": change.idempotency_key,
        }
        current = store.get_change_set(
            tenant_id=change_tenant, change_set_id=change.change_set_id
        )
        store.record_staged(
            tenant_id=change_tenant,
            change_set_id=change.change_set_id,
            expected_version=current.version,
            idempotency_key=f"staged:{change.idempotency_key}",
            staged_commit=STAGED,
            catalog_digest=DIGEST,
            platform_release=change.desired_platform_release,
            workspace_contract_digest=change.workspace_contract_digest,
        )
        return {"receipt": receipt}

    monkeypatch.setattr(service, "_harness_stage", callback_completed)

    result = service.stage(
        tenant="tenant-a",
        description="make a tool",
        mode="build",
        idempotency_key="request-a",
    )

    assert result["receipt"]["state"] == "staged"
    assert result["receipt"]["staged_commit"] == STAGED
    assert "tool" not in result
    assert policy_calls == [(ChangeState.STAGED, None)]


def test_rollback_requires_r6_and_owner_or_editor(tmp_path, monkeypatch):
    service = CustomizationService(SQLiteCustomizationStore(tmp_path / "customization.db"))
    with pytest.raises(CustomizationServiceError, match="customization_rollback_disabled"):
        service.rollback(
            tenant="tenant-a", change_set_id="change", idempotency_key="rollback"
        )

    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setattr(
        customization_service,
        "_binding",
        lambda tenant: TenantBinding(str(tenant), "reviewer", "reviewer", True),
    )
    with pytest.raises(CustomizationServiceError, match="tenant_role_denied"):
        service.rollback(
            tenant="tenant-a", change_set_id="change", idempotency_key="rollback"
        )
