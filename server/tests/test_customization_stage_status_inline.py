"""Poll the durable end-of-stage history and the bounded inline card fields."""
import json
import logging
import sqlite3
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

import build_plan
import author_quota
import customization_service
import customization_stage_worker
import deps
from branch_tools import tenant_publish
from customization_models import ChangeSetNotFoundError, ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from psycopg import Error as PostgresError
from routers import author as author_router
from test_author_stage_status import FOREIGN, PATH, _headers, _seed, status_api
from test_build_plan import minimal
from test_customization_async_stage import (
    DESCRIPTION, _HarnessErrorResponse, _admission_service, bind_removal_marker, queued,
)
from tool_record_fields import ToolRecordFieldError
from test_customization_branch_target import _change, _request, lane
from test_customization_inline_review import review
from test_tenant_publish import NAME, publishing, signer


LOCATION = {"code": "CS0117", "file": "lib/example.csx", "line": 42}
REFUSAL = {"n": 1, "decision": "compile_failed", "errors": 1, "diagnostics": [LOCATION]}
ACCEPT = {"n": 2, "decision": "accept", "errors": 0, "diagnostics": []}
DECODED = {"entities": 2, "original_sha256": "a" * 64, "compiled_sha256": "b" * 64}


def _reserve(tmp_path, **fields):
    store = SQLiteCustomizationStore(tmp_path / "status.db")
    change, _ = store.reserve_stage(
        tenant_id="tenant-a", author_subject="author", idempotency_key="stage",
        base_commit="a" * 40, workspace_contract_digest="b" * 64,
        desired_platform_release="release", request_description="build",
        request_fingerprint="c" * 64, **fields,
    )
    change = store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id, expected_version=change.version,
        next_state=ChangeState.STAGING, idempotency_key="staging",
    )
    return CustomizationService(store), change


@pytest.mark.parametrize("callback_first", [False, True])
def test_completed_branch_attempts_survive_worker_and_callback(lane, monkeypatch, callback_first):
    post = requests.post

    def stage(url, **kwargs):
        response = post(url, **kwargs)
        body = response.json()
        body["compile_attempts"] = [REFUSAL, ACCEPT]
        if callback_first:
            lane.service.record_staged_callback(tenant_id="tenant-a", receipt=body["receipt"])
        return response

    monkeypatch.setattr(requests, "post", stage)
    job = lane.service.enqueue_stage(**_request(lane))
    assert "compile" not in job
    assert customization_stage_worker.run_once("worker", service=lane.service)
    change = _change(lane, job)
    assert json.loads(change.compile_summary_json) == [REFUSAL, ACCEPT]
    status = lane.service.stage_status(tenant=lane.tenant, change_set_id=change.change_set_id)
    assert status["status"] == "staged"
    assert status["compile"] == {"attempts": [REFUSAL, ACCEPT]}
    assert "compile_attempts" not in status["receipt"]


@pytest.mark.parametrize("lose_response", [True, False])
def test_staged_callback_commits_attempts_with_receipt(lane, monkeypatch, lose_response):
    post = requests.post
    record_staged = lane.store.record_staged
    staged_rows = []

    def observe_staged(**kwargs):
        row = record_staged(**kwargs)
        staged_rows.append(row)
        return row

    def stage(url, **kwargs):
        response = post(url, **kwargs)
        body = response.json()
        lane.service.record_staged_callback(
            tenant_id="tenant-a", receipt={**body["receipt"], "compile_attempts": [REFUSAL, ACCEPT]},
        )
        if lose_response:
            raise requests.ConnectionError("stage response lost")
        body["compile_attempts"] = []
        return response

    monkeypatch.setattr(lane.store, "record_staged", observe_staged)
    monkeypatch.setattr(requests, "post", stage)
    job = lane.service.enqueue_stage(**_request(lane))
    assert customization_stage_worker.run_once("worker", service=lane.service)
    staged, = staged_rows
    assert staged.state is ChangeState.STAGED
    assert json.loads(staged.compile_summary_json) == [REFUSAL, ACCEPT]
    status = lane.service.stage_status_change(_change(lane, job))
    assert status["compile"] == {"attempts": [REFUSAL, ACCEPT]}
    assert status["receipt"] == lane.bodies[-1]["receipt"]


def test_malformed_callback_attempts_drop_only_the_history(lane, caplog):
    job = lane.service.enqueue_stage(**_request(lane))
    body = lane.service.dispatch_stage(_change(lane, job))
    lane.service.record_staged_callback(
        tenant_id="tenant-a", receipt={**body["receipt"], "compile_attempts": [dict(REFUSAL, message="private")]},
    )
    status = lane.service.stage_status_change(_change(lane, job))
    assert status["status"] == "staged"
    assert status["receipt"] == body["receipt"]
    assert "compile" not in status
    assert "invalid_compile_attempts" in caplog.messages
    assert "private" not in caplog.text


def test_failed_branch_persists_only_locations(lane, monkeypatch):
    job = lane.service.enqueue_stage(**_request(lane))
    response = _HarnessErrorResponse(422, {"error": {
        "message": "Branch author package refused",
        "reason": {"code": "branch_author_package_refused", "turns": 2, "submit_calls": 1, "rule": "V18"},
        "compile": {"diagnostics": [{"message": "private compiler text"}], "compile_attempts": [REFUSAL]},
    }})
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    assert customization_stage_worker.run_once("worker", service=lane.service)
    change = _change(lane, job)
    status = lane.service.stage_status_change(change)
    assert status["status"] == "failed"
    assert json.loads(change.compile_summary_json) == status["compile"]["attempts"] == [REFUSAL]
    assert "private compiler text" not in json.dumps(status)


@pytest.mark.parametrize("bad", [
    None, {}, [REFUSAL] * 9, ["text"], [{}], [dict(REFUSAL, message="private")],
    [dict(REFUSAL, n=0)], [dict(REFUSAL, n=9)], [dict(REFUSAL, n=True)],
    [REFUSAL, REFUSAL], [ACCEPT, REFUSAL], [dict(REFUSAL, decision="pass")],
    [dict(REFUSAL, decision={})], [dict(REFUSAL, errors=-1)], [dict(REFUSAL, errors=10001)],
    [dict(REFUSAL, errors=False)], [dict(REFUSAL, errors=1.5)], [dict(REFUSAL, diagnostics={})],
    [dict(REFUSAL, diagnostics=[LOCATION] * 11)],
])
def test_invalid_attempt_drops_entire_field(tmp_path, caplog, bad):
    service, change = _reserve(tmp_path, tool_target="branch")
    service._record_compile_attempts(change, {"compile_attempts": bad}, staged_commit=None)
    durable = service.store.get_change_set(tenant_id=change.tenant_id, change_set_id=change.change_set_id)
    assert durable.compile_summary_json is None
    assert "invalid_compile_attempts" in caplog.messages
    assert "private" not in caplog.text


@pytest.mark.parametrize("bad", [
    None, {}, dict(LOCATION, message="private"), dict(LOCATION, code="CS111"), dict(LOCATION, code=7),
    dict(LOCATION, code="CS0001\n"), dict(LOCATION, file=""), dict(LOCATION, file="a" * 121),
    dict(LOCATION, file="lib/a b"), dict(LOCATION, file=None), dict(LOCATION, line=-1),
    dict(LOCATION, line=1000001), dict(LOCATION, line=True),
])
def test_invalid_location_also_drops_previous_valid_attempt(tmp_path, caplog, bad):
    service, change = _reserve(tmp_path, tool_target="branch")
    attempts = [REFUSAL, dict(ACCEPT, diagnostics=[LOCATION, bad])]
    service._record_compile_attempts(change, {"compile_attempts": attempts}, staged_commit=None)
    durable = service.store.get_change_set(tenant_id=change.tenant_id, change_set_id=change.change_set_id)
    assert durable.compile_summary_json is None
    assert "invalid_compile_attempts" in caplog.messages


def test_legacy_stage_retains_exact_keys(tmp_path):
    service, change = _reserve(tmp_path)
    assert set(service.stage_status_change(change)) == {
        "contract", "change_set_id", "status", "change_kind", "attempt",
        "phase", "updated_at", "poll_url", "retry_after_ms",
    }


@pytest.mark.parametrize("attempts", [[], [
    {"n": n, "decision": "malformed", "errors": 10000,
     "diagnostics": [{"code": "unparsed", "file": "a" * 120, "line": 1000000}] * 10}
    for n in range(1, 9)
]])
def test_valid_limits_are_visible_only_after_completion(tmp_path, attempts):
    service, change = _reserve(tmp_path, tool_target="branch")
    service._record_compile_attempts(change, {"compile_attempts": attempts}, staged_commit=None)
    key = {"tenant_id": change.tenant_id, "change_set_id": change.change_set_id}
    assert "compile" not in service.stage_status_change(service.store.get_change_set(**key))
    failed = service.store.fail_stage_claim(**key, owner=None, reason_code="test_failure", retryable=True)
    assert service.stage_status_change(failed)["compile"] == {"attempts": attempts}


def test_retry_discards_prior_history_and_ignores_late_response(tmp_path):
    service, change = _reserve(tmp_path, tool_target="branch")
    claimed = service.store.claim_stage(owner="worker", lease_seconds=30)
    key = {"tenant_id": change.tenant_id, "change_set_id": change.change_set_id}
    service._record_compile_attempts(claimed, {"compile_attempts": [REFUSAL]}, staged_commit=None)
    failed = service.store.fail_stage_claim(**key, owner="worker", reason_code="test_failure", retryable=True)
    retried = service.store.retry_stage(
        **key, author_subject=change.author_subject, expected_version=failed.version,
        expected_attempt=failed.stage_attempt, authority_session_id="session", authority_turn_id="retry",
        admit=lambda: None,
    )
    assert retried.compile_summary_json is None
    service._record_compile_attempts(claimed, {"compile_attempts": [REFUSAL]}, staged_commit=None)
    assert service.store.get_change_set(**key).compile_summary_json is None


def test_status_projects_reserved_plan(tmp_path):
    plan = minimal()
    digest = build_plan.plan_digest(plan)
    service, change = _reserve(tmp_path, tool_target="branch", plan_json=build_plan.canonical_bytes(plan).decode(),
                               plan_digest=digest, plan_confirmation_id="plan-approval")
    status = service.stage_status_change(change)
    assert status["plan"] == {
        "title": plan["title"], "digest": digest, "target": "branch",
        "tool_name": plan["tool_name"], "change_kind": "create",
    }
    assert not {"compile", "review", "publication"}.intersection(status)


@pytest.mark.parametrize("decision", ["approve", "request_changes"])
def test_review_and_publication_follow_durable_decisions(review, decision):
    def status():
        return review.service.stage_status_change(_change(review, review.body))

    assert not {"plan", "compile", "review", "publication"}.intersection(status())
    review.store.get_or_create_publication_request(tenant_id="tenant-a", change_set_id=review.change.change_set_id)
    assert status()["publication"] == {"state": "awaiting_approval"}
    assert "review" not in status()
    response = review.post(decision=decision)
    assert response.status_code == 200, response.text
    row = review.store.get_review_decision(**review.key)
    result = status()
    assert result["review"] == {key: row[key] for key in ("decision", "approval_kind", "approver_role", "decided_at")}
    expected = {"state": response.json()["status"]}
    if decision == "approve":
        expected["published_at"] = _change(review, review.body).updated_at
        published = review.service._published_branch_result(_change(review, review.body))
        expected.update({key: published[key] for key in ("package_digest", "name", "version")})
    assert result["publication"] == expected


def test_status_route_scopes_the_review_to_its_tenant(status_api):
    change = _seed(status_api, "staged")
    decided_at = status_api.store.record_review_decision(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        staged_commit=change.staged_commit, catalog_digest=change.catalog_digest,
        decision="request_changes", approval_kind="independent", approver_role="reviewer",
        approver_subject="reviewer-subject", comment="private review comment", confirmation_id=None,
    )
    response = status_api.client.get(PATH, headers=_headers())
    assert response.status_code == 200, response.text
    assert response.json()["review"] == {
        "decision": "request_changes", "approval_kind": "independent",
        "approver_role": "reviewer", "decided_at": decided_at,
    }
    assert "publication" not in response.json()
    assert status_api.client.get(PATH, headers=_headers(FOREIGN)).status_code == 404


@pytest.fixture
def queue_admission(tmp_path, monkeypatch):
    store = SQLiteCustomizationStore(tmp_path / "queue.db")
    service, tenant = _admission_service(store, monkeypatch)
    monkeypatch.delenv("LEAF_CUSTOMIZATION_STAGE_MAX_QUEUED", raising=False)
    monkeypatch.delenv("LEAF_CUSTOMIZATION_STAGE_QUEUE_STATUS", raising=False)
    charge = Mock()
    monkeypatch.setattr(author_quota, "enforce", charge)
    request = dict(tenant=tenant, description=DESCRIPTION, mode="build", idempotency_key="new")
    return service, request, charge


def _assert_no_queue_reservation(service, request):
    key = service._author_stage_key("tenant-a", request["idempotency_key"], "auth0|author")
    with pytest.raises(ChangeSetNotFoundError):
        service.store.get_change_set_by_idempotency(tenant_id="tenant-a", idempotency_key=key)


def test_queue_cap_refuses_before_charge_and_reservation(queue_admission, monkeypatch):
    service, request, charge = queue_admission
    queued(service.store, tenant="other-tenant")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_MAX_QUEUED", "1")
    with pytest.raises(CustomizationServiceError) as caught:
        service.enqueue_stage(**request)
    assert (caught.value.code, caught.value.status_code) == ("customization_stage_queue_full", 503)
    charge.assert_not_called()
    _assert_no_queue_reservation(service, request)


def test_queue_cap_allows_exact_replay_without_another_charge(queue_admission, monkeypatch):
    service, request, charge = queue_admission
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_MAX_QUEUED", "1")
    original = service.enqueue_stage(**request)
    assert service.store.stage_queue_depth() == 1
    monkeypatch.setattr(service.store, "stage_queue_depth", Mock(side_effect=AssertionError("replay depth read")))
    assert service.enqueue_stage(**request) == original
    assert charge.call_count == 1


@pytest.mark.parametrize("limit", [None, "0", "-1", "x", "10001", "١", "+1"])
def test_disabled_or_invalid_queue_cap_makes_no_depth_read(queue_admission, monkeypatch, limit):
    service, request, charge = queue_admission
    if limit is not None:
        monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_MAX_QUEUED", limit)
    depth = Mock(side_effect=AssertionError("disabled cap read"))
    monkeypatch.setattr(service.store, "stage_queue_depth", depth)
    assert service.enqueue_stage(**request)["status"] == "queued"
    depth.assert_not_called()
    assert charge.call_count == 1


def test_queue_one_below_cap_is_admitted(queue_admission, monkeypatch):
    service, request, charge = queue_admission
    queued(service.store, tenant="other-tenant")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_MAX_QUEUED", " 2 ")
    assert service.enqueue_stage(**request)["status"] == "queued"
    assert service.store.stage_queue_depth() == 2
    assert charge.call_count == 1


@pytest.mark.parametrize("failure", [sqlite3.DatabaseError, PostgresError, OSError, RuntimeError])
def test_queue_admission_read_failure_is_closed(queue_admission, monkeypatch, failure):
    service, request, charge = queue_admission
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_MAX_QUEUED", "1")
    monkeypatch.setattr(service.store, "stage_queue_depth", Mock(side_effect=failure("unavailable")))
    with pytest.raises(CustomizationServiceError) as caught:
        service.enqueue_stage(**request)
    assert (caught.value.code, caught.value.status_code) == ("customization_stage_queue_unavailable", 503)
    charge.assert_not_called()
    _assert_no_queue_reservation(service, request)


def test_queue_admission_preserves_field_error(queue_admission, monkeypatch):
    service, request, charge = queue_admission
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_MAX_QUEUED", "1")
    monkeypatch.setattr(service.store, "stage_queue_depth", Mock(side_effect=ToolRecordFieldError("graph_input", "invalid")))
    with pytest.raises(CustomizationServiceError) as caught:
        service.enqueue_stage(**request)
    assert (caught.value.code, caught.value.status_code) == ("record_fields_invalid", 503)
    charge.assert_not_called()
    _assert_no_queue_reservation(service, request)


def test_queue_status_default_off_does_not_read(tmp_path, monkeypatch):
    service, change = _reserve(tmp_path)
    monkeypatch.delenv("LEAF_CUSTOMIZATION_STAGE_QUEUE_STATUS", raising=False)
    ahead = Mock(side_effect=AssertionError("disabled status read"))
    monkeypatch.setattr(service.store, "stage_queue_ahead", ahead)
    assert "queue" not in service.stage_status_change(change)
    ahead.assert_not_called()


@pytest.mark.parametrize("workers, waits", [("1", [0, 222, 444]), ("2", [0, 222, 222])])
def test_queue_status_counts_across_tenants_and_estimates(tmp_path, monkeypatch, workers, waits):
    store = SQLiteCustomizationStore(tmp_path / "queue.db")
    service = CustomizationService(store)
    changes = [queued(store, tenant=f"tenant-{n}") for n in range(3)]
    # Equal timestamps also exercise the change-set-id FIFO tie breaker.
    with store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET created_at = ?", ("2026-01-01T00:00:00.000Z",))
    changes = sorted(
        [store.get_change_set(tenant_id=c.tenant_id, change_set_id=c.change_set_id) for c in changes],
        key=lambda c: (c.created_at, c.change_set_id),
    )
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_QUEUE_STATUS", " 1 ")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_ESTIMATE_S", "222")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_WORKERS", workers)
    assert store.stage_queue_depth() == 3
    for n, change in enumerate(changes):
        queue = service.stage_status_change(change)["queue"]
        assert queue == {"ahead": n, "position": n + 1, "estimated_wait_s": waits[n]}
        assert set(queue) == {"ahead", "position", "estimated_wait_s"}
    # A later live lease counts ahead even of the first FIFO row.
    with store._transaction() as conn:
        conn.execute(
            "UPDATE customization_change_sets SET stage_lease_owner = ?, stage_lease_expires_at = ? "
            "WHERE tenant_id = ? AND change_set_id = ?",
            ("worker", int(time.time() * 1000) + 60000, changes[2].tenant_id, changes[2].change_set_id),
        )
    assert store.stage_queue_ahead(changes[0]) == 1
    assert store.stage_queue_ahead(changes[1]) == 2
    bind_removal_marker(store, changes[2])
    assert store.stage_queue_depth() == 2
    assert store.stage_queue_ahead(changes[0]) == 0
    assert store.stage_queue_ahead(changes[1]) == 1


@pytest.mark.parametrize("failure", [sqlite3.DatabaseError, PostgresError, OSError, RuntimeError])
def test_queue_status_read_failure_omits_hint(tmp_path, monkeypatch, failure):
    service, change = _reserve(tmp_path)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_QUEUE_STATUS", "1")
    monkeypatch.setattr(service.store, "stage_queue_ahead", Mock(side_effect=failure("unavailable")))
    status = service.stage_status_change(change)
    assert status["status"] == "queued"
    assert "queue" not in status


def test_queue_status_running_and_failed_do_not_read(tmp_path, monkeypatch):
    service, change = _reserve(tmp_path)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_QUEUE_STATUS", "1")
    monkeypatch.setattr(service.store, "stage_queue_ahead", Mock(side_effect=AssertionError("not queued")))
    running = service.store.claim_stage(owner="worker")
    assert service.stage_status_change(running)["status"] == "running"
    assert "queue" not in service.stage_status_change(running)
    failed = service.store.fail_stage_claim(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        owner="worker", reason_code="test_failure", retryable=True,
    )
    assert service.stage_status_change(failed)["status"] == "failed"
    assert "queue" not in service.stage_status_change(failed)
    assert service.store.stage_queue_depth() == 0
    assert "queue" not in service.stage_status_change(replace(change, state=ChangeState.CREATED))


def test_queue_status_staged_has_no_hint(lane, monkeypatch):
    job = lane.service.enqueue_stage(**_request(lane))
    assert customization_stage_worker.run_once("worker", service=lane.service)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_QUEUE_STATUS", "1")
    monkeypatch.setattr(lane.store, "stage_queue_ahead", Mock(side_effect=AssertionError("staged read")))
    status = lane.service.stage_status_change(_change(lane, job))
    assert status["status"] == "staged"
    assert "queue" not in status
    assert lane.store.stage_queue_depth() == 0


@pytest.mark.parametrize("estimate", [None, "0", "-1", "3601", "x", "٢٢٢"])
def test_queue_status_invalid_estimate_is_not_published(tmp_path, monkeypatch, estimate):
    service, change = _reserve(tmp_path)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_QUEUE_STATUS", "1")
    monkeypatch.delenv("LEAF_CUSTOMIZATION_STAGE_ESTIMATE_S", raising=False)
    if estimate is not None:
        monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_ESTIMATE_S", estimate)
    assert service.stage_status_change(change)["queue"] == {"ahead": 0, "position": 1}


@pytest.mark.parametrize("workers", [None, "0", "-1", "5", "x", "٢"])
def test_queue_worker_count_invalid_defaults_to_one(monkeypatch, workers):
    monkeypatch.delenv("LEAF_CUSTOMIZATION_STAGE_WORKERS", raising=False)
    if workers is not None:
        monkeypatch.setenv("LEAF_CUSTOMIZATION_STAGE_WORKERS", workers)
    assert customization_service._stage_worker_count() == 1


@pytest.mark.parametrize("code, retryable", [
    ("customization_stage_queue_full", True),
    ("customization_stage_queue_unavailable", True),
    ("customization_harness_unavailable", False),
])
def test_queue_router_error_contract(code, retryable, caplog):
    with caplog.at_level(logging.WARNING, logger=author_router.__name__):
        response = author_router._customization_error(CustomizationServiceError(code, 503))
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["reason_code"] == code
    assert body["error"]["retryable"] is retryable
    assert response.headers.get("Retry-After") == ("60" if retryable else None)
    if code == "customization_stage_queue_full":
        assert any(record.levelno == logging.WARNING and code in record.message for record in caplog.records)
        assert "not counted against your daily builds" in body["error"]["message"]


def test_decoded_attempt_round_trips_to_stage_status(lane, monkeypatch):
    post = requests.post
    attempts = [REFUSAL, dict(ACCEPT, source_decoded=DECODED)]

    def stage(url, **kwargs):
        response = post(url, **kwargs)
        response.json()["compile_attempts"] = attempts
        return response

    monkeypatch.setattr(requests, "post", stage)
    job = lane.service.enqueue_stage(**_request(lane))
    assert customization_stage_worker.run_once("worker", service=lane.service)
    status = lane.service.stage_status_change(_change(lane, job))
    assert status["compile"]["attempts"] == attempts


@pytest.mark.parametrize("decoded", [
    None, {}, [], dict(DECODED, extra=True),
    {key: value for key, value in DECODED.items() if key != "entities"},
    {key: value for key, value in DECODED.items() if key != "original_sha256"},
    {key: value for key, value in DECODED.items() if key != "compiled_sha256"},
    *[dict(DECODED, entities=value) for value in (True, False, 0, -1, 10001, 1.5, "2", None)],
    *[dict(DECODED, **{key: value}) for key in ("original_sha256", "compiled_sha256")
      for value in (None, 42, "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n")],
    dict(DECODED, compiled_sha256=DECODED["original_sha256"]),
])
def test_invalid_decoded_attempt_is_rejected(tmp_path, caplog, decoded):
    test_invalid_attempt_drops_entire_field(tmp_path, caplog, [dict(ACCEPT, source_decoded=decoded)])


@pytest.mark.parametrize("decision", ["compile_failed", "malformed", "unavailable"])
def test_decoding_disclosure_requires_acceptance(tmp_path, caplog, decision):
    test_invalid_attempt_drops_entire_field(
        tmp_path, caplog, [dict(REFUSAL, decision=decision, source_decoded=DECODED)],
    )


def test_published_branch_status_matches_signed_publish_response(publishing, monkeypatch):
    state = publishing
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(CustomizationService, "configured", lambda: state.service)
    monkeypatch.setattr(state.service, "_raw_receipt", lambda change: {
        "package_path": f"branch-tools/{NAME}",
    })
    state.client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        state.tenant, subject=state.subject)
    assert "publication" not in state.service.stage_status_change(state.change)
    state.store.get_or_create_publication_request(
        tenant_id=state.tenant, change_set_id=state.change.change_set_id)
    assert state.service.stage_status_change(state.change)["publication"] == {"state": "awaiting_approval"}
    response = state.client.post("/api/author/reviews", json={
        "change_set_id": state.change.change_set_id, "staged_commit": state.change.staged_commit,
        "catalog_digest": state.change.catalog_digest, "decision": "approve",
    })
    assert response.status_code == 200, response.text
    change = state.store.get_change_set(tenant_id=state.tenant, change_set_id=state.change.change_set_id)
    row = state.store.get_review_decision(
        tenant_id=state.tenant, change_set_id=change.change_set_id, staged_commit=change.staged_commit)
    # The publish POST can replay a committed publication without signing again.
    published = state.client.post("/api/author/register", json={
        "change_set_id": change.change_set_id, "staged_commit": change.staged_commit,
        "catalog_digest": change.catalog_digest, "platform_release": change.desired_platform_release,
        "workspace_contract_digest": change.workspace_contract_digest,
        "confirmation_id": row["confirmation_id"], "idempotency_key": "published-replay",
    })
    assert published.status_code == 200, published.text
    body = published.json()
    signed = tenant_publish.read_publication(state.root, change.tenant_id, body["name"], body["version"])
    publication = state.service.stage_status_change(change)["publication"]
    assert publication == {
        "state": "published", "published_at": change.updated_at,
        "package_digest": body["package_digest"], "name": body["name"], "version": body["version"],
    }
    assert publication["package_digest"] == signed.package_digest
    assert len(publication["package_digest"]) == 64
    assert set(publication["package_digest"]) <= set("0123456789abcdef")


@pytest.mark.parametrize("error", [CustomizationServiceError("publish_not_available", 409), RuntimeError("private")])
def test_publication_read_failure_keeps_published_status(review, monkeypatch, caplog, error):
    response = review.post()
    assert response.status_code == 200, response.text
    change = _change(review, review.body)
    expected = review.service.stage_status_change(change)
    expected["publication"] = {"state": "published", "published_at": change.updated_at}

    def unavailable(change):
        raise error

    monkeypatch.setattr(review.service, "_published_branch_result", unavailable)
    caplog.clear()
    assert review.service.stage_status_change(change) == expected
    assert caplog.messages == [f"publication_digest_unavailable: change_set={change.change_set_id}"]
