"""Connected synthetic custody, real HTTP handlers and recorded PostgreSQL statements."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import project_checkout_custody as custody
import project_execution_context as execution
import solar_project_context as project
from routers import agent

ORG = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
DRAWING = "33333333-3333-4333-8333-333333333333"
VERSION = "44444444-4444-4444-8444-444444444444"
ACTOR = "55555555-5555-4555-8555-555555555555"
BINDING = {"schema": execution.SCHEMA, "organization_id": ORG, "project_id": PROJECT,
           "drawing_id": DRAWING, "input_version_id": VERSION, "intake_sha256": "a" * 64}
PINS = {"catalog_digest": "digest", "catalog_commit": "c" * 40,
        "effective_catalog_digest": "d" * 64, "tool_manifest_sha256": "digest"}
PARAMS = {"drawing_id": DRAWING, "expected_rev": 17, "changes": {"num_mppt": 2}}
ARGS = {"tool": "solar-settings", "params": PARAMS, "dwg": DRAWING,
        execution.KEY: BINDING, **PINS}


class MemorySQL:
    """Execute only this module's parameterized statements; no claimed PG durability."""
    def __init__(self):
        self.rows, self.statements, self.result = {}, [], None
    def run_transaction(self, operation):
        before = deepcopy(self.rows)
        try:
            return operation(self)
        except Exception:
            self.rows = before
            raise
    def cursor(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *_a):
        return False
    def execute(self, sql, values):
        self.statements.append((sql, values))
        if sql.startswith("SELECT"):
            self.result = deepcopy(self.rows.get(tuple(values)))
        elif sql.startswith("INSERT"):
            delegation, tenant, cid, session, subject, actor, binding, args, fence, expires = values
            self.rows.setdefault((tenant, cid), dict(delegation_id=delegation, tenant_id=tenant,
                confirmation_id=cid, session_id=session, subject=subject, actor_binding_id=actor,
                input_binding=deepcopy(binding.obj), approved_args=deepcopy(args.obj), fence=fence,
                expires_at=expires, revoked_at=None))
        elif sql.startswith("UPDATE"):
            tenant, cid, session, delegation, subject = values
            row = self.rows.get((tenant, cid))
            self.result = None
            if row and (row["session_id"], row["delegation_id"], row["subject"]) == (session, delegation, subject):
                row["revoked_at"] = row["revoked_at"] or datetime.now(timezone.utc)
                self.result = {"delegation_id": delegation}
        else:
            raise AssertionError("Unexpected SQL")
    def fetchone(self):
        return self.result


@pytest.fixture
def lane(monkeypatch):
    tenant = custody.deps.TenantContext(ORG, subject="auth0|owner", authority_resolved=True)
    now = datetime.now(timezone.utc)
    approval = dict(confirmation_id="cid", tenant_id=ORG, session_id="session", tool="solar-settings",
        params=deepcopy(PARAMS), capability="drawing.write", kind="run_capability", decided=True,
        approved=True, decided_by=tenant.subject, consumed=False, expires_at=time.time()+240,
        payload={"dwg": DRAWING, execution.KEY: deepcopy(BINDING)})
    gate = dict(tenant_id=ORG, action="run_write_tool", granted=True, denied=False,
        decided_by=tenant.subject, args=deepcopy(ARGS), expires_at=(now+timedelta(seconds=240)).isoformat())
    gate["args_hash"] = custody.agent_gate.canonical_args_hash(gate["args"])
    session = dict(tenant_id=ORG, project_id=PROJECT, org_id=ORG, active_turn_id="resume",
                   turn_started_at=time.time())
    confirm = dict(confirmation_id="cid", approved=True, proposal=dict(tool="solar-settings",
        params=deepcopy(PARAMS), capability="drawing.write", dwg=DRAWING))
    data = {execution.KEY: deepcopy(BINDING), "confirm": confirm}
    lease = project.CheckoutLease(UUID(ORG), UUID(PROJECT), UUID(DRAWING), "Browser",
        UUID(ACTOR), now, now+timedelta(seconds=120), 7)
    state = project.CheckoutState(UUID(ORG), UUID(PROJECT), UUID(DRAWING), lease, 7, now)
    db = MemorySQL()
    monkeypatch.setattr(custody.platform_link, "platform_db", lambda: db)
    monkeypatch.setattr(custody.session_store, "get_approval", lambda _cid: deepcopy(approval) if _cid == "cid" else None)
    monkeypatch.setattr(custody.session_store, "get_session", lambda _s: deepcopy(session))
    monkeypatch.setattr(custody.session_store, "turn_started_data", lambda *_a: deepcopy(data))
    monkeypatch.setattr(custody.session_store, "active_turn_subject", lambda *_a: tenant.subject)
    monkeypatch.setattr(custody.platform_link, "require_project_session_access",
                        lambda session, principal, **kw: session if str(principal) == ORG else None)
    monkeypatch.setattr(project, "_access", lambda *_a, **_kw: (UUID(ORG), UUID(ACTOR)))
    monkeypatch.setattr(custody.agent_gate, "read_pending_strict", lambda _c: (deepcopy(gate), "ok"))
    monkeypatch.setattr(custody.deps, "find_tool", lambda *_a: {"name": "solar-settings"})
    monkeypatch.setattr(custody.deps, "catalog_tool_digest", lambda _t: "digest")
    monkeypatch.setattr(custody.deps, "catalog_tool_view", lambda _t: {"catalog_digest": "digest"})
    monkeypatch.setattr(custody.customization_service, "effective_catalog_pin", lambda _t: dict(PINS))
    context = SimpleNamespace(binding=SimpleNamespace(organization_id=UUID(ORG), project_id=UUID(PROJECT),
        drawing_id=UUID(DRAWING), input_version_id=UUID(VERSION), is_head=True), intake_sha256="a"*64)
    monkeypatch.setattr(project, "resolve_context", lambda *_a, **_kw: context)
    monkeypatch.setattr(project.graph_store(), "get_checkout", lambda *_a, **_kw: state)
    verified = []
    monkeypatch.setattr(project.checkout_capability, "_secret", lambda: "synthetic-custody-test-signing-material")
    token = project.checkout_capability.mint(tenant, project.checkout_scope(PROJECT, DRAWING), 7)
    actual_verify = project.checkout_capability.verify
    def verify(capability, *args):
        verified.append(True)
        return actual_verify(capability, *args)
    monkeypatch.setattr(project.checkout_capability, "verify", verify)
    monkeypatch.setattr(project.graph_store(), "verify_checkout", lambda *_a, **_kw: lease)
    def forbidden(*_a, **_kw):
        raise AssertionError("Custody must never acquire, renew or mint")
    monkeypatch.setattr(project, "acquire_project_checkout", forbidden)
    app = FastAPI()
    app.include_router(agent.router)
    app.dependency_overrides[custody.deps.require_active_tenant] = lambda: tenant
    return SimpleNamespace(tenant=tenant, approval=approval, gate=gate, session=session, data=data,
        lease=lease, state=state, db=db, token=token, verified=verified, client=TestClient(app))


def issue(lane):
    return custody.issue(lane.tenant, "cid", "session", VERSION, lane.token)


def proof(lane):
    lane.approval["consumed"] = True
    lane.gate["consumed_at"] = datetime.now(timezone.utc).isoformat()
    return custody.resolve_for_turn(lane.tenant, "session", "resume", tool="solar-settings",
        params=deepcopy(PARAMS), catalog_pins=dict(PINS), input_binding=deepcopy(BINDING))


def test_issue_connected_possession_idempotency_and_no_credentials_stored(lane):
    first = issue(lane)
    assert first == issue(lane) and first["fence"] == lane.lease.fence == 7
    assert first["expires_at"] == lane.lease.expires_at.isoformat()
    assert len(lane.db.rows) == 1 and len(lane.verified) == 2
    assert lane.token not in repr(lane.db.rows) and lane.token not in repr(lane.db.statements)
    assert not any(sql.startswith("UPDATE") for sql, _ in lane.db.statements)
    assert any("ON CONFLICT (tenant_id,confirmation_id) DO NOTHING" in sql for sql, _ in lane.db.statements)


@pytest.mark.parametrize("capability", [None, {}, {"holder": "Browser", "fence": 7}, "lco1." + "f"*64])
def test_public_metadata_and_wrong_capability_cannot_issue(lane, capability):
    with pytest.raises(custody.CustodyError):
        custody.issue(lane.tenant, "cid", "session", VERSION, capability)
    assert not lane.db.rows


@pytest.mark.parametrize("field,value", [("approved", False), ("decided", False),
    ("decided_by", "foreign"), ("session_id", "foreign"), ("tenant_id", "foreign"),
    ("expires_at", 0), ("consumed", True)])
def test_issue_requires_approved_owned_unexpired_unconsumed_confirmation(lane, field, value):
    lane.approval[field] = value
    with pytest.raises(custody.CustodyError):
        issue(lane)
    assert not lane.db.rows


def test_issue_does_not_require_finished_proposal_turn_to_remain_active(lane):
    lane.session.update(active_turn_id=None, turn_started_at=None)
    assert issue(lane)["delegated"]


@pytest.mark.parametrize("change", ["version", "hash", "catalog", "subject", "dispatch"])
def test_issue_rejects_current_authority_drift(lane, monkeypatch, change):
    if change == "version":
        with pytest.raises(custody.CustodyError):
            custody.issue(lane.tenant, "cid", "session", ACTOR, lane.token)
        return
    if change == "hash":
        lane.approval["payload"][execution.KEY]["intake_sha256"] = "b"*64
    elif change == "catalog":
        monkeypatch.setattr(custody.customization_service, "effective_catalog_pin", lambda _t:
                            {**PINS, "catalog_commit": "b"*40})
    elif change == "subject":
        lane.tenant = custody.deps.TenantContext(ORG, subject="foreign", authority_resolved=True)
    else:
        lane.tenant = custody.deps.TenantContext(ORG, subject="auth0|owner", authority_resolved=True, backedge=True)
    with pytest.raises(custody.CustodyError):
        issue(lane)


def test_typed_proof_rechecks_revocation_and_never_serializes(lane):
    issued = issue(lane)
    grant = proof(lane)
    assert grant.fence_for(lane.tenant, project.checkout_scope(PROJECT, DRAWING), lane.state) == 7
    assert grant.confirmation_id == "cid"
    assert grant.idempotency_key == "checkout-custody:" + issued["delegation_id"]
    copied = grant.input_binding
    copied["intake_sha256"] = "b"*64
    assert grant.input_binding == BINDING
    with pytest.raises(AttributeError):
        grant.idempotency_key = "forged"
    with pytest.raises(TypeError):
        json.dumps(grant)
    with pytest.raises(custody.CustodyError):
        custody.DelegatedCheckout(None, {}, ())
    assert custody.revoke(lane.tenant, "cid", "session", issued["delegation_id"]) == {"revoked": True}
    assert custody.revoke(lane.tenant, "cid", "session", issued["delegation_id"]) == {"revoked": True}
    with pytest.raises(project.ProjectContextError, match="CHECKOUT_DENIED"):
        grant.fence_for(lane.tenant, project.checkout_scope(PROJECT, DRAWING), lane.state)
    with pytest.raises(custody.CustodyError):
        issue(lane)


@pytest.mark.parametrize("change", ["unconsumed", "gate_unconsumed", "turn", "confirm", "params", "expiry", "fence", "scope"])
def test_consumer_and_final_fence_refuse_drift(lane, change):
    issue(lane)
    grant = proof(lane)
    state, scope = lane.state, project.checkout_scope(PROJECT, DRAWING)
    if change == "unconsumed": lane.approval["consumed"] = False
    elif change == "gate_unconsumed": lane.gate.pop("consumed_at")
    elif change == "turn": lane.session["active_turn_id"] = "other"
    elif change == "confirm": lane.data["confirm"]["proposal"]["params"]["expected_rev"] = 1
    elif change == "params": lane.approval["params"]["expected_rev"] = 1
    elif change == "expiry": next(iter(lane.db.rows.values()))["expires_at"] = datetime.now(timezone.utc)-timedelta(seconds=1)
    elif change == "fence": state = SimpleNamespace(**{**vars(state), "checkout": SimpleNamespace(**{**vars(lane.lease), "fence": 8})})
    elif change == "scope": scope = "foreign"
    with pytest.raises(project.ProjectContextError, match="CHECKOUT_DENIED"):
        grant.fence_for(lane.tenant, scope, state)


def test_http_issue_revoke_no_store_and_no_capability_body(lane):
    path = "/api/agent/approvals/cid/checkout-custody"
    response = lane.client.post(path, json={"session_id": "session", "input_version_id": VERSION},
                                headers={"X-Checkout-Capability": lane.token})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert lane.token not in response.text
    delegation = response.json()["delegation_id"]
    response = lane.client.request("DELETE", path, json={"session_id": "session", "delegation_id": delegation})
    assert response.status_code == 200 and response.json() == {"revoked": True}
    assert response.headers["cache-control"] == "no-store"
    response = lane.client.post(path, json={"session_id": "session", "input_version_id": VERSION,
        "capability": lane.token})
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert lane.token not in response.text


def test_http_auth_failure_and_store_failure_are_sanitized_no_store(lane, monkeypatch):
    path = "/api/agent/approvals/cid/checkout-custody"
    def unavailable(): raise RuntimeError("private supplier details")
    monkeypatch.setattr(custody.platform_link, "platform_db", unavailable)
    response = lane.client.post(path, json={"session_id": "session", "input_version_id": VERSION},
        headers={"X-Checkout-Capability": lane.token})
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert "private supplier" not in response.text and lane.token not in response.text
    def unauthenticated(): raise HTTPException(status_code=401, detail="private authentication detail")
    lane.client.app.dependency_overrides[custody.deps.require_active_tenant] = unauthenticated
    response = lane.client.post(path, json={"session_id": "session", "input_version_id": VERSION})
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert "private authentication" not in response.text


def test_migration_has_single_confirmation_fence_and_no_credential_columns():
    sql = (Path(__file__).resolve().parents[2] / "platform/migrations/0097_project_checkout_custody.sql").read_text()
    assert "UNIQUE (tenant_id, confirmation_id)" in sql
    assert "CHECK (fence > 0)" in sql and "revoked_at TIMESTAMPTZ" in sql
    columns = sql[sql.index("CREATE TABLE"):].lower()
    assert "capability" not in columns and "digest" not in columns and "bearer" not in columns


@pytest.mark.parametrize("extras", [{"proposal_job_id": "stored-proposal"},
    {"solve_context": {"expected_rev": 17, "phase": "initial"}}])
def test_approved_run_extras_are_bound_in_proof_and_rechecked(lane, extras):
    lane.gate["args"].update(deepcopy(extras))
    lane.gate["args_hash"] = custody.agent_gate.canonical_args_hash(lane.gate["args"])
    issue(lane)
    lane.approval["consumed"] = True
    lane.gate["consumed_at"] = datetime.now(timezone.utc).isoformat()
    options = dict(tool="solar-settings", params=deepcopy(PARAMS), catalog_pins=dict(PINS), input_binding=deepcopy(BINDING))
    grant = custody.resolve_for_turn(lane.tenant, "session", "resume", **options, run_extras=extras)
    extras.clear()
    assert grant.fence_for(lane.tenant, project.checkout_scope(PROJECT, DRAWING), lane.state) == 7
    with pytest.raises(custody.CustodyError):
        custody.resolve_for_turn(lane.tenant, "session", "resume", **options, run_extras={"proposal_job_id": "other"})
    with pytest.raises(custody.CustodyError):
        custody.resolve_for_turn(lane.tenant, "session", "resume", **options, run_extras={"unknown": None})


def test_unapproved_run_extras_refuse_and_none_is_absent(lane):
    issue(lane)
    proof(lane)
    options = dict(tool="solar-settings", params=deepcopy(PARAMS), catalog_pins=dict(PINS), input_binding=deepcopy(BINDING))
    with pytest.raises(custody.CustodyError):
        custody.resolve_for_turn(lane.tenant, "session", "resume", **options, run_extras={"proposal_job_id": "model"})
    assert custody.resolve_for_turn(lane.tenant, "session", "resume", **options,
        run_extras={"proposal_job_id": None}).confirmation_id == "cid"


@pytest.mark.parametrize("value", [True, 17.0])
def test_exact_approved_json_preserves_numeric_types(lane, value):
    issue(lane)
    proof(lane)
    params = {**PARAMS, "expected_rev": value}
    with pytest.raises(custody.CustodyError):
        custody.resolve_for_turn(lane.tenant, "session", "resume", tool="solar-settings",
            params=params, catalog_pins=dict(PINS), input_binding=deepcopy(BINDING))
