"""Real PostgreSQL proposal custody, nonpublication, terminal replay and reclaim."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import OperationalError
import pytest
from psycopg.types.json import Jsonb

import deps
import leaf_cloud_client as cloud
import solar_project_jobs as service
import solar_project_solve as solve
from leaf_cloud_grants import CloudError
from leaf_platform import canonical_jobs as jobs, project_solve_history, project_solve_store as exchanges
from routers import project_drawings
from test_jobs_callbacks_postgres import postgres_authority  # noqa: F401
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import case  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs  # noqa: F401
from test_w1c_solve_proposal import ready_graph, solve_context

ROOT = Path(__file__).resolve().parents[2]
# Platform helpers use sibling imports whose names also exist in server/tests.
# Bind those imports by origin only while loading, then restore server aliases.
with pytest.MonkeyPatch.context() as imports:
    imports.setattr(sys, "path", sys.path[:])
    for name in ("test_sip_r3a_graph", "test_sip_r3b_tools"):
        spec = importlib.util.spec_from_file_location(
            "_w1c_pg_" + name, ROOT / "platform/tests" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        imports.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location(
        "_w1c_pg_sizing", ROOT / "platform/tests/test_sip_r14_sizing.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
pg = helpers.pg

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL integration test requires DATABASE_URL")


@pytest.fixture
def make_org(postgres_authority):
    return pg.store.create_org


@pytest.fixture
def world(make_org, case, monkeypatch):
    generator = helpers.world.__wrapped__(make_org, monkeypatch)
    fresh = next(generator)
    s = fresh()
    version = pg.store.get_drawing_version(s.org, s.project, s.parent)
    value = ready_graph(case[0])
    raw = cloud.canonical_bytes({"solar_design_graph": value,
                               "solar_design_graph_sha256": solve.kernel.digest(value)})
    s.blobs.data[version.intake_ref] = raw
    provenance = deepcopy(version.provenance)
    provenance["source"]["intake"]["sha256"] = solve._sha(raw)
    with pg.db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s WHERE version_id=%s",
                    (Jsonb(provenance), s.parent))
    s.params = {"grant_ref": s.grant_ref, "request": case[1]}
    s.solve_context = solve_context(value)
    s.calls = []
    monkeypatch.setattr(solve, "SOLVE_CLASSIFICATION", "synthetic")
    monkeypatch.setattr(cloud, "resolve_grant", lambda ref, tenant:
        helpers.CloudGrant(tenant, s.token) if ref == s.grant_ref and tenant == str(s.org)
        else (_ for _ in ()).throw(CloudError("cloud_tenant_unauthorized", 403)))
    def send(request, grant, *, preserve_uncertainty):
        assert preserve_uncertainty and grant.access_token == s.token
        s.calls.append(cloud.canonical_bytes(request.wire_payload()))
        return cloud.canonical_bytes(case[2])
    monkeypatch.setattr(cloud, "post_stringer", send)
    try:
        yield s
    finally:
        with pytest.raises(StopIteration):
            next(generator)


@pytest.fixture
def http(isolated_jobs, tmp_path, monkeypatch):
    # Observation probes the legacy stores before canonical readback. The harness
    # mounts the source read-only, so even those empty SQLite stores are temporary.
    import session_store
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    try:
        yield from helpers.http.__wrapped__(monkeypatch)
    finally:
        if session_store._conn is not None:
            session_store._conn.close()
            session_store._conn = None


def submit(s, http):
    http.principal["tenant"] = helpers.principal(s)
    body = {"tool": solve.TOOL_NAME, "dwg": str(s.parent), "params": s.params,
        "catalog_digest": pg.manifest(solve.TOOL_NAME), "solve_context": s.solve_context}
    response = http.client.post("/api/run", json=body, headers=helpers.headers(s, str(uuid4())))
    assert response.status_code == 202, response.text
    return UUID(response.json()["job_id"])


def row(job):
    with pg.db.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE job_id=%s", (job,))
        return dict(cur.fetchone())


def claim(owner):
    return jobs.claim_project_graph_job(owner, tool_name=solve.TOOL_NAME, lease_seconds=120)


def expire(job):
    with pg.db.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=%s",
                    (job,))


def finish(job, owner, completion, attempt=1):
    return pg.db.run_transaction(lambda conn:
        jobs.complete_project_solve_job(job, owner, attempt, completion, conn=conn))


def test_postgres_canonical_proposal_nonpublishing_history_outbox(world, http):
    s, owner = world, uuid4().hex
    job = submit(s, http)
    before = pg.store.get_drawing_version(s.org, s.project, s.parent)
    assert service.run_once(owner, tool_name=solve.TOOL_NAME, lease_seconds=120)
    saved = row(job)
    assert saved["status"] == "succeeded", saved.get("error")
    assert saved["output_version_id"] is None and len(s.calls) == 1
    result = saved["result"]
    status, replay = finish(job, owner, result)
    assert status == "duplicate" and replay == result
    assert finish(job, owner, {**result, "phase": "background"})[0] == "conflict"
    assert finish(job, owner, result, attempt=2)[0] == "conflict"
    binding = pg.store_graph.resolve_version_binding(s.org, s.project, s.parent)
    assert binding.is_head and binding.version.version_id == before.version_id
    with pg.db.cursor() as cur:
        cur.execute("SELECT * FROM history_operations WHERE org_id=%s AND project_id=%s "
                    "AND operation_type='solar.solve.proposed'", (s.org, s.project))
        history = cur.fetchall()
        assert len(history) == 1
        operation = history[0]
        assert set(operation["payload"]) == {"jobId", "inputVersionId", "candidateSha256", "requestHash", "responseHash"}
        assert pg.store.verify_history_operation(s.org, operation["operation_id"])
        assert pg.store._get_history_operation(s.org, operation["operation_id"]).operation_type == "solar.solve.proposed"
        cur.execute("SELECT * FROM outbox_entries WHERE aggregate_id=%s AND event_type='history.operation.appended'",
                    (operation["operation_id"],))
        events = cur.fetchall()
        assert len(events) == 1 and events[0]["payload"]["jobId"] == str(job)
    observed = http.client.get("/api/jobs/" + str(job))
    assert observed.status_code == 200, observed.text
    assert observed.json()["result"] == result
    waited = http.route._wait_project_solar(
        SimpleNamespace(body=cloud.canonical_bytes({"job_id": str(job)})), helpers.principal(s))
    import json
    assert waited.status_code == 200
    # Wait serves the stored result as its body for publishing graph jobs too;
    # only GET /api/jobs/{job_id} wraps it in the job record's result field.
    assert json.loads(waited.body) == result
    pg.db.run_transaction(lambda conn: solve.project_solve_job_provenance(job, result, conn=conn))


def test_postgres_lost_sender_settles_unknown(world, http, monkeypatch):
    s = world
    job = submit(s, http)
    claimed = claim(uuid4().hex)
    assert UUID(str(claimed["job_id"])) == job
    scope, lease = solve._authority(claimed)
    def lost(*args, **kwargs):
        s.calls.append("sent")
        raise RuntimeError("sender lost after begin_send")
    with monkeypatch.context() as sender:
        sender.setattr(cloud, "post_stringer", lost)
        assert solve.prepare_job(claimed, SimpleNamespace(lost=False)) is None
    assert exchanges.get_exchange(scope, lease)["state"] == "sending"
    expire(job)
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    saved = row(job)
    assert saved["status"] == "failed" and saved["error"]["reason_code"] == "solve_outcome_unknown"
    assert saved["output_version_id"] is None and s.calls == ["sent"]
    # Settlement clears the live lease owner. Terminal reads use the finishing
    # worker identity retained in provenance and the stored attempt instead.
    lease = exchanges.JobAttempt(saved["provenance"]["worker_id"], saved["attempt"])
    assert exchanges.get_exchange(scope, lease)["state"] == "outcome_unknown"


def test_postgres_captured_response_survives_crash_and_reclaim(world, http, monkeypatch):
    s = world
    job = submit(s, http)
    claimed = claim(uuid4().hex)
    complete = exchanges.complete_exchange
    monkeypatch.setattr(exchanges, "complete_exchange", lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError("crash after capture")))
    with pytest.raises(RuntimeError):
        solve.prepare_job(claimed, SimpleNamespace(lost=False))
    scope, lease = solve._authority(claimed)
    assert exchanges.get_exchange(scope, lease)["state"] == "responded"
    monkeypatch.setattr(exchanges, "complete_exchange", complete)
    expire(job)
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    assert row(job)["status"] == "succeeded" and len(s.calls) == 1
    assert row(job)["attempt"] == 2


def test_postgres_expired_completion_remains_reclaimable(world, http):
    s = world
    job = submit(s, http)
    owner = uuid4().hex
    claimed = claim(owner)
    result = solve.prepare_job(claimed, SimpleNamespace(lost=False))
    expire(job)
    assert finish(job, owner, result)[0] == "not_owner"
    assert row(job)["status"] == "running"
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    assert row(job)["status"] == "succeeded" and len(s.calls) == 1


@pytest.mark.parametrize("failure,state", [("cloud_upstream_failure", "refused"),
    ("cloud_response_invalid", "refused"), ("solve_outcome_unknown", "outcome_unknown")])
def test_postgres_transport_failures_never_retry(world, http, monkeypatch, failure, state):
    s = world
    job = submit(s, http)
    def failed(*args, **kwargs):
        s.calls.append("one-send")
        raise CloudError(failure, 502)
    monkeypatch.setattr(cloud, "post_stringer", failed)
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    saved = row(job)
    assert saved["status"] == "failed" and saved["error"]["reason_code"] == (failure if failure == "solve_outcome_unknown" else failure.upper())
    with pg.db.cursor() as cur:
        cur.execute("SELECT state FROM project_solve_exchanges WHERE job_id=%s", (job,))
        assert cur.fetchone()["state"] == state
    assert not service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    assert s.calls == ["one-send"]


def test_postgres_invalid_captured_response_refused(world, http, monkeypatch):
    job = submit(world, http)
    monkeypatch.setattr(cloud, "post_stringer", lambda *args, **kwargs: b"{}")
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    assert row(job)["status"] == "failed"
    with pg.db.cursor() as cur:
        cur.execute("SELECT state,response_bytes FROM project_solve_exchanges WHERE job_id=%s", (job,))
        exchange = cur.fetchone()
        assert exchange["state"] == "refused" and bytes(exchange["response_bytes"]) == b"{}"


@pytest.mark.parametrize("mode,cause,state", [("upstream-400", "CLOUD_REQUEST_REJECTED", "refused"),
                                              ("timeout", "solve_outcome_unknown", "outcome_unknown")])
def test_x02_postgres_header_fault_identity_and_terminal_custody(world, http, monkeypatch, mode, cause, state):
    from test_w1c_solve_proposal import _fault_staging, _blocking_transport
    fault = _fault_staging(monkeypatch)
    monkeypatch.setattr(http.route.deps, "auth_live", lambda: True)
    s = world;http.principal["tenant"] = helpers.principal(s)
    body = {"tool": solve.TOOL_NAME, "dwg": str(s.parent), "params": s.params,
        "catalog_digest": pg.manifest(solve.TOOL_NAME), "solve_context": s.solve_context}
    headers = {**helpers.headers(s, str(uuid4())), fault.HEADER: mode}
    before = pg.store.get_drawing_version(s.org, s.project, s.parent)
    response = http.client.post("/api/run", json=body, headers=headers)
    assert response.status_code == 202, response.text
    job = UUID(response.json()["job_id"])
    assert row(job)["execution_context"]["xbench_solver_fault"] == mode
    assert http.client.post("/api/run", json=body, headers=headers).json()["job_id"] == str(job)
    changed = http.client.post("/api/run", json=body, headers={**headers, fault.HEADER:
        "timeout" if mode == "upstream-400" else "upstream-400"})
    assert changed.status_code == 409
    monkeypatch.setattr(cloud, "post_stringer", _blocking_transport)
    monkeypatch.setattr(solve, "SOLVE_ASYNC_PROTOCOL_ENABLED", True)
    calls=[];real_post=fault.Transport.post
    def post(self,*args,**kwargs):
        calls.append(self.mode)
        return real_post(self,*args,**kwargs)
    monkeypatch.setattr(fault.Transport,"post",post)
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    saved=row(job)
    assert saved["status"]=="failed" and saved["error"]["reason_code"]==cause
    assert not service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    with pg.db.cursor() as cur:
        cur.execute("SELECT state,classification,send_protocol FROM project_solve_exchanges WHERE job_id=%s",(job,))
        exchange=cur.fetchone()
        assert exchange["state"]==state and exchange["classification"]=="synthetic" and exchange["send_protocol"] is None
    binding=pg.store_graph.resolve_version_binding(s.org,s.project,s.parent)
    assert binding.is_head and binding.version.version_id==before.version_id and calls==[mode] and not s.calls


def test_x02_postgres_send_crash_reclaims_unknown_without_second_stub(world, http, monkeypatch):
    from test_w1c_solve_proposal import _fault_staging, _blocking_transport
    fault = _fault_staging(monkeypatch)
    monkeypatch.setattr(http.route.deps, "auth_live", lambda: True)
    s=world;http.principal["tenant"]=helpers.principal(s)
    body={"tool":solve.TOOL_NAME,"dwg":str(s.parent),"params":s.params,
          "catalog_digest":pg.manifest(solve.TOOL_NAME),"solve_context":s.solve_context}
    headers={**helpers.headers(s,str(uuid4())),fault.HEADER:"timeout"}
    response=http.client.post("/api/run",json=body,headers=headers)
    assert response.status_code==202,response.text
    job=UUID(response.json()["job_id"]);calls=[]
    monkeypatch.setattr(cloud,"post_stringer",_blocking_transport)
    def crash(self,*args,**kwargs):
        calls.append(self.mode)
        raise SystemExit("synthetic death after durable send intent")
    monkeypatch.setattr(fault.Transport,"post",crash)
    claimed=claim(uuid4().hex)
    with pytest.raises(SystemExit):solve.prepare_job(claimed,SimpleNamespace(lost=False))
    with pg.db.cursor() as cur:
        cur.execute("SELECT state FROM project_solve_exchanges WHERE job_id=%s",(job,))
        assert cur.fetchone()["state"]=="sending"
    expire(job)
    assert service.run_once(uuid4().hex,tool_name=solve.TOOL_NAME,lease_seconds=120)
    saved=row(job)
    assert saved["status"]=="failed" and saved["error"]["reason_code"]=="solve_outcome_unknown"
    assert calls==["timeout"] and not s.calls


def prepared_group_listing(scope):
    app = FastAPI()
    app.include_router(project_drawings.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: helpers.principal(scope)
    with TestClient(app) as client:
        response = client.get(
            f"/api/projects/{scope.project}/drawing-versions/{scope.parent}/context",
            params={"drawing_id": str(scope.drawing), "prepare_solve": "true"},
            headers={"X-Org-Id": str(scope.org), "X-Project-Id": str(scope.project)},
        )
    assert response.status_code == 200, response.text
    result = response.json()["solve_preparation"]
    assert result["available"], result
    return result


def latest_solve(scope):
    result = prepared_group_listing(scope)
    assert result["solve_history_available"]
    return next(frame["latest_solve"] for frame in result["frames"]
                if frame["id"] == scope.solve_context["frame_ref"])


def test_prepared_history_reports_failure_and_retry_without_publication(world, http, monkeypatch):
    assert latest_solve(world) is None
    failed_job = submit(world, http)

    def reject(*args, **kwargs):
        raise CloudError("cloud_upstream_failure", 502)

    with monkeypatch.context() as upstream:
        upstream.setattr(cloud, "post_stringer", reject)
        assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    assert row(failed_job)["status"] == "failed"
    assert latest_solve(world) == {"status": "failed", "reason_code": "CLOUD_UPSTREAM_FAILURE"}

    for saved_code, public_code in (("INVALID_FINAL_GRID_ORDER", "INVALID_FINAL_GRID_ORDER"),
                                    ("private-code", "GRAPH_JOB_FAILED")):
        with pg.db.cursor() as cur:
            cur.execute("UPDATE jobs SET error=%s WHERE job_id=%s",
                        (Jsonb({"reason_code": saved_code, "message": "private detail"}), failed_job))
        assert latest_solve(world) == {"status": "failed", "reason_code": public_code}

    retry_job = submit(world, http)
    assert latest_solve(world) == {"status": "queued"}
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    retry = row(retry_job)
    assert retry["status"] == "succeeded" and retry["output_version_id"] is None
    assert latest_solve(world) == {"status": "succeeded"}
    assert pg.store_graph.resolve_version_binding(world.org, world.project, world.parent).is_head


def test_prepared_history_filters_newer_mismatched_attempts_before_ranking(world, http):
    assert submit(world, http)
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    other_job = submit(world, http)
    context = row(other_job)["execution_context"]
    for field, value in (
        ("drawing_id", str(uuid4())),
        ("parent_intake_sha256", "0" * 64),
        ("solve_binding.frame_id", "other-frame"),
        ("solve_binding.graph_sha256", "0" * 64),
        ("execution_path", "remote"),
    ):
        variant = deepcopy(context)
        parent, _, child = field.partition(".")
        if child:
            variant[parent][child] = value
        else:
            variant[parent] = value
        with pg.db.cursor() as cur:
            cur.execute("UPDATE jobs SET execution_context=%s WHERE job_id=%s", (Jsonb(variant), other_job))
        assert latest_solve(world) == {"status": "succeeded"}, field


def test_prepared_history_retains_saved_coverage_when_job_read_fails(world, monkeypatch):
    before = prepared_group_listing(world)

    def unavailable():
        raise OperationalError("private database detail")

    monkeypatch.setattr(project_solve_history, "connection", unavailable)
    after = prepared_group_listing(world)
    assert after["solve_history_available"] is False
    assert after["frames"] == [
        {key: value for key, value in frame.items() if key != "latest_solve"}
        for frame in before["frames"]
    ]
