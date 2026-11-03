"""Runs HTTP contract on SQLite and PostgreSQL authorities."""
from datetime import timedelta
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
import pytest

import deps
import lab_artifacts as ledger
import lab_run_store as store
from lab_canonical import graph_digest
from routers import lab_router, lab_runs
from test_lab_evaluate import case
from test_lab_run_store import authority  # noqa: F401 -- PostgreSQL twin
from test_lab_corpus_executor import private_pg, installed, kernel_filter, lane as producer_lane  # noqa: F401


@pytest.fixture
def rig(authority, monkeypatch):
    monkeypatch.setenv("LEAF_LAB_STORE", store.store_mode())
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_BUILD_REVISION", "abc1234")
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [])
    graph, registry = case()
    projection_registry_for = lab_runs.lab_tool_manifests.registry_for
    monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", lambda tenant: registry)
    artifact = ledger.save(authority, "doc", graph, None, "member")
    context = deps.TenantContext(authority, tier="hosted_pro", subject="run-actor")
    app = FastAPI()
    app.include_router(lab_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: context
    prior = store.paused()
    with TestClient(app, raise_server_exceptions=False) as http:
        yield SimpleNamespace(http=http, tenant=authority, context=context, app=app,
                              graph=graph, registry=registry, artifact=artifact,
                              projection_registry_for=projection_registry_for)
    store.set_paused(prior, "", "test")


def create(rig):
    return rig.http.post("/api/lab/runs", json={"doc_id": "doc", "graph_revision": 1})


def test_corpus_manifest_is_normal_compilable_and_drawing_free(monkeypatch):
    import lab_tool_manifests
    from lab_evaluate import admit_graph
    monkeypatch.setenv('LEAF_LAB_TOOL_NODES', '1')
    manifest, trusted = lab_tool_manifests.corpus_capability()
    assert trusted.execution_class == 'worker'
    assert 'drawing' not in manifest['settings_schema']['properties']
    material = {'material_id': '11111111-2222-4333-8444-555555555555',
                'sha256': 'a' * 64, 'byte_size': 100}
    graph = {'schema': 'leaf.lab-graph.v1', 'blocks': [{
        'id': 'corpus-evaluator-1', 'registry': {'id': manifest['id'],
            'version': manifest['version']['semver'], 'code_sha256': manifest['version']['code_sha256']},
        'settings': {'project_id': material['material_id'], 'corpus': material, 'suite': material}}],
        'pipes': [], 'outputs': {}}
    assert admit_graph(graph, {'corpus-evaluator@1.0.0': manifest})


@pytest.fixture
def corpus_join(producer_lane, private_pg, monkeypatch):
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    import lab_tool_manifests
    import lab_tool_nodes
    context, jobs, _, _, _ = producer_lane
    for name in ('LEAF_LAB_STORE', 'LEAF_LAB_RUN_STORE'):
        monkeypatch.setenv(name, 'postgres')
    monkeypatch.setenv('LEAF_LAB_TOOL_NODES', '1')
    monkeypatch.setenv('LEAF_BUILD_REVISION', 'corpus-join-proof')
    monkeypatch.setattr(deps, 'effective_tools_with_provenance', lambda tenant: [])
    with private_pg.cursor() as cur:
        cur.execute('SELECT external_subject FROM identity_bindings WHERE binding_id=%s', (context['actor_id'],))
        subject = cur.fetchone()['external_subject']
    tenant = deps.TenantContext(context['tenant_id'], org_id=context['org_id'], tier='hosted_pro',
                                subject=subject, authority_resolved=True)
    manifest, _ = lab_tool_manifests.corpus_capability()
    graph = {'schema': 'leaf.lab-graph.v1', 'blocks': [{
        'id': 'corpus-evaluator-1', 'registry': {'id': manifest['id'],
            'version': manifest['version']['semver'], 'code_sha256': manifest['version']['code_sha256']},
        'settings': {key: context[key] for key in ('project_id', 'corpus', 'suite')}}],
        'pipes': [], 'outputs': {'evaluation': {'node': 'corpus-evaluator-1', 'port': 'result'}}}
    doc_id = 'corpus-' + uuid.uuid4().hex
    artifact = ledger.save(str(tenant), doc_id, graph, None, subject)
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setitem(jobs._executors, jobs.LANE_SLOW, pool)
    app = FastAPI()
    app.include_router(lab_runs.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    with TestClient(app, raise_server_exceptions=False) as http:
        yield SimpleNamespace(context=context, jobs=jobs, tenant=tenant, graph=graph,
            manifest=manifest, doc_id=doc_id, artifact=artifact, http=http, db=private_pg)
    for row in jobs.list_jobs(str(tenant), 200):
        if row['status'] not in jobs.TERMINAL:
            jobs.mark_job_closed(row['job_id'])
    pool.shutdown(wait=True)


def corpus_admit(lane):
    from test_lab_run_store import intent_id
    body = {'doc_id': lane.doc_id, 'graph_revision': 1, 'submission_id': intent_id()}
    response = lane.http.post('/api/lab/runs', json=body)
    assert response.status_code == 202, response.text
    return response.json()['run_id'], body


def corpus_adapter(lane, run_id, owner='corpus-worker'):
    import lab_tool_nodes
    row = store.get(str(lane.tenant), run_id)
    lease = store.claim(str(lane.tenant), run_id, owner, 30,
                        build_revision='corpus-join-proof', color='active')
    assert lease
    args = (str(lane.tenant), run_id, owner, lease['fence'])
    adapter = lab_tool_nodes.ToolNodeAdapter(str(lane.tenant), run_id, cancelled=lambda: False,
        corpus_authority=store.corpus_authority(row),
        corpus_owned=lambda: store.owns_corpus_run(*args),
        corpus_requested=lambda: store.cancel_requested(str(lane.tenant), run_id),
        corpus_cancel=lambda block, job, deadline: store.cancel_corpus_job(*args, block, job, deadline=deadline))
    return row, lease, adapter


def test_corpus_normal_compiled_run_actual_producer_and_recovery(corpus_join):
    from functools import partial
    import lab_tool_manifests
    from lab_evaluate import evaluate_graph
    lane = corpus_join
    run_id, body = corpus_admit(lane)
    body['reconcile_only'] = True
    assert lane.http.post('/api/lab/runs', json=body).json()['run_id'] == run_id
    row, lease, adapter = corpus_adapter(lane, run_id)
    authority = store.corpus_authority(row)
    assert authority['actor_id'] == lane.context['actor_id']
    assert authority['project_id'] == lane.context['project_id']
    options = dict(tool_adapter=adapter, trusted_lookup=partial(lab_tool_manifests.trusted_for, str(lane.tenant)))
    registry = lab_tool_manifests.registry_for(str(lane.tenant))
    result = evaluate_graph(lane.graph, registry, **options)
    value = result['values']['corpus-evaluator-1']['result']
    assert not value.error, value.error
    projected = json.loads(value.data)
    assert projected['row_count'] == 6001 and projected['run_id'] == run_id
    assert projected['context']['suite'] == lane.context['suite']
    assert projected['doc_id'] == lane.doc_id and projected['graph_sha256'] == lane.artifact['sha256']
    again = evaluate_graph(lane.graph, registry, **options)
    assert json.loads(again['values']['corpus-evaluator-1']['result'].data)['job_id'] == projected['job_id']
    assert len([job for job in lane.jobs.list_jobs(str(lane.tenant), 200)
                if job['idempotency_key'] == f'lab:{run_id}:corpus-evaluator-1:corpus:v1']) == 1
    lane.db.assert_schema_current()
    with lane.db.cursor() as cur:
        cur.execute("SELECT data_type FROM information_schema.columns WHERE table_name='lab_runs' "
                    "AND column_name='corpus_authority_json'")
        assert cur.fetchone()['data_type'] == 'jsonb'


def test_corpus_picker_lists_real_named_project_materials_and_refuses_client_actor(corpus_join):
    lane = corpus_join
    response = lane.http.get('/api/lab/runs/corpus-options', params={'project_id': lane.context['project_id']})
    assert response.status_code == 200, response.text
    assert any(project['name'] == 'Synthetic corpus' for project in response.json()['projects'])
    assert {material['name'] for material in response.json()['materials']} == {'synthetic.jsonl.gz', 'suite.json'}
    response = lane.http.post('/api/lab/runs', json={'doc_id': lane.doc_id, 'graph_revision': 1, 'actor_id': lane.context['actor_id']})
    assert response.status_code == 422


def test_corpus_revoked_actor_refused_before_execution_and_after_restart(corpus_join):
    lane = corpus_join
    run_id, _ = corpus_admit(lane)
    with lane.db.transaction() as conn:
        conn.execute("UPDATE identity_bindings SET status='revoked',revoked_at=NOW() WHERE binding_id=%s", (lane.context['actor_id'],))
    assert lane.http.post('/api/lab/runs', json={'doc_id': lane.doc_id, 'graph_revision': 1}).status_code == 422
    _, _, adapter = corpus_adapter(lane, run_id)
    trusted = lab_runs.lab_tool_manifests.corpus_capability()[1]
    output = adapter('corpus-evaluator-1', trusted, lane.manifest, {}, lane.graph['blocks'][0]['settings'], deadline_s=5)
    assert 'error' in output
    assert not lane.jobs.list_jobs(str(lane.tenant), 200)


@pytest.mark.parametrize('reason', ['cancel', 'deadline'])
def test_corpus_closed_intent_waits_actual_reap_not_generic_terminal(corpus_join, monkeypatch, reason):
    from concurrent.futures import ThreadPoolExecutor
    import subprocess
    import sys
    import threading
    import time
    import lab_corpus_executor as corpus
    from lab_evaluate import RunCancelled, RunBudgetExceeded
    lane = corpus_join
    run_id, _ = corpus_admit(lane)
    _, lease, adapter = corpus_adapter(lane, run_id)
    children, stages = [], []
    reaped, acknowledge = threading.Event(), threading.Event()
    real_popen, terminate = subprocess.Popen, corpus._terminate
    def start(argv, **kwargs):
        stages.append(kwargs['cwd'])
        child = real_popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    def held_cleanup(child):
        terminate(child)
        reaped.set()
        assert acknowledge.wait(5)
    monkeypatch.setattr(corpus.subprocess, 'Popen', start)
    monkeypatch.setattr(corpus, '_terminate', held_cleanup)
    trusted = lab_runs.lab_tool_manifests.corpus_capability()[1]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(adapter, 'corpus-evaluator-1', trusted, lane.manifest, {},
            lane.graph['blocks'][0]['settings'], deadline_s=0.3 if reason == 'deadline' else 30)
        stop = time.monotonic() + 5
        while not children and time.monotonic() < stop:
            time.sleep(0.01)
        assert children
        rows = lane.jobs.list_jobs(str(lane.tenant), 200)
        job_id = rows[0]['job_id']
        if reason == 'cancel':
            store.request_cancel(str(lane.tenant), run_id, 'operator')
        assert reaped.wait(5)
        assert lane.jobs.corpus_cleanup(job_id, str(lane.tenant)) is None
        # A generic reaper terminal is not evidence that staged files have gone.
        from envelopes import ErrorCode, error_obj
        current = lane.jobs.get_job(job_id)
        lane.jobs.complete_callback(job_id, 'failed',
            error=error_obj(ErrorCode.INTERNAL, 'synthetic generic reaper', False),
            provenance={'attempt': current['attempt'], 'execution_path': 'local'}, _allow_closed=True)
        time.sleep(0.3)
        assert not future.done()
        acknowledge.set()
        with pytest.raises(RunBudgetExceeded if reason == 'deadline' else RunCancelled):
            future.result(timeout=5)
        receipt = lane.jobs.corpus_cleanup(job_id, str(lane.tenant))
        assert receipt['reaped'] is True and receipt['cancel']['fence'] == lease['fence']
        assert children[0].poll() is not None and all(not path.exists() for path in stages)


def test_corpus_stale_lab_owner_does_not_cancel_successor_intent(corpus_join, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import subprocess
    import sys
    import time
    import lab_corpus_executor as corpus
    from lab_evaluate import RunCancelled
    lane = corpus_join
    run_id, _ = corpus_admit(lane)
    _, lease, adapter = corpus_adapter(lane, run_id)
    children = []
    real_popen = subprocess.Popen
    def start(argv, **kwargs):
        child = real_popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(corpus.subprocess, 'Popen', start)
    trusted = lab_runs.lab_tool_manifests.corpus_capability()[1]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(adapter, 'corpus-evaluator-1', trusted, lane.manifest, {},
            lane.graph['blocks'][0]['settings'], deadline_s=30)
        stop = time.monotonic() + 5
        while not children and time.monotonic() < stop:
            time.sleep(0.01)
        assert children
        job_id = lane.jobs.list_jobs(str(lane.tenant), 200)[0]['job_id']
        store._run(str(lane.tenant), lambda sql: sql.execute(
            'UPDATE lab_runs SET lease_expires_at=? WHERE tenant_id=? AND run_id=?',
            ((store._now() - timedelta(seconds=1)).isoformat(), str(lane.tenant), run_id)), write=True)
        successor = store.claim(str(lane.tenant), run_id, 'successor', 30,
                                build_revision='corpus-join-proof', color='active')
        assert successor['fence'] > lease['fence']
        with pytest.raises(RunCancelled):
            future.result(timeout=5)
        assert store.cancel_corpus_job(str(lane.tenant), run_id, 'corpus-worker', lease['fence'],
            'corpus-evaluator-1', job_id, deadline=True) is None
        assert children[0].poll() is None
        assert lane.jobs.get_job(job_id)['progress'] != 'closed'
        store.request_cancel(str(lane.tenant), run_id, 'operator')
        tag = store.cancel_corpus_job(str(lane.tenant), run_id, 'successor', successor['fence'],
                                     'corpus-evaluator-1', job_id)
        assert tag['fence'] == successor['fence']
        stop = time.monotonic() + 5
        while not lane.jobs.corpus_cleanup(job_id, str(lane.tenant)) and time.monotonic() < stop:
            time.sleep(0.05)
        assert lane.jobs.corpus_cleanup(job_id, str(lane.tenant))['reaped'] is True


def test_corpus_durable_authority_tamper_is_refused(corpus_join):
    import uuid
    lane = corpus_join
    run_id, _ = corpus_admit(lane)
    row = store.get(str(lane.tenant), run_id)
    value = store.corpus_authority(row)
    value['actor_id'] = str(uuid.uuid4())
    row['corpus_authority_json'] = value
    with pytest.raises(Exception, match='authority_mismatch'):
        store.corpus_authority(row)


def test_corpus_foreign_project_reader_cannot_get_events_cancel_or_open_stream(corpus_join):
    import uuid
    from routers import lab_stream
    from leaf_platform import store as platform_store
    lane = corpus_join
    run_id, _ = corpus_admit(lane)
    lane.http.app.include_router(lab_stream.router)
    subject = str(uuid.uuid4())
    platform_store.create_identity_binding(lane.context['org_id'], 'auth0', subject, role='owner')
    lane.tenant.subject = subject
    for path in (f'/api/lab/runs/{run_id}', f'/api/lab/runs/{run_id}/events',
                 f'/api/lab/stream/runs?runs={run_id}'):
        response = lane.http.get(path)
        assert response.status_code == 422, response.text
        assert 'project_refused' in response.text and 'row_count' not in response.text
    assert lane.http.post(f'/api/lab/runs/{run_id}/cancel').status_code == 422
    assert not store.cancel_requested(str(lane.tenant), run_id)
    with lane.db.cursor() as cur:
        cur.execute('SELECT COUNT(*) AS count FROM lab_stream_leases WHERE tenant_id=%s', (str(lane.tenant),))
        assert cur.fetchone()['count'] == 0


def test_corpus_stream_same_project_delivers_then_closes_before_revoked_batch(corpus_join):
    import asyncio
    import uuid
    from routers import lab_stream
    from lab_evaluate import runtime_value, export_value
    lane = corpus_join
    run_id, _ = corpus_admit(lane)
    _, lease, _ = corpus_adapter(lane, run_id)
    args = (str(lane.tenant), run_id, 'corpus-worker', lease['fence'])
    store.append_events(*args, [{'kind': 'value', 'block_id': 'corpus-evaluator-1',
        'payload': {'result': export_value(runtime_value('CORPUS-ALLOWED-DERIVED'))}}])
    async def exercise():
        principal, stream_id = lane.tenant.subject, uuid.uuid4().hex
        store.acquire_stream(str(lane.tenant), principal, stream_id, 45)
        stream = lab_stream.event_stream(lane.tenant, {run_id: -1}, principal, stream_id, asyncio.Event())
        try:
            frame = await asyncio.wait_for(anext(stream), 3)
            assert 'CORPUS-ALLOWED-DERIVED' in frame
            with lane.db.transaction() as conn:
                conn.execute("UPDATE project_member_bindings SET status='revoked',revoked_at=NOW() "
                    'WHERE org_id=%s AND project_id=%s AND binding_id=%s',
                    (lane.context['org_id'], lane.context['project_id'], lane.context['actor_id']))
            store.append_events(*args, [{'kind': 'value', 'block_id': 'corpus-evaluator-1',
                'payload': {'result': export_value(runtime_value('CORPUS-REFUSED-DERIVED'))}}])
            closed = await asyncio.wait_for(anext(stream), 3)
            assert 'corpus_project_refused' in closed
            assert 'CORPUS-REFUSED-DERIVED' not in closed
        finally:
            await stream.aclose()
        with lane.db.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS count FROM lab_stream_leases WHERE tenant_id=%s', (str(lane.tenant),))
            assert cur.fetchone()['count'] == 0
    asyncio.run(exercise())


def test_corpus_reap_ack_never_accepts_old_attempt_or_expired_open_intent(corpus_join, monkeypatch):
    lane = corpus_join
    class Queue:
        def submit(self, *args):
            pass
    monkeypatch.setitem(lane.jobs._executors, lane.jobs.LANE_SLOW, Queue())
    run_id, _ = corpus_admit(lane)
    _, lease, adapter = corpus_adapter(lane, run_id)
    context = lane.context
    job_id = lane.jobs.submit_lab_corpus(context['tenant_id'], context['org_id'], context['project_id'],
        context['actor_id'], context['corpus'], context['suite'], capability_sha256=context['capability_sha256'],
        idempotency_key=f'lab:{run_id}:corpus-evaluator-1:corpus:v1')
    with lane.db.transaction() as conn:
        conn.execute("UPDATE async_jobs SET attempt=2,status='running' WHERE job_id=%s", (job_id,))
    lane.jobs._ack_corpus_reap(job_id, 'expired-open', 2)
    assert lane.jobs.corpus_cleanup(job_id, str(lane.tenant)) is None
    tag = store.cancel_corpus_job(str(lane.tenant), run_id, 'corpus-worker', lease['fence'],
                                  'corpus-evaluator-1', job_id, deadline=True)
    assert tag['attempt'] == 2
    lane.jobs._ack_corpus_reap(job_id, 'stale-producer', 1)
    assert lane.jobs.corpus_cleanup(job_id, str(lane.tenant)) is None


def test_submission_recovery_rechecks_admission_and_authority(rig, monkeypatch):
    from test_lab_run_store import intent_id
    body = {"doc_id": "doc", "graph_revision": 1, "submission_id": intent_id()}
    response = rig.http.post("/api/lab/runs", json=body)
    assert response.status_code == 202, response.text
    body["reconcile_only"] = True
    monkeypatch.setattr(store, "MAX_QUEUED_PER_TENANT", 0)
    recovered = rig.http.post("/api/lab/runs", json=body)
    assert recovered.json() == response.json()
    monkeypatch.setenv("LEAF_BUILD_REVISION", "different")
    mismatch = rig.http.post("/api/lab/runs", json=body)
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == "lab_submission_mismatch"
    monkeypatch.setenv("LEAF_BUILD_REVISION", "abc1234")
    rig.registry.clear()
    refused = rig.http.post("/api/lab/runs", json=body)
    assert refused.status_code == 422
    rig.context.tier = "free"
    denied = rig.http.post("/api/lab/runs", json=body)
    assert denied.status_code == 403


def test_submission_missing_and_invalid_are_explicit(rig):
    from test_lab_run_store import intent_id
    body = {"doc_id": "doc", "graph_revision": 1, "submission_id": intent_id(), "reconcile_only": True}
    response = rig.http.post("/api/lab/runs", json=body)
    assert response.status_code == 409
    assert response.json()["detail"] == "lab_submission_missing"
    body["submission_id"] = None
    assert rig.http.post("/api/lab/runs", json=body).status_code == 422


def test_create_pins_and_public_row(rig):
    response = create(rig)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert response.json() == {"run_id": run_id, "state": "queued"}
    row = store.get(rig.tenant, run_id)
    assert row["graph_sha256"] == rig.artifact["sha256"]
    assert row["graph_revision"] == 1
    assert row["input_digest"] == graph_digest({})
    assert row["registry_digest"] == store.registry_digest()
    assert (row["ops_revision"], row["adapter_revision"], row["build_revision"]) == (
        "lab-ops.v1", "lab-worker.v1", "abc1234")
    public = rig.http.get("/api/lab/runs/" + run_id)
    assert public.status_code == 200
    assert public.json() == jsonable_encoder({k: v for k, v in row.items()
                                            if k not in {"lease_owner", "lease_fence"}})
    assert create(rig).json()["run_id"] != run_id


@pytest.mark.parametrize("kind,code", [
    ("host", "lab_graph_host_refused"), ("effect", "lab_graph_effect_refused"),
    ("invalid", "lab_graph_unknown_block"),
    ("malformed", "lab_graph_invalid"),
])
def test_refuses_before_enqueue(rig, monkeypatch, kind, code):
    if kind in {"host", "effect"}:
        monkeypatch.setattr(lab_runs.lab_registry, "lookup", lambda name, version:
                            SimpleNamespace(residency="host" if kind == "host" else "tenant",
                                            effect="pure" if kind == "host" else "write")
                            if name == "identity" else None)
    elif kind == "malformed":
        monkeypatch.setattr(ledger, "get", lambda *args: {**rig.artifact, "graph": {}})
    else:
        rig.registry.clear()
    def forbidden(*args, **kwargs):
        pytest.fail("refused graph reached enqueue")
    monkeypatch.setattr(store, "create", forbidden)
    response = create(rig)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": code}


@pytest.mark.parametrize("flag,drawing,host,code", [
    ("1", {"project_id": "project", "drawing_id": "drawing", "dwg_version": 1}, False, None),
    ("1", {"project_id": "project", "drawing_id": "drawing", "dwg_version": 0}, False,
     "lab_tool_drawing_required"),
    ("1", None, False, "lab_tool_drawing_required"),
    ("1", None, True, "lab_graph_host_refused"),
    ("0", {"project_id": "project", "drawing_id": "drawing", "dwg_version": 1}, False,
     "lab_graph_effect_refused"),
])
def test_tool_node_admission(rig, monkeypatch, flag, drawing, host, code):
    from dataclasses import replace
    from test_lab_tool_nodes import tool_graph
    tool = tool_graph(monkeypatch)
    tool.graph["blocks"][0]["settings"] = {"drawing": drawing} if drawing is not None else {}
    if host:
        monkeypatch.setitem(lab_runs.lab_registry.REGISTRY, (tool.trusted.kind, tool.trusted.version),
                            replace(tool.trusted, residency="host"))
    monkeypatch.setenv("LEAF_LAB_TOOL_NODES", flag)
    monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", lambda tenant: tool.registry)
    monkeypatch.setattr(ledger, "get", lambda *args: {**rig.artifact, "graph": tool.graph})
    response = create(rig)
    if code is None:
        assert response.status_code == 202, response.text
    else:
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": code}


@pytest.mark.parametrize("change,code", [
    (None, None), ("disabled", "lab_graph_effect_refused"),
    ("stale", "lab_graph_unknown_block"), ("drawing", "lab_graph_invalid_settings"),
    ("malformed-drawing", "lab_graph_invalid_settings"),
    ("host", "lab_graph_host_refused"),
])
def test_projected_tool_admission(rig, monkeypatch, change, code):
    from copy import deepcopy
    from test_lab_tool_nodes import projected_graph
    projected = projected_graph(monkeypatch)
    monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", rig.projection_registry_for)
    monkeypatch.setenv("LEAF_LAB_TOOL_NODES", "0" if change == "disabled" else "1")
    graph = projected.graph
    if change == "stale":
        graph["blocks"][0]["registry"]["code_sha256"] = "0" * 64
    elif change == "drawing":
        graph["blocks"][0]["settings"] = {}
    elif change == "malformed-drawing":
        graph["blocks"][0]["settings"]["drawing"]["dwg_version"] = "2"
    elif change == "host":
        # A typed host source still cannot feed a runnable projected tool.
        source_graph, source_registry = case("constant-number")
        source = deepcopy(source_graph["blocks"][0])
        manifest = deepcopy(next(iter(source_registry.values())))
        manifest.update(id="host-source", effect="read")
        target_pin = projected.manifest["inputs"]["max-rows"]
        for key in ("mode", "unit", "nullable", "schema"):
            manifest["outputs"]["result"][key] = json.loads(json.dumps(target_pin[key]))
        source["id"] = "host"
        source["registry"]["id"] = "host-source"
        graph["blocks"].insert(0, source)
        graph["pipes"] = [{"from": {"node": "host", "port": "result"},
                           "to": {"node": "node", "port": "max-rows"}}]
        registry = rig.projection_registry_for(str(rig.tenant))
        registry["host-source@" + source["registry"]["version"]] = manifest
        monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", lambda tenant: registry)
    monkeypatch.setattr(ledger, "get", lambda *args: {**rig.artifact, "graph": graph})
    response = create(rig)
    assert response.status_code == (202 if code is None else 422), response.text
    if code is not None:
        assert response.json() == {"detail": code}


def test_queue_full(rig):
    for _ in range(16):
        assert create(rig).status_code == 202
    response = create(rig)
    assert response.status_code == 409
    assert response.json() == {"detail": "lab_run_queue_full"}


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("GET", "/events"), ("POST", "/cancel")])
def test_foreign_and_missing_run(rig, method, suffix):
    run_id = create(rig).json()["run_id"]
    rig.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        rig.tenant + "-foreign", tier="hosted_pro", subject="other")
    for key in (run_id, "missing"):
        response = rig.http.request(method, "/api/lab/runs/" + key + suffix)
        assert response.status_code == 404
        assert response.json() == {"detail": "lab_run_not_found"}
    assert store.get(rig.tenant, run_id)["state"] == "queued"


def test_events_cursor(rig):
    run_id = create(rig).json()["run_id"]
    lease = store.claim(rig.tenant, run_id, "worker", 60, build_revision="abc1234", color="active")
    assert store.append_events(rig.tenant, run_id, "worker", lease["fence"], [
        {"kind": "progress", "payload": {"n": n}} for n in range(3)])
    path = "/api/lab/runs/" + run_id + "/events"
    first = rig.http.get(path, params={"after_seq": -1, "limit": 1})
    assert first.status_code == 200
    assert [e["seq"] for e in first.json()["events"]] == [0]
    rest = rig.http.get(path, params={"after_seq": 0, "limit": 500})
    assert [e["seq"] for e in rest.json()["events"]] == [1, 2]


def test_lease_takeover_event_hides_previous_holder(rig, monkeypatch):
    run_id = create(rig).json()["run_id"]
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    first = store.claim(rig.tenant, run_id, "private-worker", 60,
                        build_revision="abc1234", color="active")
    assert first
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=61))
    assert store.claim(rig.tenant, run_id, "replacement-worker", 60,
                       build_revision="abc1234", color="active")
    stored = store.list_events(rig.tenant, run_id, after_seq=-1)
    assert stored[0]["payload"] == {"previous_owner": "private-worker",
                                     "previous_fence": first["fence"]}
    response = rig.http.get("/api/lab/runs/" + run_id + "/events?after_seq=-1")
    assert response.status_code == 200, response.text
    assert response.json() == {"events": [jsonable_encoder({
        "seq": stored[0]["seq"], "kind": "lease_takeover", "block_id": None,
        "at": stored[0]["created_at"], "payload": {},
    })]}
    for private in ("previous_owner", "previous_fence", "private-worker", "replacement-worker"):
        assert private not in response.text


@pytest.mark.parametrize("kind,payload,expected", [
    ("value", {"out": {"type": "number", "data": 42},
               "other": {"type": "string", "data": "ok"}},
              {"out": {"type": "number", "data": 42},
               "other": {"type": "string", "data": "ok"}}),
    ("failed", {"reason": "worker_error"}, {"reason": "worker_error"}),
    ("suspended", {"reason": "maintenance"}, {"reason": "maintenance"}),
    ("cancelled", {"reason": "cancelled"}, {}),
    ("finished", {}, {}),
    ("coalesced", {"removed": 3, "count": 3}, {"removed": 3}),
    ("unknown-kind", {"reason": "private"}, {}),
])
def test_event_payload_projection(rig, kind, payload, expected):
    run_id = create(rig).json()["run_id"]
    lease = store.claim(rig.tenant, run_id, "worker", 60, build_revision="abc1234", color="active")
    if kind != "value":
        payload = {**payload, "previous_owner": "private-worker", "previous_fence": 99,
                   "owner": "private-worker", "fence": 99, "actor": "private-actor",
                   "extra": "private-extra"}
    assert store.append_events(rig.tenant, run_id, "worker", lease["fence"], [
        {"kind": kind, "block_id": "block", "payload": payload}])
    stored = store.list_events(rig.tenant, run_id, after_seq=-1)[0]
    response = rig.http.get("/api/lab/runs/" + run_id + "/events?after_seq=-1")
    assert response.status_code == 200, response.text
    assert response.json() == {"events": [jsonable_encoder({
        "seq": 0, "kind": kind, "block_id": "block", "at": stored["created_at"],
        "payload": expected,
    })]}


def test_cancel_actor_and_idempotency(rig):
    run_id = create(rig).json()["run_id"]
    path = "/api/lab/runs/" + run_id + "/cancel"
    assert rig.http.post(path).json() == {"cancel_requested": True}
    assert rig.http.post(path).json() == {"cancel_requested": False}
    row = store.get(rig.tenant, run_id)
    assert row["state"] == "cancelled" and row["cancel_requested_by"] == "run-actor"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_worker_controls_need_deploy(rig, action):
    path = "/api/lab/runs/worker/" + action
    rig.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        rig.tenant, tier="hosted_starter", subject="member")
    response = rig.http.post(path, json={"reason": "maintenance"} if action == "pause" else None)
    assert response.status_code == 403
    rig.app.dependency_overrides[deps.require_tenant] = lambda: rig.context
    response = rig.http.post(path, json={"reason": "maintenance"} if action == "pause" else None)
    assert response.status_code == 200
    assert response.json() == {"paused": action == "pause"}
    assert store.paused() is (action == "pause")


def test_status_worker_fields_and_liveness(rig, monkeypatch):
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now - timedelta(seconds=61))
    store.record_worker_status(rig.tenant + "-stale", "old", "idle", 0, 0)
    monkeypatch.setattr(store, "_now", lambda: now)
    store.record_worker_status(rig.tenant, "abc1234", "active", 1, 2)
    create(rig)
    response = rig.http.get("/api/lab/status")
    assert response.status_code == 200, response.text
    status = response.json()["worker"]
    assert set(status) == {"paused", "workers", "queue_age_max_s"}
    worker = next(w for w in status["workers"] if w["build_revision"] == "abc1234")
    assert set(worker) == {"build_revision", "color", "heartbeat_age_s", "active_runs", "queued_runs"}
    assert worker["color"] == "active" and worker["active_runs"] == 1 and worker["queued_runs"] == 2
    assert worker["heartbeat_age_s"] >= 0 and status["queue_age_max_s"] >= 0
    assert not any(w["build_revision"] == "old" for w in status["workers"])
    assert "worker_id" not in response.text and "lease_owner" not in response.text and "fence" not in response.text
    store._run("lab-worker-control", lambda sql: sql.execute(
        "DELETE FROM lab_worker_status WHERE worker_id=?", (rig.tenant + "-stale",)), write=True)


@pytest.mark.parametrize("body", [{}, {"doc_id": "doc", "graph_revision": True},
    {"doc_id": "doc", "graph_revision": 0}, {"doc_id": "doc", "graph_revision": "1"},
    {"doc_id": "doc", "graph_revision": 1, "run_id": "caller"}])
def test_exact_create_fields(rig, body):
    assert rig.http.post("/api/lab/runs", json=body).status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "after_seq=-2"])
def test_event_bounds(rig, query):
    run_id = create(rig).json()["run_id"]
    assert rig.http.get("/api/lab/runs/" + run_id + "/events?" + query).status_code == 422


@pytest.mark.parametrize("operation", ["create", "status"])
def test_store_failures_are_sanitized(rig, monkeypatch, operation):
    def fail(*args, **kwargs):
        raise RuntimeError("postgres://private-host/secret")
    monkeypatch.setattr(store, "create" if operation == "create" else "worker_status", fail)
    response = create(rig) if operation == "create" else rig.http.get("/api/lab/status")
    if operation == "status":
        assert response.status_code == 200
        assert response.json()["worker"] == {"state": "unavailable", "reason": "lab_store_unavailable"}
        assert "private-host" not in response.text
    else:
        assert response.status_code == 503
        assert response.json() == {"detail": "lab_store_unavailable"}
