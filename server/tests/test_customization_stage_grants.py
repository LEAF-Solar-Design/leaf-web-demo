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
import turn_runner
from customization_models import ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from routers import author as author_router

TENANT = "tenant-a"
SUBJECT = "auth0|author"
DESCRIPTION = "Count the selected lines"
BASE = "a" * 40
WORKSPACE = "d" * 64
WAITING_SERVICES_NOTICE = (
    "This build waited in line, so it ran without connected services. "
    "If it needs one, ask again when the line is short."
)


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


def queue(lane, *, key="request-a", target="branch", target_tool_name=None, **extra):
    change, _ = lane.store.reserve_stage(
        tenant_id=TENANT, idempotency_key=key, base_commit=BASE,
        desired_platform_release="release-a", workspace_contract_digest=WORKSPACE,
        author_subject=SUBJECT, change_kind="revise" if target_tool_name else "create",
        target_tool_name=target_tool_name,
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
    assert caught.value.harness_not_called is True


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


@pytest.mark.parametrize("has_plan", [False, True])
def test_live_plan_mode_requires_a_plan_at_issue_and_dispatch(lane, monkeypatch, has_plan):
    plan = {
        "schema": "leaf.build-plan.v1", "target": "branch", "change_kind": "create",
        "tool_name": "count-lines", "title": "Count lines", "summary": "Count lines.",
        "behavior": ["Count each line once."], "inputs": [], "output": "Count",
        "capabilities": ["drawing.read"], "undo": "", "assumptions": [],
        "acceptance": [], "answers": {},
    }
    metadata = {"plan_json": json.dumps(plan), "plan_digest": build_plan.plan_digest(plan)} if has_plan else {}
    change = queue(lane, **metadata)
    assert grants.issue(lane.service, change)
    clean(lane)
    change = lane.store.claim_stage(owner="worker-a")
    checked = Mock(return_value=True)
    monkeypatch.setattr(grants.customization_flags, "plan_mode_enabled", checked)
    assert grants.live_check(lane.service, change) == ("hosted_pro" if has_plan else None)
    checked.assert_called_with(TENANT)
    fresh = queue(lane, key="plan-required", **metadata)
    assert grants.issue(lane.service, fresh) is has_plan
    assert (lane.store.get_stage_grant(TENANT, fresh.change_set_id) is not None) is has_plan
    if has_plan:
        grants.admit(lane.service, change, "worker-a")
    else:
        refuse(lane, change)
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == int(has_plan)


def test_live_plan_mode_failure_refuses_issue_and_dispatch(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setattr(grants.customization_flags, "plan_mode_enabled", Mock(side_effect=RuntimeError("unavailable")))
    assert grants.live_check(lane.service, change) is None
    assert not grants.issue(lane.service, change)
    refuse(lane, change)
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 0


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
    code = "stage_authority_invalid" if field in {
        "tenant_id", "change_set_id", "authority_session_id", "authority_turn_id",
    } else "stage_grant_refused"
    refuse(lane, mutated, code)


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
    for route in author_router.router.routes:
        assert "grant" not in route.path.lower()
        pending = [route.dependant]
        while pending:
            dependency = pending.pop()
            for parameter in (*dependency.path_params, *dependency.header_params):
                assert "grant" not in parameter.name.lower()
                assert "grant" not in parameter.alias.lower()
            pending.extend(dependency.dependencies)


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
    refuse(lane, third, "stage_grant_exhausted", owner="worker-c")
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
    change = queue(lane)
    assert grants.issue(lane.service, change)
    for kind, data in records:
        session_store.append_event(lane.session_id, lane.turn_id, kind, data)
    assert grants.turn_ended_cleanly(lane.session_id, lane.turn_id, TENANT) is expected
    assert not grants.turn_ended_cleanly(lane.session_id, lane.turn_id, "tenant-b")
    change = lane.store.claim_stage(owner="worker-a")
    if expected:
        grants.admit(lane.service, change, "worker-a")
    else:
        refuse(lane, change, "stage_grant_turn_open")


@pytest.mark.parametrize("stage_authority", [True, False, "true", 1])
def test_T10_timeout_requires_explicit_stage_authority(lane, stage_authority):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    clean(lane, reason="timeout", stage_authority=stage_authority)
    assert grants.turn_ended_cleanly(lane.session_id, lane.turn_id, TENANT) is (stage_authority is True)
    change = lane.store.claim_stage(owner="worker-a")
    if stage_authority is True:
        grants.admit(lane.service, change, "worker-a")
    else:
        refuse(lane, change, "stage_grant_turn_open")


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
    assert lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject="auth0|other", now_ms=int(time.time() * 1000),
    ) == {"withdrawn": False, "revoked": False}
    assert lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject=SUBJECT, now_ms=int(time.time() * 1000),
    ) == {"withdrawn": True, "revoked": True}
    failed = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert failed.state is ChangeState.FAILED
    assert failed.stage_error_code == "stage_grant_withdrawn"
    assert not failed.stage_error_retryable
    assert lane.store.claim_stage(owner="late-worker") is None
    refuse(lane, change, "stage_grant_withdrawn")
    assert lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject=SUBJECT, now_ms=int(time.time() * 1000),
    ) == {"withdrawn": False, "revoked": True}
    events = lane.store.audit_events(tenant_id=TENANT, change_set_id=change.change_set_id)
    withdrawn = [event for event in events if event.reason_code == "stage_grant_withdrawn"]
    assert len(withdrawn) == 1
    assert (withdrawn[0].prior_state, withdrawn[0].next_state) == (ChangeState.STAGING, ChangeState.FAILED)


@pytest.mark.parametrize("dispatched", [False, True])
def test_withdraw_revokes_under_live_claim_but_refuses_other_author(lane, monkeypatch, dispatched):
    change = issued_claim(lane)
    if dispatched:
        grants.admit(lane.service, change, "worker-a")
    for subject, revoked in (("auth0|other", False), (SUBJECT, True)):
        assert lane.store.withdraw_stage(
            tenant_id=TENANT, change_set_id=change.change_set_id,
            author_subject=subject, now_ms=int(time.time() * 1000),
        ) == {"withdrawn": False, "revoked": revoked}
    row = lane.store.get_stage_grant(TENANT, change.change_set_id)
    assert row["revoked_ms"] is not None and row["revoked_reason"] == "withdrawn"
    assert lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id) == change
    assert not lane.store.consume_stage_grant(
        tenant_id=TENANT, change_set_id=change.change_set_id, owner="worker-a",
        attempt=change.stage_attempt, binding_digest=row["binding_digest"],
        now_ms=int(time.time() * 1000),
    )
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: SUBJECT)
    with pytest.raises(CustomizationServiceError, match="stage_grant_withdrawn"):
        lane.service.dispatch_stage(change)
    post.assert_not_called()
    events = lane.store.audit_events(tenant_id=TENANT, change_set_id=change.change_set_id)
    withdrawn = [event for event in events if event.reason_code == "stage_grant_withdrawn"]
    assert len(withdrawn) == 1
    assert withdrawn[0].prior_state is withdrawn[0].next_state is ChangeState.STAGING


@pytest.mark.parametrize("lease", ["expired", "released"])
def test_withdraw_after_unused_claim_only_revokes(lane, monkeypatch, lease):
    change = issued_claim(lane)
    if lease == "expired":
        with lane.store._transaction() as conn:
            conn.execute("UPDATE customization_change_sets SET stage_lease_expires_at = 0")
    else:
        lane.store.defer_stage_claim(
            tenant_id=TENANT, change_set_id=change.change_set_id, owner="worker-a",
            reason_code="retry", delay_seconds=0,
        )
    before = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id, author_subject=SUBJECT,
        now_ms=int(time.time() * 1000),
    ) == {"withdrawn": False, "revoked": True}
    assert lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id) == before
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 0
    assert lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id, author_subject=SUBJECT,
        now_ms=int(time.time() * 1000),
    ) == {"withdrawn": False, "revoked": True}
    second = lane.store.claim_stage(owner="worker-b")
    assert second.stage_attempt == 2
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: SUBJECT)
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CustomizationServiceError, match="stage_grant_withdrawn"):
        lane.service.dispatch_stage(second)
    post.assert_not_called()


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
    for name in ("insert_stage_grant", "get_stage_grant", "consume_stage_grant", "withdraw_stage", "revoke_stage_grants_for_turn"):
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


@pytest.mark.parametrize("has_grant", [False, True])
def test_active_turn_keeps_authority_headers_and_never_consumes(lane, monkeypatch, has_grant):
    change = issued_claim(lane) if has_grant else queue(lane)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: SUBJECT)
    consume = Mock(side_effect=AssertionError("active"))
    monkeypatch.setattr(lane.store, "consume_stage_grant", consume)
    post = Mock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}))
    monkeypatch.setattr(requests, "post", post)
    lane.service.dispatch_stage(change)
    assert post.call_args.kwargs["headers"] == {
        "X-Harness-Secret": "test-secret", "X-Authority-Session-Id": lane.session_id,
        "X-Authority-Turn-Id": lane.turn_id,
    }
    consume.assert_not_called()


def test_granted_path_sends_only_services_selector_and_harness_secret(lane, monkeypatch):
    change = issued_claim(lane)
    post = Mock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}))
    monkeypatch.setattr(requests, "post", post)
    lane.service.dispatch_stage(change)
    assert post.call_args.kwargs["headers"] == {"X-Harness-Secret": "test-secret", "X-Author-Services": "none"}
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 1


@pytest.mark.parametrize("failure", ["url", "revision"])
def test_local_dispatch_checks_fail_before_grant_admission(lane, monkeypatch, failure):
    change = issued_claim(lane)
    change = replace(change, target_tool_name="count-lines")
    admit = Mock(side_effect=AssertionError("local checks must precede admission"))
    post = Mock()
    revision = Mock(side_effect=CustomizationServiceError("revision_base_unavailable", 503))
    monkeypatch.setattr(grants, "admit", admit)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(lane.service, "_branch_revision_base", revision)
    if failure == "url":
        monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "")
    code = "customization_harness_unavailable" if failure == "url" else "revision_base_unavailable"
    with pytest.raises(CustomizationServiceError, match=code):
        lane.service.dispatch_stage(change)
    assert revision.call_count == int(failure == "revision")
    admit.assert_not_called()
    post.assert_not_called()
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 0


@pytest.mark.parametrize("case", ["off", "unbound", "subject", "tenant"])
def test_non_grant_authority_error_still_precedes_local_dispatch_checks(lane, monkeypatch, case):
    change = issued_claim(lane)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "")
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: "auth0|other" if case == "subject" else None)
    if case == "off":
        monkeypatch.delenv("LEAF_STAGE_GRANT_TTL_S")
    admit = Mock()
    post = Mock()
    monkeypatch.setattr(grants, "admit", admit)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CustomizationServiceError, match="stage_authority_invalid"):
        CustomizationService._harness_stage(
            None if case == "unbound" else lane.service,
            "tenant-b" if case == "tenant" else TENANT, DESCRIPTION, change,
        )
    admit.assert_not_called()
    post.assert_not_called()


def test_revision_base_is_resolved_before_consuming_grant(lane, monkeypatch):
    change = queue(lane, target_tool_name="count-lines")
    assert grants.issue(lane.service, change)
    clean(lane)
    change = lane.store.claim_stage(owner="worker-a")

    def revision(*args):
        assert args == (TENANT, BASE, "count-lines")
        assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 0
        return (None, BASE)

    def post(*args, **kwargs):
        assert lane.store.get_stage_grant(TENANT, change.change_set_id)["dispatches"] == 1
        assert kwargs["json"]["revisionBaseCommit"] == BASE
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    monkeypatch.setattr(lane.service, "_branch_revision_base", revision)
    monkeypatch.setattr(requests, "post", post)
    lane.service.dispatch_stage(change)


@pytest.mark.parametrize("subject,tenant", [("auth0|other", TENANT), (None, "tenant-b")])
def test_mismatched_active_subject_or_tenant_never_uses_grant(lane, monkeypatch, subject, tenant):
    change = issued_claim(lane)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: subject)
    monkeypatch.setattr(grants, "admit", Mock(side_effect=AssertionError("must not admit")))
    read = Mock(side_effect=RuntimeError("must not read"))
    monkeypatch.setattr(lane.store, "get_stage_grant", read)
    with pytest.raises(CustomizationServiceError, match="stage_authority_invalid"):
        lane.service._harness_stage(tenant, DESCRIPTION, change)
    read.assert_not_called()


@pytest.mark.parametrize("code,retryable", [
    ("stage_grant_expired", True), ("stage_grant_exhausted", True),
    ("stage_grant_turn_open", False), ("stage_grant_refused", False),
    ("stage_grant_unavailable", True),
])
def test_refused_build_fails_before_http_with_expected_retryability(lane, monkeypatch, code, retryable):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    if code == "stage_grant_expired":
        with lane.store._transaction() as conn:
            conn.execute("UPDATE customization_stage_grants SET issued_ms = 1, expires_ms = 2")
    elif code == "stage_grant_exhausted":
        with lane.store._transaction() as conn:
            conn.execute("UPDATE customization_stage_grants SET dispatches = 2")
    elif code == "stage_grant_refused":
        clean(lane)
        monkeypatch.setattr(grants, "live_check", lambda *_: None)
    elif code == "stage_grant_unavailable":
        clean(lane)
        monkeypatch.setattr(lane.store, "consume_stage_grant", Mock(side_effect=RuntimeError("store down")))
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    assert worker.run_once("worker-a", service=lane.service)
    failed = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert failed.state is ChangeState.FAILED
    assert failed.stage_error_code == code
    assert failed.stage_error_retryable is retryable
    post.assert_not_called()


@pytest.mark.parametrize("method", ["get_stage_grant", "consume_stage_grant"])
def test_dispatch_store_failures_refuse_before_http(lane, monkeypatch, method):
    change = issued_claim(lane)
    monkeypatch.setattr(lane.store, method, Mock(side_effect=RuntimeError("private database detail")))
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    code = "stage_grant_refused" if method == "get_stage_grant" else "stage_grant_unavailable"
    with pytest.raises(CustomizationServiceError, match=code) as caught:
        lane.service.dispatch_stage(change)
    assert caught.value.harness_not_called is True
    post.assert_not_called()


def test_active_dispatch_grant_read_error_refuses_before_http(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: SUBJECT)
    read = Mock(side_effect=RuntimeError("private database detail"))
    monkeypatch.setattr(lane.store, "get_stage_grant", read)
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CustomizationServiceError, match="stage_grant_refused") as caught:
        lane.service.dispatch_stage(change)
    assert caught.value.harness_not_called is True
    read.assert_called_once_with(TENANT, change.change_set_id)
    post.assert_not_called()


@pytest.mark.parametrize("method", ["get_stage_grant", "consume_stage_grant"])
def test_admit_store_errors_are_unavailable(lane, monkeypatch, method):
    change = issued_claim(lane)
    monkeypatch.setattr(lane.store, method, Mock(side_effect=RuntimeError("private detail")))
    refuse(lane, change, "stage_grant_unavailable")


@pytest.mark.parametrize("failure,code", [
    ("identity", "stage_grant_refused"),
    ("authority_session_id", "stage_authority_invalid"),
    ("authority_turn_id", "stage_authority_invalid"),
    ("revoked", "stage_grant_withdrawn"),
    ("expired", "stage_grant_expired"),
    ("exhausted", "stage_grant_exhausted"),
    ("digest", "stage_grant_refused"),
    ("turn", "stage_grant_turn_open"),
    ("live", "stage_grant_refused"),
])
def test_admit_refusal_order_never_consumes_or_calls_harness(lane, monkeypatch, failure, code):
    change = issued_claim(lane)
    row = lane.store.get_stage_grant(TENANT, change.change_set_id)
    if failure == "identity":
        row["author_subject"] = "auth0|other"
    if failure in {"authority_session_id", "authority_turn_id"}:
        row[failure] = "other"
    if failure == "exhausted":
        row["dispatches"] = 2
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
        grants.admit(lane.service, change, "worker-a")
    assert (caught.value.code, caught.value.status_code) == (code, 409)
    assert caught.value.harness_not_called is True
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


def test_dispatched_grant_cannot_be_withdrawn_after_lease_release(lane, monkeypatch):
    change = issued_claim(lane)
    grants.admit(lane.service, change, "worker-a")
    lane.store.defer_stage_claim(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        owner="worker-a", reason_code="retry", delay_seconds=0,
    )
    assert lane.store.withdraw_stage(
        tenant_id=TENANT, change_set_id=change.change_set_id,
        author_subject=SUBJECT, now_ms=int(time.time() * 1000),
    ) == {"withdrawn": False, "revoked": True}
    row = lane.store.get_stage_grant(TENANT, change.change_set_id)
    assert row["revoked_ms"] is not None and row["revoked_reason"] == "withdrawn"
    second = lane.store.claim_stage(owner="worker-b")
    assert second.stage_attempt == 2
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CustomizationServiceError, match="stage_grant_withdrawn"):
        lane.service.dispatch_stage(second)
    post.assert_not_called()


@pytest.mark.parametrize("finish,consumed", [("interrupted", False), ("end_turn", False), ("end_turn", True)])
def test_cancel_revokes_before_terminal_gap_and_blocks_dispatch(lane, monkeypatch, finish, consumed):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    if consumed:
        clean(lane)
        claimed = lane.store.claim_stage(owner="worker-a")
        grants.admit(lane.service, claimed, "worker-a")
        lane.store.defer_stage_claim(
            tenant_id=TENANT, change_set_id=change.change_set_id,
            owner="worker-a", reason_code="retry", delay_seconds=0,
        )
    assert session_store.try_begin_turn(lane.session_id, lane.turn_id, 300.0)
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))

    def cancel():
        assert lane.store.get_stage_grant(TENANT, change.change_set_id)["revoked_reason"] == "cancelled"
        if not consumed:
            clean(lane, reason=finish)
        session_store.end_turn(lane.session_id, lane.turn_id)

    monkeypatch.setitem(turn_runner._cancellers, lane.turn_id, cancel)
    assert turn_runner.request_cancel(lane.tenant, lane.session_id, lane.turn_id) == "cancelled"
    if finish == "end_turn":
        assert grants.turn_ended_cleanly(lane.session_id, lane.turn_id, TENANT)
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    assert worker.run_once("worker-b", service=lane.service)
    failed = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert failed.stage_error_code == "stage_grant_cancelled"
    assert not failed.stage_error_retryable
    assert "harness_not_called" not in lane.service.stage_status_change(failed)["error"]
    post.assert_not_called()
    events = lane.store.audit_events(tenant_id=TENANT, change_set_id=change.change_set_id)
    cancelled = [event for event in events if event.reason_code == "stage_grant_cancelled"]
    assert len(cancelled) == 2
    assert cancelled[0].prior_state is cancelled[0].next_state is ChangeState.STAGING
    assert cancelled[1].prior_state is ChangeState.STAGING
    assert cancelled[1].next_state is ChangeState.FAILED


@pytest.mark.parametrize("case", ["off", "failure", "unrelated", "orphan", "wrapper_failure"])
def test_cancel_grant_is_best_effort_and_bound_to_submitting_turn(lane, monkeypatch, caplog, capsys, case):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    active_turn = "unrelated" if case == "unrelated" else lane.turn_id
    assert session_store.try_begin_turn(lane.session_id, active_turn, 300.0)
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))
    cancel = Mock(side_effect=lambda: session_store.end_turn(lane.session_id, active_turn))
    monkeypatch.setitem(turn_runner._cancellers, active_turn, cancel)
    revoke = Mock(wraps=lane.store.revoke_stage_grants_for_turn)
    monkeypatch.setattr(lane.store, "revoke_stage_grants_for_turn", revoke)
    if case == "off":
        monkeypatch.delenv("LEAF_STAGE_GRANT_TTL_S")
    elif case == "failure":
        revoke.side_effect = RuntimeError("private detail")
    elif case == "wrapper_failure":
        monkeypatch.setattr(grants, "revoke_for_turn", Mock(side_effect=RuntimeError("private detail")))
    elif case == "orphan":
        monkeypatch.delitem(turn_runner._cancellers, active_turn)
        monkeypatch.setattr(turn_runner, "_auto_confirm_reads", Mock())
        monkeypatch.setattr(turn_runner, "_kick_queued", Mock())
    assert turn_runner.request_cancel(lane.tenant, lane.session_id, active_turn) == "cancelled"
    row = lane.store.get_stage_grant(TENANT, change.change_set_id)
    assert (row["revoked_ms"] is not None) is (case == "orphan")
    if case in {"off", "wrapper_failure"}:
        revoke.assert_not_called()
    else:
        revoke.assert_called_once()
    if case in {"failure", "wrapper_failure"}:
        logs = caplog.text + capsys.readouterr().err
        assert "RuntimeError" in logs
        assert "private detail" not in logs


@pytest.mark.parametrize("field", ["tenant_id", "authority_session_id", "authority_turn_id"])
def test_cancel_store_requires_all_authority_fields(lane, field):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    arguments = dict(tenant_id=TENANT, authority_session_id=lane.session_id,
                     authority_turn_id=lane.turn_id, reason="cancelled", now_ms=123)
    assert lane.store.revoke_stage_grants_for_turn(**{**arguments, field: "other"}) == 0
    assert lane.store.revoke_stage_grants_for_turn(**arguments) == 1
    assert lane.store.revoke_stage_grants_for_turn(**arguments) == 0
    assert lane.store.get_stage_grant(TENANT, change.change_set_id)["revoked_ms"] == 123


def test_cancel_revokes_each_matching_grant_once(lane):
    changes = [queue(lane, key=key) for key in ("first", "second")]
    for change in changes:
        assert grants.issue(lane.service, change)
    arguments = dict(tenant_id=TENANT, authority_session_id=lane.session_id,
                     authority_turn_id=lane.turn_id, reason="cancelled", now_ms=123)
    assert lane.store.revoke_stage_grants_for_turn(**arguments) == 2
    assert lane.store.revoke_stage_grants_for_turn(**arguments) == 0
    for change in changes:
        events = lane.store.audit_events(tenant_id=TENANT, change_set_id=change.change_set_id)
        cancelled = [event for event in events if event.reason_code == "stage_grant_cancelled"]
        assert len(cancelled) == 1
        assert cancelled[0].prior_state is cancelled[0].next_state is ChangeState.STAGING


def test_cancelled_grant_refuses_even_while_turn_still_active(lane, monkeypatch):
    change = issued_claim(lane)
    lane.store.revoke_stage_grants_for_turn(
        tenant_id=TENANT, authority_session_id=lane.session_id,
        authority_turn_id=lane.turn_id, reason="cancelled", now_ms=123,
    )
    refuse(lane, change, "stage_grant_cancelled")
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: SUBJECT)
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CustomizationServiceError, match="stage_grant_cancelled") as caught:
        lane.service.dispatch_stage(change)
    assert caught.value.harness_not_called is True
    post.assert_not_called()


def test_revoke_configured_service_failure_never_raises(lane, monkeypatch, caplog):
    def unavailable(cls):
        raise RuntimeError("private configuration")

    monkeypatch.setattr(CustomizationService, "configured", classmethod(unavailable))
    grants.revoke_for_turn(TENANT, lane.session_id, lane.turn_id, reason="cancelled")
    assert "RuntimeError" in caplog.text
    assert "private configuration" not in caplog.text


def test_no_services_transport_failure_is_unchanged(lane, monkeypatch):
    change = issued_claim(lane)
    monkeypatch.setattr(requests, "post", Mock(side_effect=requests.ConnectionError("offline")))
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.dispatch_stage(change)
    assert caught.value.code == "customization_harness_unavailable"
    assert getattr(caught.value, "harness_reason", None) is None


@pytest.mark.parametrize("without_services,reason", [
    (True, None), (True, "Safe reason."), (True, "x" * 600), (False, "Safe reason."),
])
def test_job_failure_stage_status_explains_waiting_without_services(lane, monkeypatch, without_services, reason):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    clean(lane)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: None if without_services else SUBJECT)
    response = SimpleNamespace(status_code=500)
    error = requests.HTTPError(response=response)
    post = Mock(side_effect=error)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(CustomizationService, "_harness_job_failure", classmethod(lambda cls, _: (True, reason)))
    assert worker.run_once("worker-a", service=lane.service)
    failed = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    status = lane.service.stage_status_change(failed)
    assert status["error"]["reason_code"] == "customization_author_job_failed"
    message = status["error"]["message"]
    assert len(message) <= 500
    if without_services:
        assert post.call_args.kwargs["headers"]["X-Author-Services"] == "none"
        assert message.endswith(WAITING_SERVICES_NOTICE)
        if reason:
            assert message.startswith(reason[:20])
    else:
        assert message == reason
        assert WAITING_SERVICES_NOTICE not in message


def test_issue_store_failure_leaves_enqueue_successful_and_logs_only_type(lane, monkeypatch, caplog):
    monkeypatch.setattr(lane.store, "insert_stage_grant", Mock(side_effect=RuntimeError("private detail")))
    assert enqueue(lane)["status"] == "queued"
    assert "RuntimeError" in caplog.text
    assert "private detail" not in caplog.text


@pytest.mark.parametrize("code,message", [
    ("stage_grant_exhausted", "This build was tried twice and could not be finished. Ask for it again. " + WAITING_SERVICES_NOTICE),
    ("stage_grant_cancelled", "This build was stopped from the conversation before it started."),
    ("stage_grant_turn_open", "The conversation that asked for this build was stopped, so it was not started. Ask for it again."),
    ("stage_grant_unavailable", "This build could not be checked and was not started. Ask for it again."),
])
def test_grant_refusals_have_actionable_route_messages(code, message):
    response = author_router._customization_error(CustomizationServiceError(code, 409))
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["reason_code"] == code
    assert body["error"]["message"] == message
    if code == "stage_grant_cancelled":
        assert body["harness_not_called"] is True
        assert body["error"]["retryable"] is False


@pytest.mark.parametrize("case,status,withdrawn,revoked", [
    ("success", 200, True, True), ("claimed", 200, False, True),
    ("other_author", 404, None, None), ("other_tenant", 404, None, None),
    ("off", 200, True, True), ("missing", 404, None, None),
    ("rollout_off", 200, True, True), ("viewer", 200, True, True),
    ("entitlement_lost", 200, True, True), ("no_grant", 200, False, False),
    ("auth_off", 503, None, None), ("no_subject", 403, None, None),
    ("invalid_tenant", 403, None, None), ("invalid_id", 422, None, None),
])
def test_withdraw_route_is_authenticated_and_author_bound(lane, monkeypatch, case, status, withdrawn, revoked):
    change = queue(lane)
    finish = Mock()
    monkeypatch.setattr(service_module, "finish_stage_authority_for_change", finish)
    if case != "no_grant":
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
    elif case == "auth_off":
        monkeypatch.setattr(deps, "auth_live", lambda: False)
    elif case == "no_subject":
        tenant = deps.TenantContext(TENANT, tier="hosted_pro")
    elif case == "invalid_tenant":
        tenant = deps.TenantContext("../invalid", tier="hosted_pro", subject=SUBJECT)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: case != "rollout_off")
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))
    app = FastAPI()
    app.include_router(author_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    change_id = "00000000-0000-4000-8000-000000000000" if case == "missing" else change.change_set_id
    if case == "invalid_id":
        change_id = "invalid"
    with TestClient(app) as client:
        response = client.post(f"/api/author/stages/{change_id}/withdraw")
    assert response.status_code == status
    if status == 200:
        assert response.json() == {
            "contract": "leaf.customization-stage-withdraw.v1", "withdrawn": withdrawn, "revoked": revoked,
        }
    if status != 200:
        assert lane.store.get_stage_grant(TENANT, change.change_set_id)["revoked_ms"] is None
    if withdrawn:
        finish.assert_called_once_with(lane.store.get_change_set(
            tenant_id=TENANT, change_set_id=change.change_set_id,
        ))
        assert finish.call_args.args[0].state is ChangeState.FAILED
    else:
        finish.assert_not_called()


@pytest.mark.parametrize("failure", ["read", "finish"])
def test_withdraw_cleanup_failure_preserves_success(lane, monkeypatch, failure):
    change = queue(lane)
    assert grants.issue(lane.service, change)
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))
    original_read = lane.store.get_change_set
    read = Mock(side_effect=[change, RuntimeError("cleanup read failed")])
    finish = Mock(side_effect=RuntimeError("cleanup failed"))
    monkeypatch.setattr(service_module, "finish_stage_authority_for_change", finish)
    if failure == "read":
        monkeypatch.setattr(lane.store, "get_change_set", read)
    response = author_router.withdraw_stage(change.change_set_id, tenant=lane.tenant)
    assert response == {
        "contract": "leaf.customization-stage-withdraw.v1", "withdrawn": True, "revoked": True,
    }
    failed = original_read(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert failed.state is ChangeState.FAILED
    if failure == "read":
        assert read.call_count == 2
        finish.assert_not_called()
    else:
        finish.assert_called_once_with(failed)


@pytest.fixture
def refund_lane(lane, monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_STORE", "memory")
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_REFUND_UNSTARTED", "1")
    author_quota.reset_memory_state()
    yield lane
    author_quota.reset_memory_state()


def charge_build(change):
    assert author_quota.charge(
        TENANT, "2026-11-25", 10, idempotency_key=change.change_set_id,
    )[0]


def refund_count():
    return author_quota._MEMORY_STATE["2026-11-25:tenant-a"]


def refuse_dispatch(lane, monkeypatch, code):
    original = lane.service.dispatch_stage

    def dispatch(change):
        if code == "stage_grant_expired":
            with lane.store._transaction() as conn:
                conn.execute("UPDATE customization_stage_grants SET issued_ms = 1, expires_ms = 2")
        elif code == "stage_grant_refused":
            monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda _: (None, None))
        elif code == "stage_grant_withdrawn":
            assert lane.store.withdraw_stage(
                tenant_id=TENANT, change_set_id=change.change_set_id,
                author_subject=SUBJECT, now_ms=int(time.time() * 1000),
            ) == {"withdrawn": False, "revoked": True}
        elif code == "stage_grant_unavailable":
            monkeypatch.setattr(lane.store, "consume_stage_grant", Mock(side_effect=RuntimeError("store down")))
        elif code == "stage_grant_exhausted":
            with lane.store._transaction() as conn:
                conn.execute("UPDATE customization_stage_grants SET dispatches = 2")
        return original(change)

    monkeypatch.setattr(lane.service, "dispatch_stage", dispatch)


@pytest.mark.parametrize("code", [
    "stage_grant_expired", "stage_grant_refused", "stage_grant_withdrawn",
    "stage_grant_turn_open", "stage_grant_unavailable",
])
@pytest.mark.parametrize("refund_on,attempt", [(True, 1), (False, 1), (True, 2)])
def test_grant_refusal_refunds_only_enabled_first_attempt(
    refund_lane, monkeypatch, code, refund_on, attempt,
):
    lane = refund_lane
    if not refund_on:
        monkeypatch.delenv("LEAF_AUTHOR_QUOTA_REFUND_UNSTARTED")
    change = queue(lane)
    assert grants.issue(lane.service, change)
    charge_build(change)
    if code != "stage_grant_turn_open":
        clean(lane)
    if attempt == 2:
        prior = lane.store.claim_stage(owner="prior-worker")
        assert prior.stage_attempt == 1
        # A previous dispatch might have reached the harness.
        if code != "stage_grant_turn_open":
            grants.admit(lane.service, prior, "prior-worker")
        lane.store.defer_stage_claim(
            tenant_id=TENANT, change_set_id=change.change_set_id,
            owner="prior-worker", reason_code="retry", delay_seconds=0,
        )
    refuse_dispatch(lane, monkeypatch, code)
    refund = Mock(wraps=author_quota.refund_unstarted) if refund_on else Mock(
        side_effect=AssertionError("refund disabled"),
    )
    monkeypatch.setattr(author_quota, "refund_unstarted", refund)
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    assert worker.run_once("worker-a", service=lane.service)
    failed = lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id)
    assert failed.state is ChangeState.FAILED
    assert failed.stage_attempt == attempt
    assert failed.stage_error_code == code
    assert failed.stage_error_retryable is (code in {"stage_grant_expired", "stage_grant_unavailable"})
    if refund_on and attempt == 1:
        assert refund_count() == 0
        refund.assert_called_once_with(TENANT, change.change_set_id)
    else:
        assert refund_count() == 1
        refund.assert_not_called()
    post.assert_not_called()


def test_exhausted_grant_never_refunds_even_on_first_claim(refund_lane, monkeypatch):
    lane = refund_lane
    change = queue(lane)
    assert grants.issue(lane.service, change)
    clean(lane)
    charge_build(change)
    refuse_dispatch(lane, monkeypatch, "stage_grant_exhausted")
    refund = Mock(side_effect=AssertionError("exhausted"))
    post = Mock()
    monkeypatch.setattr(author_quota, "refund_unstarted", refund)
    monkeypatch.setattr(requests, "post", post)
    assert worker.run_once("worker-a", service=lane.service)
    assert refund_count() == 1
    refund.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("case", ["success", "revoked_only", "disabled", "raising"])
def test_withdraw_refund_follows_committed_withdrawal(refund_lane, monkeypatch, caplog, case):
    lane = refund_lane
    change = queue(lane)
    assert grants.issue(lane.service, change)
    charge_build(change)
    if case == "revoked_only":
        lane.store.claim_stage(owner="worker-a")
    if case == "disabled":
        monkeypatch.delenv("LEAF_AUTHOR_QUOTA_REFUND_UNSTARTED")
    original = author_quota.refund_unstarted

    def refund(tenant, change_id):
        assert lane.store.get_change_set(tenant_id=tenant, change_set_id=change_id).state is ChangeState.FAILED
        if case == "raising":
            raise RuntimeError("private refund detail")
        return original(tenant, change_id)

    refund = Mock(side_effect=refund)
    monkeypatch.setattr(author_quota, "refund_unstarted", refund)
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))
    app = FastAPI()
    app.include_router(author_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: lane.tenant
    with TestClient(app) as client:
        first = client.post(f"/api/author/stages/{change.change_set_id}/withdraw")
        second = client.post(f"/api/author/stages/{change.change_set_id}/withdraw")
    assert first.status_code == second.status_code == 200
    assert first.json()["withdrawn"] is (case != "revoked_only")
    assert second.json()["withdrawn"] is False
    if case in {"success", "raising"}:
        refund.assert_called_once_with(TENANT, change.change_set_id)
    else:
        refund.assert_not_called()
    assert refund_count() == (0 if case == "success" else 1)
    if case != "revoked_only":
        assert lane.store.get_change_set(tenant_id=TENANT, change_set_id=change.change_set_id).state is ChangeState.FAILED
    if case == "raising":
        warnings = [record for record in caplog.records if "author_quota_refund_unstarted_failed" in record.message]
        assert len(warnings) == 1
        assert "RuntimeError" in warnings[0].message
        assert "private refund detail" not in caplog.text


@pytest.mark.parametrize("route_first", [True, False])
def test_withdraw_and_worker_refusal_refund_at_most_once(refund_lane, monkeypatch, route_first):
    lane = refund_lane
    change = queue(lane)
    assert grants.issue(lane.service, change)
    charge_build(change)
    clean(lane)
    refund = Mock(wraps=author_quota.refund_unstarted)
    post = Mock()
    monkeypatch.setattr(author_quota, "refund_unstarted", refund)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))

    def withdraw():
        return author_router.withdraw_stage(change.change_set_id, tenant=lane.tenant)

    if route_first:
        assert withdraw()["withdrawn"] is True
        assert worker.run_once("worker-a", service=lane.service) is False
    else:
        original = lane.service.dispatch_stage

        def dispatch(claimed):
            assert withdraw() == {
                "contract": "leaf.customization-stage-withdraw.v1",
                "withdrawn": False, "revoked": True,
            }
            return original(claimed)

        monkeypatch.setattr(lane.service, "dispatch_stage", dispatch)
        assert worker.run_once("worker-a", service=lane.service)
        assert withdraw()["withdrawn"] is False
    refund.assert_called_once_with(TENANT, change.change_set_id)
    assert refund_count() == 0
    post.assert_not_called()


@pytest.mark.parametrize("route_first", [True, False])
def test_worker_and_route_share_three_refunds_per_tenant_day(refund_lane, monkeypatch, route_first):
    lane = refund_lane
    clean(lane)
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    for index in range(4):
        change = queue(lane, key=f"refund-{index}")
        assert grants.issue(lane.service, change)
        charge_build(change)
        if (index % 2 == 0) == route_first:
            assert author_router.withdraw_stage(change.change_set_id, tenant=lane.tenant)["withdrawn"] is True
        else:
            with monkeypatch.context() as dispatch_patch:
                refuse_dispatch(lane, dispatch_patch, "stage_grant_refused")
                assert worker.run_once("worker-a", service=lane.service)
        assert refund_count() == (0 if index < 3 else 1)
    post.assert_not_called()
