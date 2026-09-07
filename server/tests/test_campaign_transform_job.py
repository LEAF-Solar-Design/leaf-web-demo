"""Authored completion execution, immutable input and independently checked CSV."""
from copy import deepcopy
from contextlib import contextmanager
from types import SimpleNamespace
import hashlib
import json
import sqlite3
import time
import uuid

import pytest

import campaign_transform_job as adapter
import campaign_web_tool_static as static
import customization_service
import deps
import jobs
import job_pg_store
import tool_loader
from customization_models import ChangeState


SOURCE = '''import json
def run(intake, params):
    records = json.loads(params["source_json"])
    headers = []
    for row in records:
        for key in row:
            if key not in headers: headers.append(key)
    def neutral(s):
        return "'" + s if s and s[0] in '=+-@\\t\\r' else s
    def cell(v):
        if v is None: return ''
        if isinstance(v, bool): return 'true' if v else 'false'
        if isinstance(v, float): return str(int(v)) if v == int(v) else repr(v)
        if isinstance(v, int): return str(v)
        return neutral(v)
    def quote(s): return '"' + s.replace('"', '""') + '"'
    lines = [','.join(quote(neutral(h)) for h in headers)]
    for row in records: lines.append(','.join(quote(cell(row.get(h))) for h in headers))
    return {"csv": '\\r\\n'.join(lines) + '\\r\\n'}
'''


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def published(monkeypatch, tmp_path):
    job_pg_store._db()  # Package registration only, no connection.
    source_json = json.dumps([{'name': 'a,"b\n', '=header': '=SUM(A1)', 'yes': True,
                              'empty': None, 'number': 2.0}, {'name': '@cmd', 'later': 'x'}])
    params = {'source_json': source_json}
    tool = {'name': adapter.CONSTANTS['tool_name'], 'version': '1', 'entry': 'transform.py',
            'params': {'type': 'object', 'properties': {'source_json': {'type': 'string'}},
                       'required': ['source_json'], 'additionalProperties': False}}
    ctx = {**adapter.CONSTANTS, **{key: str(uuid.uuid4()) for key in adapter.IDS},
           'contract_version': 1, 'tenant_id': 'tenant-transform', 'change_set_id': 'transform-change',
           'catalog_commit': 'a' * 40, 'effective_catalog_digest': 'b' * 64,
           'tool_manifest_sha256': deps.catalog_tool_digest(tool),
           'tool_source_sha256': digest(SOURCE.encode()), 'input_sha256': digest(source_json.encode())}
    path = tmp_path / 'transform.py'
    path.write_bytes(SOURCE.replace('\n', '\r\n').encode())
    pin = SimpleNamespace(tenant_id=ctx['tenant_id'], change_set_id=ctx['change_set_id'],
                          catalog_commit=ctx['catalog_commit'], catalog_digest=ctx['effective_catalog_digest'])
    change = SimpleNamespace(tenant_id=ctx['tenant_id'], change_set_id=ctx['change_set_id'],
                             state=ChangeState.PUBLISHED, staged_commit=ctx['catalog_commit'],
                             catalog_digest=ctx['effective_catalog_digest'])
    service = SimpleNamespace(store=SimpleNamespace(get_effective_catalog=lambda **k: pin,
                                                     get_change_set=lambda **k: change),
                              _staged_tool=lambda c: tool)
    monkeypatch.setattr(customization_service.CustomizationService, 'configured', lambda: service)
    monkeypatch.setattr(customization_service, 'effective_catalog_pin', lambda t: {
        k: ctx[k] for k in ('catalog_commit', 'effective_catalog_digest')})
    monkeypatch.setattr(deps, 'effective_tools_with_provenance', lambda t: [(tool, deps.TOOL_SOURCE_TENANT_REPO)])
    monkeypatch.setattr(tool_loader, '_tenant_repo_root', lambda t: tmp_path)
    monkeypatch.setattr(adapter, 'check_authority', lambda c: None)
    monkeypatch.delenv('LEAF_TOOL_SANDBOX_PROVIDER', raising=False)
    monkeypatch.setenv('LEAF_SANDBOX', 'e2b')
    monkeypatch.setenv('LEAF_SANDBOX_TIMEOUT_S', '5')
    monkeypatch.setenv('JOB_LEASE_S', '60')
    monkeypatch.setenv('HEARTBEAT_STALE_S', '120')
    return SimpleNamespace(ctx=ctx, tool=tool, params=params, path=path, pin=pin, change=change,
                           jid=str(uuid.uuid4()))


def invoke(p, **kw):
    return adapter.run(p.jid, p.ctx, p.tool, kw.pop('params', p.params),
                       kw.pop('heartbeat', lambda: True), kw.pop('cancelled', lambda: False),
                       kw.pop('deadline', time.monotonic() + 30))


def test_actual_captured_source_executes_and_matches_independent_csv(published, monkeypatch):
    real = tool_loader.run_tool_dynamic
    calls = []

    def execute(tool, intake, params, aps_live, **kw):
        calls.append(kw['test_source'])
        published.path.write_text('raise RuntimeError("replacement must not execute")')
        return real(tool, intake, params, aps_live, **kw)

    monkeypatch.setattr(tool_loader, 'run_tool_dynamic', execute)
    result = invoke(published)
    assert result['ok'] is True, result
    assert calls == [SOURCE]
    assert result['result']['csv'].encode() == static.expected_output(published.params['source_json'].encode())
    assert "'=SUM(A1)" in result['result']['csv']


def test_cumulative_catalog_retains_csv_when_latest_change_added_another_tool(published, monkeypatch):
    service = customization_service.CustomizationService.configured()
    monkeypatch.setattr(service, '_staged_tool', lambda change: {
        'name': 'different-published-tool', 'version': '1', 'entry': 'other.py'})
    result = invoke(published)
    assert result['ok'] is True, result
    assert result['result']['csv'].encode() == static.expected_output(published.params['source_json'].encode())


@pytest.mark.parametrize('source', [
    'def run(i,p): return {"csv": "wrong"}\n',
    'def run(i,p): return {"csv": "wrong", "output_sha256": "0" * 64}\n',
    'def run(i,p): raise RuntimeError("sensitive message")\n',
])
def test_actual_wrong_or_failed_output_never_becomes_expected_csv(published, source):
    published.path.write_text(source)
    published.ctx['tool_source_sha256'] = digest(source.encode())
    env = invoke(published)
    assert env['ok'] is False and env.get('result') is None
    assert 'sensitive message' not in json.dumps(env)


@pytest.mark.parametrize('source_json', ['[{"x":{}}]', '[{"x":NaN}]', '[]',
                                        json.dumps([{'x': 1}] * 1001), json.dumps([{'x': 'a' * 2001}])])
def test_invalid_records_never_start_tool(published, monkeypatch, source_json):
    published.ctx['input_sha256'] = digest(source_json.encode())
    monkeypatch.setattr(tool_loader, 'run_tool_dynamic', lambda *a, **k: pytest.fail('invalid input executed'))
    assert not invoke(published, params={'source_json': source_json})['ok']


@pytest.mark.parametrize('change', ['source', 'catalog', 'manifest', 'winner', 'change_set', 'params', 'sandbox_off'])
def test_publication_and_execution_boundaries(published, monkeypatch, change):
    if change == 'source':
        published.path.write_text('wrong source')
    elif change == 'catalog':
        monkeypatch.setattr(customization_service, 'effective_catalog_pin', lambda t: None)
    elif change == 'manifest':
        published.tool['version'] = '2'
    elif change == 'winner':
        monkeypatch.setattr(deps, 'effective_tools_with_provenance', lambda t: [(published.tool, deps.TOOL_SOURCE_AUTHORED)])
    elif change == 'change_set':
        published.change.change_set_id = 'other'
    elif change == 'sandbox_off':
        monkeypatch.setenv('LEAF_TOOL_SANDBOX_PROVIDER', 'off')
        monkeypatch.setenv('LEAF_SANDBOX', 'off')
    else:
        published.params['source'] = SOURCE
    assert not invoke(published)['ok']


@pytest.mark.parametrize('stop', ['cancelled', 'heartbeat', 'deadline', 'lease_budget'])
def test_unowned_or_unbounded_attempt_does_not_execute(published, monkeypatch, stop):
    monkeypatch.setattr(tool_loader, 'run_tool_dynamic', lambda *a, **k: pytest.fail('stopped execution'))
    options = {}
    if stop == 'cancelled': options['cancelled'] = lambda: True
    elif stop == 'heartbeat': options['heartbeat'] = lambda: False
    elif stop == 'deadline': options['deadline'] = time.monotonic() - 1
    else: monkeypatch.setenv('JOB_LEASE_S', '5')
    assert not invoke(published, **options)['ok']


def test_authority_or_publication_revoked_after_execution_fails(published, monkeypatch):
    real = tool_loader.run_tool_dynamic

    def execute(*args, **kwargs):
        env = real(*args, **kwargs)
        published.change.state = ChangeState.SUPERSEDED
        return env

    monkeypatch.setattr(tool_loader, 'run_tool_dynamic', execute)
    assert not invoke(published)['ok']


def test_context_is_closed_and_row_scoped(published):
    p = published
    row = {'tenant_id': p.ctx['tenant_id'], 'org_id': p.ctx['org_id'],
           'project_id': p.ctx['project_id'], 'tool': p.ctx['tool_name']}
    assert adapter.record_context({'completion_provenance': p.ctx}, row) == p.ctx
    for context in (None, {**p.ctx, 'shell': 'bad'}, {**p.ctx, 'recipe_version': True},
                    {**p.ctx, 'binding_id': 'bad'}, {**p.ctx, 'contract_version': 0}):
        with pytest.raises(ValueError):
            adapter.execution_context({'completion_provenance': context})
    with pytest.raises(ValueError):
        adapter.record_context({'completion_provenance': p.ctx}, {**row, 'org_id': str(uuid.uuid4())})
    with pytest.raises(ValueError):
        adapter.execution_context({'completion_provenance': p.ctx, 'capability_provenance': {}})


def test_both_record_serializers_validate_present_completion_scope(published):
    p = published
    names = ('job_id tenant_id tool params_json dwg status progress created_at started_at updated_at '
             'finished_at elapsed_ms result_json error_json attempt lease_owner lease_expires_at '
             'heartbeat_at provenance_json org_id project_id authority_mode idempotency_key dwg_version execution_json')
    columns = dict.fromkeys(names.split())
    columns.update(job_id=p.jid, tenant_id=p.ctx['tenant_id'], org_id=p.ctx['org_id'],
                   project_id=p.ctx['project_id'], tool=p.tool['name'], params_json=json.dumps(p.params),
                   dwg='', status='submitted', attempt=0, authority_mode='legacy_sqlite',
                   idempotency_key='one', execution_json=json.dumps({'completion_provenance': p.ctx}))
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        def sqlite_row():
            return conn.execute('SELECT ' + ','.join('? AS ' + key for key in columns), list(columns.values())).fetchone()
        assert jobs._row_to_record(sqlite_row())['completion_provenance'] == p.ctx
        assert job_pg_store._record(columns)['completion_provenance'] == p.ctx
        for changed in (None, {**p.ctx, 'project_id': str(uuid.uuid4())}, {**p.ctx, 'extra': 'bad'}):
            columns['execution_json'] = json.dumps({'completion_provenance': changed})
            with pytest.raises(ValueError): jobs._row_to_record(sqlite_row())
            with pytest.raises(ValueError): job_pg_store._record(columns)
    finally:
        conn.close()


def test_submit_persists_idempotent_context_before_enqueue(published, monkeypatch):
    rows, queued = [], []

    class Store:
        def submit(self, row):
            if rows:
                if rows[0]['submission_fingerprint'] != row['submission_fingerprint']:
                    raise ValueError('idempotency collision')
                return rows[0]['job_id'], False
            rows.append(deepcopy(row))
            return row['job_id'], True

    class Executor:
        def submit(self, *a, **k):
            assert json.loads(rows[0]['execution'])['completion_provenance'] == published.ctx
            queued.append((a, k))

    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, 'ensure_started', lambda: None)
    monkeypatch.setattr(jobs, '_pg_store', Store())
    monkeypatch.setattr(jobs, '_executors', {jobs.LANE_FAST: Executor()})
    monkeypatch.setattr(jobs.platform_link, 'on_submit', lambda *a, **k: None)
    kwargs = {'org_id': published.ctx['org_id'], 'project_id': published.ctx['project_id'],
              'idempotency_key': 'transform-1', 'completion_provenance': published.ctx}
    args = (published.ctx['tenant_id'], published.tool, published.params, '', False)
    first = jobs.submit_job(*args, **kwargs)
    assert jobs.submit_job(*args, **kwargs) == first and len(queued) == 1
    with pytest.raises(ValueError, match='collision'):
        jobs.submit_job(*args, **{**kwargs, 'completion_provenance': {**published.ctx, 'catalog_commit': 'c' * 40}})
    for extra in ({'capability_provenance': {}}, {'platform_context': {}}, {'org_id': None}, {'checkout_holder': 'x'}):
        with pytest.raises(ValueError):
            jobs.submit_job(*args, **{**kwargs, **extra})
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'legacy')
    with pytest.raises(ValueError): jobs.submit_job(*args, **kwargs)


def test_terminal_uses_durable_input_and_rejects_external_callback(published, monkeypatch):
    p = published
    actual = {'csv': static.expected_output(p.params['source_json'].encode()).decode()}
    proof = {'attempt': 1, 'execution_path': 'local', 'completion_provenance': p.ctx,
             **adapter.validate_result(p.ctx, p.params, actual)}
    execution = {'tool': p.tool, 'aps_live': False, 'completion_provenance': p.ctx}
    durable = {'attempt': 1, 'execution': execution, 'params': p.params}
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, '_pg_store', SimpleNamespace(durable_context=lambda j: durable,
        complete=lambda *a, **k: 'not_owner'))
    env = {'ok': True, 'result': actual}
    with pytest.raises(ValueError, match='owning job adapter'):
        jobs.complete_callback(p.jid, 'complete', result_env=env, worker_id='external', provenance=proof)
    assert jobs.complete_callback(p.jid, 'complete', result_env=env, worker_id='stale',
                                  provenance=proof, _completion_owner=True) == 'not_owner'
    for edit in ({'output_sha256': 'f' * 64}, {'attempt': 2}):
        with pytest.raises(ValueError):
            jobs._validate_terminal_context('complete', env, {**proof, **edit}, 1, execution,
                                             durable_params=p.params)
    with pytest.raises(ValueError):
        jobs._validate_terminal_context('complete', {'ok': True, 'result': {'csv': 'fake'}}, proof, 1,
                                         execution, durable_params=p.params)


def test_recovery_retains_durable_context_and_uses_same_runner(published, monkeypatch):
    p = published
    execution = {'tool': p.tool, 'aps_live': False, 'dwg_version': None, 'completion_provenance': p.ctx}
    durable = {'execution': execution, 'params': p.params, 'tenant_id': p.ctx['tenant_id'],
               'org_id': p.ctx['org_id'], 'project_id': p.ctx['project_id'], 'tool': p.tool['name']}
    queued = []
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, '_pg_store', SimpleNamespace(execution=lambda j: deepcopy(execution),
                                                         durable_context=lambda j: durable))
    monkeypatch.setattr(jobs, '_executors', {jobs.LANE_FAST: SimpleNamespace(submit=lambda *a, **k: queued.append(a))})
    record = {'job_id': p.jid, 'tenant_id': p.ctx['tenant_id'], 'tool': p.tool['name'],
              'params': p.params, 'dwg': '', 'status': 'submitted', 'attempt': 0}
    monkeypatch.setattr(jobs, 'get_job', lambda j: record)
    assert jobs._redispatch_record(p.jid)
    assert queued[0][0] == jobs._run_job
    assert jobs.completion_context(p.jid) == p.ctx


@pytest.mark.parametrize('stop', ['lease', 'closed'])
def test_runner_does_not_settle_after_losing_ownership(published, monkeypatch, stop):
    p = published
    rec = {'status': 'running', 'progress': 'queued', 'attempt': 1,
           'lease': {'owner': None, 'expires_at': time.time() + 60}}
    def claim(j, owner):
        rec['lease']['owner'] = owner
        return 1
    def run(*a):
        if stop == 'lease': rec['lease']['owner'] = 'replacement'
        else: rec['progress'] = jobs.CLOSED_PROGRESS
        return {'ok': True, 'result': {'csv': 'not accepted'}}
    monkeypatch.setattr(jobs, 'claim_lease', claim)
    monkeypatch.setattr(jobs, 'completion_context', lambda j: p.ctx)
    monkeypatch.setattr(jobs.platform_link, 'on_running', lambda j: None)
    monkeypatch.setattr(jobs, 'get_job', lambda j: rec)
    monkeypatch.setattr(adapter, 'run', run)
    monkeypatch.setattr(jobs, 'complete_callback', lambda *a, **k: pytest.fail('stale completion'))
    jobs._run_job(p.jid, p.ctx['tenant_id'], p.tool, p.params, '', False)


def test_authority_requires_active_binding_campaign_and_current_release(published, monkeypatch):
    from leaf_platform import campaigns, campaign_release
    # Fixture replaced the public seam; execute the real function via its saved module source.
    import importlib.util
    spec = importlib.util.spec_from_file_location('transform_authority_check', adapter.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p = published
    @contextmanager
    def cursor(): yield object()
    monkeypatch.setattr(campaigns, '_cursor', cursor)
    monkeypatch.setattr(campaigns, '_principal', lambda *a: None)
    monkeypatch.setattr(campaigns, 'get_campaign', lambda *a: {'tenant_id': p.ctx['tenant_id']})
    release = {k: p.ctx[k] for k in ('org_id', 'project_id', 'campaign_id', 'release_id', 'contract_version')}
    release['status'] = 'active'
    monkeypatch.setattr(campaign_release, 'get_release', lambda *a: {'release': release})
    module.check_authority(p.ctx)
    release['contract_version'] = 2
    with pytest.raises(ValueError): module.check_authority(p.ctx)
    release['contract_version'] = 1
    def denied(*a): raise ValueError('revoked principal')
    monkeypatch.setattr(campaigns, '_principal', denied)
    with pytest.raises(ValueError, match='revoked'): module.check_authority(p.ctx)
