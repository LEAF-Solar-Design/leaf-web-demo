"""Real dependency chain for the conversation back edge, with synthetic producers."""
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
import session_store
import platform_link
from routers import campaigns, campaign_conversation


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setenv('LEAF_AUTH_LIVE', '1')
    monkeypatch.setenv('LEAF_APP_DISPATCH_SECRET', 'synthetic-test-only')
    monkeypatch.setattr(deps, 'backedge_tier', lambda tid: 'pro')
    monkeypatch.setattr(deps, 'resolve_active_platform_tenant_authority', lambda sub: ('org-one', 'pro'))
    monkeypatch.setattr(session_store, 'active_turn_subject',
        lambda sid, turn, tid, age: 'author-one' if (sid, turn, tid) == ('session-one', 'turn-one', 'org-one') else None)
    project = str(uuid.uuid4())
    monkeypatch.setattr(session_store, 'get_session', lambda sid: {
        'tenant_id': 'org-one', 'org_id': 'org-one', 'project_id': project})
    monkeypatch.setattr(platform_link, 'require_project_session_access', lambda row, actor, **kwargs: row)
    calls = []
    monkeypatch.setattr(campaigns, '_finish_campaign', lambda *args: calls.append(args) or {'campaign_id': 'result'})
    app = FastAPI()
    app.include_router(campaign_conversation.router)
    app.include_router(campaigns.router)
    headers = {'X-Dispatch-Secret': 'synthetic-test-only', 'X-Tenant-Id': 'org-one',
               'X-Authority-Session-Id': 'session-one', 'X-Authority-Turn-Id': 'turn-one'}
    body = {'title': 'Finish', 'prompt': 'Deliver a useful file', 'delivery_profile': 'cad_file',
            'intended_user': 'Owner', 'workflow': 'Retrieve file', 'artifact_refs': []}
    return TestClient(app), headers, body, calls


def test_real_tenant_dependency_admits_exact_conversation_backedge(boundary):
    client, headers, body, calls = boundary
    result = client.post('/api/campaigns/conversation/finish', headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert len(calls) == 1 and calls[0][0].subject == 'author-one'


@pytest.mark.parametrize('field,value,status', [
    ('X-Dispatch-Secret', 'wrong', 401), ('X-Dispatch-Secret', None, 401),
    ('X-Authority-Turn-Id', 'stale', 409), ('X-Tenant-Id', 'org-two', 409)])
def test_invalid_authority_never_reaches_finish(boundary, field, value, status):
    client, headers, body, calls = boundary
    if value is None:
        headers.pop(field)
    else:
        headers[field] = value
    result = client.post('/api/campaigns/conversation/finish', headers=headers, json=body)
    assert result.status_code == status, result.text
    assert calls == []


def test_revoked_binding_never_reaches_finish(boundary, monkeypatch):
    client, headers, body, calls = boundary
    def revoked(subject):
        raise platform_link.ProjectSessionForbidden('revoked')
    monkeypatch.setattr(deps, 'resolve_active_platform_tenant_authority', revoked)
    assert client.post('/api/campaigns/conversation/finish', headers=headers, json=body).status_code == 409
    assert calls == []


def test_dispatch_secret_does_not_admit_general_campaign_writes(boundary):
    client, headers, body, calls = boundary
    assert client.post('/api/campaigns', headers=headers, json=body).status_code == 401
    assert calls == []
    assert not deps._dispatch_backedge_route('POST', '/api/campaigns/conversation/finish/extra')


def test_app_mounts_conversation_router():
    # Importing app starts unrelated services; bind this structural check to its actual AST.
    import ast
    tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'include_router' and node.args
        and ast.unparse(node.args[0]) == 'campaign_conversation.router' for node in ast.walk(tree))
