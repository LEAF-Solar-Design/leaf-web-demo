import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
import campaign_release_service as service
from routers import campaigns, campaign_mcp


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(campaigns.router)
    app.include_router(campaign_mcp.router)
    app.dependency_overrides[deps.require_tenant] = lambda: 'tenant'
    return TestClient(app)


def test_finish_rejects_executable_fields_before_admission(client):
    result = client.post('/api/campaigns', headers={'Idempotency-Key': 'finish'}, json={
        'project_id': str(uuid.uuid4()), 'title': 'Finish', 'prompt': 'Deliver file', 'mode': 'finish',
        'finish': {'delivery_profile': 'cad_file', 'intended_user': 'owner',
                   'workflow': 'download', 'artifact_refs': [], 'command': 'execute'}})
    assert result.status_code == 400


@pytest.mark.parametrize('field', ['contract', 'grant', 'command', 'required_checks', 'evidence',
                                  'status', 'selected_artifact', 'transform_recipe'])
def test_revision_rejects_privileged_fields(client, monkeypatch, field):
    monkeypatch.setattr(service, 'revise', lambda *a: pytest.fail('must reject before service'))
    ids = {key: str(uuid.uuid4()) for key in ('project_id', 'campaign_id', 'release_id')}
    body = dict(project_id=ids['project_id'], workflow='Use the published tool', reason='Reuse publication')
    path = f"/api/campaigns/{ids['campaign_id']}/releases/{ids['release_id']}/revise"
    assert client.post(path, json={**body, field: {}}, headers={'Idempotency-Key': 'revision'}).status_code == 400
    rpc = client.post('/api/mcp/campaigns', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'campaign.release.revise', 'arguments': {**ids, **body,
                   'idempotency_key': 'revision', field: {}}}})
    assert rpc.json()['error']['code'] == -32602


def test_revision_preserves_contract_and_replays_without_dispatch(monkeypatch):
    contract = {'workflow': 'Old workflow', 'original_goal': 'Whole ambition',
        'required_checks': [{'check_id': 'delivery.verified'}], 'release_boundary': 'CSV',
        'transform_recipe': {'source_artifact': {'sha256': 'a' * 64}},
        'selected_artifact': {'sha256': 'b' * 64}, 'deferred_items': ['Later scope']}
    original = deepcopy(contract)
    completion = {'release': {'contract': contract, 'status': 'needs_approach', 'contract_version': 1},
                  'stages': [{'stage': 'publication', 'status': 'passed'}]}
    requests = []
    def revise(*args, **kwargs):
        requests.append(deepcopy(kwargs))
        completion['release'].update(contract=kwargs['contract'], status='paused', contract_version=2)
    monkeypatch.setattr(service, 'authority', lambda *a: ('org', 'project', 'actor'))
    monkeypatch.setattr(service, '_STORE', SimpleNamespace(
        get_release=lambda *a: deepcopy(completion), revise_contract=revise))
    monkeypatch.setattr(service, 'advance', lambda *a: pytest.fail('revision must not dispatch'))
    for _ in range(2):
        result = service.revise('tenant', 'project', 'campaign', 'release', 'Use published tool', 'Recover', 'key')
        assert result['stages'] == completion['stages']
        assert result['release']['status'] == 'paused'
    assert requests[0] == requests[1]
    assert requests[0] == {'contract': dict(original, workflow='Use published tool'), 'reason': 'Recover', 'idempotency_key': 'key', 'pause': True}
    assert contract == original


@pytest.mark.parametrize('workflow,reason,key', [(' ', 'why', 'key'), ('x' * 16385, 'why', 'key'),
    ('new', '', 'key'), ('new', 'x' * 4097, 'key'), ('new', 'why', ''), ('new', 'why', 'x' * 129)])
def test_revision_bounds_before_store(monkeypatch, workflow, reason, key):
    monkeypatch.setattr(service, 'authority', lambda *a: pytest.fail('invalid request'))
    with pytest.raises(ValueError):
        service.revise('tenant', 'project', 'campaign', 'release', workflow, reason, key)


def test_revision_transport_authority_and_conflicts(client, monkeypatch):
    ids = {key: str(uuid.uuid4()) for key in ('project_id', 'campaign_id', 'release_id')}
    body = dict(project_id=ids['project_id'], workflow='New workflow', reason='Recover')
    path = f"/api/campaigns/{ids['campaign_id']}/releases/{ids['release_id']}/revise"
    assert client.post(path, json=body).status_code == 400
    monkeypatch.setattr(service.platform_link, 'require_project_access', lambda *a, **k:
                        (_ for _ in ()).throw(service.platform_link.ProjectSessionForbidden('foreign')))
    assert client.post(path, json=body, headers={'Idempotency-Key': 'key'}).status_code == 403
    rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'campaign.release.revise', 'arguments': {**ids, **body, 'idempotency_key': 'key'}}}
    assert client.post('/api/mcp/campaigns', json=rpc).json()['result']['isError'] is True
    monkeypatch.setattr(service, 'authority', lambda *a: ('org', 'project', 'actor'))
    def conflict(*a, **k):
        raise service.delivery.DeliveryConflict('approach_change_required')
    monkeypatch.setattr(service, '_STORE', SimpleNamespace(
        get_release=lambda *a: {'release': {'contract': {'workflow': 'New workflow'}}}, revise_contract=conflict))
    assert client.post(path, json=body, headers={'Idempotency-Key': 'key'}).status_code == 409


def test_revision_api_and_mcp_forward_bounded_unicode_fields(client, monkeypatch):
    calls = []
    monkeypatch.setattr(service, 'revise', lambda *args: calls.append(args) or {'release': None})
    ids = {key: str(uuid.uuid4()) for key in ('project_id', 'campaign_id', 'release_id')}
    body = {'project_id': ids['project_id'], 'workflow': '\U0001f600' * 16384, 'reason': '\U0001f600' * 4096}
    path = f"/api/campaigns/{ids['campaign_id']}/releases/{ids['release_id']}/revise"
    assert client.post(path, json=body, headers={'Idempotency-Key': 'key'}).status_code == 200
    response = client.post('/api/mcp/campaigns', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'campaign.release.revise', 'arguments': {**ids, **body, 'idempotency_key': 'key'}}})
    assert response.json()['result']['isError'] is False
    assert calls[0] == calls[1] == ('tenant', ids['project_id'], ids['campaign_id'], ids['release_id'],
                                  body['workflow'], body['reason'], 'key')


def test_mcp_initialize_list_and_call_service_parity(client, monkeypatch):
    def rpc(method, params):
        return client.post('/api/mcp/campaigns', json={'jsonrpc': '2.0', 'id': 1,
                                                     'method': method, 'params': params}).json()
    assert rpc('initialize', {})['result']['capabilities'] == {'tools': {}}
    listing = rpc('tools/list', {})['result']['tools']
    assert len(listing) == 8
    assert all(t['inputSchema']['additionalProperties'] is False for t in listing)
    called = []
    monkeypatch.setattr(service, 'snapshot', lambda *args: called.append(args) or {'release': None})
    args = {key: str(uuid.uuid4()) for key in ('project_id', 'campaign_id', 'release_id')}
    response = rpc('tools/call', {'name': 'campaign.release.get', 'arguments': args})
    assert response['result']['isError'] is False and called[0][0] == 'tenant'
    assert rpc('tools/call', {'name': 'campaign.release.get', 'arguments': dict(args, status='passed')})['error']['code'] == -32602
    assert rpc('unknown', {})['error']['code'] == -32601
    assert client.post('/api/mcp/campaigns', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status_code == 202


def test_cross_project_and_revocation_rejected_at_service_boundary(monkeypatch):
    def forbidden(*args, **kwargs):
        raise service.platform_link.ProjectSessionForbidden('revoked')
    monkeypatch.setattr(service.platform_link, 'require_project_access', forbidden)
    with pytest.raises(service.platform_link.ProjectSessionForbidden):
        service.snapshot('tenant', str(uuid.uuid4()), str(uuid.uuid4()))


def test_worker_cannot_supply_evidence():
    import campaign_bridge
    with pytest.raises(campaign_bridge.BridgeError):
        campaign_bridge.handle('deliver', {'enrollment_id': str(uuid.uuid4()),
            'release_id': str(uuid.uuid4()), 'status': 'passed'}, 'worker')


def test_unavailable_completion_preserves_authorized_campaign(client, monkeypatch):
    project, campaign = str(uuid.uuid4()), str(uuid.uuid4())
    row = {'campaign_id': campaign, 'title': 'Existing work', 'dispatch_ref': 'known'}
    monkeypatch.setattr(campaigns, '_STORE', SimpleNamespace(get_campaign=lambda *args: row))
    monkeypatch.setattr(campaigns.platform_link, 'require_project_access', lambda *args, **kwargs: 'org')
    def unavailable(*args):
        raise RuntimeError('projection down')
    monkeypatch.setattr(service, '_STORE', SimpleNamespace(release_snapshot=unavailable))
    response = client.get(f'/api/campaigns/{campaign}', params={'project_id': project})
    assert response.status_code == 200
    assert response.json()['campaign'] == row
    assert response.headers['x-completion-status'] == 'unavailable'


def test_mcp_deadline_schema_and_authority_header_transport(client, monkeypatch):
    finish = next(t for t in campaign_mcp.tools_list() if t['name'] == 'campaign.finish')
    assert finish['inputSchema']['properties']['finish']['properties']['deadline_at']['format'] == 'date-time'
    calls = []
    monkeypatch.setattr(service, 'transition', lambda *args, **kwargs:
                        calls.append((args, kwargs)) or {'release': None})
    args = {key: str(uuid.uuid4()) for key in ('project_id', 'campaign_id', 'release_id')}
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'campaign.release.resume', 'arguments': args}}
    response = client.post('/api/mcp/campaigns', json=body,
        headers={'X-Authority-Session-Id': 'session', 'X-Authority-Turn-Id': 'turn'})
    assert response.json()['result']['isError'] is False
    assert calls[0][1] == {'authority_session_id': 'session', 'authority_turn_id': 'turn'}
    body['params']['arguments']['authority_session_id'] = 'forged-model-argument'
    assert client.post('/api/mcp/campaigns', json=body).json()['error']['code'] == -32602
