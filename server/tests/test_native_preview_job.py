"""Dormant adapter proofs: real F19 custody, synthetic executor evidence only.

Custody controls DB page authority, while using the real exporter and filesystem
reader. PostgreSQL cases reuse the job spine's real admission/lease fixture.
Other store doubles explicitly isolate wire/admission and callback boundaries.
"""
from copy import deepcopy
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace
import uuid

import pytest

import jobs
import native_preview_job as native
from test_project_preview_source import custody  # noqa: F401
from test_jobs_callbacks_postgres import postgres_authority  # noqa: F401


def synthetic(**invocation):
    """A receipt for controlled tests, never a claim of a built/installed app."""
    return {'schema': native.RESULT_SCHEMA, 'target': 'ios-simulator',
            **{k: invocation[k] for k in ('job_id', 'worker_id', 'attempt')},
            'archive_sha256': invocation['descriptor']['archive_sha256'],
            'receipt': {'kind': 'synthetic', 'receipt_id': str(uuid.uuid4()),
                        'sha256': hashlib.sha256(b'synthetic evidence').hexdigest()}}


@pytest.fixture(autouse=True)
def no_production_executor(monkeypatch):
    monkeypatch.setattr(native, '_executor', None)


def context(c):
    return native.validate_context({'schema': native.SCHEMA, 'tenant_id': c.org,
        'org_id': c.org, 'project_id': c.project, 'actor_id': c.actor, 'source': c.prepare()})


def submit(c, descriptor=None, **kwargs):
    return jobs.submit_native_preview(c.org, c.org, c.project, c.actor,
        c.prepare() if descriptor is None else descriptor,
        **{'idempotency_key': 'native-one', **kwargs})


def invoke(c, ctx=None, **guards):
    return native.run(str(uuid.uuid4()), 'controlled-worker', 1,
        context(c) if ctx is None else ctx,
        guards.get('heartbeat', lambda: True), guards.get('cancelled', lambda: False),
        guards.get('deadline', time.monotonic() + 60))


@pytest.fixture
def admission(monkeypatch):
    """Wire-only double; PostgreSQL cases below exercise authoritative SQL."""
    rows, queued = [], []

    def insert(row, *, max_inflight):
        assert max_inflight == jobs.tenant_max_inflight()
        if rows:
            if rows[0]['submission_fingerprint'] != row['submission_fingerprint']:
                raise ValueError('different run input')
            return rows[0]['job_id'], False
        rows.append(deepcopy(row))
        return row['job_id'], True

    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, 'ensure_started', lambda: None)
    monkeypatch.setattr(jobs, '_pg_store', SimpleNamespace(submit=insert))
    monkeypatch.setattr(jobs, '_executors', {jobs.LANE_FAST: SimpleNamespace(
        submit=lambda *a, **k: queued.append((a, k)))})
    return rows, queued


def test_unavailable_rejects_before_insertion(custody, admission):
    with pytest.raises(ValueError, match='unavailable'):
        submit(custody)
    assert admission == ([], [])


def test_exact_custody_and_actor_are_idempotent_input(custody, admission, monkeypatch):
    c = custody
    native.register_executor(synthetic)
    descriptor = c.prepare()
    first = submit(c, descriptor)
    assert submit(c, descriptor) == first
    rows, queued = admission
    assert len(rows) == len(queued) == 1
    execution = json.loads(rows[0]['execution'])
    assert set(execution) == {'operation', 'native_preview'}
    assert execution['operation'] == rows[0]['tool'] == native.OPERATION
    assert execution['native_preview']['source'] == descriptor
    assert queued[0][0] == (jobs._run_native_preview, first)
    # Isolate key input identity from F19 authority for changed descriptors.
    monkeypatch.setattr(native, 'read_source', lambda ctx: b'wire-only')
    with pytest.raises(ValueError, match='different run input'):
        submit(c, {**descriptor, 'archive_sha256': 'a' * 64})
    with pytest.raises(ValueError, match='different run input'):
        jobs.submit_native_preview(c.org, c.org, c.project, str(uuid.uuid4()),
                                   descriptor, idempotency_key='native-one')


@pytest.mark.parametrize('key', ['', None, 42, 'x\n', 'x' * 201])
def test_key_is_required(custody, admission, key):
    native.register_executor(synthetic)
    with pytest.raises(ValueError):
        submit(custody, idempotency_key=key)
    assert not admission[0]


def test_scope_conflicts_and_revocation_reject_before_insert(custody, admission):
    c = custody
    native.register_executor(synthetic)
    descriptor = c.prepare()
    with pytest.raises(ValueError):
        jobs.submit_native_preview(str(uuid.uuid4()), c.org, c.project, c.actor,
                                   descriptor, idempotency_key='one')
    with pytest.raises(ValueError):
        submit(c, {**descriptor, 'project_id': str(uuid.uuid4())})
    c.active = False
    with pytest.raises(Exception):
        submit(c, descriptor)
    assert not admission[0]


def test_postgres_required_and_no_tool_activation(custody, admission, monkeypatch):
    native.register_executor(synthetic)
    with pytest.raises(ValueError, match='internal admission'):
        jobs.submit_job(custody.org, {'name': native.OPERATION},
                        {'native_preview': context(custody)}, '', False)
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'sqlite')
    with pytest.raises(ValueError, match='PostgreSQL'):
        submit(custody)
    assert not admission[0]


def test_controlled_executor_receives_exact_archive_and_owning_identity(custody):
    c = custody
    ctx = context(c)
    calls = []
    def execute(**kw):
        calls.append(kw)
        assert kw['source_bytes'] == c.exported[1]
        assert kw['descriptor'] == ctx['source']
        assert kw['heartbeat']() and not kw['cancelled']()
        return synthetic(**kw)
    native.register_executor(execute)
    result = invoke(c, ctx)
    assert len(calls) == 1
    assert result['receipt']['kind'] == 'synthetic'
    assert set(result) == {'schema', 'target', 'job_id', 'worker_id', 'attempt',
                           'archive_sha256', 'receipt'}


@pytest.mark.parametrize('stop', ['cancelled', 'heartbeat', 'deadline', 'revoked', 'source'])
def test_guard_or_authority_failure_prevents_executor(custody, stop):
    c = custody
    ctx = context(c)
    native.register_executor(lambda **kw: pytest.fail('stopped invocation executed'))
    options = {}
    if stop == 'cancelled': options['cancelled'] = lambda: True
    elif stop == 'heartbeat': options['heartbeat'] = lambda: False
    elif stop == 'deadline': options['deadline'] = time.monotonic() - 1
    elif stop == 'revoked': c.active = False
    else: c.backend.put(native.source._keys(ctx['source'])[0], b'corrupt')
    with pytest.raises(Exception):
        invoke(c, ctx, **options)


@pytest.mark.parametrize('stop', ['cancelled', 'heartbeat', 'deadline', 'revoked', 'source'])
def test_changes_during_executor_prevent_success(custody, monkeypatch, stop):
    c = custody
    ctx = context(c)
    state = {'active': True, 'clock': time.monotonic()}
    def execute(**kw):
        state['active'] = False
        if stop == 'revoked': c.active = False
        if stop == 'source': c.backend.put(native.source._keys(ctx['source'])[0], b'bad')
        if stop == 'deadline': state['clock'] += 120
        return synthetic(**kw)
    native.register_executor(execute)
    monkeypatch.setattr(native.time, 'monotonic', lambda: state['clock'])
    with pytest.raises(Exception):
        invoke(c, ctx, cancelled=lambda: stop == 'cancelled' and not state['active'],
               heartbeat=lambda: stop != 'heartbeat' or state['active'])


@pytest.mark.parametrize('edit', [
    {'schema': 'bad'}, {'target': 'ios'}, {'attempt': True}, {'attempt': 2},
    {'worker_id': 'other'}, {'job_id': str(uuid.uuid4())}, {'archive_sha256': '0' * 64},
    {'built': True}, {'receipt': {}}, {'receipt': {'kind': 'synthetic', 'path': '/secret'}},
])
def test_result_contract_rejects_invalid_or_unbound_evidence(custody, edit):
    ctx = context(custody)
    jid = str(uuid.uuid4())
    result = synthetic(job_id=jid, worker_id='owner', attempt=1, descriptor=ctx['source'])
    with pytest.raises(ValueError):
        native.validate_result(ctx, jid, 'owner', 1, {**result, **edit})


@pytest.mark.parametrize('extra', ['plan', 'tool', 'params', 'completion_provenance',
                                  'capability_provenance', 'solve_scope', 'entity_scope',
                                  'aps_live', 'dwg_version'])
def test_durable_native_context_is_closed(custody, extra):
    ctx = context(custody)
    with pytest.raises(ValueError):
        native.execution_context({'operation': native.OPERATION, 'native_preview': ctx, extra: {}})
    with pytest.raises(ValueError):
        native.validate_context({**ctx, extra: {}})
    with pytest.raises(ValueError):
        native.record_context({}, {'tool': native.OPERATION})


def test_generic_callback_cannot_forge_native_success(custody, monkeypatch):
    ctx = context(custody)
    jid = str(uuid.uuid4())
    durable = {**{k: ctx[k] for k in ('tenant_id', 'org_id', 'project_id')},
               'tool': native.OPERATION, 'attempt': 1,
               'execution': {'operation': native.OPERATION, 'native_preview': ctx}}
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, '_pg_store', SimpleNamespace(durable_context=lambda j: durable,
        complete=lambda *a, **k: pytest.fail('forged success reached store')))
    env = {'ok': True, 'result': synthetic(job_id=jid, worker_id='owner', attempt=1, descriptor=ctx['source'])}
    with pytest.raises(ValueError, match='owning job adapter'):
        jobs.complete_callback(jid, 'complete', result_env=env, worker_id='owner',
            provenance={'attempt': 1, 'execution_path': 'local'}, _completion_owner=True)


def test_recovery_uses_native_discriminator_before_catalog_reconstruction(custody, monkeypatch):
    ctx = context(custody)
    queued = []
    execution = {'operation': native.OPERATION, 'native_preview': ctx}
    rec = {**{k: ctx[k] for k in ('tenant_id', 'org_id', 'project_id')},
           'tool': native.OPERATION, 'status': 'submitted', 'attempt': 0}
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, 'get_job', lambda j: rec)
    monkeypatch.setattr(jobs, '_pg_store', SimpleNamespace(execution=lambda j: deepcopy(execution)))
    monkeypatch.setattr(jobs, '_executors', {jobs.LANE_FAST: SimpleNamespace(submit=lambda *a: queued.append(a))})
    assert jobs._redispatch_record('recover-me')
    assert queued == [(jobs._run_native_preview, 'recover-me')]
    assert native._executor is None  # recovery queues a fail-closed leased attempt


def test_record_serializers_project_only_validated_native_context(custody):
    import job_pg_store
    ctx = context(custody)
    fields = ('job_id tenant_id tool params_json dwg status progress created_at started_at '
              'updated_at finished_at elapsed_ms result_json error_json attempt lease_owner '
              'lease_expires_at heartbeat_at provenance_json org_id project_id authority_mode '
              'idempotency_key dwg_version execution_json').split()
    row = dict.fromkeys(fields)
    row.update(job_id=str(uuid.uuid4()), tenant_id=ctx['tenant_id'], org_id=ctx['org_id'],
               project_id=ctx['project_id'], tool=native.OPERATION, params_json='{}',
               dwg='', status='submitted', attempt=0)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        for invalid in (False, True):
            row['execution_json'] = json.dumps({'operation': native.OPERATION,
                'native_preview': {**ctx, 'actor_id': 'bad'} if invalid else ctx})
            sqlite_row = conn.execute('SELECT ' + ','.join('? AS ' + k for k in row),
                                      list(row.values())).fetchone()
            if invalid:
                with pytest.raises(ValueError): jobs._row_to_record(sqlite_row)
                with pytest.raises(ValueError): job_pg_store._record(row)
            else:
                assert jobs._row_to_record(sqlite_row)['native_preview'] == ctx
                assert job_pg_store._record(row)['native_preview'] == ctx
    finally:
        conn.close()


def test_terminal_recomputes_time_after_blocking_source_read(custody, monkeypatch):
    """A fence double observes the timestamp; real SQL fencing is tested below."""
    ctx = context(custody)
    jid = str(uuid.uuid4())
    durable = {**{k: ctx[k] for k in ('tenant_id', 'org_id', 'project_id')},
        'tool': native.OPERATION, 'attempt': 1,
        'execution': {'operation': native.OPERATION, 'native_preview': ctx}}
    clock, observed = [100.0], []
    real_read = native.read_source
    def read(context):
        raw = real_read(context)
        clock[0] = 200.0
        return raw
    def complete(*args, **kwargs):
        observed.append(args[8])
        return 'not_owner'
    monkeypatch.setattr(native, 'read_source', read)
    monkeypatch.setattr(jobs.time, 'time', lambda: clock[0])
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, '_pg_store', SimpleNamespace(
        durable_context=lambda j: durable, complete=complete))
    env = {'ok': True, 'result': synthetic(job_id=jid, worker_id='owner', attempt=1,
                                         descriptor=ctx['source'])}
    assert jobs.complete_callback(jid, 'complete', result_env=env, worker_id='owner',
        provenance={'attempt': 1, 'execution_path': 'local'}, _native_owner=native._OWNER,
        _native_deadline=time.monotonic() + 60) == 'not_owner'
    assert observed == [200.0]


def test_real_postgres_execution_and_immutable_duplicate(custody, postgres_authority):
    c = custody
    native.register_executor(synthetic)
    jid = submit(c)
    jobs._run_native_preview(jid)
    rec = jobs.get_job(jid)
    assert rec['status'] == 'complete', rec
    assert rec['native_preview']['source'] == c.prepare()
    assert rec['result']['result']['receipt']['kind'] == 'synthetic'
    assert 'source_bytes' not in json.dumps(rec)
    assert jobs.complete_callback(jid, 'complete', result_env=rec['result'],
        worker_id=rec['result']['result']['worker_id'], provenance=rec['provenance'],
        _native_owner=native._OWNER, _native_deadline=time.monotonic() + 60) == 'duplicate'
    changed = deepcopy(rec['result'])
    changed['result']['receipt']['receipt_id'] = str(uuid.uuid4())
    assert jobs.complete_callback(jid, 'complete', result_env=changed,
        worker_id=rec['result']['result']['worker_id'], provenance=rec['provenance'],
        _native_owner=native._OWNER, _native_deadline=time.monotonic() + 60) == 'conflict'
    assert jobs.get_job(jid)['result'] == rec['result']


@pytest.mark.parametrize('stop', ['closed', 'expired', 'attempt', 'unavailable', 'revoked'])
def test_real_postgres_recovery_cannot_succeed_without_owner_or_executor(
        custody, postgres_authority, stop):
    c = custody
    native.register_executor(synthetic)
    jid = submit(c)
    def execute(**kw):
        if stop == 'closed': jobs.mark_job_closed(jid)
        elif stop in ('expired', 'attempt'):
            from job_pg_store import _db
            with _db().transaction() as conn:
                conn.execute('UPDATE async_jobs SET ' + (
                    'lease_expires_at = 0' if stop == 'expired' else 'attempt = attempt + 1')
                    + ' WHERE job_id = %s', (jid,))
        elif stop == 'revoked': c.active = False
        return synthetic(**kw)
    native.register_executor(None if stop == 'unavailable' else execute)
    assert jobs._redispatch_record(jid)
    _, executors = postgres_authority
    args, kwargs = executors[jobs.LANE_FAST].calls[-1]
    args[0](*args[1:], **kwargs)
    assert jobs.get_job(jid)['status'] != 'complete'


def test_real_postgres_retry_keeps_custody_and_attempt(custody, postgres_authority):
    c = custody
    native.register_executor(synthetic)
    jid = submit(c)
    owner = 'first-controlled-owner'
    assert jobs.claim_lease(jid, owner) == 1
    assert jobs._pg_store.release_for_retry(jid, owner, 1, time.time(),
        {'retryable': True}, {'attempt': 1, 'execution_path': 'local'})
    assert jobs._redispatch_record(jid)
    jobs._run_native_preview(jid)
    rec = jobs.get_job(jid)
    assert rec['status'] == 'complete' and rec['attempt'] == 2
    assert rec['native_preview']['source'] == c.prepare()
