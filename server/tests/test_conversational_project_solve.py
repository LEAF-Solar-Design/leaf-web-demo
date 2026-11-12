"""Assistant solve uses the selected project and genuine private checkout custody.

Real HTTP, custody and canonical producer with the established synthetic SQL/graph
supplier seams. These checks never call the paid solver or claim live execution.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

import deps
import platform_link
import project_checkout_custody as custody
import project_execution_context as execution
import solar_project_context as project
from leaf_platform import canonical_jobs
from routers import jobs as route
import test_conversational_project_write as writes
from test_conversational_project_write import selected  # noqa: F401
import test_sip_r4_jobs as producer_cases
import test_sip_r5_routes as routes
from test_sip_r5_routes import lane, catalog_proposal  # noqa: F401


@pytest.fixture
def selected_solve(lane, selected, catalog_proposal, monkeypatch):
    s, a = lane.s, catalog_proposal.a
    selected.bound.update(input_version_id=str(s.parent.version_id),
                          intake_sha256=s.context().intake_sha256)
    # Keep the actual catalog proposal params; drawing_id is not a cloud param.
    selected.request = {
        "tool": "solar-solve-proposal", "dwg": str(s.drawing),
        "params": deepcopy(a.params), "solve_context": deepcopy(a.solve_context),
        "catalog_digest": deps.catalog_tool_digest(lane.tools["solar-solve-proposal"]),
        "catalog_commit": "fixture-commit", "effective_catalog_digest": "sha256:" + "e" * 64,
    }
    selected.request["tool_manifest_sha256"] = selected.request["catalog_digest"]
    monkeypatch.setattr(platform_link, "resolve_submission_context", producer_cases.forbidden)
    return selected


@pytest.fixture
def solve_custody(lane, selected_solve, monkeypatch):
    import test_project_checkout_custody as custody_cases
    c = custody_cases.lane.__wrapped__(monkeypatch)
    selected = selected_solve
    s = lane.s
    tenant = deps.TenantContext(str(s.org), subject="auth0|owner", authority_resolved=True)
    lane.principal["tenant"] = tenant
    c.tenant = tenant
    params, binding = deepcopy(selected.request["params"]), deepcopy(selected.bound)
    c.approval.update(tenant_id=str(s.org), tool="solar-solve-proposal", params=deepcopy(params),
        capability="solve", decided_by=tenant.subject,
        payload={"dwg": str(s.drawing), execution.KEY: binding})
    c.session.update(tenant_id=str(s.org), org_id=str(s.org), project_id=str(s.project))
    c.gate.update(tenant_id=str(s.org), action="run_solve_tool", decided_by=tenant.subject)
    c.gate["args"] = {**deepcopy(selected.request), execution.KEY: binding}
    c.gate["args_hash"] = custody.agent_gate.canonical_args_hash(c.gate["args"])
    c.data.clear()
    c.data.update({execution.KEY: binding, "confirm": {
        "confirmation_id": "cid", "approved": True, "proposal": {
            "tool": "solar-solve-proposal", "params": deepcopy(params),
            "capability": "solve", "dwg": str(s.drawing)}}})
    now = datetime.now(timezone.utc)
    lease = project.CheckoutLease(s.org, s.project, s.drawing, "Browser", s.actor,
                                  now, now + timedelta(seconds=120), 7)
    state = project.CheckoutState(s.org, s.project, s.drawing, lease, 7, now)
    c.state = state
    monkeypatch.setattr(deps, "find_tool", lambda name, *_a: lane.tools.get(name))
    monkeypatch.setattr(deps, "catalog_tool_digest", writes._MANIFEST)
    monkeypatch.setattr(deps, "catalog_tool_view", writes._TOOL_VIEW)
    monkeypatch.setattr(project, "_access", lambda *_a, **_k: (s.org, s.actor))
    monkeypatch.setattr(project, "resolve_context", lambda *_a, **_k: s.context())
    monkeypatch.setattr(project, "verify_at_admission", writes._VERIFY_ADMISSION)
    monkeypatch.setattr(project.graph_store(), "get_checkout", lambda *_a, **_k: state)
    monkeypatch.setattr(project.graph_store(), "verify_checkout", lambda *_a, **_k: lease)
    monkeypatch.setattr(custody.platform_link, "require_project_session_access", lambda *_a, **_k: str(s.org))
    monkeypatch.setattr(custody.platform_link, "resolve_project_authority", lambda *_a: {
        "org_id": s.org, "project_id": s.project, "authority_mode": "postgres_canonical"})
    monkeypatch.setattr(route.customization_service, "effective_catalog_pin", lambda *_a: {
        key: selected.request[key] for key in ("catalog_commit", "effective_catalog_digest")})
    token = project.checkout_capability.mint(tenant, project.checkout_scope(s.project, s.drawing), 7)
    c.issued = custody.issue(tenant, "cid", "session", str(s.parent.version_id), token)
    c.approval["consumed"] = True
    c.gate["consumed_at"] = now.isoformat()
    selected.headers["X-Authority-Turn-Id"] = "resume"
    return c


def test_selected_solve_reaches_existing_canonical_producer(lane, selected_solve, solve_custody):
    selected = selected_solve
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == str(lane.s.job)
    assert len(lane.a.accepted) == 1
    accepted = lane.a.accepted[0]
    context = accepted[-1]["execution_context"]
    assert accepted[3] == "solar-solve-proposal"
    assert accepted[5] == "checkout-custody:" + solve_custody.issued["delegation_id"]
    assert accepted[-1]["input_version_id"] == UUID(selected.bound["input_version_id"])
    assert context["drawing_id"] == selected.bound["drawing_id"]
    assert context["parent_intake_sha256"] == selected.bound["intake_sha256"]
    assert context["checkout_fence"] == "7"
    assert context["solve_binding"]["frame_id"] == selected.request["solve_context"]["frame_ref"]
    assert context["solve_binding"]["expected_rev"] == selected.request["solve_context"]["expected_rev"]
    assert context["solve_binding"]["phase"] == "initial"


def test_selected_solve_without_custody_never_falls_back(lane, selected_solve):
    response = lane.client.post("/api/run", json=selected_solve.request, headers=selected_solve.headers)
    assert response.status_code in (403, 409), response.text
    assert not lane.a.accepted and not lane.records


@pytest.mark.parametrize("change", ["revoked", "foreign-subject", "context", "params", "caller-key"])
def test_selected_solve_authority_drift_creates_no_job(lane, selected_solve, solve_custody, change):
    selected = selected_solve
    if change == "revoked":
        next(iter(solve_custody.db.rows.values()))["revoked_at"] = datetime.now(timezone.utc)
    elif change == "foreign-subject":
        lane.principal["tenant"] = deps.TenantContext(str(lane.s.org), subject="foreign", authority_resolved=True)
    elif change == "context":
        selected.request["solve_context"]["expected_rev"] += 1
    elif change == "params":
        selected.request["params"]["grant_ref"] = "changed-grant"
    else:
        selected.headers["Idempotency-Key"] = "second-job"
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code in (403, 409), response.text
    assert not lane.a.accepted and not lane.records


@pytest.mark.parametrize("change", ["dwg", "project", "org", "catalog", "missing-context", "disabled"])
def test_selected_solve_request_conflict_creates_no_job(lane, selected_solve, monkeypatch, change):
    selected = selected_solve
    if change == "dwg":
        selected.request["dwg"] = str(UUID(int=999))
    elif change in ("project", "org"):
        selected.headers["X-Project-Id" if change == "project" else "X-Org-Id"] = str(UUID(int=999))
    elif change == "catalog":
        selected.request["tool_manifest_sha256"] = "sha256:" + "f" * 64
    elif change == "missing-context":
        selected.request.pop("solve_context")
    else:
        monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0")
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code in (403, 409), response.text
    assert not lane.a.accepted and not lane.records


def test_selected_solve_replay_returns_same_canonical_job(lane, selected_solve, solve_custody, monkeypatch):
    first = lane.client.post("/api/run", json=selected_solve.request, headers=selected_solve.headers)
    assert first.status_code == 202, first.text
    accepted = lane.a.accepted[0][-1]
    existing = {"job_id": first.json()["job_id"], "deleted_at": None,
        "submission_fingerprint": accepted["submission_fingerprint"],
        "execution_context": accepted["execution_context"]}
    monkeypatch.setattr(canonical_jobs, "get_project_graph_job_by_key", lambda *_a, **_k: existing)
    monkeypatch.setattr(project, "verify_at_admission", producer_cases.forbidden)
    replay = lane.client.post("/api/run", json=selected_solve.request, headers=selected_solve.headers)
    assert replay.status_code == 202, replay.text
    assert replay.json()["job_id"] == first.json()["job_id"] and len(lane.a.accepted) == 1


def test_unbound_solve_preserves_existing_direct_project_route(lane, catalog_proposal, monkeypatch):
    monkeypatch.setattr(route.entity_scope, "resolve_turn_binding", lambda *_a: None)
    monkeypatch.setattr(execution, "resolve_turn_binding", lambda *_a: None)
    response = routes.run(lane, catalog_proposal.a.params, "solar-solve-proposal",
                          solve_context=catalog_proposal.a.solve_context)
    assert response.status_code == 202, response.text
    assert len(lane.a.accepted) == 1
