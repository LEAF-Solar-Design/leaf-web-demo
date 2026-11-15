"""Closed initializer diagnostics through the actual source route/middleware."""
import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import deps
import browser_project_flow as flow
from routers import browser_project as routes

PROJECT='22222222-2222-4222-8222-222222222222'
EDIT='44444444-4444-4444-8444-444444444444'
ORG='11111111-1111-4111-8111-111111111111'
ACTOR='33333333-3333-4333-8333-333333333333'
CORRELATION='55555555-5555-4555-8555-555555555555'
DIAGNOSTIC=dict(source_request_id=EDIT,correlation_id=CORRELATION,phase='stage',author_subphase='initialize_http',author_http_status=503)

@pytest.fixture
def app(monkeypatch):
    app=FastAPI();app.middleware('http')(routes.no_store_responses);app.include_router(routes.router)
    app.dependency_overrides[deps.require_active_tenant]=lambda:deps.TenantContext(ORG,subject='fixture',authority_resolved=True)
    monkeypatch.setattr(flow,'scope',lambda *a,**kw:(ORG,PROJECT,ACTOR))
    monkeypatch.setattr(flow.inspection,'authority',lambda *a:(ORG,PROJECT,ACTOR,'fixture'))
    return app

def post(app, headers=None):
    with TestClient(app) as client:
        return client.post(f'/api/projects/{PROJECT}/browser/source/requests',json=dict(source_request_id=EDIT,requirements='Fixture',source_operation='create',material_refs=[]),headers=headers or {})

@pytest.mark.parametrize('optional',[True,False])
def test_initializer_diagnostic_survives_actual_route_and_private_middleware(app,monkeypatch,optional):
    error=flow.FlowError(503);error.code='author_unavailable';error.public_diagnostic=dict(DIAGNOSTIC)
    if not optional:error.public_diagnostic.pop('author_http_status')
    def fail(*a):raise error
    monkeypatch.setattr(flow,'mutate',fail)
    response=post(app);body=response.json()['error']
    assert response.status_code==503
    assert response.headers['cache-control']=='private, no-store'
    assert body==dict(code='author_unavailable',message='Project source initialization failed. Review the original request before retrying.',**error.public_diagnostic)

def test_serializer_drops_unlisted_properties_and_keeps_code_and_fixed_text():
    error=flow.FlowError(503);error.code='author_unavailable';error.args=('PRIVATE EXCEPTION',)
    error.public_diagnostic=dict(DIAGNOSTIC,token='PRIVATE TOKEN',code='private_override',message='PRIVATE MESSAGE')
    response=routes._failure(error);body=json.loads(response.body)['error']
    assert set(body)=={'code','message',*DIAGNOSTIC}
    assert body['code']=='author_unavailable'
    assert 'PRIVATE' not in response.body.decode()

def test_legacy_error_without_property_is_unchanged():
    error=flow.FlowError(409)
    if hasattr(error,'public_diagnostic'):del error.public_diagnostic
    assert json.loads(routes._failure(error).body)=={'error':{'code':error.code,'message':str(error)}}

def test_other_failure_cannot_expose_diagnostic(app,monkeypatch):
    error=flow.FlowError(503);error.public_diagnostic=dict(DIAGNOSTIC)
    def fail(*a):raise error
    monkeypatch.setattr(flow,'mutate',fail)
    response=post(app)
    assert response.json()=={'error':{'code':error.code,'message':str(error)}}

def test_client_headers_cannot_preserve_private_auth_failure(app):
    def deny():raise HTTPException(403,'PRIVATE AUTH DETAIL')
    app.dependency_overrides[deps.require_active_tenant]=deny
    response=post(app,{'X-Leaf-Source-Failure':'validated'})
    assert response.status_code==403
    assert 'PRIVATE' not in response.text
    assert set(response.json()['error'])=={'code','message'}
