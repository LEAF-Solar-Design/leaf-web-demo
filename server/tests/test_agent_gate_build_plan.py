"""Exercise the approval record, the author back edge and the durable build reservation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import agent_gate
import agent_pg_store
import agent_policy
import author_quota
import build_plan
import customization_service
import deps
from customization_models import ChangeState
from customization_store import SQLiteCustomizationStore
from routers import agent as agent_router, author
from test_agent_gate import agent_env
from test_agent_gate_postgres import _AtomicCounter, _AtomicDatabase, _database_row
from test_build_plan import field, maximal, minimal
from test_customization_async_stage import _admission_service


SUBJECT = "auth0|author"
TENANT = "tenant-a"
SESSION = "app-session"
TURN = "app-build-turn"


@pytest.fixture(autouse=True)
def plan_mode(agent_env, monkeypatch):
    monkeypatch.setenv("LEAF_AGENT_STORE", "legacy")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", "all")


def call(action, args, *, tenant=TENANT, session=SESSION, turn=TURN, subject=SUBJECT, **authority):
    return agent_gate.gate(tenant, session, turn, action, args,
                           {"build": True, "converse": True, "deploy": True},
                           subject=subject, **authority)


def create_plan():
    return dict(minimal(), target="studio", tool_name="spiral-panels")


def propose(plan=None, **context):
    result = call("propose_build_plan", {"plan": plan or create_plan()}, **context)
    assert result["decision"] == "awaiting_approval"
    return result["confirmation_id"]


def approve(plan=None, **context):
    cid = propose(plan, **context)
    assert agent_gate.grant_approval(cid, by=SUBJECT)[0]
    return cid


def redeem(cid, *, retry=None, **context):
    args = {"description": "unapproved model description", "plan_confirmation_id": cid}
    if retry is not None:
        args["retry_of_change_set_id"] = retry
    return call("author_tool", args, **context)


@pytest.mark.parametrize("ttl", [None, False, "1800", 59, 3601])
def test_invalid_action_ttl_is_a_load_error(ttl):
    record = dict(agent_policy._HARDCODED_DEFAULTS["actions"]["propose_build_plan"], approval_ttl_s=ttl)
    with pytest.raises(agent_policy.PolicyError, match="approval_ttl_s"):
        agent_policy._parse_action("propose_build_plan", record)


def test_proposal_records_subject_but_never_dispatches():
    cid = propose()
    record = agent_gate.read_pending(cid)
    assert record["args"] == {"plan": create_plan()}
    assert record["proposer_subject"] == SUBJECT
    assert record["granted"] is False
    ok, _, reason = agent_gate.grant_approval(cid, by="someone-else")
    assert not ok and reason == "plan_approver_mismatch"
    assert not agent_gate.read_pending(cid)["granted"]
    assert agent_gate.grant_approval(cid, by=SUBJECT)[0]
    assert "consumed_at" not in agent_gate.read_pending(cid)
    assert not agent_gate.grants_file().exists()
    action = agent_policy.load_policy().actions["propose_build_plan"]
    assert action.dispatch == {"kind": "none", "routes": []}
    assert (action.policy, action.required_capability, action.rung) == ("always-confirm", "build", 5)


def test_proposal_refuses_invalid_plan_without_jsonschema(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    result = call("propose_build_plan", {"plan": dict(create_plan(), target="desktop")})
    assert result["decision"] == "deny" and result["reason"] == "plan_invalid_field:target"
    assert not agent_gate.list_pending()


@pytest.mark.parametrize("cid", ["", "existing"])
def test_proposal_never_accepts_confirmation_id(cid):
    result = call("propose_build_plan", {"plan": create_plan(), "confirmation_id": cid})
    assert result["reason"] == "plan_confirmation_id_not_allowed"
    assert not agent_gate.list_pending()


@pytest.mark.parametrize("minutes,expected", [(20, "allow"), (31, "plan_expired")])
def test_plan_lifetime_starts_at_proposal(monkeypatch, minutes, expected):
    start = agent_gate._now().replace(microsecond=0)
    monkeypatch.setattr(agent_gate, "_now", lambda: start)
    cid = propose()
    assert agent_gate.session_card_ttl_s(TENANT, cid, 600) == 1800
    assert agent_gate.session_card_ttl_s(TENANT, "legacy-card", 600) == 600
    monkeypatch.setattr(agent_gate, "_now", lambda: start + timedelta(minutes=20))
    assert agent_gate.session_card_ttl_s(TENANT, cid, 600) == 600
    assert agent_gate.grant_approval(cid, by=SUBJECT)[0]
    monkeypatch.setattr(agent_gate, "_now", lambda: start + timedelta(minutes=minutes))
    result = redeem(cid)
    if expected == "allow":
        assert result["decision"] == "allow"
    else:
        assert result["decision"] == "deny" and result["reason"] == expected


@pytest.mark.parametrize("context,reason", [
    ({"session": "other-session"}, "plan_session_mismatch"),
    ({"tenant": "other-tenant"}, "plan_not_approved"),
    ({"subject": "other-user"}, "plan_subject_mismatch"),
    ({"subject": None}, "plan_subject_mismatch"),
])
def test_foreign_redemption_does_not_spend_the_plan(context, reason):
    cid = approve()
    result = redeem(cid, **context)
    assert result["decision"] == "deny" and result["reason"] == reason
    assert "consumed_at" not in agent_gate.read_pending(cid)


def test_author_requires_an_approved_plan_even_with_an_ordinary_confirmation():
    cid = propose()
    missing = call("author_tool", {"description": "model", "confirmation_id": cid})
    assert missing["reason"] == "plan_required"
    assert redeem("absent")["reason"] == "plan_not_approved"
    assert redeem(cid)["reason"] == "plan_not_approved"
    assert agent_gate.deny_approval(cid, by=SUBJECT)[0]
    assert redeem(cid)["reason"] == "plan_not_approved"


def test_concurrent_redemptions_authorize_once_without_a_grant_or_second_card():
    cid = approve(turn="proposal-turn")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: redeem(cid), range(2)))
    assert sorted(r["decision"] for r in results) == ["allow", "deny"]
    assert {r["reason"] for r in results} == {"allow_via_approval", "plan_consumed"}
    assert agent_gate.read_pending(cid)["consumed_turn_id"] == TURN
    assert not agent_gate.grants_file().exists()
    assert not agent_gate.list_pending()
    assert len(list(agent_gate.approvals_dir().glob("*.json"))) == 1


def test_plan_registers_the_app_authority_tuple(monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "test-secret")
    sessions = agent_router.session_store
    monkeypatch.setattr(sessions, "get_session", lambda _: {"tenant_id": TENANT})
    monkeypatch.setattr(sessions, "turn_started_data", lambda *_: {})
    monkeypatch.setattr(sessions, "active_turn_tier", lambda *_: "hosted_pro")
    monkeypatch.setattr(sessions, "active_turn_subject", lambda *_: SUBJECT)

    def gate(action, args, app_turn, harness_turn):
        request = agent_router.GateRequest(
            tenant_id=TENANT, session_id="loop-session", turn_id=harness_turn,
            authority_session_id=SESSION, authority_turn_id=app_turn, action=action, args=args,
        )
        return agent_router.internal_gate(request, x_dispatch_secret="test-secret")

    proposal = gate("propose_build_plan", {"plan": create_plan()}, "app-proposal-turn", "loop-proposal-turn")
    cid = proposal["confirmation_id"]
    assert agent_gate.read_pending(cid)["turn_id"] == "app-proposal-turn"
    assert agent_gate.grant_approval(cid, by=SUBJECT)[0]
    result = gate("author_tool", {"description": "model", "plan_confirmation_id": cid}, TURN, "loop-build-turn")
    assert result["decision"] == "allow"
    assert agent_gate.consumed_build_plan(TENANT, SESSION, TURN, SUBJECT)["confirmation_id"] == cid
    assert agent_gate.consumed_build_plan(TENANT, SESSION, "loop-build-turn", SUBJECT) is None


def test_foreign_approver_cannot_mark_the_session_card_approved(monkeypatch):
    sessions = agent_router.session_store
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "legacy")
    session = sessions.get_or_create_session(TENANT, str(uuid4()))
    session_id = session["session_id"]
    cid = propose(session=session_id)
    sessions.create_approval(cid, session_id, TENANT, TURN, tool=None, params=None,
                             capability=None, rationale=None, kind="propose_build_plan",
                             payload=None, ttl_s=1800)
    response = agent_router.decide_approval(
        cid, agent_router.ApprovalDecisionRequest(approved=True),
        tenant=deps.TenantContext(TENANT, subject="someone-else"),
    )
    assert response.status_code == 403
    assert b"plan_approver_mismatch" in response.body
    assert not sessions.get_approval(cid)["decided"]
    assert not agent_gate.read_pending(cid)["granted"]


def test_postgres_audit_failure_rolls_back_plan_consumption_and_rate_charge(monkeypatch):
    plan = dict(create_plan(), inputs=[field(type="number", default=-0.0)])
    normalized = dict(plan, inputs=[field(type="number", default=0.0)])
    row = dict(_database_row(granted=True), tenant_id=TENANT, session_id=SESSION,
               action="propose_build_plan", args={"plan": normalized}, proposed_plan=plan,
               args_hash=agent_gate.canonical_args_hash({"plan": plan}),
               proposer_subject=SUBJECT, decided_by=SUBJECT)
    db = _AtomicDatabase(row)
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (db, None))
    monkeypatch.setattr(agent_pg_store, "_counter_store", _AtomicCounter(db))
    monkeypatch.setattr(agent_pg_store, "kill_switch_details", lambda: {"active": False})
    monkeypatch.setattr(agent_pg_store, "tenant_state",
                        lambda _: {"agent_disabled": False, "overlay": {}, "revision": 1})

    def audit_fails(conn, event):
        assert event["kind"] == "allowed"
        assert db.row["consumed_at"] is not None and db.counter == 1
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(agent_pg_store, "_append_audit_in_transaction", audit_fails)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        redeem(row["confirmation_id"])
    assert db.rolled_back and db.counter == 0 and db.row["consumed_at"] is None
    assert not db.grants


def test_authoring_supersedes_plan_first_only_for_the_plan_flow(monkeypatch):
    monkeypatch.setattr(agent_gate, "_plan_first_session", lambda *_: True)
    assert call("read_platform_state", {"what": "capabilities"})["decision"] == "allow"
    assert call("request_publication", {"change_set_id": str(uuid4())})["decision"] == "allow"
    assert redeem(approve())["decision"] == "allow"


def test_disabled_tenant_keeps_legacy_gate(monkeypatch):
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "other-tenant")
    assert call("propose_build_plan", {"plan": create_plan()})["reason"] == "unknown_action"
    assert call("author_tool", {"description": "model"})["decision"] == "awaiting_approval"


@pytest.fixture
def author_api(tmp_path, monkeypatch):
    store = SQLiteCustomizationStore(tmp_path / "changes.db")
    service, tenant = _admission_service(store, monkeypatch)
    monkeypatch.setattr(author.CustomizationService, "configured", lambda: service)
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(deps, "active_stage_author_subject",
                        lambda tid, sid, turn: SUBJECT if (tid, sid, turn) == (TENANT, SESSION, TURN) else None)
    charges = []
    monkeypatch.setattr(author_quota, "enforce", lambda *_, idempotency_key: charges.append(idempotency_key))
    app = FastAPI()
    app.include_router(author.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    with TestClient(app) as client:
        def post(*, key="request", **body):
            return client.post("/api/author", json={"description": "model input", **body}, headers={
                "Idempotency-Key": key, "X-Authority-Session-Id": SESSION, "X-Authority-Turn-Id": TURN,
            })
        yield post, store, charges


def test_route_refuses_an_unspent_plan(author_api):
    post, _, charges = author_api
    cid = approve()
    response = post(plan_confirmation_id=cid)
    assert response.status_code == 409 and response.json()["reason_code"] == "plan_required"
    assert not charges


@pytest.mark.parametrize("body", [
    {"target_tool_name": "leaf-morph", "target": "branch"},
    {"target_tool_name": "leaf-morph"}, {"target": "branch"},
])
def test_model_cannot_retarget_the_approved_build(author_api, body):
    post, _, charges = author_api
    assert redeem(approve())["decision"] == "allow"
    response = post(**body)
    assert response.status_code == 422 and response.json()["reason_code"] == "plan_binding_mismatch"
    assert not charges


def test_route_reserves_rendered_plan_once_even_over_body_description_limit(author_api):
    post, store, charges = author_api
    plan = dict(maximal(), tool_name="leaf-morph", target_tool_name="leaf-morph")
    cid = approve(plan)
    assert redeem(cid)["decision"] == "allow"
    assert not charges
    response = post()
    assert response.status_code == 202, response.text
    change_id = response.json()["change_set_id"]
    row = store.get_change_set(tenant_id=TENANT, change_set_id=change_id)
    assert row.request_description == build_plan.render_description(plan)
    assert len(row.request_description) > author.MAX_AUTHOR_DESCRIPTION
    assert json.loads(row.plan_json) == plan
    assert row.plan_digest == build_plan.plan_digest(plan)
    assert row.plan_confirmation_id == cid
    assert (row.change_kind, row.target_tool_name) == ("revise", "leaf-morph")
    assert post().json()["change_set_id"] == change_id
    assert post(key="another-request").status_code == 409
    assert charges == [change_id]


def test_branch_stage_request_uses_approved_name_target_and_description(author_api, monkeypatch):
    import requests
    post, store, charges = author_api
    plan = dict(create_plan(), target="branch")
    assert redeem(approve(plan))["decision"] == "allow"
    response = post(target="studio")
    assert response.status_code == 422 and response.json()["reason_code"] == "plan_binding_mismatch"
    job = post().json()
    change = store.get_change_set(tenant_id=TENANT, change_set_id=job["change_set_id"])
    assert change.tool_target == "branch" and change.change_kind == "create"
    bodies = []

    def stage(url, *, json, **kwargs):
        assert url.endswith("/author/stage")
        bodies.append(json)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    monkeypatch.setattr(customization_service, "_harness_config", lambda: ("http://harness", "secret"))
    monkeypatch.setattr(requests, "post", stage)
    author.CustomizationService.configured()._harness_stage(TENANT, change.request_description, change)
    assert len(bodies) == 1 and charges == [change.change_set_id]
    assert bodies[0]["description"] == build_plan.render_description(plan)
    assert (bodies[0]["toolName"], bodies[0]["target"]) == ("spiral-panels", "branch")
    assert "targetToolName" not in bodies[0]


@pytest.mark.parametrize("prior", ["missing", "not_failed", "other_tenant", "other_plan", "matching"])
def test_retry_requires_failed_same_tenant_same_digest(author_api, prior):
    post, store, charges = author_api
    original_id = str(uuid4())
    if prior != "missing":
        plan = create_plan()
        if prior == "other_plan":
            plan["tool_name"] = "different-tool"
        digest = build_plan.plan_digest(plan)
        tenant = "other-tenant" if prior == "other_tenant" else TENANT
        row, _ = store.reserve_stage(
            change_set_id=original_id, tenant_id=tenant, idempotency_key="prior",
            author_subject=SUBJECT, base_commit="a" * 40, desired_platform_release="rel",
            workspace_contract_digest="b" * 64, request_description=build_plan.render_description(plan),
            request_fingerprint="c" * 64, plan_confirmation_id="old",
            plan_digest=digest,
            plan_json=build_plan.canonical_bytes(plan).decode(),
        )
        if prior != "not_failed":
            store.transition(tenant_id=tenant, change_set_id=original_id, next_state=ChangeState.FAILED,
                             expected_version=row.version, idempotency_key="fail", expected_state=ChangeState.CREATED)
    assert redeem(approve(), retry=original_id)["decision"] == "allow"
    response = post()
    if prior == "matching":
        assert response.status_code == 202, response.text
        assert charges == [response.json()["change_set_id"]]
    else:
        assert response.status_code == 409 and response.json()["reason_code"] == "retry_target_invalid"
        assert not charges
