import uuid
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


def test_mcp_initialize_list_and_call_service_parity(client, monkeypatch):
    def rpc(method, params):
        return client.post('/api/mcp/campaigns', json={'jsonrpc': '2.0', 'id': 1,
                                                     'method': method, 'params': params}).json()
    assert rpc('initialize', {})['result']['capabilities'] == {'tools': {}}
    listing = rpc('tools/list', {})['result']['tools']
    assert len(listing) == 7
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
