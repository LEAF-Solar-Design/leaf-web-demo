"""Focused proof for the agent-safe publication continuation route."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import agent_policy
import customization_service
import deps
from customization_models import ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore


BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64


def _staged(store: SQLiteCustomizationStore, tenant_id: str = "tenant-a"):
    created = store.create_change_set(
        tenant_id=tenant_id,
        idempotency_key="author-request",
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author",
    )
    staging = store.transition(
        tenant_id=tenant_id,
        change_set_id=created.change_set_id,
        next_state=ChangeState.STAGING,
        expected_version=created.version,
        idempotency_key="staging",
    )
    return store.record_staged(
        tenant_id=tenant_id,
        change_set_id=created.change_set_id,
        expected_version=staging.version,
        idempotency_key="staged",
        staged_commit=STAGED,
        catalog_digest=DIGEST,
        platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
    )


@pytest.fixture
def publication_service(tmp_path, monkeypatch):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    service = CustomizationService(store)
    staged = _staged(store)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_SECRET", "signing-secret")
    monkeypatch.setenv("LEAF_AGENT_STORE", "legacy")
    tenant_state = tmp_path / "agent-tenants.json"
    tenant_state.write_text(json.dumps({
        "tenant-a": {
            "overlay": {
                "request_publication": {"policy": "always-confirm"},
            },
        },
    }), encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_TENANTS_FILE", str(tenant_state))
    monkeypatch.setenv(
        "LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT", "auth0|approver"
    )
    monkeypatch.setattr(service, "_verify_catalog", lambda *args: None)
    monkeypatch.setattr(service, "_harness_publish", lambda change: change.staged_commit)
    tenant = deps.TenantContext("tenant-a", tier="hosted_pro")
    return service, store, staged, tenant


def test_default_off_auto_confirms_exact_receipt_and_publishes(
    publication_service, monkeypatch
):
    service, store, staged, tenant = publication_service
    monkeypatch.setattr(service, "_publication_policy_state", lambda _tid: (True, False))

    published = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )
    replay = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert published == replay == {
        "contract": "leaf.customization.v1",
        "change_set_id": staged.change_set_id,
        "status": "published",
        "catalog_digest": DIGEST,
    }
    durable = store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    )
    assert durable.approver_subject == "leaf:server:auto-publication-policy"
    with store._connection() as conn:
        confirmation = conn.execute(
            "SELECT tenant_id, change_set_id, payload_json, consumed "
            "FROM customization_confirmations"
        ).fetchone()
    payload = json.loads(confirmation["payload_json"])
    assert confirmation["tenant_id"] == "tenant-a"
    assert confirmation["change_set_id"] == staged.change_set_id
    assert confirmation["consumed"] == 1
    assert payload["staged_commit"] == STAGED
    assert payload["catalog_digest"] == DIGEST
    assert payload["workspace_contract_digest"] == WORKSPACE
    assert "signature" not in json.dumps(published)


def test_missing_account_overlay_defaults_publication_approval_off(
    publication_service, monkeypatch, tmp_path
):
    service, _store, _staged_change, _tenant = publication_service
    monkeypatch.setenv(
        "LEAF_AGENT_TENANTS_FILE", str(tmp_path / "missing-agent-tenants.json")
    )

    assert service._publication_policy_state("tenant-a") == (True, False)


def test_publication_policy_authority_outage_fails_closed(
    publication_service, monkeypatch
):
    service, _store, _staged_change, _tenant = publication_service
    monkeypatch.setattr(
        agent_policy,
        "load_tenant_state",
        lambda _tid: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    assert service._publication_policy_state("tenant-a") == (True, True)


def test_publication_request_waits_without_leaking_confirmation(publication_service):
    service, store, staged, tenant = publication_service

    first = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )
    replay = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert first == replay == {
        "contract": "leaf.customization.v1",
        "change_set_id": staged.change_set_id,
        "status": "awaiting_approval",
    }
    assert "confirmation" not in json.dumps(first)
    with store._connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM customization_publication_requests"
        ).fetchone()["n"]
    assert count == 1


def test_independent_approval_then_publication_is_replay_safe(publication_service):
    service, store, staged, tenant = publication_service
    awaiting = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )
    issued = service.confirm(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    )

    published = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )
    replay = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert awaiting["status"] == "awaiting_approval"
    assert published == replay == {
        "contract": "leaf.customization.v1",
        "change_set_id": staged.change_set_id,
        "status": "published",
        "catalog_digest": DIGEST,
    }
    assert issued["confirmation_id"] not in json.dumps(published)
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.PUBLISHED


def test_retry_recovers_after_publish_was_prepared(publication_service, monkeypatch):
    service, store, staged, tenant = publication_service
    service.request_publication(tenant=tenant, change_set_id=staged.change_set_id)
    service.confirm(tenant_id="tenant-a", change_set_id=staged.change_set_id)
    attempts = []

    def interrupted(change):
        attempts.append(change.change_set_id)
        if len(attempts) == 1:
            raise CustomizationServiceError("customization_publish_incomplete", 503)
        return change.staged_commit

    monkeypatch.setattr(service, "_harness_publish", interrupted)

    with pytest.raises(
        CustomizationServiceError, match="customization_publish_incomplete"
    ):
        service.request_publication(tenant=tenant, change_set_id=staged.change_set_id)
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.PUBLISHING

    recovered = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert recovered["status"] == "published"
    assert len(attempts) == 2


def test_independent_denial_is_durable_and_publishes_nothing(
    publication_service, monkeypatch
):
    service, store, staged, tenant = publication_service
    service.request_publication(tenant=tenant, change_set_id=staged.change_set_id)

    denied = service.deny_publication(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    )
    monkeypatch.setattr(service, "_publication_policy_state", lambda _tid: (True, False))
    replay = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert denied == replay == {
        "contract": "leaf.customization.v1",
        "change_set_id": staged.change_set_id,
        "status": "denied",
    }
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.STAGED


def test_confirm_once_policy_still_requires_approval(publication_service, monkeypatch):
    service, _store, staged, tenant = publication_service
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: {
        "agent_disabled": False,
        "overlay": {"request_publication": {"policy": "confirm-once"}},
    })

    result = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert result["status"] == "awaiting_approval"


def test_later_confirmation_cannot_override_terminal_denial(
    publication_service, monkeypatch
):
    service, store, staged, tenant = publication_service
    service.request_publication(tenant=tenant, change_set_id=staged.change_set_id)
    service.deny_publication(tenant_id="tenant-a", change_set_id=staged.change_set_id)

    service.confirm(tenant_id="tenant-a", change_set_id=staged.change_set_id)
    monkeypatch.setattr(
        store,
        "find_unconsumed_confirmation",
        lambda **_kwargs: pytest.fail("denied continuation looked up a confirmation"),
    )
    monkeypatch.setattr(
        service,
        "_publish",
        lambda **_kwargs: pytest.fail("denied continuation reached publish"),
    )
    denied = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert denied["status"] == "denied"
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.STAGED


def test_disabled_publication_policy_blocks_before_confirmation_or_publish(
    publication_service, monkeypatch
):
    service, store, staged, tenant = publication_service
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: {
        "agent_disabled": False,
        "overlay": {"request_publication": {"enabled": False}},
    })
    monkeypatch.setattr(
        store,
        "find_unconsumed_confirmation",
        lambda **_kwargs: pytest.fail("disabled publication looked up a confirmation"),
    )
    monkeypatch.setattr(
        service,
        "_issue_automatic_publication_confirmation",
        lambda _change: pytest.fail("disabled publication auto-confirmed"),
    )
    monkeypatch.setattr(
        service,
        "_publish",
        lambda **_kwargs: pytest.fail("disabled publication reached publish"),
    )

    with pytest.raises(CustomizationServiceError, match="tool_publication_disabled"):
        service.request_publication(tenant=tenant, change_set_id=staged.change_set_id)

    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.STAGED


def test_strict_policy_ignores_existing_automatic_confirmation(
    publication_service, monkeypatch
):
    service, store, staged, tenant = publication_service
    automatic = service._issue_automatic_publication_confirmation(staged)
    monkeypatch.setattr(
        service,
        "_publish",
        lambda **_kwargs: pytest.fail("strict policy used an automatic receipt"),
    )

    result = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert result["status"] == "awaiting_approval"
    record = store.get_confirmation(confirmation_id=automatic["confirmation_id"])
    assert record is not None and record["consumed"] is False


def test_strict_toggle_preserves_prepared_automatic_publish_recovery(
    publication_service, monkeypatch
):
    service, store, staged, tenant = publication_service
    states = iter((
        {"agent_disabled": False, "overlay": {}},
        {
            "agent_disabled": False,
            "overlay": {
                "request_publication": {"policy": "always-confirm"},
            },
        },
    ))
    monkeypatch.setattr(agent_policy, "load_tenant_state", lambda _tid: next(states))
    attempts = []

    def interrupted(change):
        attempts.append(change.change_set_id)
        if len(attempts) == 1:
            raise CustomizationServiceError("customization_publish_incomplete", 503)
        return change.staged_commit

    monkeypatch.setattr(service, "_harness_publish", interrupted)
    with pytest.raises(
        CustomizationServiceError, match="customization_publish_incomplete"
    ):
        service.request_publication(tenant=tenant, change_set_id=staged.change_set_id)
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.PUBLISHING

    recovered = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert recovered["status"] == "published"
    assert len(attempts) == 2


def test_denial_revokes_approval_issued_before_it(publication_service):
    service, store, staged, tenant = publication_service
    service.request_publication(tenant=tenant, change_set_id=staged.change_set_id)
    service.confirm(tenant_id="tenant-a", change_set_id=staged.change_set_id)

    service.deny_publication(tenant_id="tenant-a", change_set_id=staged.change_set_id)
    denied = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert denied["status"] == "denied"
    assert store.find_unconsumed_confirmation(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ) is None
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.STAGED


def test_expired_confirmation_does_not_poison_publication(publication_service):
    service, store, staged, tenant = publication_service
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    store.put_confirmation(
        confirmation_id="expired-confirmation",
        payload={
            "tenant_id": "tenant-a",
            "change_set_id": staged.change_set_id,
            "expires_at": expired,
        },
        signature="expired-signature",
    )

    result = service.request_publication(
        tenant=tenant, change_set_id=staged.change_set_id
    )

    assert result["status"] == "awaiting_approval"
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.STAGED


def test_publication_request_is_tenant_scoped(publication_service):
    service, _store, staged, _tenant = publication_service
    other = deps.TenantContext("tenant-b", tier="hosted_pro")

    with pytest.raises(
        CustomizationServiceError, match="publication_request_not_available"
    ) as exc:
        service.request_publication(
            tenant=other, change_set_id=staged.change_set_id
        )

    assert exc.value.status_code == 404


def test_backedge_allows_only_publication_request_continuation():
    assert deps._dispatch_backedge_route(
        "POST", "/api/author/publication-requests"
    ) is True
    assert deps._dispatch_backedge_route("POST", "/api/author/register") is False
    assert deps._dispatch_backedge_route("POST", "/api/author/confirmations") is False
    assert deps._dispatch_backedge_route(
        "POST", "/internal/customization/confirm"
    ) is False
    assert deps._dispatch_backedge_route(
        "POST", "/internal/customization/deny"
    ) is False
