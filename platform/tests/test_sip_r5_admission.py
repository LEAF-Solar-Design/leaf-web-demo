"""PostgreSQL HTTP admission, preview custody and terminal graph observation."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
_jobs_spec = importlib.util.spec_from_file_location(
    "_sip_r5_pg_job_helpers", ROOT / "platform/tests/test_sip_r4_jobs.py")
pg = importlib.util.module_from_spec(_jobs_spec)
_jobs_spec.loader.exec_module(pg)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")


def _fresh_scope(make_org, monkeypatch):
    """Create the artifact before publishing the initial explicit drawing version."""
    helpers = pg.base_scope_original.__wrapped__.__globals__
    org = make_org("Solar HTTP admission", tier="hosted_pro")
    proj = pg.store.create_project(org.org_id, "Solar", authority_mode="postgres_canonical")
    actors = [pg.store.create_identity_binding(org.org_id, "auth0", str(uuid4()), role="editor")
              for _ in range(2)]
    artifact = pg.store.create_drawing_artifact(org.org_id, proj.project_id)
    source = str(uuid4())
    key = f"tenants/{org.org_id}/drawings/{source}/v/00000001.intake.json"
    parent = pg.store.create_drawing_version(org.org_id, proj.project_id,
        drawing_id=artifact.drawing_id, oss_object=key[:-12] + ".dwg", intake_ref=key)
    blobs = helpers["Blobs"]()
    raw = pg.local.canonical_bytes(helpers["INTAKE"])
    blobs.data[key] = raw
    provenance = {"schema": "leaf.drawing-import.v1", "source": {
        "kind": "account_upload", "tenant_id": str(org.org_id), "drawing_id": source,
        "version": 1, "stored_object": {"ref": parent.oss_object},
        "intake": {"ref": key, "sha256": sha256(raw).hexdigest()}}}
    with pg.db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                    (Jsonb(provenance), org.org_id, parent.version_id))
        for actor in actors:
            cur.execute("INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%s, %s, %s, %s, 'editor', %s)",
                (uuid4(), org.org_id, proj.project_id, actor.binding_id, actor.binding_id))
    lease = pg.db.run_transaction(lambda conn: pg.store_graph.acquire_checkout(
        org.org_id, proj.project_id, artifact.drawing_id, actor_binding_id=actors[0].binding_id,
        holder="Solar editor", ttl_s=60, expected_fence=None, conn=conn))
    return SimpleNamespace(org=org.org_id, project=proj.project_id, drawing=artifact.drawing_id,
        parent=parent.version_id, actor=actors[0].binding_id, other=actors[1].binding_id,
        lease=lease, job=uuid4(), blobs=blobs)


@pytest.fixture
def world(make_org, monkeypatch):
    """Reuse checkout setup and cancellation, with explicit artifact creation."""
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(pg, "base_scope_original", pg.base_scope, raising=False)
    monkeypatch.setattr(pg, "base_scope", SimpleNamespace(__wrapped__=_fresh_scope))
    generator = pg.world.__wrapped__(make_org, monkeypatch)
    fresh = next(generator)
    try:
        yield fresh
    finally:
        with pytest.raises(StopIteration):
            next(generator)


@pytest.fixture
def http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import envelopes
    from routers import capabilities, jobs
    principal = {"tenant": None}
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.include_router(jobs.router)
    app.include_router(capabilities.router)
    app.dependency_overrides[pg.deps.require_tenant] = lambda: principal["tenant"]
    # Legacy submission and waiting must never receive a project graph request.
    monkeypatch.setattr(jobs.jobs, "submit_job", pg.blocked)
    monkeypatch.setattr(jobs.jobs, "wait_for_terminal", pg.blocked)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, principal=principal, route=jobs)


def _principal(s):
    return pg.deps.TenantContext(
        str(s.org), org_id=str(s.org), tier="hosted_pro", subject="verified-editor")


def _bind(http, s):
    http.principal["tenant"] = _principal(s)


def _headers(s, key="http-key"):
    # The capability binds the caller's subject, so it is minted for the same principal the route sees.
    return {"X-Org-Id": str(s.org), "X-Project-Id": str(s.project), "Idempotency-Key": key,
        "X-Checkout-Capability": pg.checkout_capability.mint(_principal(s),
            pg.project.checkout_scope(s.project, s.drawing), s.lease.fence)}


def _body(s, params=None, **changes):
    parameters = deepcopy(pg.seed_params(s) if params is None else params)
    parameters.setdefault("drawing_id", str(s.drawing))
    result = {"tool": "solar-settings", "params": parameters, "dwg": str(s.parent),
              "catalog_digest": pg.manifest("solar-settings")}
    result.update(changes)
    return result


def _run(http, s, params=None, *, wait=0, key="http-key", **changes):
    return http.client.post(f"/api/run?wait={wait}", json=_body(s, params, **changes),
                            headers=_headers(s, key))


def _preview(http, s, params=None, *, key="http-key", **changes):
    request = _body(s, params)
    query = {"project_id": str(s.project), "drawing_id": str(s.drawing),
        "input_version_id": str(s.parent), "project_runs": json.dumps({"solar-settings": {
            "params": request["params"], "catalog_digest": request["catalog_digest"],
            "idempotency_key": key}})}
    query.update(changes)
    return http.client.get("/api/capabilities", params=query, headers=_headers(s, key))


def _state(response):
    assert response.status_code == 200, response.text
    return next(row["availability"] for family in response.json()["families"]
                for row in family["capabilities"] if row["name"] == "solar-settings")


def _snapshot(s):
    with pg.db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        count = cur.fetchone()["n"]
    return count, pg.census(s), deepcopy(s.blobs.data), list(s.blobs.writes)


def _settle(s, job_id):
    row = pg.claim(s, {**pg.load(job_id), "job_id": job_id})
    prepared = pg.prepared(row)
    outcome, result = pg.complete(row, prepared)
    assert outcome == "applied"
    return result


def _refusal(response, status, reason):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["reason_code"] == reason
    assert error["message"] == "Project Solar admission unavailable."


def test_sip_r5_pg_route_to_terminal(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s = world()
    _bind(http, s)
    before = _snapshot(s)
    submitted = _run(http, s)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    stored = pg.load(job_id)
    assert stored["status"] == "queued" and stored["attempt"] == 0
    assert stored["execution_context"]["schema"] == pg.jobs.PROJECT_GRAPH_JOB_SCHEMA
    assert _snapshot(s)[0] == before[0] + 1
    observed = http.client.get(f"/api/jobs/{job_id}")
    assert observed.status_code == 200 and observed.json()["status"] == "submitted"
    result = _settle(s, job_id)
    assert pg.delta(pg.census(s), before[1]) == (1, 1, 1, 0)
    terminal = http.client.get(f"/api/jobs/{job_id}")
    assert terminal.status_code == 200 and terminal.json()["status"] == "complete"
    assert terminal.json()["result"] == result
    assert result["output_version_id"] == str(pg.load(job_id)["output_version_id"])
    assert result["graph_commit"]["output_version_id"] == result["output_version_id"]
    assert pg.store.verify_history_operation(s.org, UUID(result["history_operation_id"]))


def test_sip_r5_pg_preview_and_refusal_no_rows(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s, foreign = world(), world()
    _bind(http, s)
    params = pg.seed_params(s)
    before = _snapshot(s), _snapshot(foreign), deepcopy(params)
    state = _state(_preview(http, s, params))
    assert state["runnable"] and state["admission_checked"]
    assert state["admission"] == {"status_code": 200, "error": None}
    nested = deepcopy(params)
    nested["changes"]["surprise"] = 1
    invalid = _state(_preview(http, s, nested))
    assert not invalid["runnable"]
    assert invalid["admission"]["error"]["reason_code"] == "INVALID_SETTINGS_REQUEST"
    response = _run(http, s, nested)
    assert response.status_code == 409 and response.json()["error"] == invalid["admission"]["error"]
    for fields in ({"dwg": str(foreign.parent)},):
        _refusal(_run(http, s, params, **fields), 404, "SIP_R1_CONTEXT_NOT_FOUND")
    foreign_params = {**params, "drawing_id": str(foreign.drawing)}
    _refusal(_run(http, s, foreign_params), 404, "SIP_R1_CONTEXT_NOT_FOUND")
    assert (_snapshot(s), _snapshot(foreign), params) == before
    assert not s.blobs.writes and not foreign.blobs.writes


def test_sip_r5_pg_replay_and_revoked_access(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s, foreign = world(), world()
    _bind(http, s)
    params = pg.seed_params(s)
    submitted = _run(http, s, params)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    result = _settle(s, job_id)
    before = _snapshot(s)
    with monkeypatch.context() as patch:
        patch.setattr(pg.project, "verify_at_admission", pg.blocked)
        patch.setattr(pg.service, "_validate_new_project_graph_params", pg.blocked)
        patch.setattr(pg.entitlements, "stored_job_entitlement_verdict", pg.blocked)
        replay = _run(http, s, params)
        assert replay.status_code == 202 and replay.json()["job_id"] == job_id
        state = _state(_preview(http, s, params))
        assert state["admission"] == {"status_code": 200, "error": None}
    assert http.client.get(f"/api/jobs/{job_id}").json()["result"] == result
    _refusal(_run(http, s, {**params, "cancel": False}), 409, "SIP_R4_IDEMPOTENCY_CONFLICT")
    assert _snapshot(s) == before
    # Another current project member can observe without matching the submitter.
    original_actor = s.actor
    s.actor = s.other
    assert http.client.get(f"/api/jobs/{job_id}").status_code == 200
    s.actor = original_actor
    with pg.db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=clock_timestamp() "
                    "WHERE org_id=%s AND project_id=%s AND binding_id=%s", (s.org, s.project, s.actor))
    _refusal(_run(http, s, params), 403, "SIP_R1_PROJECT_FORBIDDEN")
    state = _state(_preview(http, s, params))
    assert state["admission"]["status_code"] == 403
    assert state["admission"]["error"]["reason_code"] == "SIP_R1_PROJECT_FORBIDDEN"
    for enabled in (True, False):
        if enabled:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
        else:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        response = http.client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 403 and response.json()["error"]["message"] == "project access forbidden"
        assert all(row["job_id"] != job_id for row in http.client.get("/api/jobs").json()["jobs"])
        _bind(http, foreign)
        response = http.client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 404
        assert response.json()["error"]["message"] == f"unknown job_id: {job_id}"
        _bind(http, s)
    assert _snapshot(s) == before


def test_sip_r5_pg_unclaimed_job(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s = world()
    _bind(http, s)
    monkeypatch.setattr(http.route.jobs, "job_max_s", lambda: -30)
    submitted = _run(http, s, wait=1)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    before = deepcopy(pg.load(job_id))
    census = _snapshot(s)
    assert before["status"] == "queued" and before["attempt"] == 0
    assert before["result"] is None and before["output_version_id"] is None
    assert pg.jobs.claim_next("generic", tool_name="solar-settings") is None
    assert pg.jobs.claim_project_graph_job("wrong-tool", tool_name="solar-string-add") is None
    assert pg.load(job_id) == before and _snapshot(s) == census
    params = pg.seed_params(s)
    assert _run(http, s, params, wait=1).status_code == 202
    assert pg.load(job_id) == before and _snapshot(s) == census
    assert pg.jobs.complete_solve(UUID(job_id), "generic", {}, {}) == "conflict"
    assert pg.jobs.fail_or_retry(UUID(job_id), "generic", {}, {}) == "conflict"
    assert pg.load(job_id) == before
    result = _settle(s, job_id)
    observed = http.client.get(f"/api/jobs/{job_id}")
    assert observed.status_code == 200 and observed.json()["status"] == "complete"
    assert observed.json()["result"]["output_version_id"] == result["output_version_id"]
