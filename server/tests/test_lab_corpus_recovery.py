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
            if sql.startswith('SELECT job_id, tenant_id, org_id, project_id, tool, idempotency_key, status, attempt '):
                assert 'FROM async_jobs WHERE tenant_id=%s AND org_id=%s AND project_id=%s ' in sql
                assert sql.endswith('AND idempotency_key = ANY(%s)')
                assert params[:3] == (TENANT, TENANT, PROJECT)
                state.sql_calls.append((sql, params))
                self.rows = [copy.deepcopy(state.jobs[match['job_id']])
                             for intent in params[3] for match in state.matches.get(intent, [])]
                self.batched = True
                return
            self.batched = False
            assert sql.startswith('SELECT job_id,status,attempt,corpus_cancel_json,corpus_cleanup_json ')
            assert 'FROM async_jobs WHERE tenant_id=%s AND org_id=%s AND project_id=%s ' in sql
            assert 'AND idempotency_key=%s' in sql and sql.endswith('LIMIT 2')
            assert params[:3] == (TENANT, TENANT, PROJECT)
            state.sql_calls.append((sql, params))
            self.rows = copy.deepcopy(state.matches.get(params[3], []))

        def fetchall(self):
            assert self.batched or len(self.rows) <= 2
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


def bind_producers(rig):
    authority = store.corpus_authority(rig.source)
    for block in rig.graph['blocks']:
        intent = f'lab:source-run:{block["id"]}:corpus:v1'
        if intent not in rig.matches:
            continue
        job = rig.jobs[rig.matches[intent][0]['job_id']]
        settings = block['settings']
        context = corpus.validate_context({'schema': corpus.SCHEMA, 'tenant_id': TENANT,
            'org_id': TENANT, 'project_id': PROJECT, 'actor_id': ACTOR,
            'capability_sha256': corpus.ARCHIVE_SHA256, 'corpus': settings['corpus'],
            'suite': settings['suite'],
            'policy': corpus.compile_policy(authority, block['id'], settings)})
        job['lab_corpus'] = context
        if job['status'] == 'complete':
            job['result']['result'].update(schema=corpus.RESULT_SCHEMA,
                context=copy.deepcopy(context), evaluated_suite_sha256='e' * 64)


def confirm(rig, review, *, revoke_at=None):
    checks = []

    def authorize_write(source):
        assert source == rig.source
        checks.append(source['run_id'])
        if len(checks) == revoke_at:
            raise recovery.GraphRefused('write_authority_revoked')

    lineage = recovery.confirmation_lineage(TENANT, 'source-run', review['digest'],
        authorize_write=authorize_write, include_cleanup=review['schema'] == recovery.SCHEMA_V2)
    assert len(checks) == 3
    return lineage


@pytest.mark.parametrize('include_cleanup', [False, True])
def test_confirmation_binds_failed_subset_and_original_retained_producers(rig, include_cleanup):
    bind_producers(rig)
    review = cleanup_plan(rig) if include_cleanup else plan(rig)
    before = copy.deepcopy((rig.source, rig.artifact, rig.jobs, rig.matches))
    lineage = confirm(rig, review)
    assert lineage['source'] == {**review['source'], 'input_digest': rig.source['input_digest']}
    assert lineage['project_id'] == PROJECT
    assert lineage['reviewed_digest'] == review['digest']
    assert lineage['draft_graph'] == review['draft_graph']
    assert [entry['block_id'] for entry in lineage['selected']] == ['oak-volatility']
    assert [(entry['block_id'], entry['job_id'], entry['attempt'], entry['result_sha256'])
            for entry in lineage['retained']] == [
        ('birch-trend', 'original-producer-1', 1, 'c' * 64),
        ('maple-holdout', 'original-producer-2', 1, 'c' * 64)]
    for entry in lineage['selected'] + lineage['retained']:
        assert entry['source_policy'] == rig.jobs[entry['job_id']]['lab_corpus']['policy']
    assert (rig.source, rig.artifact, rig.jobs, rig.matches) == before


@pytest.mark.parametrize('mode,error', [
    ('stale', 'lab_recovery_plan_changed'),
    ('unresolved', 'lab_recovery_plan_ineligible'),
    ('complete-only', 'lab_recovery_plan_ineligible'),
    ('foreign-actor', 'lab_recovery_producer_binding_invalid'),
    ('wrong-attempt', 'lab_recovery_producer_result_invalid'),
    ('revoked', 'write_authority_revoked'),
])
def test_confirmation_refuses_changed_or_unproven_lineage(rig, mode, error):
    if mode == 'unresolved':
        rig.configure(['failed', 'complete', None] * 4)
    if mode == 'complete-only':
        rig.configure(['complete', 'complete'])
    bind_producers(rig)
    if mode == 'foreign-actor':
        rig.jobs['original-producer-0']['lab_corpus']['actor_id'] = PROJECT
    if mode == 'wrong-attempt':
        rig.jobs['original-producer-1']['result']['result']['attempt'] = 2
    review = plan(rig)
    if mode == 'stale':
        rig.jobs['original-producer-1']['result']['result']['result_sha256'] = 'd' * 64
    with pytest.raises(recovery.GraphRefused, match=error):
        confirm(rig, review, revoke_at=3 if mode == 'revoked' else None)


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


def cleanup_plan(rig):
    response = rig.http.get(URL + '?include_cleanup=true')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    result = response.json()
    assert result['schema'] == recovery.SCHEMA_V2
    assert result['execution_allowed'] is False and result['confirmation_required'] is True
    assert result['digest'] == graph_digest({k: v for k, v in result.items() if k != 'digest'})
    assert PRIVATE not in response.text
    for block in result['blocks']:
        evidence = block['cleanup_evidence']
        assert set(evidence) == {'schema', 'job_id', 'attempt', 'state', 'cancel', 'receipt'}
        assert evidence['schema'] == recovery.CLEANUP_SCHEMA
        assert evidence['job_id'] == block['job_id']
    return result


def set_cleanup(rig, marker='reaped', attempt=1):
    job = rig.jobs['original-producer-0']
    job.update(attempt=attempt, progress='closed')
    tag = {'run_id': 'source-run', 'block_id': 'oak-volatility',
           'attempt': attempt, 'fence': 7, 'reason': 'deadline'}
    receipt = {'attempt': attempt, 'cancel': copy.deepcopy(tag), marker: True}
    row = rig.matches[job['idempotency_key']][0]
    row.update(attempt=attempt, corpus_cancel_json=tag, corpus_cleanup_json=receipt)
    return row


@pytest.mark.parametrize('marker,attempt', [('reaped', 1), ('not_started', 0)])
def test_opt_in_projects_exact_native_cleanup_without_writes(rig, marker, attempt):
    row = set_cleanup(rig, marker, attempt)
    if marker == 'reaped':
        row['corpus_cleanup_json']['worker_id'] = PRIVATE
    before = copy.deepcopy((rig.source, rig.artifact, rig.jobs, rig.matches))
    result = cleanup_plan(rig)
    evidence = result['blocks'][0]['cleanup_evidence']
    assert evidence == {'schema': recovery.CLEANUP_SCHEMA, 'job_id': 'original-producer-0',
        'attempt': attempt, 'state': 'confirmed_' + marker,
        'cancel': row['corpus_cancel_json'], 'receipt': {'attempt': attempt,
            'cancel': row['corpus_cancel_json'], 'reaped': marker == 'reaped',
            'not_started': marker == 'not_started'}}
    assert result['blocks'][0]['reason_category'] == 'cancelled'
    assert result['retained_completed_block_ids'] == ['birch-trend', 'maple-holdout']
    assert result['draft_graph'] is None
    assert (rig.source, rig.artifact, rig.jobs, rig.matches) == before
    assert len(rig.sql_calls) == len(rig.job_calls) == 6


@pytest.mark.parametrize('mode', [
    'missing', 'wrong-attempt', 'bool-attempt', 'wrong-tag', 'wrong-fence', 'foreign-run',
    'foreign-block', 'bad-reason', 'bool-fence', 'extra-tag', 'extra-receipt',
    'both-true', 'neither-true', 'nonbool', 'nested-bool-attempt', 'malformed',
    'zero-fence', 'negative-fence', 'wrong-tag-attempt',
])
def test_opt_in_invalid_cleanup_never_confirms_or_echoes_untrusted_fields(rig, mode):
    row = set_cleanup(rig)
    tag, receipt = row['corpus_cancel_json'], row['corpus_cleanup_json']
    if mode == 'missing': row['corpus_cleanup_json'] = None
    elif mode == 'wrong-attempt': receipt['attempt'] = 2
    elif mode == 'bool-attempt': receipt['attempt'] = True
    elif mode == 'wrong-tag': receipt['cancel']['reason'] = 'cancel'
    elif mode == 'wrong-fence': receipt['cancel']['fence'] = 8
    elif mode == 'foreign-run': tag['run_id'] = PRIVATE
    elif mode == 'foreign-block': tag['block_id'] = PRIVATE
    elif mode == 'bad-reason': tag['reason'] = PRIVATE
    elif mode == 'bool-fence': tag['fence'] = True
    elif mode == 'zero-fence': tag['fence'] = 0
    elif mode == 'negative-fence': tag['fence'] = -1
    elif mode == 'wrong-tag-attempt': tag['attempt'] = 2
    elif mode == 'extra-tag': tag['private'] = PRIVATE
    elif mode == 'extra-receipt': receipt['private'] = PRIVATE
    elif mode == 'both-true': receipt['not_started'] = True
    elif mode == 'neither-true': receipt['reaped'] = False
    elif mode == 'nonbool': receipt['reaped'] = 1
    elif mode == 'nested-bool-attempt': receipt['cancel']['attempt'] = True
    else: row['corpus_cancel_json'] = PRIVATE
    result = cleanup_plan(rig)
    evidence = result['blocks'][0]['cleanup_evidence']
    assert evidence['state'] == 'unconfirmed' and evidence['receipt'] is None
    assert result['blocks'][0]['reason_category'] == plan(rig)['blocks'][0]['reason_category']


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'running'])
def test_opt_in_status_and_closed_progress_are_not_cleanup_proof(rig, status):
    job = rig.jobs['original-producer-0']
    job.update(status=status, progress='closed')
    rig.matches[job['idempotency_key']][0]['status'] = status
    assert cleanup_plan(rig)['blocks'][0]['cleanup_evidence']['state'] == 'unconfirmed'


@pytest.mark.parametrize('mode', ['attempt', 'status', 'binding', 'receipt', 'cancel'])
def test_opt_in_changed_bounded_read_refuses_combined_proof(rig, monkeypatch, mode):
    set_cleanup(rig)
    original = recovery.lookup_job
    calls = 0

    def changed(*args):
        nonlocal calls
        calls += 1
        receipt = original(*args)
        if calls == 2:
            if mode == 'attempt': receipt['job']['attempt'] = 2
            elif mode == 'status': receipt['job']['status'] = 'running'
            elif mode == 'binding': receipt['job'].update(project_id=PRIVATE, job_id=PRIVATE)
            elif mode == 'receipt': receipt['cleanup']['reaped'] = False
            else: receipt['cancel']['fence'] = 8
        return receipt

    monkeypatch.setattr(recovery, 'lookup_job', changed)
    result = cleanup_plan(rig)
    evidence = result['blocks'][0]['cleanup_evidence']
    assert evidence['state'] == 'unconfirmed'
    assert evidence['attempt'] is None and evidence['cancel'] is None and evidence['receipt'] is None
    assert result['blocks'][0]['reason_category'] == 'cancelled'


@pytest.mark.parametrize('field', ['tenant_id', 'org_id', 'project_id', 'idempotency_key', 'tool'])
def test_opt_in_foreign_binding_never_projects_job_or_cleanup(rig, field):
    set_cleanup(rig)
    rig.jobs['original-producer-0'][field] = PRIVATE
    evidence = cleanup_plan(rig)['blocks'][0]['cleanup_evidence']
    assert evidence == {'schema': recovery.CLEANUP_SCHEMA, 'job_id': None,
                        'attempt': None, 'state': 'unconfirmed', 'cancel': None, 'receipt': None}


@pytest.mark.parametrize('attempt', [None, -1, True, '1'])
def test_opt_in_malformed_native_attempt_never_confirms(rig, attempt):
    row = set_cleanup(rig)
    row['attempt'] = rig.jobs['original-producer-0']['attempt'] = attempt
    evidence = cleanup_plan(rig)['blocks'][0]['cleanup_evidence']
    assert evidence['attempt'] is None and evidence['state'] == 'unconfirmed'
    assert evidence['cancel'] is None and evidence['receipt'] is None


def test_opt_in_missing_and_foreign_jobs_keep_identifiers_private(rig):
    rig.configure([None, 'failed'], ['oak-volatility', 'birch-trend'])
    job = rig.jobs.pop('original-producer-1')
    job.update(job_id=PRIVATE, project_id=ACTOR)
    rig.jobs[PRIVATE] = job
    rig.matches[job['idempotency_key']][0]['job_id'] = PRIVATE
    result = cleanup_plan(rig)
    assert [block['reason_category'] for block in result['blocks']] == ['missing_job', 'job_binding_mismatch']
    assert all(block['cleanup_evidence']['job_id'] is None for block in result['blocks'])


@pytest.mark.parametrize('revoke_at', [1, 2])
def test_opt_in_revocation_refuses_before_private_read_or_response(rig, revoke_at):
    rig.revoke_at = revoke_at
    response = rig.http.get(URL + '?include_cleanup=true')
    assert response.status_code == 422
    assert response.json() == {'detail': 'lab_corpus_project_refused'}
    assert 'original-producer' not in response.text and PRIVATE not in response.text
    assert len(rig.sql_calls) == (0 if revoke_at == 1 else 6)


def test_opt_in_foreign_tenant_is_refused_before_private_access(rig):
    rig.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        ACTOR, org_id=ACTOR, tier='hosted_pro', subject='auth0|foreign', authority_resolved=True)
    response = rig.http.get(URL + '?include_cleanup=true')
    assert response.status_code == 404
    assert not rig.sql_calls and not rig.ledger_calls


def test_v1_default_and_false_remain_identical_and_v2_digest_covers_cleanup(rig):
    row = set_cleanup(rig)
    default = rig.http.get(URL)
    explicit = rig.http.get(URL + '?include_cleanup=false')
    assert default.content == explicit.content
    v1, v2 = default.json(), cleanup_plan(rig)
    assert all('cleanup_evidence' not in block for block in v1['blocks'])
    unsigned = copy.deepcopy(v2)
    unsigned.pop('digest')
    unsigned['schema'] = recovery.SCHEMA
    for block in unsigned['blocks']:
        block.pop('cleanup_evidence')
    assert graph_digest(unsigned) == v1['digest']
    row['corpus_cancel_json']['fence'] = 9
    row['corpus_cleanup_json']['cancel']['fence'] = 9
    changed = cleanup_plan(rig)
    assert changed['digest'] != v2['digest']
    assert changed['blocks'][0]['cleanup_evidence']['state'] == 'confirmed_reaped'


JOBS_URL = '/api/lab/runs/source-run/corpus-jobs'


@pytest.mark.parametrize('run_state,job_states,expected', [
    ('queued', ['submitted', None], ['queued', 'not submitted']),
    ('running', ['submitted', 'running', 'failed'], ['queued', 'running', 'failed']),
    ('finished', ['complete', 'failed', None], ['completed', 'failed', 'unavailable']),
])
def test_corpus_jobs_projects_status_and_pinned_inputs(rig, run_state, job_states, expected):
    rig.configure(job_states)
    rig.source['state'] = run_state
    response = rig.http.get(JOBS_URL)
    assert response.status_code == 200, response.text
    assert response.headers['Cache-Control'] == 'no-store'
    result = response.json()
    assert result['corpus'] is True
    assert result['source'] == {key: rig.source[key] for key in
                                ('run_id', 'graph_revision', 'graph_sha256', 'input_digest')}
    assert [block['status'] for block in result['blocks']] == expected
    for i, item in enumerate(result['blocks']):
        block = rig.graph['blocks'][i]
        assert item['block_id'] == block['id']
        assert item['job_id'] == (None if job_states[i] is None else f'original-producer-{i}')
        assert item['inputs'] == {
            'corpus_sha256': block['settings']['corpus']['sha256'],
            'suite_sha256': block['settings']['suite']['sha256'],
            'settings_sha256': graph_digest(block['settings']),
        }
    assert str(ACTOR) not in response.text and PRIVATE not in response.text
    assert len(rig.sql_calls) == 1 and not rig.job_calls
    assert rig.authorizations == 2


def test_corpus_jobs_withholds_mismatched_and_ambiguous_jobs(rig):
    rig.configure(['failed', 'complete', 'running'])
    rig.jobs['original-producer-0']['project_id'] = 'other-project'
    rig.jobs['original-producer-1']['tool'] = 'other-operation'
    intent = 'lab:source-run:corpus-2:corpus:v1'
    rig.matches[intent].append(rig.matches[intent][0])
    response = rig.http.get(JOBS_URL)
    assert response.status_code == 200, response.text
    assert all(item['status'] == 'unavailable' and item['job_id'] is None and item['attempt'] is None
               for item in response.json()['blocks'])
    assert 'original-producer' not in response.text


@pytest.mark.parametrize('revoke_at', [1, 2])
def test_corpus_jobs_enforces_live_access_before_read_and_return(rig, revoke_at):
    rig.revoke_at = revoke_at
    response = rig.http.get(JOBS_URL)
    assert response.status_code == 422
    assert response.json() == {'detail': 'lab_corpus_project_refused'}
    assert len(rig.sql_calls) == revoke_at - 1


def test_corpus_jobs_ignores_non_corpus_runs_without_reading_private_inputs(rig):
    rig.source['corpus_authority_json'] = None
    response = rig.http.get(JOBS_URL)
    assert response.status_code == 200
    assert response.json() == {'corpus': False}
    assert not rig.ledger_calls and not rig.sql_calls


def test_corpus_jobs_include_piped_corpus_blocks_in_mixed_graphs(rig):
    rig.configure(['failed', 'complete'], ['oak-volatility', 'birch-trend'])
    rig.graph['blocks'][1]['registry']['id'] = 'other-operation'
    rig.graph['pipes'] = [{'from': {'node': 'oak-volatility', 'port': 'result'},
                           'to': {'node': 'birch-trend', 'port': 'result'}}]
    rig.pin()
    reply = rig.http.get(JOBS_URL)
    assert reply.status_code == 200, reply.text
    assert [item['block_id'] for item in reply.json()['blocks']] == ['oak-volatility']

@pytest.fixture
def saved_retry_inputs(monkeypatch, tmp_path):
    import hashlib
    import json

    monkeypatch.setenv('LEAF_LAB_STORE', 'sqlite')
    monkeypatch.setenv('LEAF_LAB_DB', str(tmp_path / 'lab.db'))
    monkeypatch.setenv('LEAF_RUNTIME_ENV', 'test')
    monkeypatch.setenv('LEAF_PLATFORM_POSTGRES_REQUIRED', '0')
    monkeypatch.setenv('LEAF_LAB_TOOL_NODES', '1')
    manifest, _ = lab_tool_manifests.corpus_capability()
    raw = json.dumps({'parent_policy': {'min_k': 2, 'edge_threshold_cents': 5.0},
                      'child_min_k': [1], 'blocks': {'null.identity': [{}]}}).encode()
    material = {'material_id': '44444444-4444-4444-8444-000000000001',
                'sha256': hashlib.sha256(raw).hexdigest(), 'byte_size': len(raw)}
    block = {'id': 'failed-evaluation', 'registry': {
        'id': manifest['id'], 'version': manifest['version']['semver'],
        'code_sha256': manifest['version']['code_sha256']},
        'settings': {'project_id': PROJECT, 'corpus': material, 'suite': material,
                     'parameters': {'child_min_k': [0], 'min_select_tickets': 0}}}

    def save(doc_id, saved_block):
        graph = {'schema': 'leaf.lab-graph.v1', 'blocks': [saved_block], 'pipes': [], 'outputs': {}}
        artifact = ledger.save(TENANT, doc_id, graph, None, ACTOR)
        return {'tenant_id': TENANT, 'project_id': PROJECT, 'doc_id': doc_id,
                'graph_revision': artifact['revision'], 'graph_sha256': artifact['sha256']}

    source = save('original', block)
    old_policy = corpus.compile_policy(source, block['id'], block['settings'])
    selected = {'block_id': block['id'], 'settings_sha256': graph_digest(block['settings']),
                'corpus_sha256': material['sha256'], 'source_policy': old_policy}
    return SimpleNamespace(raw=raw, block=block, source=source, save=save,
                           confirmation={'selected': [selected]})


def test_retry_inputs_preserve_effective_bytes_under_distinct_policy(saved_retry_inputs):
    import hashlib

    row = saved_retry_inputs
    successor = row.save('successor', row.block)
    digest = recovery.verify_retry_inputs(row.confirmation, successor, row.block, row.raw)
    original = row.confirmation['selected'][0]['source_policy']
    candidate = corpus.compile_policy(successor, row.block['id'], row.block['settings'])
    effective = corpus.compiled_suite(row.raw, original)
    assert effective == corpus.compiled_suite(row.raw, candidate)
    assert digest == {'source_policy_digest': original['digest'],
                      'successor_policy_digest': candidate['digest'],
                      'evaluated_suite_sha256': hashlib.sha256(effective).hexdigest()}
    assert digest['source_policy_digest'] != digest['successor_policy_digest']


@pytest.mark.parametrize('changed', ['settings', 'suite_bytes', 'source_policy', 'source_document', 'selection'])
def test_retry_inputs_refuse_scope_or_effective_input_drift(saved_retry_inputs, changed):
    row = saved_retry_inputs
    if changed == 'settings':
        row.block['settings']['parameters']['min_select_tickets'] = 1
    successor = row.source if changed == 'source_document' else row.save('successor', row.block)
    if changed == 'suite_bytes':
        row.raw += b' '
    if changed == 'source_policy':
        row.confirmation['selected'][0]['source_policy']['parameters']['min_select_tickets'] = 1
    if changed == 'selection':
        row.confirmation['selected'].clear()
    with pytest.raises(recovery.GraphRefused, match='^lab_recovery_successor_inputs_changed$'):
        recovery.verify_retry_inputs(row.confirmation, successor, row.block, row.raw)
