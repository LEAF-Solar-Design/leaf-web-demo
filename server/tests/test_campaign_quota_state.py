"""The tenant in-flight cap reaches campaign clients as a named, retryable quota refusal.

Both campaign submit paths (capability invoke, transform acquisition) used to fold
jobs.TenantInflightCapExceeded into invocation_unknown / 'working'. They now raise
campaign_capability_api.QuotaExceeded, which the campaigns router answers with the
POST /api/run envelope: HTTP 429 quota_exceeded, quota_kind tenant_inflight, limit, used.
Every other submit failure keeps its existing response.
"""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

import campaign_acquisition_service as service
import campaign_capability_api as capability
import jobs
import tool_validate
from routers import campaigns
from test_campaign_acquisition_service import advance, setup  # noqa: F401 (fixture)

TENANT, ORG, PROJECT, CAMPAIGN, ENROLLMENT, PRINCIPAL = (
    'tenant-1', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222',
    '33333333-3333-3333-3333-333333333333', '44444444-4444-4444-4444-444444444444', 'binding-1')
DIGEST = 'a' * 64
CONTEXT = {'tenant_id': TENANT, 'org_id': ORG, 'project_id': PROJECT, 'tool_name': 'campaign-host-enrollment',
           'change_set_id': 'change-1', 'effective_catalog_digest': DIGEST}
PUBLICATION = {'change_set_id': 'change-1', 'effective_catalog_digest': DIGEST}


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)

    def fetchone(self):
        return {'acquired': True} if 'pg_try_advisory_lock' in self.conn.sql[-1] else {'released': True}


class _Conn:
    def __init__(self):
        self.sql, self.closed = [], False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def execute(self, sql):
        self.sql.append(sql)

    def close(self):
        self.closed = True


class _Pool:
    """The production admission lock runs unchanged over this in-memory pool."""
    def __init__(self):
        self.conn, self.returned = _Conn(), []

    def getconn(self, timeout=None):
        return self.conn

    def putconn(self, conn):
        self.returned.append(conn)


@pytest.fixture
def invoke_env(monkeypatch):
    pool = _Pool()
    caps = SimpleNamespace(PUBLICATION=tuple(PUBLICATION), invocation_context=lambda *args: dict(CONTEXT),
                           _context=lambda context: None)
    monkeypatch.setattr(capability, '_platform', lambda: (None, caps, None, SimpleNamespace(get_pool=lambda: pool)))
    monkeypatch.setattr(capability, '_scope', lambda *args: (ORG, PRINCIPAL, TENANT))
    monkeypatch.setattr(capability, '_stored_context', lambda *args: dict(CONTEXT))
    monkeypatch.setattr(capability, '_publication', lambda tenant_id: (dict(PUBLICATION), {'name': 'campaign-host-enrollment'}))
    monkeypatch.setattr(tool_validate, 'validate_params', lambda *args, **kwargs: [])
    state = SimpleNamespace(pool=pool, lookups=0, rows=[], recovered=[], submits=0)

    def lookup(tenant, project, key):
        state.lookups += 1
        return state.rows.pop(0) if state.rows else None

    def recover(row, context, expected):
        state.recovered.append(row)
        return {'job_id': row['job_id'], 'status': 'submitted', 'progress': 'Queued'}

    monkeypatch.setattr(capability, '_lookup', lookup)
    monkeypatch.setattr(capability, '_recover', recover)

    def submits(error):
        def submit(**kwargs):
            state.submits += 1
            raise error
        monkeypatch.setattr(jobs, 'submit_job', submit)
    state.submits_raise = submits
    return state


def _invoke():
    return capability.invoke('caller', PROJECT, CAMPAIGN, ENROLLMENT, DIGEST, 'client-key-1')


def test_capability_invoke_maps_tenant_cap_to_quota_refusal(invoke_env):
    invoke_env.submits_raise(jobs.TenantInflightCapExceeded(TENANT, 32, 32))
    with pytest.raises(capability.QuotaExceeded) as caught:
        _invoke()
    assert (caught.value.status, caught.value.code) == (429, 'quota_exceeded')
    assert (caught.value.limit, caught.value.used) == (32, 32)
    # The refusal is definite, so no post-submit durable lookup or recovery ran.
    assert invoke_env.submits == 1 and invoke_env.lookups == 1 and invoke_env.recovered == []
    assert invoke_env.pool.returned == [invoke_env.pool.conn] and not invoke_env.pool.conn.closed


def test_capability_invoke_other_failure_without_durable_row_stays_invocation_unknown(invoke_env):
    invoke_env.submits_raise(TimeoutError('response lost'))
    with pytest.raises(capability.CapabilityError) as caught:
        _invoke()
    assert not isinstance(caught.value, capability.QuotaExceeded)
    assert (caught.value.status, caught.value.code) == (503, 'invocation_unknown')
    assert invoke_env.lookups == 2 and invoke_env.recovered == []


def test_capability_invoke_other_failure_with_durable_row_recovers_it(invoke_env):
    invoke_env.submits_raise(TimeoutError('response lost after insert'))
    durable = {'job_id': '55555555-5555-5555-5555-555555555555'}
    invoke_env.rows.extend([None, durable])
    assert _invoke()['job_id'] == durable['job_id']
    assert invoke_env.recovered == [durable]


def test_admission_lock_passes_quota_refusal_through_and_still_collapses_other_errors(invoke_env):
    with pytest.raises(capability.QuotaExceeded):
        with capability._admission_lock(TENANT, ORG, PROJECT, 'key'):
            raise capability.QuotaExceeded(8, 9)
    assert any('pg_advisory_unlock' in sql for sql in invoke_env.pool.conn.sql)
    with pytest.raises(capability.CapabilityError) as caught:
        with capability._admission_lock(TENANT, ORG, PROJECT, 'key'):
            raise RuntimeError('unrelated')
    assert (caught.value.status, caught.value.code) == (503, 'invocation_unknown')


def test_acquisition_maps_tenant_cap_to_quota_refusal(setup, monkeypatch):  # noqa: F811
    def refuse(**kwargs):
        setup.calls['submit'] += 1
        raise jobs.TenantInflightCapExceeded(kwargs['tenant_id'], 4, 5)
    monkeypatch.setattr(jobs, 'submit_job', refuse)
    with pytest.raises(capability.QuotaExceeded) as caught:
        advance(setup)
    assert (caught.value.status, caught.value.code, caught.value.limit, caught.value.used) == (429, 'quota_exceeded', 4, 5)
    assert not isinstance(caught.value, service.AcquisitionError)
    assert setup.calls['submit'] == 1 and setup.state['row'] is None
    assert not any(d['decision_key'].startswith('acquisition-v1-invocation') for d in setup.store.decisions)


def test_acquisition_other_failure_keeps_uncertain_working_state(setup, monkeypatch):  # noqa: F811
    def uncertain(**kwargs):
        setup.calls['submit'] += 1
        raise TimeoutError('before durable submission')
    monkeypatch.setattr(jobs, 'submit_job', uncertain)
    result = advance(setup)
    assert result['state'] == 'working'
    assert result['reason'] == 'Transform submission is uncertain'
    assert result['recommended_action'] == 'Retry this release to read the same invocation'


def _body(response):
    return response.status_code, json.loads(response.body)


def _quota_envelope(limit, used):
    return {'ok': False, 'error': {
        'error_code': 'quota_exceeded', 'retryable': True,
        'message': f'The workspace already has {used} runs queued or running; retry when one finishes.'},
        'quota_kind': 'tenant_inflight', 'limit': limit, 'used': used}


def test_router_capability_call_returns_quota_429_envelope():
    def refused(*args):
        raise capability.QuotaExceeded(32, 32)
    assert _body(campaigns._capability_call('invocation', refused)) == (429, _quota_envelope(32, 32))


def test_router_release_call_returns_quota_429_envelope(monkeypatch):
    monkeypatch.setattr(campaigns, '_STORE', SimpleNamespace(
        CampaignConflict=type('CampaignConflict', (Exception,), {}),
        CampaignUnavailable=type('CampaignUnavailable', (Exception,), {})))

    def refused(*args, **kwargs):
        raise capability.QuotaExceeded(8, 8)
    assert _body(campaigns._release_call('completion', refused)) == (429, _quota_envelope(8, 8))

    def invalid(*args, **kwargs):
        raise ValueError('bad')
    assert _body(campaigns._release_call('completion', invalid))[0] == 400


def test_router_other_capability_errors_keep_their_response():
    def unknown(*args):
        raise capability.CapabilityError(503, 'invocation_unknown')
    assert _body(campaigns._capability_call('invocation', unknown)) == (503, {'ok': False, 'error': {
        'error_code': 'invocation_unknown', 'message': 'Campaign capability request failed', 'retryable': True}})

    def unexpected(*args):
        raise RuntimeError('boom')
    assert _body(campaigns._capability_call('invocation', unexpected))[1]['error']['error_code'] == 'capability_unavailable'
