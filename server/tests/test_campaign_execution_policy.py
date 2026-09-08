"""Completion reads broker authority without acquiring broker process behavior."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import broker_pg_store
import campaign_execution_policy as policy


@pytest.mark.parametrize('runtime,production', [
    (None, False), ('', False), ('test', False), ('staging', False),
    ('production', True), (' Production ', True), ('prod', False),
])
@pytest.mark.parametrize('flag,enabled', [
    (None, None), ('1', True), (' TRUE ', True), ('yes', True), ('On', True),
    ('0', False), ('false', False), ('', False), ('unknown', False),
])
def test_authored_environment_semantics(monkeypatch, runtime, production, flag, enabled):
    for key, value in [('LEAF_RUNTIME_ENV', runtime), ('LEAF_AUTHORED_EXECUTION', flag)]:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert policy.production_runtime() is production
    assert policy.authored_execution_enabled() is (not production if enabled is None else enabled)


@pytest.mark.parametrize('provider,configured', [
    (None, False), ('', False), ('off', False), ('subprocess', False),
    ('e2b-microvm', False), ('e2b', True), (' E2B ', True),
])
def test_sandbox_uses_only_existing_provider_signal(monkeypatch, provider, configured):
    monkeypatch.setenv('LEAF_SANDBOX', 'e2b')
    if provider is None:
        monkeypatch.delenv('LEAF_TOOL_SANDBOX_PROVIDER', raising=False)
    else:
        monkeypatch.setenv('LEAF_TOOL_SANDBOX_PROVIDER', provider)
    assert policy.sandbox_configured() is configured


@pytest.mark.parametrize('record,disabled', [
    (None, False), ({'disabled': False}, False), ({'disabled': True}, True),
    ({}, True), ([], True), ('bad', True), ({'disabled': None}, True),
    ({'disabled': 0}, True), ({'disabled': 1}, True), ({'disabled': ''}, True),
    ({'disabled': 'false'}, True), ({'disabled': []}, True),
])
def test_postgres_tenant_state(monkeypatch, record, disabled):
    monkeypatch.setenv('LEAF_BROKER_STORE', ' Postgres ')
    calls = []

    def tenant(tid):
        calls.append(tid)
        return record

    monkeypatch.setattr(broker_pg_store, 'get_store', lambda: SimpleNamespace(tenant=tenant))
    assert policy.tenant_disabled('tenant-under-check') is disabled
    assert calls == ['tenant-under-check']


@pytest.mark.parametrize('mode', [None, '', 'legacy', 'sqlite', 'unknown'])
def test_unsupported_authority_never_reads_a_store(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv('LEAF_BROKER_STORE', raising=False)
    else:
        monkeypatch.setenv('LEAF_BROKER_STORE', mode)
    monkeypatch.setattr(broker_pg_store, 'get_store', lambda: pytest.fail('unsupported store read'))
    with pytest.raises(RuntimeError, match='PostgreSQL broker authority'):
        policy.tenant_disabled('tenant-under-check')


@pytest.mark.parametrize('failure', ['store', 'tenant'])
def test_unavailable_authority_propagates_without_enabling(monkeypatch, failure):
    monkeypatch.setenv('LEAF_BROKER_STORE', 'postgres')
    error = RuntimeError('authority unavailable')

    def unavailable(*args):
        raise error

    monkeypatch.setattr(broker_pg_store, 'get_store', unavailable if failure == 'store'
                        else lambda: SimpleNamespace(tenant=unavailable))
    with pytest.raises(RuntimeError) as caught:
        policy.tenant_disabled('tenant-under-check')
    assert caught.value is error


def test_real_completion_checks_leave_app_transport_and_broker_guard_separate(tmp_path):
    # A fresh interpreter avoids both pytest's module cache and earlier tests
    # that intentionally import the broker. Only stores/producers are fake;
    # both completion authority functions below execute their real code.
    script = r'''
import platform
import socket
import sys
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])

def no_network(*args, **kwargs):
    raise AssertionError('real network attempted')
socket.socket.connect = no_network
socket.create_connection = no_network

import requests
from requests.adapters import HTTPAdapter
sent = []
def transport(self, request, *args, **kwargs):
    sent.append(request.url)
    response = requests.Response()
    response.status_code = 200
    response._content = b'{}'
    response.request = request
    response.url = request.url
    return response
HTTPAdapter.send = transport

import campaign_execution_policy
import campaign_transform_job as transform
import campaign_acquisition_service as acquisition
import broker_pg_store
import job_pg_store
import agent_policy
import entitlements
import deps
job_pg_store._db()  # Register the package, without connecting to a database.
from leaf_platform import campaigns, campaign_release
import campaign_release_service as runtime

tenant_id = str(uuid.uuid4())
tool = {'name': transform.CONSTANTS['tool_name'], 'kind': 'script',
        'capabilities': ['drawing.read']}
context = {**transform.CONSTANTS,
           **{key: str(uuid.uuid4()) for key in transform.IDS},
           'tenant_id': tenant_id, 'contract_version': 1,
           'tool_manifest_sha256': deps.catalog_tool_digest(tool)}
release = {key: context[key] for key in
           ('org_id', 'project_id', 'campaign_id', 'release_id', 'contract_version')}
release['status'] = 'active'
reads = []
def tenant(tid):
    assert tid == tenant_id
    reads.append(tid)
    return {'disabled': False}
broker_pg_store.get_store = lambda: SimpleNamespace(tenant=tenant)
entitlements.resolve_tier = lambda actor: 'hosted_pro'
entitlements.resolve_roles = lambda actor: ((), False)
agent_policy.load_tenant_state = lambda tid: {'agent_disabled': False, 'overlay': {}}
agent_policy.load_policy = lambda: None
agent_policy.effective_action = lambda *a, **kw: SimpleNamespace(enabled=True, policy='auto')
deps.effective_tools_with_provenance = lambda tid: [(tool, deps.TOOL_SOURCE_TENANT_REPO)]
@contextmanager
def cursor():
    yield object()
campaigns._cursor = cursor
principals = []
campaigns._principal = lambda *args: principals.append(args)
campaigns.get_campaign = lambda *args: {'tenant_id': tenant_id}
campaign_release.get_release = lambda *args: {'release': release}
runtime.actor_for_release = lambda row: tenant_id

acquisition._run_authority(tenant_id, tool)
transform.check_authority(context)
assert reads == [tenant_id] * 3
assert len(principals) == 1
assert 'broker' not in sys.modules
assert HTTPAdapter.send is transport
with requests.Session() as session:
    session.trust_env = False
    request = session.prepare_request(requests.Request('POST', 'http://broker.internal/broker/run', json={}))
    assert session.send(request).status_code == 200
assert sent == ['http://broker.internal/broker/run']

# Importing the actual broker in its own process must still install its guard.
import broker
assert HTTPAdapter.send is not transport
with requests.Session() as session:
    session.trust_env = False
    request = session.prepare_request(requests.Request('GET', 'https://unrelated.invalid/'))
    try:
        session.send(request)
    except broker.EgressBlocked:
        pass
    else:
        raise AssertionError('broker allowed unrelated egress')
    assert sent == ['http://broker.internal/broker/run']
    allowed = session.prepare_request(requests.Request('GET', 'http://localhost/'))
    assert session.send(allowed).status_code == 200
assert sent == ['http://broker.internal/broker/run', 'http://localhost/']
print('completion policy transport boundary preserved')
'''
    env = dict(os.environ)
    env.update(LEAF_BROKER_STORE='postgres', LEAF_RUNTIME_ENV='production',
               LEAF_AUTHORED_EXECUTION='1', LEAF_TOOL_SANDBOX_PROVIDER='e2b',
               BROKER_URL='http://broker.internal', LEAF_BROKER_SECRET='test-only-secret',
               BROKER_EGRESS_EXTRA='', BROKER_TENANTS=str(tmp_path / 'absent-tenants.json'),
               BROKER_LEDGER=str(tmp_path / 'ledger.jsonl'),
               SESSIONS_DB=str(tmp_path / 'sessions.db'), JOBS_DB=str(tmp_path / 'jobs.db'),
               LEAF_GUEST_PURGE_DISABLED='1', LEAF_CUSTOMIZATION_STAGE_WORKER_DISABLED='1')
    # Ignore Python environment overrides, but retain user-site dependencies
    # used by the parent interpreter. A fresh process isolates the module cache.
    result = subprocess.run([sys.executable, '-E', '-B', '-c', script,
                             str(Path(__file__).resolve().parents[1])],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'completion policy transport boundary preserved' in result.stdout
