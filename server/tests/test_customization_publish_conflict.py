"""A losing or stale publish ends terminal instead of wedging the tenant (M1 risk R1).

The fake harness below keeps the real publish contract: main moves only from the
receipt's expected head, and a lost CAS answers 409 {"error": "publish_conflict"}.
"""
from __future__ import annotations

import pytest
import requests

import deps
from customization_models import ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore


TENANT = "tenant-a"
BASE = "a" * 40
A_COMMIT = "1" * 40
B_COMMIT = "2" * 40
C_COMMIT = "3" * 40
D_COMMIT = "4" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64
RELEASE = "release-a"


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}", response=self)


class FakeHarness:
    """POST /author/publish with the harness's expected-head CAS on main."""

    def __init__(self, main):
        self.main = main
        self.calls = []
        self.answer = None  # (status, error) returned instead of publishing

    def post(self, url, *, timeout, headers, json):
        assert url.endswith("/author/publish")
        receipt = json["receipt"]
        self.calls.append(receipt["change_set_id"])
        if self.answer is not None:
            status, error = self.answer
            return _Response(status, {"error": error})
        staged = receipt["staged_commit"]
        if self.main not in (json["expectedMainSha"], staged):
            return _Response(409, {"error": "publish_conflict"})
        self.main = staged
        return _Response(200, {"commit": staged})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    for name, value in {
        "LEAF_AUTH_LIVE": "1",
        "LEAF_CUSTOMIZATION_R5_MODE": "all",
        "LEAF_CUSTOMIZATION_R6_MODE": "all",
        "LEAF_CUSTOMIZATION_CONFIRMATION_KEY": "test-confirmation-key",
        "LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT": "staff|reviewer",
        "LEAF_AUTHOR_HARNESS_URL": "http://harness.internal:8150",
        "LEAF_HARNESS_SECRET": "test-harness-secret",
    }.items():
        monkeypatch.setenv(name, value)
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    service = CustomizationService(store)
    fake = FakeHarness(BASE)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(service, "_verify_catalog", lambda *args: None)
    # Approval off: each request auto-confirms the exact receipt.
    monkeypatch.setattr(service, "_publication_policy_state", lambda _tid: (True, False))
    tenant = deps.TenantContext(TENANT, tier="hosted_pro")
    return service, store, fake, tenant


def _staged(store, suffix, *, base, staged, kind="create", target=None):
    created = store.create_change_set(
        tenant_id=TENANT, idempotency_key=f"author-{suffix}", base_commit=base,
        desired_platform_release=RELEASE, workspace_contract_digest=WORKSPACE,
        author_subject=f"auth0|author-{suffix}", change_kind=kind,
        target_tool_name=target,
    )
    staging = store.transition(
        tenant_id=TENANT, change_set_id=created.change_set_id,
        next_state=ChangeState.STAGING, expected_version=created.version,
        idempotency_key=f"staging-{suffix}",
    )
    return store.record_staged(
        tenant_id=TENANT, change_set_id=created.change_set_id,
        expected_version=staging.version, idempotency_key=f"staged-{suffix}",
        staged_commit=staged, catalog_digest=DIGEST, platform_release=RELEASE,
        workspace_contract_digest=WORKSPACE,
    )


def _state(store, change):
    return store.get_change_set(
        tenant_id=TENANT, change_set_id=change.change_set_id
    ).state


def _superseded_events(store, change):
    return [
        event for event in store.audit_events(
            tenant_id=TENANT, change_set_id=change.change_set_id
        )
        if event.next_state is ChangeState.SUPERSEDED
    ]


def test_losing_publish_ends_superseded_and_the_tenant_keeps_publishing(harness):
    service, store, fake, tenant = harness
    a = _staged(store, "a", base=BASE, staged=A_COMMIT)
    # A revision on the same base reaches the harness, whose CAS it loses.
    b = _staged(store, "b", base=BASE, staged=B_COMMIT,
                kind="revise", target="panel-count")

    assert service.request_publication(
        tenant=tenant, change_set_id=a.change_set_id
    )["status"] == "published"

    with pytest.raises(CustomizationServiceError) as caught:
        service.request_publication(tenant=tenant, change_set_id=b.change_set_id)
    assert (caught.value.code, caught.value.status_code) == ("publish_conflict", 409)
    assert _state(store, b) is ChangeState.SUPERSEDED
    assert store.recovery_candidates(tenant_id=TENANT) == []
    [event] = _superseded_events(store, b)
    assert event.prior_state is ChangeState.PUBLISHING
    assert (event.result, event.reason_code) == ("failed", "harness_publish_conflict")
    assert fake.main == A_COMMIT

    # A repeat is a precise terminal answer and never calls the harness again.
    calls = len(fake.calls)
    with pytest.raises(CustomizationServiceError) as again:
        service.request_publication(tenant=tenant, change_set_id=b.change_set_id)
    assert (again.value.code, again.value.status_code) == ("publish_superseded", 409)
    assert len(fake.calls) == calls

    # The single publish slot is free: a third change still publishes.
    c = _staged(store, "c", base=A_COMMIT, staged=C_COMMIT)
    assert service.request_publication(
        tenant=tenant, change_set_id=c.change_set_id
    )["status"] == "published"
    assert store.get_effective_catalog(tenant_id=TENANT).catalog_commit == C_COMMIT
    assert fake.main == C_COMMIT


def test_remote_not_accepting_an_addition_ends_superseded(harness):
    service, store, fake, tenant = harness
    a = _staged(store, "a", base=BASE, staged=A_COMMIT)
    fake.answer = (409, "publish_not_accepted")

    with pytest.raises(CustomizationServiceError) as caught:
        service.request_publication(tenant=tenant, change_set_id=a.change_set_id)
    assert (caught.value.code, caught.value.status_code) == ("publish_not_accepted", 409)
    assert _state(store, a) is ChangeState.SUPERSEDED
    [event] = _superseded_events(store, a)
    assert event.reason_code == "harness_publish_not_accepted"

    fake.answer = None
    c = _staged(store, "c", base=BASE, staged=C_COMMIT)
    assert service.request_publication(
        tenant=tenant, change_set_id=c.change_set_id
    )["status"] == "published"


def test_stale_base_addition_is_refused_before_the_confirmation_is_consumed(
    harness, monkeypatch
):
    service, store, fake, tenant = harness
    a = _staged(store, "a", base=BASE, staged=A_COMMIT)
    service.request_publication(tenant=tenant, change_set_id=a.change_set_id)
    stale = _staged(store, "stale", base=BASE, staged=D_COMMIT)

    monkeypatch.setattr(service, "_publication_policy_state", lambda _tid: (True, True))
    issued = service.confirm(tenant_id=TENANT, change_set_id=stale.change_set_id)
    calls = len(fake.calls)

    with pytest.raises(CustomizationServiceError) as caught:
        service.request_publication(tenant=tenant, change_set_id=stale.change_set_id)
    assert (caught.value.code, caught.value.status_code) == ("stale_base", 409)
    assert _state(store, stale) is ChangeState.STAGED
    record = store.find_unconsumed_confirmation(
        tenant_id=TENANT, change_set_id=stale.change_set_id
    )
    assert record is not None
    assert record["confirmation_id"] == issued["confirmation_id"]
    assert len(fake.calls) == calls
    assert store.recovery_candidates(tenant_id=TENANT) == []
    assert store.get_effective_catalog(tenant_id=TENANT).catalog_commit == A_COMMIT


@pytest.mark.parametrize("status, error", [
    (503, "publish_outcome_unknown"),
    # An unrecognised 409 body is not proof of a loss: it stays recoverable.
    (409, {"message": "legacy error shape"}),
])
def test_unknown_outcome_stays_publishing_and_recovers(harness, status, error):
    service, store, fake, tenant = harness
    a = _staged(store, "a", base=BASE, staged=A_COMMIT)
    fake.answer = (status, error)

    with pytest.raises(CustomizationServiceError) as caught:
        service.request_publication(tenant=tenant, change_set_id=a.change_set_id)
    assert (caught.value.code, caught.value.status_code) == (
        "customization_publish_incomplete", 503
    )
    assert _state(store, a) is ChangeState.PUBLISHING
    assert [c.change_set_id for c in store.recovery_candidates(tenant_id=TENANT)] == [
        a.change_set_id
    ]
    assert _superseded_events(store, a) == []

    fake.answer = None
    assert service.request_publication(
        tenant=tenant, change_set_id=a.change_set_id
    )["status"] == "published"
    assert store.get_effective_catalog(tenant_id=TENANT).catalog_commit == A_COMMIT
