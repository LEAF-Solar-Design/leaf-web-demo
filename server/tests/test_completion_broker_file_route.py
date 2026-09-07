"""Completion file execution through the existing authenticated broker rail."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import broker
import broker_client
import tool_loader


SOURCE = 'def run(intake, params): return {"csv": "pinned"}\n'
TOOL = {'name': 'campaign-records-to-csv', 'version': '1', 'entry': 'transform.py',
        'params': {'type': 'object', 'additionalProperties': False}}


@pytest.fixture
def rail(monkeypatch):
    monkeypatch.setenv('LEAF_BROKER_SECRET', 'test-only-broker-secret')
    monkeypatch.setenv('BROKER_URL', 'http://broker.test')
    monkeypatch.setenv('LEAF_SANDBOX', 'e2b')
    monkeypatch.delenv('LEAF_TOOL_SANDBOX_PROVIDER', raising=False)
    monkeypatch.setattr(broker, '_deployed_runtime', lambda: False)
    monkeypatch.setattr(broker, '_broker_store_mode', lambda: 'legacy')
    monkeypatch.setattr(broker, 'tenant_disabled', lambda tenant: False)
    monkeypatch.setattr(broker, '_cap_preflight', lambda *a: None)
    monkeypatch.setattr(broker, '_tenant_tier', lambda tenant: 'demo')
    monkeypatch.setattr(broker.entitlements, 'entitlements_for', lambda tier: {'read': True})
    monkeypatch.setattr(broker.entitlements, 'tool_required_capability', lambda tool: 'read')
    monkeypatch.setattr(broker, '_ledger_append', lambda *a: None)
    monkeypatch.setattr(broker, '_emit_aps_metric', lambda *a: None)
    monkeypatch.setattr(broker.write_loop, 'backend_for_tenant',
                        lambda *a, **k: pytest.fail('drawing store accessed'))
    monkeypatch.setattr(broker.write_loop, 'ensure_demo_drawing',
                        lambda *a, **k: pytest.fail('demo accessed'))
    monkeypatch.setattr(broker, 'DATA_FILE', SimpleNamespace(
        read_text=lambda *a, **k: pytest.fail('cached drawing accessed')))
    return TestClient(broker.app)


def request(**changes):
    return {'tenant_id': 'tenant-file', 'tool': dict(TOOL), 'params': {}, 'dwg': '',
            'aps_live': False, 'file_only': True, 'test_source': SOURCE,
            'ledger_event_key': 'completion:immutable-job', **changes}


def test_real_client_transport_and_sandbox_source(rail, monkeypatch):
    calls, executions = [], []

    def sandbox(source, filename, intake, params):
        executions.append((source, filename, intake, params))
        return 'ok', {'csv': 'pinned'}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return rail.post('/broker/run', json=kwargs['json'], headers=kwargs['headers'])

    monkeypatch.setattr(tool_loader, '_run_source_in_sandbox', sandbox)
    monkeypatch.setattr(broker_client.requests, 'post', post)
    for _ in range(2):
        env = broker_client.run_via_broker(
            'tenant-file', TOOL, {}, '', False, timeout_s=5,
            ledger_event_key='completion:immutable-job', job_id='immutable-job',
            file_only=True, test_source=SOURCE)
        assert env['ok'] is True and env['result'] == {'csv': 'pinned'}
    assert executions == [(SOURCE, 'transform.py', {}, {})] * 2
    assert calls[0] == calls[1]
    assert calls[0][0] == 'http://broker.test/broker/run'
    assert calls[0][1]['timeout'] == 5
    assert calls[0][1]['headers'] == {'X-Broker-Secret': 'test-only-broker-secret'}
    assert calls[0][1]['json']['dwg_version'] is None
    assert rail.post('/broker/run', json=request()).status_code in (401, 403)


@pytest.mark.parametrize('change', [
    {'aps_live': True}, {'dwg': 'tenant-drawing'}, {'dwg_version': 1},
    {'test_source': None}, {'test_source': '  '},
    {'tool': {**TOOL, 'capabilities': ['drawing.write']}},
])
def test_invalid_file_requests_never_execute(rail, monkeypatch, change):
    monkeypatch.setattr(broker, 'run_tool_dynamic', lambda *a, **k: pytest.fail('executed'))
    response = rail.post('/broker/run', json=request(**change), headers=broker_client.broker_headers())
    assert response.status_code >= 400
    assert response.json()['ok'] is False


def test_off_sandbox_never_executes(rail, monkeypatch):
    monkeypatch.setenv('LEAF_SANDBOX', 'off')
    monkeypatch.setenv('LEAF_TOOL_SANDBOX_PROVIDER', 'off')
    monkeypatch.setattr(tool_loader, '_load_module', lambda *a: pytest.fail('in-process execution'))
    response = rail.post('/broker/run', json=request(), headers=broker_client.broker_headers())
    assert response.status_code >= 400 and response.json()['ok'] is False


@pytest.mark.parametrize('gate', ['tenant', 'entitlement', 'authored', 'production', 'params'])
def test_file_branch_preserves_broker_gates(rail, monkeypatch, gate):
    monkeypatch.setattr(tool_loader, '_run_source_in_sandbox',
                        lambda *a, **k: pytest.fail('sandbox executed'))
    body = request()
    if gate == 'tenant':
        monkeypatch.setattr(broker, 'tenant_disabled', lambda tenant: True)
    elif gate == 'entitlement':
        monkeypatch.setattr(broker.entitlements, 'entitlements_for', lambda tier: {'read': False})
    elif gate == 'params':
        body['params'] = {'unexpected': True}
    else:
        monkeypatch.setattr(broker, '_deployed_runtime', lambda: True)
        monkeypatch.setattr(broker, 'is_trusted_builtin_tool', lambda *a: False)
        monkeypatch.setattr(broker, '_authored_execution_enabled', lambda: gate != 'authored')
        monkeypatch.setattr(broker, '_production_runtime', lambda: gate == 'production')
        monkeypatch.setattr(broker, '_sandbox_configured', lambda: False)
    response = rail.post('/broker/run', json=body, headers=broker_client.broker_headers())
    assert response.status_code >= 400 and response.json()['ok'] is False


def test_file_branch_marks_admission_before_sandbox(rail, monkeypatch):
    admission = {'lease_token': 'test-lease'}
    calls = []

    def start(req, current, **kwargs):
        assert current is admission and kwargs == {'aps_submission': False}
        calls.append('admitted')

    def sandbox(*args):
        assert calls == ['admitted']
        calls.append('sandbox')
        return 'ok', {'csv': 'pinned'}

    monkeypatch.setattr(broker, '_start_admitted_execution', start)
    monkeypatch.setattr(tool_loader, '_run_source_in_sandbox', sandbox)
    req = broker.BrokerRunRequest(**request())
    env, status = broker._execute(req, req.tool, '', 0, {}, admission=admission)
    assert status == 200 and env['ok'] is True
    assert calls == ['admitted', 'sandbox']


@pytest.mark.parametrize('failure', ['timeout', 'connection', 'http', 'json', 'preparation'])
def test_file_client_failure_has_no_retry(monkeypatch, failure):
    calls = []

    def post(*args, **kwargs):
        calls.append(True)
        if failure == 'timeout':
            raise broker_client.requests.Timeout('provider detail')
        if failure == 'connection':
            raise broker_client.requests.ConnectionError('provider detail')
        if failure == 'preparation':
            raise ValueError('private request JSON')
        def body():
            if failure == 'json':
                raise ValueError('invalid JSON')
            return {'ok': True}
        return SimpleNamespace(status_code=500 if failure == 'http' else 200, json=body)

    monkeypatch.setattr(broker_client.requests, 'post', post)
    with pytest.raises(broker_client.BrokerUnreachable) as caught:
        broker_client.run_via_broker('tenant', TOOL, {}, '', False,
                                     timeout_s=5, file_only=True, test_source=SOURCE)
    assert calls == [True]
    label = ('file-only broker request failed' if failure == 'http' else
             'file-only broker response invalid' if failure in ('json', 'preparation') else
             'file-only broker request unavailable')
    assert str(caught.value) == label
    assert getattr(caught.value, 'status_code', None) == (500 if failure == 'http' else None)
    assert caught.value.reason == {'timeout': 'timeout', 'connection': 'connect',
                                   'json': 'nonjson', 'preparation': 'nonjson'}.get(failure)
    if failure != 'http':
        assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize('status', [None, True, False, '403', 403.0, 99, 600])
def test_file_client_invalid_status_is_safe(monkeypatch, status):
    monkeypatch.setattr(broker_client.requests, 'post', lambda *a, **k: SimpleNamespace(
        status_code=status, json=lambda: pytest.fail('invalid response parsed')))
    with pytest.raises(broker_client.BrokerUnreachable) as caught:
        broker_client.run_via_broker('tenant', TOOL, {}, '', False, file_only=True)
    assert str(caught.value) == 'file-only broker request failed'
    assert getattr(caught.value, 'status_code', None) is None


@pytest.mark.parametrize('status', [100, 199, 302, 399, 400, 500, 599])
def test_file_client_retains_non_success_http_status(monkeypatch, status):
    monkeypatch.setattr(broker_client.requests, 'post', lambda *a, **k: SimpleNamespace(
        status_code=status, json=lambda: pytest.fail('rejected response parsed')))
    with pytest.raises(broker_client.BrokerHTTPRejected) as caught:
        broker_client.run_via_broker('tenant', TOOL, {}, '', False, file_only=True)
    assert caught.value.status_code == status
    assert str(caught.value) == 'file-only broker request failed'


@pytest.mark.parametrize('failure', ['timeout', 'connection', 'json'])
def test_ordinary_client_failure_message_unchanged(monkeypatch, failure):
    monkeypatch.setenv('BROKER_URL', 'http://broker.test')
    error = {'timeout': broker_client.requests.Timeout,
             'connection': broker_client.requests.ConnectionError, 'json': ValueError}[failure]('legacy detail')

    def post(*args, **kwargs):
        raise error

    monkeypatch.setattr(broker_client.requests, 'post', post)
    with pytest.raises(broker_client.BrokerUnreachable) as caught:
        broker_client.run_via_broker('tenant', TOOL, {}, '', False)
    label = 'returned non-JSON' if failure == 'json' else 'unreachable'
    assert str(caught.value) == 'broker at http://broker.test ' + label + ': legacy detail'
    assert caught.value.reason is None
    assert caught.value.__cause__ is error


def test_fingerprint_preserves_ordinary_identity_and_hashes_source():
    ordinary = broker.BrokerRunRequest(tenant_id='tenant-file', tool=TOOL, params={}, dwg='')
    canonical = json.dumps({'tenant_id': ordinary.tenant_id, 'tool': TOOL, 'params': {},
                            'dwg': '', 'aps_live': False, 'dwg_version': None},
                           sort_keys=True, separators=(',', ':'), default=str)
    assert broker._broker_request_fingerprint(ordinary) == hashlib.sha256(canonical.encode()).hexdigest()
    staged = broker.BrokerRunRequest(**request(file_only=False))
    file = broker.BrokerRunRequest(**request())
    assert broker._broker_request_fingerprint(staged) != broker._broker_request_fingerprint(file)
    assert broker._broker_request_fingerprint(file) == broker._broker_request_fingerprint(
        broker.BrokerRunRequest(**request()))
    assert broker._broker_request_fingerprint(file) != broker._broker_request_fingerprint(
        broker.BrokerRunRequest(**request(test_source=SOURCE + '\n')))


def test_ordinary_client_payload_unchanged(monkeypatch):
    payloads = []

    def post(url, **kwargs):
        payloads.append(kwargs['json'])
        return type('Response', (), {'json': lambda self: {'ok': True}})()

    monkeypatch.setattr(broker_client.requests, 'post', post)
    broker_client.run_via_broker('tenant', TOOL, {}, 'rooftop_demo', False)
    assert payloads == [{'tenant_id': 'tenant', 'tool': TOOL, 'params': {},
                         'dwg': 'rooftop_demo', 'aps_live': False, 'dwg_version': None,
                         'ledger_event_key': None, 'checkout_holder': None,
                         'checkout_fence': None, 'job_id': None}]
