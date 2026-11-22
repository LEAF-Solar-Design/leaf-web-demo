"""Poll the durable end-of-stage history and the bounded inline card fields."""
import json

import pytest
import requests

import build_plan
import customization_stage_worker
from customization_models import ChangeState
from customization_service import CustomizationService
from customization_store import SQLiteCustomizationStore
from test_author_stage_status import FOREIGN, PATH, _headers, _seed, status_api
from test_build_plan import minimal
from test_customization_async_stage import _HarnessErrorResponse
from test_customization_branch_target import _change, _request, lane
from test_customization_inline_review import review


LOCATION = {"code": "CS0117", "file": "lib/example.csx", "line": 42}
REFUSAL = {"n": 1, "decision": "compile_failed", "errors": 1, "diagnostics": [LOCATION]}
ACCEPT = {"n": 2, "decision": "accept", "errors": 0, "diagnostics": []}


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
