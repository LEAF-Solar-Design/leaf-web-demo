"""Conversational canonical Solar admission, beneath the shared project workflow.

These synthetic seams prove refusal and producer binding, not live publication.
Private custody tests use the actual port A module after root composition.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

import deps
import project_execution_context
import solar_project_context as project
import solar_project_jobs as service
from leaf_platform import canonical_jobs
from routers import jobs as route
import test_sip_r4_jobs as producer_cases
import test_sip_r5_policy as policy_cases
import test_sip_r5_routes as routes
from test_sip_r5_routes import lane  # noqa: F401

_MANIFEST = deps.catalog_tool_digest
_TOOL_VIEW = deps.catalog_tool_view
_VERIFY_ADMISSION = project.verify_at_admission


@pytest.fixture
def selected(lane, monkeypatch):
    s = lane.s
    bound = {"schema": project_execution_context.SCHEMA,
        "organization_id": str(s.org), "project_id": str(s.project),
        "drawing_id": str(s.drawing), "input_version_id": str(s.parent.version_id),
        "intake_sha256": s.context().intake_sha256}
    monkeypatch.setattr(route.entity_scope, "resolve_turn_binding", lambda *_a: None)
    monkeypatch.setattr(project_execution_context, "resolve_turn_binding", lambda *_a: deepcopy(bound))
    pins = {"catalog_commit": "fixture-commit", "effective_catalog_digest": "sha256:" + "e" * 64}
    monkeypatch.setattr(route.customization_service, "effective_catalog_pin", lambda *_a: deepcopy(pins))
    request = routes.body(lane)
    request.update(pins, tool_manifest_sha256=request["catalog_digest"])
    headers = {"X-Authority-Session-Id": "session", "X-Authority-Turn-Id": "turn"}
    return SimpleNamespace(bound=bound, request=request, headers=headers)


@pytest.mark.parametrize("field,value", [
    ("catalog_digest", "sha256:" + "f" * 64),
    ("catalog_commit", "old-commit"), ("catalog_commit", None),
    ("effective_catalog_digest", "sha256:" + "f" * 64),
    ("tool_manifest_sha256", None), ("tool_manifest_sha256", "sha256:" + "f" * 64),
    ("dwg", str(UUID(int=999))),
])
def test_selected_write_refuses_conflicting_catalog_or_version_before_custody(lane, selected, field, value):
    selected.request[field] = value
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code == 409, response.text
    assert not lane.a.accepted and not lane.records


def test_selected_write_refuses_parameter_drawing_conflict(lane, selected):
    selected.request["params"]["drawing_id"] = str(UUID(int=999))
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code == 409
    assert not lane.a.accepted


def test_selected_write_preserves_entity_scope_refusal(lane, selected, monkeypatch):
    monkeypatch.setattr(route.entity_scope, "resolve_turn_binding", lambda *_a: {"scope": "stored"})
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code == 403
    assert response.json()["reason_code"] == route.entity_scope.SCOPED_MUTATION_REASON
    assert not lane.a.accepted


def test_selected_write_disabled_cannot_fall_back_to_legacy(lane, selected, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0")
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code == 409
    assert response.json()["error"]["reason_code"] == "project_execution_disabled"
    assert not lane.a.accepted


@pytest.mark.parametrize("field", ["dwg_version", "expected_drawing_head"])
def test_selected_write_validates_legacy_pins_before_clearing(lane, selected, monkeypatch, field):
    monkeypatch.setattr(project, "resolve_context", lambda *_a, **_k: SimpleNamespace(
        binding=SimpleNamespace(is_head=True, version=SimpleNamespace(seq=3)),
        intake_sha256=selected.bound["intake_sha256"]))
    selected.request[field] = 999
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code == 409
    assert not lane.a.accepted


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, "a" * 65, False, "a" * 64 + "\n"])
def test_producer_rejects_malformed_expected_parent_before_insert(monkeypatch, digest):
    a = policy_cases.setup(monkeypatch)
    with pytest.raises(project.ProjectContextError, match="SIP_R1_INVALID_BINDING"):
        producer_cases.submit(a, expected_intake_sha256=digest)
    assert not a.accepted


def test_producer_checks_admitted_parent_hash_before_insert(monkeypatch):
    a = policy_cases.setup(monkeypatch)
    with pytest.raises(project.ProjectContextError, match="SIP_R1_INTAKE_DIGEST_MISMATCH"):
        producer_cases.submit(a, expected_intake_sha256="f" * 64)
    assert not a.accepted


def test_producer_stores_exact_expected_parent_hash(monkeypatch):
    a = policy_cases.setup(monkeypatch)
    digest = a.s.context().intake_sha256
    row = producer_cases.submit(a, expected_intake_sha256=digest)
    assert row["job_id"] == str(a.s.job)
    assert a.accepted[0][-1]["execution_context"]["parent_intake_sha256"] == digest


@pytest.mark.parametrize("expected", ["f" * 64, "a" * 64])
def test_producer_replay_compares_stored_parent_without_intake_io(monkeypatch, expected):
    a = policy_cases.setup(monkeypatch)
    existing = {"job_id": str(a.s.job), "execution_context": {"parent_intake_sha256": "a" * 64}}
    monkeypatch.setattr(canonical_jobs, "get_project_graph_job_by_key", lambda *_a, **_k: existing)
    monkeypatch.setattr(project, "verify_at_admission", producer_cases.forbidden)
    monkeypatch.setattr(canonical_jobs, "_graph_replay", lambda row, _fp: row)
    if expected == "a" * 64:
        assert producer_cases.submit(a, expected_intake_sha256=expected) is existing
    else:
        with pytest.raises(project.ProjectContextError, match="SIP_R4_IDEMPOTENCY_CONFLICT"):
            producer_cases.submit(a, expected_intake_sha256=expected)
    assert not a.accepted


def test_selected_write_missing_real_custody_creates_no_job(lane, selected):
    # No counterfeit DelegatedCheckout is used to make this refusal pass.
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code in (403, 409), response.text
    assert not lane.a.accepted and not lane.records


@pytest.fixture
def real_custody(lane, selected, monkeypatch):
    # Root composes immutable A+B before these cases run. Reuse A's SQL seam,
    # but issue a genuine proof against this route fixture's canonical parent.
    import test_project_checkout_custody as custody_cases
    import project_checkout_custody as custody
    c = custody_cases.lane.__wrapped__(monkeypatch)
    s = lane.s
    selected.request["dwg"] = str(s.drawing)
    tenant = deps.TenantContext(str(s.org), subject="auth0|owner", authority_resolved=True)
    lane.principal["tenant"] = tenant
    c.tenant = tenant
    params = deepcopy(selected.request["params"])
    binding = deepcopy(selected.bound)
    c.approval.update(tenant_id=str(s.org), params=deepcopy(params), decided_by=tenant.subject,
        payload={"dwg": str(s.drawing), project_execution_context.KEY: binding})
    c.session.update(tenant_id=str(s.org), org_id=str(s.org), project_id=str(s.project))
    c.gate.update(tenant_id=str(s.org), decided_by=tenant.subject)
    c.gate["args"] = {**deepcopy(selected.request), project_execution_context.KEY: binding}
    c.gate["args_hash"] = custody.agent_gate.canonical_args_hash(c.gate["args"])
    c.data.clear()
    c.data.update({project_execution_context.KEY: binding, "confirm": {
        "confirmation_id": "cid", "approved": True, "proposal": {
            "tool": "solar-settings", "params": deepcopy(params),
            "capability": "drawing.write", "dwg": str(s.drawing)}}})
    now = datetime.now(timezone.utc)
    lease = project.CheckoutLease(s.org, s.project, s.drawing, "Browser", s.actor,
                                  now, now + timedelta(seconds=120), 7)
    state = project.CheckoutState(s.org, s.project, s.drawing, lease, 7, now)
    c.state = state
    monkeypatch.setattr(deps, "find_tool", lambda name, *_a: lane.tools.get(name))
    monkeypatch.setattr(deps, "catalog_tool_digest", _MANIFEST)
    monkeypatch.setattr(deps, "catalog_tool_view", _TOOL_VIEW)
    monkeypatch.setattr(project, "_access", lambda *_a, **_k: (s.org, s.actor))
    monkeypatch.setattr(project, "resolve_context", lambda *_a, **_k: s.context())
    monkeypatch.setattr(project, "verify_at_admission", _VERIFY_ADMISSION)
    monkeypatch.setattr(project.graph_store(), "get_checkout", lambda *_a, **_k: state)
    monkeypatch.setattr(project.graph_store(), "verify_checkout", lambda *_a, **_k: lease)
    monkeypatch.setattr(custody.platform_link, "require_project_session_access", lambda *_a, **_k: str(s.org))
    monkeypatch.setattr(custody.platform_link, "resolve_project_authority", lambda *_a: {
        "org_id": s.org, "project_id": s.project, "authority_mode": "postgres_canonical"})
    monkeypatch.setattr(route.customization_service, "effective_catalog_pin", lambda *_a: {
        key: selected.request[key] for key in ("catalog_commit", "effective_catalog_digest")})
    token = project.checkout_capability.mint(tenant, project.checkout_scope(s.project, s.drawing), 7)
    issued = custody.issue(tenant, "cid", "session", str(s.parent.version_id), token)
    c.approval["consumed"] = True
    c.gate["consumed_at"] = now.isoformat()
    selected.headers["X-Authority-Turn-Id"] = "resume"
    c.issued = issued
    return c


def test_real_custody_reaches_existing_canonical_producer(lane, selected, real_custody, monkeypatch):
    import platform_link
    monkeypatch.setattr(platform_link, "resolve_submission_context", producer_cases.forbidden)
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code == 202, response.text
    assert len(lane.a.accepted) == 1
    accepted = lane.a.accepted[0]
    assert accepted[5] == "checkout-custody:" + real_custody.issued["delegation_id"]
    assert accepted[-1]["execution_context"]["parent_intake_sha256"] == selected.bound["intake_sha256"]
    assert response.json()["job_id"] == str(lane.s.job)


def test_real_custody_replay_preserves_job_without_parent_or_checkout_io(lane, selected, real_custody,
                                                                      monkeypatch):
    first = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert first.status_code == 202, first.text
    accepted = lane.a.accepted[0][-1]
    existing = {"job_id": first.json()["job_id"], "deleted_at": None,
        "submission_fingerprint": accepted["submission_fingerprint"],
        "execution_context": accepted["execution_context"]}
    monkeypatch.setattr(canonical_jobs, "get_project_graph_job_by_key", lambda *_a, **_k: existing)
    monkeypatch.setattr(project, "verify_at_admission", producer_cases.forbidden)
    monkeypatch.setattr(project.graph_store(), "get_checkout", producer_cases.forbidden)
    replay = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert replay.status_code == 202 and replay.json()["job_id"] == first.json()["job_id"]
    assert len(lane.a.accepted) == 1


@pytest.mark.parametrize("change", ["revoked", "foreign-subject", "unapproved-params", "caller-key"])
def test_real_custody_drift_refuses_without_job(lane, selected, real_custody, change):
    if change == "revoked":
        next(iter(real_custody.db.rows.values()))["revoked_at"] = datetime.now(timezone.utc)
    elif change == "foreign-subject":
        lane.principal["tenant"] = deps.TenantContext(str(lane.s.org), subject="foreign", authority_resolved=True)
    elif change == "unapproved-params":
        selected.request["params"]["changes"] = {"num_mppt": 999}
    else:
        selected.headers["Idempotency-Key"] = "second-job"
    response = lane.client.post("/api/run", json=selected.request, headers=selected.headers)
    assert response.status_code in (403, 409), response.text
    assert not lane.a.accepted and not lane.records
