"""Mounted public auth and pre-body bounded transport checks."""
from __future__ import annotations

import sys
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deps
import browser_project_flow as flow
from routers import browser_project as routes

PROJECT = '22222222-2222-4222-8222-222222222222'
EDIT = '44444444-4444-4444-8444-444444444444'
ORG = '11111111-1111-4111-8111-111111111111'
ACTOR = '33333333-3333-4333-8333-333333333333'
PREFIX = '/api/projects/' + PROJECT + '/browser/source'
HEADERS = {'X-Authority-Session-Id': 'session', 'X-Authority-Turn-Id': 'turn'}
BODY = dict(source_request_id=EDIT, requirements='Create my source', source_operation='create', material_refs=[])


@pytest.fixture
def app(monkeypatch):
    app = FastAPI()
    app.middleware('http')(routes.no_store_responses)
    app.include_router(routes.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: deps.TenantContext(
        ORG, subject='subject', authority_resolved=True)
    monkeypatch.setattr(flow, 'scope', lambda *a, **kw: (ORG, PROJECT, ACTOR))
    monkeypatch.setattr(flow.inspection, 'authority', lambda *a: (ORG, PROJECT, ACTOR, 'subject'))
    return app


def test_registered_routes_use_active_public_auth(app, monkeypatch):
    def deny():
        raise HTTPException(403, 'denied')
    app.dependency_overrides[deps.require_active_tenant] = deny
    monkeypatch.setattr(flow, 'mutate', lambda *a: pytest.fail('unauthenticated mutation'))
    with TestClient(app) as client:
        for suffix in ('/requests', '/requests/' + EDIT, '/requests/' + EDIT + '/export?repo=bad'):
            response = client.get(PREFIX + suffix, headers={'X-Dispatch-Secret': 'untrusted'})
            assert response.status_code == 403
            assert response.headers['cache-control'] == 'private, no-store'
        for suffix in ('/requests', *('/requests/' + EDIT + '/' + action for action in ('confirm', 'recover', 'reject'))):
            response = client.post(PREFIX + suffix, content=b'not json')
            assert response.status_code == 403
            assert response.headers['cache-control'] == 'private, no-store'


def test_project_denial_precedes_body_stream(app, monkeypatch):
    async def never_read(self):
        pytest.fail('denied request body was read')
        yield b''
    def deny(*a, **kw):
        raise flow.FlowError(403)
    monkeypatch.setattr(Request, 'stream', never_read)
    monkeypatch.setattr(flow, 'scope', deny)
    with TestClient(app) as client:
        response = client.post(PREFIX + '/requests', content=b'bad', headers=HEADERS)
    assert response.status_code == 403


def test_real_app_registers_router_and_private_failure_middleware():
    # Avoid constructing unrelated app startup services in this focused suite.
    source = (Path(__file__).resolve().parents[1] / 'app.py').read_text()
    assert 'app.include_router(browser_project.router)' in source
    assert 'app.middleware("http")(browser_project.no_store_responses)' in source
    for route in routes.router.routes:
        assert any(dependency.call is deps.require_active_tenant for dependency in route.dependant.dependencies)


@pytest.mark.parametrize('payload', [
    b'{"source_request_id":"a","source_request_id":"b"}',
    b'{"x":{"a":1,"\\u0061":2}}', b'{"x":NaN}', b'\xff',
])
def test_strict_body_is_rejected_without_dispatch(app, monkeypatch, payload):
    monkeypatch.setattr(flow, 'mutate', lambda *a: pytest.fail('invalid body dispatched'))
    with TestClient(app) as client:
        response = client.post(PREFIX + '/requests', content=payload,
            headers=dict(HEADERS, **{'Content-Type': 'application/json'}))
    assert response.status_code == 422
    assert response.headers['cache-control'] == 'private, no-store'
    assert set(response.json()['error']) == {'code', 'message'}


def test_declared_and_streamed_limits(app, monkeypatch):
    monkeypatch.setattr(flow, 'mutate', lambda *a: pytest.fail('oversized body dispatched'))
    with TestClient(app) as client:
        declared = client.post(PREFIX + '/requests', content=b'{}', headers=dict(HEADERS, **{
            'Content-Type': 'application/json', 'Content-Length': str(flow.MAX_INPUT_BYTES + 1)}))
        streamed = client.post(PREFIX + '/requests', content=iter([b' ' * 65536] * 3),
            headers=dict(HEADERS, **{'Content-Type': 'application/json'}))
    assert declared.status_code == streamed.status_code == 413


def test_declared_length_must_equal_actual_chunk_count(app):
    with TestClient(app) as client:
        response = client.post(PREFIX + '/requests', content=b'{}', headers=dict(HEADERS, **{
            'Content-Type': 'application/json', 'Content-Length': '3'}))
    assert response.status_code == 422


def test_unsupported_encoding_and_extra_body_fields(app, monkeypatch):
    monkeypatch.setattr(flow, 'mutate', lambda *a: pytest.fail('invalid body dispatched'))
    with TestClient(app) as client:
        encoded = client.post(PREFIX + '/requests', json=BODY, headers=dict(HEADERS, **{'Content-Encoding': 'gzip'}))
        extra = client.post(PREFIX + '/requests', json=dict(BODY, tenant_id=ORG), headers=HEADERS)
    assert encoded.status_code == extra.status_code == 422


def test_connected_handlers_and_closed_action_bodies(app, monkeypatch):
    calls = []
    def mutate(*args):
        calls.append(args)
        return {'state': 'awaiting_confirmation'}
    monkeypatch.setattr(flow, 'mutate', mutate)
    monkeypatch.setattr(flow, 'read', lambda *a: [])
    with TestClient(app) as client:
        assert client.get(PREFIX + '/requests').json() == []
        assert client.post(PREFIX + '/requests', json=BODY, headers=HEADERS).status_code == 200
        for action in ('confirm', 'recover', 'reject'):
            body = dict(receipt_digest='a' * 64, expected_version=2)
            if action == 'confirm':
                body['confirmation_id'] = '77777777-7777-4777-8777-777777777777'
            assert client.post(PREFIX + '/requests/' + EDIT + '/' + action, json=body, headers=HEADERS).status_code == 200
    assert [args[4] for args in calls] == ['request', 'confirm', 'recover', 'reject']
    assert all(args[1:4] == (PROJECT, 'session', 'turn') for args in calls)


def test_response_limit_and_fixed_diagnostics(app, monkeypatch):
    monkeypatch.setattr(flow, 'read', lambda *a: {'secret': 'x' * (flow.MAX_RESPONSE_BYTES + 1)})
    with TestClient(app) as client:
        response = client.get(PREFIX + '/requests')
    assert response.status_code == 503 and 'secret' not in response.text


@pytest.fixture
def bundle_route(app, monkeypatch):
    row = dict(project_id=PROJECT, source_request_id=EDIT, state='published', version=2,
               source_commit='c' * 40, source_tree='d' * 40, receipt_digest='a' * 64)
    bundle = b'# v3 git bundle\n@object-format=sha1\n' + b'c' * 40 + b' refs/heads/main\n\nPACK'
    monkeypatch.setattr(flow, 'repository', lambda *a: dict(tenant_id=ORG, organization_id=ORG,
        project_id=PROJECT, repo_key=ACTOR))
    monkeypatch.setattr(flow, 'read', lambda *a: row)
    calls = []
    def produce(*a, **kw):
        calls.append(a)
        return dict(bundle=bundle, size_bytes=len(bundle), bundle_sha256=hashlib.sha256(bundle).hexdigest(),
                    source_commit=row['source_commit'], source_tree=row['source_tree'], lease_id=ACTOR)
    monkeypatch.setattr(routes.source_export.source, 'export_project_source_bundle', produce)
    query = '?source_commit=' + 'c' * 40 + '&source_tree=' + 'd' * 40 + '&receipt_digest=' + 'a' * 64 + '&version=2'
    return app, PREFIX + '/requests/' + EDIT + '/export' + query, bundle, calls


def test_mounted_export_returns_only_verified_binary(bundle_route):
    app, url, bundle, calls = bundle_route
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 200
    assert response.content == bundle
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['content-length'] == str(len(bundle))
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-leaf-bundle-sha256'] == hashlib.sha256(bundle).hexdigest()
    assert not any('lease' in key or 'repo' in key for key in response.headers)
    assert len(calls) == 1


@pytest.mark.parametrize('suffix', ['&version=2', '&tenant_id=foreign', '&format=zip'])
def test_mounted_export_closed_query(bundle_route, suffix):
    app, url, _, calls = bundle_route
    with TestClient(app) as client:
        response = client.get(url + suffix)
    assert response.status_code == 422
    assert not calls


def test_export_project_denial_precedes_bad_query_and_producer(bundle_route, monkeypatch):
    app, url, _, calls = bundle_route
    def denied(*a, **kw):
        raise flow.FlowError(403)
    monkeypatch.setattr(flow, 'scope', denied)
    with TestClient(app) as client:
        response = client.get(url + '&version=garbage')
    assert response.status_code == 403
    assert not calls


def test_export_hides_unexpected_provider_diagnostics(bundle_route, monkeypatch):
    app, url, _, _ = bundle_route
    def fail(*a, **kw):
        raise RuntimeError('private repo and lease diagnostics')
    monkeypatch.setattr(routes.source_export.source, 'export_project_source_bundle', fail)
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 503
    assert response.json() == {'error': {'code': 'service_unavailable', 'message': str(flow.FlowError(503))}}
