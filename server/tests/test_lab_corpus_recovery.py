"""C4 preparation through the real route, with inert authoritative job reads."""
import copy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import jobs
import lab_artifacts as ledger
import lab_activation
import lab_corpus_executor as corpus
import lab_corpus_recovery as recovery
import lab_run_store as store
import lab_tool_manifests
from lab_canonical import graph_digest
from routers import lab_router, lab_runs

TENANT = '11111111-1111-4111-8111-111111111111'
PROJECT = '22222222-2222-4222-8222-222222222222'
ACTOR = '33333333-3333-4333-8333-333333333333'
URL = '/api/lab/runs/source-run/recovery-plan'
PRIVATE = 'private exception /secret/path credential=do-not-expose'


@pytest.fixture
def rig(monkeypatch):
    for name in ('LEAF_LAB_SURFACE_ENABLED', 'LEAF_AUTH_LIVE', 'LEAF_LAB_TOOL_NODES'):
        monkeypatch.setenv(name, '1')
    monkeypatch.delenv('LEAF_ENTITLEMENTS_FILE', raising=False)
    lab_runs.platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle
    context = deps.TenantContext(TENANT, org_id=TENANT, tier='hosted_pro',
                               subject='auth0|recovery-reader', authority_resolved=True)
    manifest, _ = lab_tool_manifests.corpus_capability()
    state = SimpleNamespace(graph=None, source=None, artifact=None, jobs={}, matches={},
                            sql_calls=[], job_calls=[], ledger_calls=[], authorizations=0,
                            revoke_at=None, binding=True)

    def authorize(tenant, project, actor, *, write):
        assert (tenant, project, actor, write) == (TENANT, PROJECT, ACTOR, False)
        state.authorizations += 1
        if state.revoke_at is not None and state.authorizations >= state.revoke_at:
            raise project_lifecycle.LifecycleForbidden(PRIVATE)
        return 'read_only'

    monkeypatch.setattr(project_lifecycle, 'require_project_role', authorize)
    monkeypatch.setattr(lab_runs.platform_link, 'platform_store', lambda: SimpleNamespace(
        resolve_active_identity_binding=lambda provider, subject: SimpleNamespace(
            binding_id=ACTOR, platform_tenant_id=TENANT) if state.binding else None))

    def source_get(tenant, run_id):
        if (tenant, run_id) != (TENANT, 'source-run'):
            raise ledger.NotFound('lab_run_not_found')
        return copy.deepcopy(state.source)

    def artifact_get(tenant, doc_id, revision):
        assert (tenant, doc_id, revision) == (TENANT, 'original-doc', 7)
        state.ledger_calls.append((tenant, doc_id, revision))
        return copy.deepcopy(state.artifact)

    def job_get(job_id):
        state.job_calls.append(job_id)
        return copy.deepcopy(state.jobs.get(job_id))

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params):
            assert sql.startswith('SELECT job_id,status,attempt,corpus_cancel_json,corpus_cleanup_json ')
            assert 'FROM async_jobs WHERE tenant_id=%s AND org_id=%s AND project_id=%s ' in sql
            assert 'AND idempotency_key=%s' in sql and sql.endswith('LIMIT 2')
            assert params[:3] == (TENANT, TENANT, PROJECT)
            state.sql_calls.append((sql, params))
            self.rows = copy.deepcopy(state.matches.get(params[3], []))

        def fetchall(self):
            assert len(self.rows) <= 2
            return self.rows

    monkeypatch.setattr(recovery.platform_link, 'platform_db', lambda: SimpleNamespace(cursor=Cursor))
    monkeypatch.setattr(store, 'get', source_get)
    monkeypatch.setattr(ledger, 'get', artifact_get)
    monkeypatch.setattr(jobs, 'get_job', job_get)

    def forbidden(*args, **kwargs):
        pytest.fail('recovery planning attempted a write, submission, event read, or capped job list')

    for owner, names in (
        (jobs, ('submit_job', 'submit_lab_corpus', '_submit_job', 'list_jobs', 'corpus_cleanup')),
        (corpus, ('submit_policy', 'run', 'read_sources', 'require_executor')),
        (ledger, ('save', 'restore', 'publish')),
        (lab_activation, ('create', 'pause', 'resume', 'renew', 'off')),
        (store, ('create', 'request_cancel', 'cancel_corpus_job', 'append_events', 'list_events',
                 'set_paused', '_run')),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)

    def configure(statuses, names=None):
        names = names or [f'corpus-{i}' for i in range(len(statuses))]
        blocks = []
        state.jobs.clear()
        state.matches.clear()
        for i, (name, status) in enumerate(zip(names, statuses)):
            material = {'material_id': f'44444444-4444-4444-8444-{i:012d}',
                        'sha256': f'{i + 1:064x}', 'byte_size': 100 + i}
            blocks.append({'id': name, 'registry': {'id': manifest['id'],
                'version': manifest['version']['semver'],
                'code_sha256': manifest['version']['code_sha256']},
                'settings': {'project_id': PROJECT, 'corpus': material,
                    'suite': dict(material, sha256='b' * 64),
                    'parameters': {'draws': 31 + i, 'seed': 41 + i,
                                   'child_min_k': [0, 1], 'min_select_tickets': 9 + i}}})
            intent = f'lab:source-run:{name}:corpus:v1'
            if status is None:
                continue
            job_id = f'original-producer-{i}'
            state.jobs[job_id] = {'job_id': job_id, 'tenant_id': TENANT, 'org_id': TENANT,
                'project_id': PROJECT, 'tool': corpus.OPERATION, 'idempotency_key': intent,
                'status': status, 'progress': 'done', 'attempt': 1,
                'error': {'error_code': 'TIMEOUT', 'message': PRIVATE} if status == 'failed' else None,
                'result': {'result': {'job_id': job_id, 'worker_id': 'original-worker',
                    'attempt': 1, 'result_sha256': 'c' * 64,
                    'result': {'verdict': 'fail' if i % 2 else 'pass'}}} if status == 'complete' else None}
            state.matches[intent] = [{'job_id': job_id, 'status': status, 'attempt': 1,
                                     'corpus_cancel_json': None, 'corpus_cleanup_json': None}]
        state.graph = {'schema': 'leaf.lab-graph.v1', 'blocks': blocks, 'pipes': [],
                       'outputs': {f'out-{i}': {'node': name, 'port': 'result'}
                                   for i, name in enumerate(names)}}
        state.pin()

    def pin():
        digest = graph_digest(state.graph)
        authority = {'schema': 'leaf.lab-corpus-authority.v1', 'tenant_id': TENANT,
            'project_id': PROJECT, 'actor_id': ACTOR, 'doc_id': 'original-doc',
            'graph_revision': 7, 'graph_sha256': digest}
        state.source = {'tenant_id': TENANT, 'run_id': 'source-run', 'doc_id': 'original-doc',
            'graph_revision': 7, 'graph_sha256': digest, 'state': 'failed',
            'input_digest': graph_digest(authority), 'corpus_authority_json': authority,
            'cancel_requested_at': None}
        state.artifact = {'tenant_id': TENANT, 'doc_id': 'original-doc', 'revision': 7,
                          'sha256': digest, 'graph': state.graph}

    state.configure, state.pin = configure, pin
    configure(['failed', 'complete', 'complete'], ['oak-volatility', 'birch-trend', 'maple-holdout'])
    app = FastAPI()
    app.include_router(lab_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: context
    state.app = app
    with TestClient(app, raise_server_exceptions=False) as http:
        state.http = http
        yield state


def plan(rig):
    response = rig.http.get(URL)
    assert response.status_code == 200, response.text
    assert PRIVATE not in response.text
    result = response.json()
    assert result['execution_allowed'] is False
    assert result['confirmation_required'] is True
    assert result['schema'] == recovery.SCHEMA
    assert result['digest'] == graph_digest({k: v for k, v in result.items() if k != 'digest'})
    return result


def test_failed_only_preparation_and_v1_keep_original_producers_and_settings(rig):
    before = copy.deepcopy((rig.source, rig.artifact, rig.jobs, rig.matches))
    result = plan(rig)
    assert result['selected_failed_block_ids'] == ['oak-volatility']
    assert result['retained_completed_block_ids'] == ['birch-trend', 'maple-holdout']
    assert result['unresolved_block_ids'] == []
    assert result['draft_graph'] == dict(rig.graph, blocks=rig.graph['blocks'][:1],
                                       outputs={'out-0': rig.graph['outputs']['out-0']})
    assert [(b['job_id'], b['status']) for b in result['blocks']] == [
        ('original-producer-0', 'failed'), ('original-producer-1', 'complete'),
        ('original-producer-2', 'complete')]
    assert rig.jobs['original-producer-1']['result']['result']['result']['verdict'] == 'fail'
    assert result['source'] == {k: rig.source[k] for k in
        ('run_id', 'doc_id', 'graph_revision', 'graph_sha256', 'state')}
    assert (rig.source, rig.artifact, rig.jobs, rig.matches) == before
    assert rig.authorizations == 2
    assert len(rig.sql_calls) == len(rig.job_calls) == 3
    assert rig.ledger_calls == [(TENANT, 'original-doc', 7)]


def test_original_single_failed_node_is_an_eligible_draft(rig):
    rig.configure(['failed'], ['failed-only'])
    result = plan(rig)
    assert result['draft_graph'] == rig.graph
    assert result['selected_failed_block_ids'] == ['failed-only']


def test_v2_twelve_nodes_resolve_exactly_without_fabricating_a_draft(rig):
    statuses = ['failed', 'complete', 'failed', 'queued', None, 'failed'] + ['complete'] * 6
    rig.configure(statuses)
    rig.jobs['original-producer-2']['error'] = {'error_code': 'INVALID_INPUT', 'message': PRIVATE}
    rig.jobs['original-producer-5']['error'] = {'error_code': 'INTERNAL', 'message': 'out of memory ' + PRIVATE}
    result = plan(rig)
    assert result['draft_graph'] is None
    assert result['selected_failed_block_ids'] == ['corpus-0']
    assert result['unresolved_block_ids'] == ['corpus-2', 'corpus-3', 'corpus-4', 'corpus-5']
    assert [b['reason_category'] for b in result['blocks'][:6]] == [
        'timeout', 'completed', 'invalid_input', 'job_nonterminal', 'missing_job', 'unknown_failure']
    assert result['blocks'][4]['job_id'] is None
    assert len(rig.sql_calls) == 12 and len(rig.job_calls) == 11
    assert len({params[3] for _, params in rig.sql_calls}) == 12


def test_v2_terminal_control_produces_precisely_the_failed_subset(rig):
    statuses = ['failed' if i % 3 == 0 else 'complete' for i in range(12)]
    rig.configure(statuses)
    result = plan(rig)
    selected = [block for block, status in zip(rig.graph['blocks'], statuses) if status == 'failed']
    assert result['draft_graph']['blocks'] == selected
    assert result['selected_failed_block_ids'] == [block['id'] for block in selected]
    assert set(result['draft_graph']['outputs']) == {'out-0', 'out-3', 'out-6', 'out-9'}


def test_v3_concurrent_reads_are_equal_and_receipt_changes_invalidate_confirmation(rig):
    before = copy.deepcopy((rig.source, rig.artifact, rig.jobs, rig.matches))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: plan(rig), range(8)))
    assert all(result == results[0] for result in results)
    assert (rig.source, rig.artifact, rig.jobs, rig.matches) == before
    rig.jobs['original-producer-1']['result']['result']['result_sha256'] = 'd' * 64
    changed = plan(rig)
    assert changed['digest'] != results[0]['digest']
    assert changed['blocks'][1]['receipt_sha256'] != results[0]['blocks'][1]['receipt_sha256']
    assert len(rig.sql_calls) == len(rig.job_calls) == 27


@pytest.mark.parametrize('field,value', [
    ('tenant_id', 'foreign-tenant'), ('org_id', 'foreign-org'), ('project_id', 'foreign-project'),
    ('idempotency_key', 'foreign-intent'), ('tool', 'effectful-other-operation'),
])
def test_foreign_or_mismatched_authoritative_job_never_escapes(rig, field, value):
    job = rig.jobs['original-producer-0']
    job[field] = value
    job['job_id'] = 'foreign-private-job'
    rig.jobs['foreign-private-job'] = rig.jobs.pop('original-producer-0')
    rig.matches['lab:source-run:oak-volatility:corpus:v1'][0]['job_id'] = 'foreign-private-job'
    result = plan(rig)
    assert result['draft_graph'] is None
    item = result['blocks'][0]
    assert item['reason_category'] == 'job_binding_mismatch'
    assert item['job_id'] is None and item['status'] is None and item['receipt_sha256'] is None
    assert 'foreign-private-job' not in str(result) and value not in str(result)


@pytest.mark.parametrize('mode,reason', [
    ('duplicate', 'duplicate_jobs'), ('disappeared', 'job_receipt_changed'),
    ('changed-attempt', 'job_receipt_changed'), ('changed-status', 'job_receipt_changed'),
])
def test_ambiguous_or_changed_exact_lookup_is_unresolved(rig, mode, reason):
    intent = rig.jobs['original-producer-0']['idempotency_key']
    if mode == 'duplicate':
        rig.matches[intent].append(dict(rig.matches[intent][0], job_id='second-job'))
    elif mode == 'disappeared':
        del rig.jobs['original-producer-0']
    elif mode == 'changed-attempt':
        rig.jobs['original-producer-0']['attempt'] = 2
    else:
        rig.jobs['original-producer-0']['status'] = 'running'
    result = plan(rig)
    assert result['blocks'][0]['reason_category'] == reason
    assert result['draft_graph'] is None
    assert len(rig.sql_calls) == 3
    assert len(rig.job_calls) == (2 if mode == 'duplicate' else 3)


@pytest.mark.parametrize('code,reason', [
    ('INVALID_INPUT', 'invalid_input'), ('BAD_PARAMS', 'invalid_input'),
    ('ACCESS_REVOKED', 'access_revoked'), ('TENANT_DISABLED', 'access_revoked'),
    ('CANCELLED', 'cancelled'), ('INTERNAL', 'unknown_failure'),
])
def test_nonrecoverable_failure_requires_manual_resolution(rig, code, reason):
    rig.jobs['original-producer-0']['error'] = {'error_code': code, 'message': PRIVATE}
    result = plan(rig)
    assert result['blocks'][0]['reason_category'] == reason
    assert result['unresolved_block_ids'] == ['oak-volatility']
    assert result['draft_graph'] is None


@pytest.mark.parametrize('cleanup', [None, {'reaped': True}, {'attempt': 9, 'reaped': True}])
def test_cancellation_requires_exact_cleanup_receipt_and_stays_manual(rig, cleanup):
    intent = rig.jobs['original-producer-0']['idempotency_key']
    tag = {'run_id': 'source-run', 'block_id': 'oak-volatility', 'attempt': 1, 'reason': 'deadline'}
    rig.matches[intent][0].update(corpus_cancel_json=tag, corpus_cleanup_json=cleanup)
    assert plan(rig)['blocks'][0]['reason_category'] == 'cleanup_unconfirmed'
    rig.matches[intent][0]['corpus_cleanup_json'] = {'attempt': 1, 'cancel': tag, 'reaped': True}
    result = plan(rig)
    assert result['blocks'][0]['reason_category'] == 'cancelled'
    assert result['draft_graph'] is None


@pytest.mark.parametrize('revoke_at', [1, 2])
def test_live_project_access_is_checked_before_read_and_before_return(rig, revoke_at):
    rig.revoke_at = revoke_at
    response = rig.http.get(URL)
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'lab_corpus_project_refused'}
    assert 'original-producer' not in response.text and PRIVATE not in response.text
    assert len(rig.sql_calls) == (0 if revoke_at == 1 else 3)
    assert len(rig.ledger_calls) == (0 if revoke_at == 1 else 1)


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_nonterminal_source_refused_before_graph_or_jobs(rig, state):
    rig.source['state'] = state
    response = rig.http.get(URL)
    assert response.status_code == 422
    assert response.json() == {'detail': 'lab_recovery_terminal_required'}
    assert not rig.ledger_calls and not rig.sql_calls


@pytest.mark.parametrize('mode', ['artifact-digest', 'graph-bytes', 'revision', 'authority-digest'])
def test_digest_and_revision_drift_refused_before_jobs(rig, mode):
    if mode == 'artifact-digest':
        rig.artifact['sha256'] = 'f' * 64
    elif mode == 'graph-bytes':
        rig.graph['blocks'][0]['settings']['parameters']['seed'] += 1
    elif mode == 'revision':
        rig.artifact['revision'] = 8
    else:
        rig.source['input_digest'] = 'f' * 64
    response = rig.http.get(URL)
    assert response.status_code == 422, response.text
    assert 'original-producer' not in response.text
    assert not rig.sql_calls


@pytest.mark.parametrize('mode', ['pipes', 'other-operation', 'project', 'empty'])
def test_unsupported_graph_shapes_are_explicitly_refused(rig, mode):
    if mode == 'pipes':
        rig.graph['pipes'] = [{'from': {'node': 'oak-volatility', 'port': 'result'},
                               'to': {'node': 'birch-trend', 'port': 'result'}}]
    elif mode == 'other-operation':
        rig.graph['blocks'][0]['registry']['id'] = 'effectful-operation'
    elif mode == 'project':
        rig.graph['blocks'][0]['settings']['project_id'] = ACTOR
    else:
        rig.graph.update(blocks=[], outputs={})
    rig.pin()
    response = rig.http.get(URL)
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'lab_recovery_project_mismatch' if mode == 'project'
                               else 'lab_recovery_independent_corpus_required'}
    assert not rig.sql_calls


def test_complete_only_plan_keeps_outputs_in_original_run(rig):
    rig.configure(['complete', 'complete'])
    result = plan(rig)
    assert result['draft_graph'] is None and result['selected_failed_block_ids'] == []
    assert result['retained_completed_block_ids'] == ['corpus-0', 'corpus-1']


@pytest.mark.parametrize('state,cancel_requested', [('cancelled', None), ('failed', 'stored-cancel-time')])
def test_cancelled_source_keeps_completed_jobs_and_requires_manual_resolution(rig, state, cancel_requested):
    rig.source.update(state=state, cancel_requested_at=cancel_requested)
    result = plan(rig)
    assert result['blocks'][0]['reason_category'] == 'source_cancelled'
    assert result['draft_graph'] is None
    assert result['retained_completed_block_ids'] == ['birch-trend', 'maple-holdout']


def test_finished_source_and_fixed_executor_budget_diagnostic_are_supported(rig):
    rig.source['state'] = 'finished'
    rig.jobs['original-producer-0']['error'] = {
        'error_code': 'INTERNAL', 'corpus_failure': 'lease_or_budget', 'message': PRIVATE}
    result = plan(rig)
    assert result['selected_failed_block_ids'] == ['oak-volatility']
    assert result['blocks'][0]['reason_category'] == 'lease_or_budget'
    assert result['draft_graph']['blocks'] == rig.graph['blocks'][:1]


def test_closed_job_and_unavailable_runtime_require_manual_resolution(rig):
    job = rig.jobs['original-producer-0']
    job['progress'] = 'closed'
    assert plan(rig)['blocks'][0]['reason_category'] == 'cleanup_unconfirmed'
    job['progress'] = 'done'
    job['error'] = {'error_code': 'INTERNAL', 'corpus_failure': 'runtime_unavailable', 'message': PRIVATE}
    result = plan(rig)
    assert result['blocks'][0]['reason_category'] == 'runtime_unavailable'
    assert result['draft_graph'] is None


def test_corpus_feature_gate_refuses_before_graph_or_jobs(rig, monkeypatch):
    monkeypatch.setenv('LEAF_LAB_TOOL_NODES', '0')
    response = rig.http.get(URL)
    assert response.status_code == 422
    assert response.json() == {'detail': 'lab_corpus_unavailable'}
    assert not rig.ledger_calls and not rig.sql_calls


def test_invalid_binding_and_job_store_outage_are_sanitized(rig, monkeypatch):
    rig.binding = False
    response = rig.http.get(URL)
    assert response.status_code == 422
    assert response.json() == {'detail': 'lab_corpus_authority_required'}
    assert not rig.sql_calls and not rig.ledger_calls
    rig.binding = True

    def unavailable():
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(recovery.platform_link, 'platform_db', unavailable)
    response = rig.http.get(URL)
    assert response.status_code == 503
    assert response.json() == {'detail': 'lab_store_unavailable'}
    assert PRIVATE not in response.text
