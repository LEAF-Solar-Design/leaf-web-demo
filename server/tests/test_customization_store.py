from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from customization_models import (
    ChangeSetConflictError,
    ChangeSetNotFoundError,
    ChangeState,
    IdempotencyReplayError,
    InvalidTransitionError,
)
from customization_store import SQLiteCustomizationStore


BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64


@pytest.fixture
def store(tmp_path):
    result = SQLiteCustomizationStore(tmp_path / "customization.db")
    result.initialize()
    result.initialize()
    return result


def create(store, tenant="tenant-a", key="create-1"):
    return store.create_change_set(
        tenant_id=tenant, idempotency_key=key, base_commit=BASE,
        desired_platform_release="platform@sha256:abc", workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author",
    )


def advance_to_publishing(store, change, key_prefix=""):
    staging = store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING, expected_version=change.version, idempotency_key=f"{key_prefix}staging",
    )
    staged = store.record_staged(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        expected_version=staging.version, idempotency_key=f"{key_prefix}staged", staged_commit=STAGED,
        catalog_digest=DIGEST, platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE,
    )
    awaiting = store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.AWAITING_APPROVAL, expected_version=staged.version,
        idempotency_key=f"{key_prefix}awaiting",
    )
    approved = store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.APPROVED, expected_version=awaiting.version,
        idempotency_key=f"{key_prefix}approved", approver_subject="auth0|approver",
    )
    return store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.PUBLISHING, expected_version=approved.version,
        idempotency_key=f"{key_prefix}publishing",
    )


def test_create_is_idempotent_and_replay_with_new_intent_is_rejected(store):
    first = create(store)
    replay = create(store)
    assert replay.change_set_id == first.change_set_id
    with pytest.raises(IdempotencyReplayError):
        store.create_change_set(
            tenant_id="tenant-a", idempotency_key="create-1", base_commit="e" * 40,
            desired_platform_release="platform@sha256:abc", workspace_contract_digest=WORKSPACE,
            author_subject="auth0|author",
        )


def test_revision_binding_is_durable_and_part_of_idempotency(store):
    revised = store.create_change_set(
        tenant_id="tenant-a", idempotency_key="revise-1", base_commit=BASE,
        desired_platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE, author_subject="auth0|author",
        change_kind="revise", target_tool_name="drape-onto-spheres",
    )
    loaded = store.get_change_set(
        tenant_id="tenant-a", change_set_id=revised.change_set_id
    )
    assert (loaded.change_kind, loaded.target_tool_name) == (
        "revise", "drape-onto-spheres"
    )
    with pytest.raises(IdempotencyReplayError):
        store.create_change_set(
            tenant_id="tenant-a", idempotency_key="revise-1", base_commit=BASE,
            desired_platform_release="platform@sha256:abc",
            workspace_contract_digest=WORKSPACE, author_subject="auth0|author",
        )


def test_illegal_transition_is_rejected_without_state_change(store):
    change = create(store)
    with pytest.raises(InvalidTransitionError):
        store.transition(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id,
            next_state=ChangeState.STAGED, expected_state=ChangeState.CREATED,
            expected_version=change.version, idempotency_key="skip",
        )
    assert store.get_change_set(tenant_id="tenant-a", change_set_id=change.change_set_id).state is ChangeState.CREATED


def test_optimistic_race_allows_one_writer(store):
    change = create(store)

    def attempt(key):
        return store.transition(
            tenant_id="tenant-a", change_set_id=change.change_set_id,
            next_state=ChangeState.STAGING, expected_version=0, idempotency_key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda key: _outcome(attempt, key), ("race-a", "race-b")))
    assert sum(value == "ok" for value in outcomes) == 1
    assert sum(value == "conflict" for value in outcomes) == 1


def _outcome(operation, key):
    try:
        operation(key)
        return "ok"
    except ChangeSetConflictError:
        return "conflict"


def test_publish_flips_pointer_and_appends_audit_in_one_transaction(store):
    publishing = advance_to_publishing(store, create(store))
    effective = store.publish(
        tenant_id="tenant-a", change_set_id=publishing.change_set_id,
        expected_version=publishing.version, idempotency_key="published",
    )
    assert effective.catalog_commit == STAGED
    assert effective.catalog_digest == DIGEST
    events = store.audit_events(tenant_id="tenant-a", change_set_id=publishing.change_set_id)
    assert events[-1].next_state is ChangeState.PUBLISHED
    assert events[-1].ts
    with store._connection() as conn:
        payload = conn.execute(
            "SELECT payload_json FROM customization_audit_events WHERE event_id = ?", (events[-1].event_id,)
        ).fetchone()[0]
    assert "prompt" not in payload


def test_removal_binding_refuses_stale_digest_and_pointer_drift(store):
    predecessor = advance_to_publishing(store, create(store))
    before = store.publish(
        tenant_id="tenant-a", change_set_id=predecessor.change_set_id,
        expected_version=predecessor.version, idempotency_key="publish-before",
    )
    removal = store.create_change_set(
        tenant_id="tenant-a", idempotency_key="remove-one",
        base_commit=before.catalog_commit,
        desired_platform_release=before.effective_platform_release,
        workspace_contract_digest=before.workspace_contract_digest,
        author_subject="auth0|author", change_kind="revise",
        target_tool_name="count-by-layer",
    )
    with pytest.raises(ChangeSetConflictError, match="digest changed"):
        store.bind_removal_request(
            tenant_id="tenant-a", change_set_id=removal.change_set_id,
            target_tool_name="count-by-layer", expected_catalog_digest="0" * 64,
        )
    store.bind_removal_request(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        target_tool_name="count-by-layer",
        expected_catalog_digest=before.catalog_digest,
    )
    successor = advance_to_publishing(
        store, create(store, key="intervening"), "intervening-"
    )
    store.publish(
        tenant_id="tenant-a", change_set_id=successor.change_set_id,
        expected_version=successor.version, idempotency_key="publish-intervening",
    )
    with pytest.raises(ChangeSetConflictError, match="changed before removal"):
        store.verify_removal_predecessor(
            tenant_id="tenant-a", change_set_id=removal.change_set_id
        )
    assert store.get_effective_catalog(tenant_id="tenant-a").change_set_id == successor.change_set_id


def test_removal_publish_can_restore_exact_predecessor(store):
    predecessor = advance_to_publishing(store, create(store))
    before = store.publish(
        tenant_id="tenant-a", change_set_id=predecessor.change_set_id,
        expected_version=predecessor.version, idempotency_key="publish-before",
    )
    removal = store.create_change_set(
        tenant_id="tenant-a", idempotency_key="remove-one",
        base_commit=before.catalog_commit,
        desired_platform_release=before.effective_platform_release,
        workspace_contract_digest=before.workspace_contract_digest,
        author_subject="auth0|author", change_kind="revise",
        target_tool_name="count-by-layer",
    )
    store.bind_removal_request(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        target_tool_name="count-by-layer",
        expected_catalog_digest=before.catalog_digest,
    )
    staging = store.transition(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        next_state=ChangeState.STAGING, expected_version=removal.version,
        idempotency_key="remove-staging",
    )
    staged = store.record_staged(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        expected_version=staging.version, idempotency_key="remove-staged",
        staged_commit="e" * 40, catalog_digest="f" * 64,
        platform_release=before.effective_platform_release,
        workspace_contract_digest=before.workspace_contract_digest,
    )
    awaiting = store.transition(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        next_state=ChangeState.AWAITING_APPROVAL, expected_version=staged.version,
        idempotency_key="remove-awaiting",
    )
    approved = store.transition(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        next_state=ChangeState.APPROVED, expected_version=awaiting.version,
        idempotency_key="remove-approved", approver_subject="auth0|approver",
    )
    publishing = store.transition(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        next_state=ChangeState.PUBLISHING, expected_version=approved.version,
        idempotency_key="remove-publishing",
    )
    after = store.publish(
        tenant_id="tenant-a", change_set_id=removal.change_set_id,
        expected_version=publishing.version, idempotency_key="remove-published",
    )
    assert after.change_set_id == removal.change_set_id
    restored = store.restore_effective_catalog(
        tenant_id="tenant-a", target_change_set_id=predecessor.change_set_id,
        prior_change_set_id=removal.change_set_id,
        idempotency_key="restore-predecessor",
    )
    assert (
        restored.change_set_id, restored.catalog_commit,
        restored.catalog_digest, restored.effective_platform_release,
        restored.workspace_contract_digest,
    ) == (
        before.change_set_id, before.catalog_commit,
        before.catalog_digest, before.effective_platform_release,
        before.workspace_contract_digest,
    )


def test_tenant_isolation_and_effective_pointer(store):
    a = advance_to_publishing(store, create(store, "tenant-a", "a-create"), "a-")
    b = advance_to_publishing(store, create(store, "tenant-b", "b-create"), "b-")
    store.publish(tenant_id="tenant-a", change_set_id=a.change_set_id, expected_version=a.version, idempotency_key="a-publish")
    store.publish(tenant_id="tenant-b", change_set_id=b.change_set_id, expected_version=b.version, idempotency_key="b-publish")
    assert store.get_effective_catalog(tenant_id="tenant-a").change_set_id == a.change_set_id
    assert store.get_effective_catalog(tenant_id="tenant-b").change_set_id == b.change_set_id
    with pytest.raises(ChangeSetNotFoundError):
        store.get_change_set(tenant_id="tenant-b", change_set_id=a.change_set_id)


def test_recovery_query_returns_only_interrupted_rows(store):
    staging = store.transition(
        tenant_id="tenant-a", change_set_id=create(store).change_set_id,
        next_state=ChangeState.STAGING, expected_version=0, idempotency_key="staging",
    )
    publishing = advance_to_publishing(store, create(store, key="create-2"), "second-")
    stranded = store.recovery_candidates()
    assert {row.change_set_id for row in stranded} == {staging.change_set_id, publishing.change_set_id}


def test_published_change_can_only_roll_back(store):
    publishing = advance_to_publishing(store, create(store))
    store.publish(
        tenant_id="tenant-a", change_set_id=publishing.change_set_id,
        expected_version=publishing.version, idempotency_key="published",
    )
    published = store.get_change_set(
        tenant_id="tenant-a", change_set_id=publishing.change_set_id,
    )
    with pytest.raises(InvalidTransitionError):
        store.transition(
            tenant_id="tenant-a", change_set_id=published.change_set_id,
            next_state=ChangeState.FAILED, expected_version=published.version,
            idempotency_key="late-failure",
        )


def test_contract_string_limits_are_enforced(store):
    with pytest.raises(ValueError):
        create(store, tenant="t" * 201)
    change = create(store)
    with pytest.raises(ValueError):
        store.transition(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id,
            next_state=ChangeState.STAGING, expected_version=change.version,
            idempotency_key="i" * 201,
        )
