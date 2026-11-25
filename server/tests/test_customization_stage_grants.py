"""Queued Branch permissions use live authority, exact bindings and a lease CAS."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

import author_quota
import build_plan
import customization_service as service_module
import customization_stage_grants as grants
import customization_stage_worker as worker
import deps
import entitlements
import platform_link
import session_store
from customization_models import ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from routers import author as author_router

TENANT = "tenant-a"
SUBJECT = "auth0|author"
DESCRIPTION = "Count the selected lines"
BASE = "a" * 40
WORKSPACE = "d" * 64


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_STAGE_GRANT_TTL_S", "300")
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "legacy")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_ALLOW_STATIC_BINDINGS", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
        TENANT: {"subject": SUBJECT, "role": "owner"},
    }))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "k" * 64)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "test-secret")
    monkeypatch.delenv("LEAF_TENANT_MCP_BROKER_URL", raising=False)
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda _: (TENANT, "hosted_pro"))
    monkeypatch.setattr(service_module, "enabled", lambda *_: True)
    monkeypatch.setattr(service_module.customization_flags, "plan_mode_enabled", lambda _: False)
    monkeypatch.setattr(entitlements, "load_policy", lambda: entitlements._HARDCODED_DEFAULTS)
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    session_store.ensure_started()
    session = session_store.get_or_create_session(TENANT, "drawing-a")
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.initialize()
    service = CustomizationService(store)
    monkeypatch.setattr(service, "_release", lambda: SimpleNamespace(
        release_id="release-a", workspace_contract_sha256=WORKSPACE,
    ))
    monkeypatch.setattr(service, "_stage_admission_base", lambda *_, **__: (BASE, None))
    charges = Mock()
    monkeypatch.setattr(author_quota, "enforce", charges)
    tenant = deps.TenantContext(TENANT, tier="hosted_pro", subject=SUBJECT)
    result = SimpleNamespace(
        store=store, service=service, tenant=tenant, session_id=session["session_id"],
        turn_id="turn-a", charges=charges,
    )
    yield result
    session_store._conn.close()


def queue(lane, *, key="request-a", target="branch", **extra):
    change, _ = lane.store.reserve_stage(
        tenant_id=TENANT, idempotency_key=key, base_commit=BASE,
        desired_platform_release="release-a", workspace_contract_digest=WORKSPACE,
        author_subject=SUBJECT, change_kind="create", target_tool_name=None,
        request_description=DESCRIPTION,
        request_fingerprint=service_module._stage_request_fingerprint(DESCRIPTION, target),
        tool_target=target, authority_session_id=lane.session_id,
        authority_turn_id=lane.turn_id, **extra,
    )
    return lane.store.transition(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING, expected_version=change.version,
        idempotency_key=f"staging:{key}",
    )


def enqueue(lane, **extra):
    return lane.service.enqueue_stage(
        tenant=lane.tenant, description=DESCRIPTION, mode="build",
        idempotency_key="enqueue-a", target="branch",
        authority_session_id=lane.session_id, authority_turn_id=lane.turn_id, **extra,
    )


def clean(lane, *, reason="end_turn", stage_authority=False):
    session_store.append_event(lane.session_id, lane.turn_id, "turn_started", {
        "stage_authority": stage_authority,
    })
    session_store.append_event(lane.session_id, lane.turn_id, "turn_complete", {"stop_reason": reason})


def issued_claim(lane):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    clean(lane)
    return lane.store.claim_stage(owner="worker-a")


def refuse(lane, change, code="stage_grant_refused", owner="worker-a"):
    with pytest.raises(CustomizationServiceError) as caught:
        grants.admit(lane.service, change, owner)
    assert (caught.value.code, caught.value.status_code) == (code, 409)


@pytest.mark.parametrize("failure", ["removed", "suspended", "store_down"])
def test_T1_current_membership_must_resolve_at_issue_and_dispatch(lane, monkeypatch, failure):
    change = issued_claim(lane)

    def unavailable(_):
        if failure == "removed":
            return None, None
        raise RuntimeError(failure)

    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", unavailable)
    assert not grants.issue(lane.service, change)
    refuse(lane, change)
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 0


def test_T2_viewer_at_issue_or_dispatch_is_refused(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
        TENANT: {"subject": SUBJECT, "role": "viewer"},
    }))
    assert not grants.issue(lane.service, change)
    refuse(lane, change)


@pytest.mark.parametrize("tier", ["restricted", "admin", "hosted_starter"])
def test_T3_lost_or_changed_tier_refuses_even_if_build_is_still_allowed(lane, monkeypatch, tier):
    change = issued_claim(lane)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda _: (TENANT, tier))
    refuse(lane, change)


def test_T3_role_only_entitlement_never_grants(lane, monkeypatch):
    change = queue(lane)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda _: (TENANT, "restricted"))
    calls = []

    def entitlement(tier, roles, elevated):
        calls.append((roles, elevated))
        return {"build": bool(roles) or elevated}

    monkeypatch.setattr(entitlements, "entitlements_for", entitlement)
    assert not grants.issue(lane.service, change)
    assert calls == [([], False)]
    assert lane.store.get_stage_grant(TENANT, change.change_set_id) is None


def test_T4_live_rollout_off_refuses_issue_and_dispatch(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setattr(service_module, "enabled", lambda *_: False)
    assert not grants.issue(lane.service, change)
    refuse(lane, change)


@pytest.mark.parametrize("field", grants._BINDING_FIELDS)
def test_T5_every_binding_field_is_authenticated(lane, field):
    change = issued_claim(lane)
    previous = getattr(change, field)
    value = previous + 1 if isinstance(previous, int) else (previous or "") + "-changed"
    mutated = replace(change, **{field: value})
    try:
        assert grants.binding_digest(mutated) != grants.binding_digest(change)
    except ValueError:
        pass
    refuse(lane, mutated, "stage_authority_invalid" if field in {"tenant_id", "change_set_id"}
           else "stage_grant_refused")


def test_T5_description_and_plan_bytes_are_revalidated(lane):
    plan = {
        "schema": "leaf.build-plan.v1", "target": "branch", "change_kind": "create",
        "tool_name": "count-lines", "title": "Count lines", "summary": "Count lines.",
        "behavior": ["Count each line once."], "inputs": [], "output": "Count",
        "capabilities": ["drawing.read"], "undo": "", "assumptions": [],
        "acceptance": [], "answers": {},
    }
    change = queue(lane, plan_json=json.dumps(plan), plan_digest=build_plan.plan_digest(plan))
    assert grants.issue(lane.service, change)
    clean(lane)
    change = lane.store.claim_stage(owner="worker-a")
    refuse(lane, replace(change, request_description="Different request"))
    plan["title"] = "Different title"
    refuse(lane, replace(change, plan_json=json.dumps(plan)))
    with pytest.raises(ValueError):
        grants.binding_digest(replace(change, plan_json="not-json"))


def test_T6_live_tenant_move_refuses_issue_and_dispatch(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda _: ("tenant-b", "hosted_pro"))
    assert not grants.issue(lane.service, change)
    refuse(lane, change)


def test_T7_other_subject_and_grant_identity_fields_refuse(lane):
    change = issued_claim(lane)
    refuse(lane, replace(change, author_subject="auth0|other"))
    with lane.store._transaction() as conn:
        conn.execute("UPDATE customization_stage_grants SET author_subject = ?", ("auth0|other",))
    refuse(lane, change)


def test_T7_no_api_model_accepts_a_grant_id():
    for model in (author_router.AuthorRequest, author_router.StageRequest, author_router.StageRecoveryRequest):
        assert not any("grant" in name for name in model.model_fields)


def test_T8_two_distinct_attempts_survive_restart_but_replay_and_third_refuse(lane):
    change = issued_claim(lane)
    grants.admit(lane.service, change, "worker-a")
    refuse(lane, change)
    assert lane.store.defer_stage_claim(
        tenant_id=TENANT, change_set_id=change.change_set_id, owner="worker-a",
        reason_code="retry", delay_seconds=0,
    )
    restarted = SQLiteCustomizationStore(lane.store.database_path)
    lane.service.store = restarted
    second = restarted.claim_stage(owner="worker-b")
    refuse(lane, second, owner="worker-a")
    grants.admit(lane.service, second, "worker-b")
    restarted.defer_stage_claim(
        tenant_id=TENANT, change_set_id=change.change_set_id, owner="worker-b",
        reason_code="retry", delay_seconds=0,
    )
    third = restarted.claim_stage(owner="worker-c")
    refuse(lane, third, owner="worker-c")
    assert restarted.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 2


@pytest.mark.parametrize("raw", ["", "299", "14401", "3.0", "+300", "-300", "３００", "٣٠٠", "300s", "3 00"])
def test_T9_invalid_settings_are_off(monkeypatch, raw):
    monkeypatch.setenv("LEAF_STAGE_GRANT_TTL_S", raw)
    assert grants.ttl_s() is None


@pytest.mark.parametrize("raw,expected", [("300", 300), (" 14400 \n", 14400), ("00300", 300)])
def test_T9_valid_settings(monkeypatch, raw, expected):
    monkeypatch.setenv("LEAF_STAGE_GRANT_TTL_S", raw)
    assert grants.ttl_s() == expected


def test_T9_expiry_is_fixed_at_issue_even_after_setting_increases(lane, monkeypatch):
    change = issued_claim(lane)
    row = lane.store.get_stage_grant(TENANT, change.change_set_id)
    monkeypatch.setenv("LEAF_STAGE_GRANT_TTL_S", "14400")
    assert not grants.issue(lane.service, change)
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["expires_ms"] == row["expires_ms"]
    monkeypatch.setattr(grants.time, "time", lambda: row["expires_ms"] / 1000)
    refuse(lane, change, "stage_grant_expired")


@pytest.mark.parametrize("records,expected", [
    ([], False),
    ([("turn_complete", {"stop_reason": "end_turn"})], True),
    ([("turn_complete", {"stop_reason": "interrupted"})], False),
    ([("turn_complete", {"stop_reason": "unknown"})], False),
    ([("turn_complete", {})], False),
    ([("error", {})], False),
    ([("turn_complete", {"stop_reason": "end_turn"}), ("error", {})], False),
    ([("turn_complete", {"stop_reason": "end_turn"})] * 2, False),
    ([("turn_complete", {"stop_reason": "timeout"})], False),
])
def test_T10_all_terminal_records_are_checked(lane, records, expected):
    for kind, data in records:
        session_store.append_event(lane.session_id, lane.turn_id, kind, data)
    assert grants.turn_ended_cleanly(lane.session_id, lane.turn_id, TENANT) is expected
    assert not grants.turn_ended_cleanly(lane.session_id, lane.turn_id, "tenant-b")


@pytest.mark.parametrize("stage_authority", [True, False, "true", 1])
def test_T10_timeout_requires_explicit_stage_authority(lane, stage_authority):
    clean(lane, reason="timeout", stage_authority=stage_authority)
    assert grants.turn_ended_cleanly(lane.session_id, lane.turn_id, TENANT) is (stage_authority is True)


def test_T10_unreadable_terminal_record_fails_closed(lane, monkeypatch):
    clean(lane)
    with session_store._lock:
        session_store._conn.execute("UPDATE session_events SET data_json = 'bad-json' WHERE type = 'turn_complete'")
        session_store._conn.commit()
    assert not grants.turn_ended_cleanly(lane.session_id, lane.turn_id, TENANT)
    monkeypatch.setattr(session_store, "turn_terminal_events", Mock(side_effect=OSError("unreadable")))
    assert not grants.turn_ended_cleanly(lane.session_id, lane.turn_id, TENANT)


def test_withdraw_before_claim_fails_change_and_revokes_grant(lane):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    assert not lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject="auth0|other", now_ms=int(time.time() * 1000),
    )
    assert lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject=SUBJECT, now_ms=int(time.time() * 1000),
    )
    failed = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert failed.state is ChangeState.FAILED
    assert failed.stage_error_code == "stage_grant_withdrawn"
    assert not failed.stage_error_retryable
    assert lane.store.claim_stage(owner="late-worker") is None
    refuse(lane, change, "stage_grant_withdrawn")
    assert not lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject=SUBJECT, now_ms=int(time.time() * 1000),
    )
    events = lane.store.audit_events(tenant_id=TENANT, change_set_id=change.change_set_id)
    withdrawn = [event for event in events if event.reason_code == "stage_grant_withdrawn"]
    assert len(withdrawn) == 1
    assert (withdrawn[0].prior_state, withdrawn[0].next_state) == (ChangeState.STAGING, ChangeState.FAILED)


def test_withdraw_loses_to_live_claim_and_other_author(lane):
    change = issued_claim(lane)
    for subject in (SUBJECT, "auth0|other"):
        assert not lane.store.withdraw_stage(
            tenant_id=TENANT, change_set_id=change.change_set_id,
            author_subject=subject, now_ms=int(time.time() * 1000),
        )
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["revoked_ms"] is None


def test_T12_issue_only_after_charge_and_never_on_replay(lane, monkeypatch):
    original = lane.store.insert_stage_grant
    issued = []

    def insert(**kwargs):
        assert lane.charges.call_count == 1
        assert lane.store.get_change_set(
            tenant_id=TENANT, change_set_id=kwargs["change_set_id"],
        ).state is ChangeState.STAGING
        issued.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(lane.store, "insert_stage_grant", insert)
    first = enqueue(lane)
    replay = enqueue(lane)
    assert first == replay
    assert len(issued) == lane.charges.call_count == 1


def test_T12_quota_refusal_issues_nothing(lane, monkeypatch):
    lane.charges.side_effect = author_quota.AuthorQuotaExceeded("hosted_pro", 1, 1, "2026-11-25")
    insert = Mock(side_effect=AssertionError("must not issue"))
    monkeypatch.setattr(lane.store, "insert_stage_grant", insert)
    with pytest.raises(author_quota.AuthorQuotaExceeded):
        enqueue(lane)
    insert.assert_not_called()


def test_T16_sqlite_consume_race_has_exactly_one_winner(lane):
    change = issued_claim(lane)
    barrier = threading.Barrier(2)

    def consume(_):
        store = SQLiteCustomizationStore(lane.store.database_path)
        barrier.wait()
        return store.consume_stage_grant(
            tenant_id=TENANT, change_set_id=change.change_set_id,
            owner="worker-a", attempt=change.stage_attempt,
            binding_digest=grants.binding_digest(change), now_ms=int(time.time() * 1000),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]


def test_T17_each_success_has_one_audit_and_no_refusal_adds_one(lane):
    change = issued_claim(lane)
    assert not grants.issue(lane.service, change)
    grants.admit(lane.service, change, "worker-a")
    refuse(lane, change)
    events = lane.store.audit_events(tenant_id=TENANT, change_set_id=change.change_set_id)
    relevant = [event for event in events if (event.reason_code or "").startswith("stage_grant_")]
    assert sorted(event.reason_code for event in relevant) == ["stage_grant_consumed", "stage_grant_issued"]
    assert all(event.prior_state is event.next_state is ChangeState.STAGING for event in relevant)
    assert len({event.idempotency_key for event in events}) == len(events)


def test_T18_off_with_existing_grant_keeps_todays_refusal(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.delenv("LEAF_STAGE_GRANT_TTL_S")
    monkeypatch.setattr(lane.store, "get_stage_grant", Mock(side_effect=AssertionError("off")))
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CustomizationServiceError, match="stage_authority_invalid"):
        lane.service.dispatch_stage(change)
    post.assert_not_called()


def test_studio_target_and_missing_authority_get_no_grant(lane):
    studio = queue(lane, target=None)
    assert not grants.issue(lane.service, studio)
    branch = queue(lane, key="branch")
    for field in ("author_subject", "authority_session_id", "authority_turn_id"):
        assert not grants.issue(lane.service, replace(branch, **{field: None}))
    assert lane.store.get_stage_grant(TENANT, studio.change_set_id) is None


def test_project_write_lost_refuses_at_issue_and_dispatch(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setattr(platform_link, "require_project_session_access", lambda *_, **__: None)
    assert not grants.issue(lane.service, change)
    refuse(lane, change)


def test_setting_off_never_calls_any_grant_store_method(lane, monkeypatch):
    monkeypatch.delenv("LEAF_STAGE_GRANT_TTL_S")
    grant_methods = []
    for name in ("insert_stage_grant", "get_stage_grant", "consume_stage_grant", "withdraw_stage"):
        method = Mock(side_effect=AssertionError(name))
        grant_methods.append(method)
        monkeypatch.setattr(lane.store, name, method)
    job = enqueue(lane)
    change = lane.store.claim_stage(owner="worker-a")
    assert job["status"] == "queued"
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: SUBJECT)
    post = Mock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}))
    monkeypatch.setattr(requests, "post", post)
    lane.service.dispatch_stage(change)
    assert post.call_args.kwargs["headers"]["X-Authority-Turn-Id"] == lane.turn_id
    for method in grant_methods:
        method.assert_not_called()


def test_active_turn_keeps_authority_headers_and_never_consumes(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: SUBJECT)
    monkeypatch.setattr(lane.store, "consume_stage_grant", Mock(side_effect=AssertionError("active")))
    post = Mock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}))
    monkeypatch.setattr(requests, "post", post)
    lane.service.dispatch_stage(change)
    assert post.call_args.kwargs["headers"] == {
        "X-Harness-Secret": "test-secret", "X-Authority-Session-Id": lane.session_id,
        "X-Authority-Turn-Id": lane.turn_id,
    }


def test_granted_path_sends_only_services_selector_and_harness_secret(lane, monkeypatch):
    change = issued_claim(lane)
    post = Mock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}))
    monkeypatch.setattr(requests, "post", post)
    lane.service.dispatch_stage(change)
    assert post.call_args.kwargs["headers"] == {"X-Harness-Secret": "test-secret", "X-Author-Services": "none"}
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 1


@pytest.mark.parametrize("subject,tenant", [("auth0|other", TENANT), (None, "tenant-b")])
def test_mismatched_active_subject_or_tenant_never_uses_grant(lane, monkeypatch, subject, tenant):
    change = issued_claim(lane)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: subject)
    monkeypatch.setattr(grants, "admit", Mock(side_effect=AssertionError("must not admit")))
    with pytest.raises(CustomizationServiceError, match="stage_authority_invalid"):
        lane.service._harness_stage(tenant, DESCRIPTION, change)


@pytest.mark.parametrize("expired", [True, False])
def test_refused_build_fails_before_http_and_only_expired_is_retryable(lane, monkeypatch, expired):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    if expired:
        with lane.store._transaction() as conn:
            conn.execute("UPDATE customization_stage_grants SET issued_ms = 1, expires_ms = 2")
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    assert worker.run_once("worker-a", service=lane.service)
    failed = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert failed.state is ChangeState.FAILED
    assert failed.stage_error_code == ("stage_grant_expired" if expired else "stage_grant_refused")
    assert failed.stage_error_retryable is expired
    post.assert_not_called()


@pytest.mark.parametrize("method", ["get_stage_grant", "consume_stage_grant"])
def test_dispatch_store_failures_refuse_before_http(lane, monkeypatch, method):
    change = issued_claim(lane)
    monkeypatch.setattr(lane.store, method, Mock(side_effect=RuntimeError("private database detail")))
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CustomizationServiceError, match="stage_grant_refused"):
        lane.service.dispatch_stage(change)
    post.assert_not_called()


@pytest.mark.parametrize("failure,code", [
    ("identity", "stage_grant_refused"),
    ("revoked", "stage_grant_withdrawn"),
    ("expired", "stage_grant_expired"),
    ("digest", "stage_grant_refused"),
    ("turn", "stage_grant_refused"),
    ("live", "stage_grant_refused"),
])
def test_refusal_order_never_consumes_or_calls_harness(lane, monkeypatch, failure, code):
    change = issued_claim(lane)
    row = lane.store.get_stage_grant(TENANT, change.change_set_id)
    if failure == "identity":
        row["author_subject"] = "auth0|other"
    if failure in {"identity", "revoked"}:
        row["revoked_ms"] = 1
    if failure in {"identity", "revoked", "expired"}:
        row["expires_ms"] = 2
    if failure in {"identity", "revoked", "expired", "digest"}:
        row["binding_digest"] = "invalid"
    monkeypatch.setattr(lane.store, "get_stage_grant", lambda *_: row)
    terminal = Mock(return_value=False)
    live = Mock(return_value=None)
    consume = Mock(side_effect=AssertionError("refusal must not consume"))
    post = Mock(side_effect=AssertionError("refusal must not call harness"))
    monkeypatch.setattr(grants, "turn_ended_cleanly", terminal)
    monkeypatch.setattr(grants, "live_check", live)
    monkeypatch.setattr(lane.store, "consume_stage_grant", consume)
    monkeypatch.setattr(requests, "post", post)
    if failure == "live":
        terminal.return_value = True
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.dispatch_stage(change)
    assert (caught.value.code, caught.value.status_code) == (code, 409)
    assert terminal.call_count == (1 if failure in {"turn", "live"} else 0)
    assert live.call_count == (1 if failure == "live" else 0)
    consume.assert_not_called()
    post.assert_not_called()


def test_missing_grant_keeps_todays_code(lane):
    change = queue(lane)
    refuse(lane, change, "stage_authority_invalid")


def test_expired_lease_cannot_consume_and_refusals_do_not_log_private_fields(lane, caplog):
    change = issued_claim(lane)
    with lane.store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET stage_lease_expires_at = 0")
    refuse(lane, change)
    row = lane.store.get_stage_grant(TENANT, change.change_set_id)
    assert row["dispatches"] == row["last_attempt"] == 0
    assert change.change_set_id in caplog.text
    assert SUBJECT not in caplog.text
    assert lane.session_id not in caplog.text
    assert row["binding_digest"] not in caplog.text


def test_dispatched_grant_cannot_be_withdrawn_after_lease_release(lane):
    change = issued_claim(lane)
    grants.admit(lane.service, change, "worker-a")
    lane.store.defer_stage_claim(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        owner="worker-a", reason_code="retry", delay_seconds=0,
    )
    assert not lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject=SUBJECT, now_ms=int(time.time() * 1000),
    )


def test_issue_store_failure_leaves_enqueue_successful_and_logs_only_type(lane, monkeypatch, caplog):
    monkeypatch.setattr(lane.store, "insert_stage_grant", Mock(side_effect=RuntimeError("private detail")))
    assert enqueue(lane)["status"] == "queued"
    assert "RuntimeError" in caplog.text
    assert "private detail" not in caplog.text


@pytest.mark.parametrize("case,status,withdrawn", [
    ("success", 200, True), ("claimed", 200, False), ("other_author", 404, None),
    ("other_tenant", 404, None), ("off", 404, None), ("missing", 404, None),
    ("rollout_off", 404, None), ("viewer", 403, None), ("entitlement_lost", 403, None),
])
def test_withdraw_route_is_gated_and_author_bound(lane, monkeypatch, case, status, withdrawn):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    tenant = lane.tenant
    if case == "claimed":
        lane.store.claim_stage(owner="worker-a")
    elif case == "other_author":
        tenant = deps.TenantContext(TENANT, tier="hosted_pro", subject="auth0|other")
        monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
            TENANT: {"subject": "auth0|other", "role": "owner"},
        }))
    elif case == "other_tenant":
        tenant = deps.TenantContext("tenant-b", tier="hosted_pro", subject=SUBJECT)
        monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
            "tenant-b": {"subject": SUBJECT, "role": "owner"},
        }))
    elif case == "off":
        monkeypatch.delenv("LEAF_STAGE_GRANT_TTL_S")
    elif case == "viewer":
        monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
            TENANT: {"subject": SUBJECT, "role": "viewer"},
        }))
    elif case == "entitlement_lost":
        tenant = deps.TenantContext(TENANT, tier="restricted", subject=SUBJECT)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: case != "rollout_off")
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))
    app = FastAPI()
    app.include_router(author_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    change_id = "00000000-0000-4000-8000-000000000000" if case == "missing" else change.change_set_id
    with TestClient(app) as client:
        response = client.post(f"/api/author/stages/{change_id}/withdraw")
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"contract": "leaf.customization-stage-withdraw.v1", "withdrawn": withdrawn}
