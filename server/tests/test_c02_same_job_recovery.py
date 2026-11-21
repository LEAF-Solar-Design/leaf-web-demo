"""C02 existing-ID recovery through the authenticated, read-only route."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from customization_models import ChangeState
from customization_models import ChangeSetConflictError
import customization_service
import deps
import turn_runner
from test_customization_stage_recovery import (
    MISS, RECOVER, MALLORY, _pinned_reads, _reserve, _snapshot, recovery,
)


def _retryable(recovery):
    change = _reserve(recovery)
    claimed = recovery.store.claim_stage(owner="c02-worker", lease_seconds=30)
    assert claimed.change_set_id == change.change_set_id
    return recovery.store.fail_stage_claim(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        owner="c02-worker", reason_code="author_job_failed", retryable=True,
        reason_message="Author worker unavailable.",
    )


def _retry_args(change):
    return dict(tenant_id=change.tenant_id, change_set_id=change.change_set_id,
                author_subject=change.author_subject, expected_version=change.version,
                expected_attempt=change.stage_attempt,
                authority_session_id="retry-session", authority_turn_id="retry-turn")


def test_concurrent_retry_archives_failure_and_claims_one_new_attempt(recovery):
    failed = _retryable(recovery)
    admit = Mock()
    def retry(_):
        return recovery.store.retry_stage(**_retry_args(failed), admit=admit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        queued = list(pool.map(retry, range(2)))
    assert [row.change_set_id for row in queued] == [failed.change_set_id] * 2
    assert [row.version for row in queued] == [failed.version + 1] * 2
    admit.assert_called_once_with()
    history = recovery.store.stage_failure_history(tenant_id=failed.tenant_id, change_set_id=failed.change_set_id)
    assert len(history) == 1
    assert history[0].attempt == failed.stage_attempt
    assert history[0].failed_version == failed.version
    assert history[0].reason_message == failed.stage_error_message
    assert history[0].reason_code == failed.stage_error_code
    assert history[0].retryable is True
    assert (history[0].authority_session_id, history[0].authority_turn_id) == (
        failed.authority_session_id, failed.authority_turn_id)
    assert (history[0].retry_authority_session_id, history[0].retry_authority_turn_id) == ("retry-session", "retry-turn")
    for field in ("request_description", "request_fingerprint", "idempotency_key", "base_commit", "author_subject"):
        assert getattr(queued[0], field) == getattr(failed, field)
    winner = recovery.store.claim_stage(owner="next-worker", lease_seconds=30)
    assert winner.stage_attempt == failed.stage_attempt + 1
    assert recovery.store.claim_stage(owner="other-worker", lease_seconds=30) is None
    assert recovery.store.stage_failure_history(tenant_id=failed.tenant_id, change_set_id=failed.change_set_id) == history


@pytest.mark.parametrize("invalid", ["version", "attempt", "old_authority", "nonretryable", "staged", "published"])
def test_retry_cas_and_state_refusals_leave_everything_unchanged(recovery, invalid):
    failed = _retryable(recovery)
    kwargs = _retry_args(failed)
    if invalid in {"version", "attempt"}:
        kwargs[f"expected_{invalid}"] += 1
    elif invalid == "old_authority":
        kwargs.update(authority_session_id=failed.authority_session_id, authority_turn_id=failed.authority_turn_id)
    else:
        with recovery.store._transaction() as conn:
            if invalid == "nonretryable":
                conn.execute("UPDATE customization_change_sets SET stage_error_retryable = ? WHERE change_set_id = ?", (False, failed.change_set_id))
            else:
                conn.execute("UPDATE customization_change_sets SET state = ? WHERE change_set_id = ?", (invalid, failed.change_set_id))
    before = _snapshot(recovery.store)
    admit = Mock()
    with pytest.raises(ChangeSetConflictError):
        recovery.store.retry_stage(**kwargs, admit=admit)
    admit.assert_not_called()
    assert _snapshot(recovery.store) == before


def test_failed_quota_admission_does_not_requeue_or_archive(recovery):
    failed = _retryable(recovery)
    before = _snapshot(recovery.store)
    with pytest.raises(RuntimeError, match="quota refusal"):
        recovery.store.retry_stage(**_retry_args(failed), admit=Mock(side_effect=RuntimeError("quota refusal")))
    assert _snapshot(recovery.store) == before


def test_explicit_route_retry_requires_fresh_active_authority_and_normal_quota(recovery, monkeypatch):
    failed = _retryable(recovery)
    request = dict(change_set_id=failed.change_set_id, retry=True,
                   expected_version=failed.version, expected_attempt=failed.stage_attempt)
    before = _snapshot(recovery.store)
    assert recovery.client.post(RECOVER, json=request).status_code == 409
    assert _snapshot(recovery.store) == before
    monkeypatch.setattr(deps, "stage_author_identity", lambda *args: recovery.tenant)
    monkeypatch.setattr(customization_service, "_harness_misconfigured", lambda: False)
    authority = Mock()
    monkeypatch.setattr(recovery.service, "_authority", lambda: authority)
    quota = Mock()
    monkeypatch.setattr(customization_service.author_quota, "enforce", quota)
    headers = {"X-Authority-Session-Id": "retry-session", "X-Authority-Turn-Id": "retry-turn"}
    response = recovery.client.post(RECOVER, json=request, headers=headers)
    assert response.status_code == 200
    assert response.json()["job"]["change_set_id"] == failed.change_set_id
    assert response.json()["job"]["status"] == "queued"
    assert authority.authorize_stage.call_count == 1
    quota.assert_called_once_with(failed.tenant_id, "hosted_pro",
        idempotency_key=f"stage-retry:{failed.change_set_id}:{failed.version}:{failed.stage_attempt}")
    assert recovery.client.post(RECOVER, json=request, headers=headers).status_code == 200
    assert quota.call_count == 1


@pytest.mark.parametrize("foreign", ["author", "tenant"])
def test_retry_of_foreign_request_is_not_found_without_mutation(recovery, foreign):
    failed = _retryable(recovery)
    recovery.identity(**({"subject": MALLORY} if foreign == "author" else {"tenant": "tenant-b"}))
    before = _snapshot(recovery.store)
    response = recovery.client.post(RECOVER, json=dict(change_set_id=failed.change_set_id, retry=True,
        expected_version=failed.version, expected_attempt=failed.stage_attempt))
    assert response.status_code == 404
    assert response.json() == MISS
    assert _snapshot(recovery.store) == before


def test_concurrent_authority_loser_is_released_without_closing_winning_turn(recovery, monkeypatch):
    failed = _retryable(recovery)
    monkeypatch.setattr(deps, "stage_author_identity", lambda *args: recovery.tenant)
    monkeypatch.setattr(customization_service, "_harness_misconfigured", lambda: False)
    monkeypatch.setattr(recovery.service, "_authority", lambda: Mock())
    monkeypatch.setattr(customization_service.author_quota, "enforce", Mock())
    finish = Mock()
    monkeypatch.setattr(turn_runner, "finish_stage_authority", finish)
    args = dict(tenant=recovery.tenant, change_set_id=failed.change_set_id,
                expected_version=failed.version, expected_attempt=failed.stage_attempt)
    first = recovery.service.retry_author_stage(**args, authority_session_id="winner", authority_turn_id="winner-turn")
    assert first["job"]["status"] == "queued"
    finish.assert_not_called()
    second = recovery.service.retry_author_stage(**args, authority_session_id="loser", authority_turn_id="loser-turn")
    assert second["job"]["change_set_id"] == failed.change_set_id
    finish.assert_called_once_with(failed.tenant_id, "loser", "loser-turn", failed.author_subject,
                                 stop_reason="stage_retry_not_admitted")


def test_ended_authority_cannot_retry_or_charge(recovery, monkeypatch):
    failed = _retryable(recovery)
    monkeypatch.setattr(deps, "stage_author_identity", lambda *args: None)
    quota = Mock()
    monkeypatch.setattr(customization_service.author_quota, "enforce", quota)
    before = _snapshot(recovery.store)
    response = recovery.client.post(RECOVER, json=dict(change_set_id=failed.change_set_id,
        retry=True, expected_version=failed.version, expected_attempt=failed.stage_attempt),
        headers={"X-Authority-Session-Id": "closed-session", "X-Authority-Turn-Id": "closed-turn"})
    assert response.status_code == 409
    assert _snapshot(recovery.store) == before
    quota.assert_not_called()


def test_existing_id_route_requires_authentication(recovery):
    failed = _retryable(recovery)
    recovery.app.dependency_overrides.pop(deps.require_tenant)
    before = _snapshot(recovery.store)
    response = recovery.client.post(RECOVER, json={"change_set_id": failed.change_set_id})
    assert response.status_code == 401
    assert _snapshot(recovery.store) == before


@pytest.mark.parametrize("state", [
    ChangeState.CREATED, ChangeState.STAGING, ChangeState.FAILED, ChangeState.STAGED,
])
def test_existing_id_lookup_is_read_only(recovery, monkeypatch, state):
    change = _reserve(recovery, state=state)
    if state is ChangeState.STAGED:
        _pinned_reads(monkeypatch, change.tenant_id)
    before = _snapshot(recovery.store)
    response = recovery.client.post(RECOVER, json={"change_set_id": change.change_set_id})
    assert response.status_code == (202 if state is ChangeState.CREATED else 200)
    body = response.json()
    if state is ChangeState.CREATED:
        assert body["status"] == "admission_pending"
    else:
        assert body["status"] == "found"
        assert body["job"]["change_set_id"] == change.change_set_id
        assert body["job"]["poll_url"] == f"/api/author/stages/{change.change_set_id}"
        assert body["job"]["attempt"] == change.stage_attempt
        assert body["job"]["status"] == {
            ChangeState.STAGING: "queued", ChangeState.FAILED: "failed", ChangeState.STAGED: "staged",
        }[state]
    assert _snapshot(recovery.store) == before


@pytest.mark.parametrize("foreign", ["author", "tenant", "missing"])
def test_existing_id_does_not_reveal_foreign_requests(recovery, foreign):
    change = _reserve(recovery, state=ChangeState.FAILED)
    if foreign == "author":
        recovery.identity(subject=MALLORY)
    elif foreign == "tenant":
        recovery.identity(tenant="tenant-b")
    target = str(uuid4()) if foreign == "missing" else change.change_set_id
    before = _snapshot(recovery.store)
    response = recovery.client.post(RECOVER, json={"change_set_id": target})
    assert response.status_code == 404
    assert response.json() == MISS
    assert _snapshot(recovery.store) == before


@pytest.mark.parametrize("body", [
    {}, {"change_set_id": "x" * 36}, {"change_set_id": 123},
    {"change_set_id": str(uuid4()), "idempotency_key": "both"},
    {"change_set_id": str(uuid4()), "retry": True},
])
def test_lookup_refuses_ambiguous_invalid_or_mutating_input(recovery, body):
    before = _snapshot(recovery.store)
    response = recovery.client.post(RECOVER, json=body)
    assert response.status_code == 422
    assert _snapshot(recovery.store) == before


@pytest.mark.parametrize("role,tier", [("viewer", "hosted_pro"), ("owner", "free")])
def test_existing_id_requires_builder_and_role(recovery, role, tier):
    change = _reserve(recovery)
    recovery.identity(role=role, tier=tier)
    before = _snapshot(recovery.store)
    response = recovery.client.post(RECOVER, json={"change_set_id": change.change_set_id})
    assert response.status_code == 403
    assert "job" not in response.json()
    assert _snapshot(recovery.store) == before
