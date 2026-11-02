"""Mounted callback contracts; all photos are synthetic and all stores are fake."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deps
import platform_link
import project_material_images
import project_material_inspection as inspection
import session_store
from routers import sessions

ORG = '11111111-1111-4111-8111-111111111111'
PROJECT = '22222222-2222-4222-8222-222222222222'
BINDING = '44444444-4444-4444-8444-444444444444'
HEADERS = {'x-tenant-id': ORG, 'x-dispatch-secret': 'inspection-test-secret',
           'X-Authority-Session-Id': 's', 'X-Authority-Turn-Id': 't'}
ROUTE = '/api/sessions/s/materials/inspect'


@pytest.fixture
def mounted(monkeypatch):
    platform_link._ensure_platform_package()
    from leaf_platform import project_materials, project_web_page, project_page_proposal

    output = io.BytesIO()
    Image.new('RGB', (8, 6), 'red').save(output, format='JPEG')
    raw = output.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    rows = [dict(material_id=str(UUID(int=i + 1)), name=f'Photo {i}.png', media_type='image/png',
                 byte_size=1234, sha256=hashlib.sha256(str(i).encode()).hexdigest(),
                 preview_sha256=digest, preview_media_type='image/jpeg', width=8, height=6,
                 created_at='2026-01-01T00:00:00+00:00') for i in range(94)]
    image = {key: rows[0][key] for key in ('material_id', 'sha256', 'preview_sha256')}
    image.update(title='Saved caption', alt='Saved alternative')
    document = {'schema': 'leaf.portfolio.v1', 'name': 'Synthetic', 'headline': 'Work',
                'introduction': '', 'about': '', 'theme': 'light', 'resume': [],
                'contact': {'email': '', 'location': ''},
                'categories': [{'slug': 'cakes', 'title': 'Cakes', 'description': '',
                                'hero': 0, 'images': [image]}]}
    state = dict(raw=raw, rows=rows, active=True, revoked=False, calls=[], hook=None,
                 page={'revision': 7, 'digest': project_page_proposal.document_digest(document),
                       'document': document},
                 session={'session_id': 's', 'tenant_id': ORG, 'org_id': ORG,
                          'project_id': PROJECT, 'active_turn_id': 't'},
                 binding=SimpleNamespace(platform_tenant_id=UUID(ORG), binding_id=UUID(BINDING)))
    monkeypatch.setenv('LEAF_APP_DISPATCH_SECRET', HEADERS['x-dispatch-secret'])
    monkeypatch.setattr(deps, 'auth_live', lambda: True)
    monkeypatch.setattr(deps, 'backedge_tier', lambda tenant: 'pro')
    monkeypatch.setattr(deps, 'resolve_active_platform_tenant_authority', lambda subject: (ORG, 'pro'))
    monkeypatch.setattr(session_store, 'get_session', lambda sid: state['session'] if sid == 's' else None)
    monkeypatch.setattr(session_store, 'active_turn_subject', lambda sid, tid, tenant, age:
                        'auth0|reader' if state['active'] and (sid, tid, tenant) == ('s', 't', ORG) else None)
    monkeypatch.setattr(platform_link, 'resolve_caller_binding', lambda tenant: state['binding'])

    def access(tenant, project_id, *, write, binding=None):
        assert write is False
        assert str(tenant) == ORG
        if state['revoked']:
            raise platform_link.ProjectSessionForbidden('private revoked diagnostics')
        return ORG

    monkeypatch.setattr(platform_link, 'require_project_access', access)

    def boundary(name, value):
        value = copy.deepcopy(value)
        state['calls'].append(name)
        if state['hook']:
            state['hook'](name)
        return value

    def scope(args):
        assert tuple(map(str, args)) == (ORG, PROJECT, BINDING)

    def page(*args):
        scope(args)
        return boundary('page', state['page'])

    def listing(*args, cursor=None, limit=200):
        scope(args)
        assert limit == 50
        selected = [row for row in state['rows'] if cursor is None or row['material_id'] > cursor]
        return boundary('list', {'materials': selected[:limit],
                               'next_cursor': selected[limit - 1]['material_id'] if len(selected) > limit else None})

    def get(*args):
        scope(args[:3])
        from leaf_platform.project_lifecycle import LifecycleUnavailable
        row = next((row for row in state['rows'] if row['material_id'] == args[3]), None)
        if row is None:
            raise LifecycleUnavailable('private foreign photo')
        return boundary('get', row)

    class Backend:
        def get(self, key):
            assert key == project_material_images.object_key(ORG, PROJECT, state['rows'][0]['preview_sha256'], 'preview')
            return boundary('pixels', state['raw'])

        def put_if_absent_or_verify(self, *args):
            pytest.fail('inspection wrote customer data')

    import write_loop
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda org: Backend())
    monkeypatch.setattr(project_materials, 'list_materials', listing)
    monkeypatch.setattr(project_materials, 'get_material', get)
    monkeypatch.setattr(project_materials, 'commit', lambda *a, **k: pytest.fail('material write'))
    monkeypatch.setattr(project_web_page, 'read', page)
    monkeypatch.setattr(project_web_page, 'save', lambda *a, **k: pytest.fail('page write'))
    app = FastAPI()
    app.include_router(sessions.router)
    with TestClient(app) as client:
        yield client, state


def post(mounted, body=None, headers=None, route=ROUTE, **kwargs):
    client, _ = mounted
    response = client.post(route, headers=HEADERS if headers is None else headers,
                           **({'json': body or {'op': 'catalog'}} if not kwargs else kwargs))
    assert response.headers['cache-control'] == 'private, no-store'
    if response.status_code != 200:
        assert 'data' not in response.json()
        assert 'private ' not in response.text
    return response


def preview(state):
    page = state['page']
    return dict(op='preview', material_id=state['rows'][0]['material_id'],
                expected_revision=page['revision'] if page else 0,
                expected_digest=page['digest'] if page else None,
                expected_preview_sha256=state['rows'][0]['preview_sha256'])


def test_catalog_all_94_stable_ids_and_saved_assignments(mounted):
    first = post(mounted).json()
    assert first['schema'] == 'leaf.project-material-catalog.v1'
    assert first['assignment_basis'] == 'saved_project_page'
    assert (first['org_id'], first['project_id'], first['session_id'], first['turn_id']) == (ORG, PROJECT, 's', 't')
    assert first['page_count'] == 50
    assert first['materials'][0]['assignments'] == [dict(gallery_slug='cakes', gallery_title='Cakes',
        index=0, hero=True, caption='Saved caption', alt='Saved alternative')]
    assert first['materials'][1]['assignments'] == []
    assert set(first['materials'][0]) == set(inspection._PUBLIC) | {'assignments'}
    second = post(mounted, dict(op='catalog', cursor=first['next_cursor'],
        expected_revision=first['source']['revision'], expected_digest=first['source']['digest'])).json()
    assert second['page_count'] == 44 and second['next_cursor'] is None
    assert [r['material_id'] for r in first['materials'] + second['materials']] == [r['material_id'] for r in mounted[1]['rows']]
    assert 'pixels' not in mounted[1]['calls']


def test_real_jpeg_preview_provenance_and_no_saved_page(mounted):
    state = mounted[1]
    result = post(mounted, preview(state)).json()
    assert result['schema'] == 'leaf.project-material-image.v1'
    assert result['original_sha256'] == state['rows'][0]['sha256']
    assert result['sha256'] == result['preview_sha256'] == hashlib.sha256(state['raw']).hexdigest()
    assert base64.b64decode(result['data'], validate=True) == state['raw']
    assert result['byte_size'] == len(state['raw']) and result['media_type'] == 'image/jpeg'
    assert len(result['assignments']) == 1
    state['page'] = None
    for body in ({'op': 'catalog'}, preview(state)):
        result = post(mounted, body).json()
        assert result['source'] == {'project_id': PROJECT, 'revision': 0, 'digest': None}
        assert result['assignment_basis'] == 'no_saved_page'
        assert (result.get('materials', [{}])[0].get('assignments', result.get('assignments'))) == []


@pytest.mark.parametrize('body', [[], {'op': 'original'}, {'op': 'catalog', 'project_id': PROJECT},
    {'op': 'catalog', 'cursor': 'bad'}, {'op': 'catalog', 'cursor': str(UUID(int=1))},
    {'op': 'catalog', 'expected_revision': True, 'expected_digest': None},
    {'op': 'catalog', 'expected_revision': 0, 'expected_digest': 'a' * 64},
    {'op': 'catalog', 'expected_revision': 1, 'expected_digest': None},
    {'op': 'catalog', 'expected_revision': 1, 'expected_digest': 'A' * 64},
    {'op': 'preview', 'material_id': str(UUID(int=1))}])
def test_closed_request_before_material_reads(mounted, body):
    response = post(mounted, content=json.dumps(body))
    assert response.status_code == 422
    assert mounted[1]['calls'] == []


@pytest.mark.parametrize('raw,status', [(b'{"op":"catalog","op":"preview"}', 422),
                                      (b'{', 422), (b' ' * 4097, 413)])
def test_streamed_body_bounds(mounted, raw, status):
    assert post(mounted, content=iter([raw[:2000], raw[2000:]])).status_code == status
    assert mounted[1]['calls'] == []


@pytest.mark.parametrize('header', list(HEADERS))
def test_all_four_headers_required(mounted, header):
    headers = dict(HEADERS)
    del headers[header]
    assert post(mounted, headers=headers).status_code in (401, 403, 404)
    assert mounted[1]['calls'] == []


@pytest.mark.parametrize('failure', ['turn', 'path', 'foreign', 'revoked', 'demo', 'guest', 'binding'])
def test_authority_rejection_before_reads(mounted, failure):
    state = mounted[1]
    headers = dict(HEADERS)
    route = ROUTE
    if failure == 'turn':
        state['active'] = False
    elif failure == 'path':
        route = '/api/sessions/other/materials/inspect'
    elif failure == 'foreign':
        headers['x-tenant-id'] = PROJECT
    elif failure == 'revoked':
        state['revoked'] = True
    elif failure in ('demo', 'guest'):
        headers[f'x-{failure}-session'] = 'untrusted'
    else:
        state['binding'].platform_tenant_id = UUID(PROJECT)
    assert post(mounted, headers=headers, route=route).status_code in (403, 404)
    assert state['calls'] == []


@pytest.mark.parametrize('boundary', ['page', 'list', 'get', 'pixels'])
@pytest.mark.parametrize('change', ['revoked', 'turn', 'binding', 'project'])
def test_authority_rechecked_after_each_read(mounted, boundary, change):
    state = mounted[1]

    def mutate(name):
        if name != boundary:
            return
        if change == 'revoked':
            state['revoked'] = True
        elif change == 'turn':
            state['active'] = False
        elif change == 'binding':
            state['binding'].binding_id = UUID(int=9)
        else:
            state['session']['project_id'] = str(UUID(int=9))

    state['hook'] = mutate
    body = {'op': 'catalog'} if boundary == 'list' else preview(state)
    assert post(mounted, body).status_code in (403, 404)
    if boundary != 'pixels':
        assert 'pixels' not in state['calls']


@pytest.mark.parametrize('change', ['page', 'original', 'preview', 'metadata'])
def test_source_changed_during_pixel_read_returns_no_pixels(mounted, change):
    state = mounted[1]

    def mutate(name):
        if name == 'pixels':
            if change == 'page':
                state['page']['revision'] += 1
            elif change == 'original':
                state['rows'][0]['sha256'] = 'e' * 64
            elif change == 'preview':
                state['rows'][0]['preview_sha256'] = 'e' * 64
            else:
                state['rows'][0]['name'] = 'Changed.png'

    state['hook'] = mutate
    assert post(mounted, preview(state)).status_code == 409


@pytest.mark.parametrize('failure', ['oversize', 'png', 'hash', 'metadata', 'foreign', 'source', 'digest'])
def test_invalid_preview_and_source_fail_closed(mounted, failure):
    state = mounted[1]
    body = preview(state)
    if failure in ('oversize', 'png'):
        state['raw'] = (b'\xff\xd8\xff' + b'x' * inspection.MAX_IMAGE_BYTES + b'\xff\xd9'
                        if failure == 'oversize' else b'\x89PNG\r\n\x1a\nnot-jpeg')
        state['rows'][0]['preview_sha256'] = hashlib.sha256(state['raw']).hexdigest()
        state['page'] = None
        body = preview(state)
    elif failure == 'hash':
        state['raw'] += b'changed'
    elif failure == 'metadata':
        state['rows'][0]['preview_media_type'] = 'image/png'
    elif failure == 'foreign':
        body['material_id'] = str(UUID(int=999))
    elif failure == 'source':
        body['expected_revision'] += 1
    else:
        state['page']['digest'] = 'e' * 64
    expected = {'oversize': 422, 'png': 422, 'hash': 503, 'metadata': 422,
                'foreign': 404, 'source': 409, 'digest': 409}
    response = post(mounted, body)
    assert response.status_code == expected[failure]
    assert base64.b64encode(state['raw']).decode() not in response.text


def test_storage_diagnostics_never_returned(mounted):
    def fail(name):
        raise RuntimeError('private provider secret pixels')
    mounted[1]['hook'] = fail
    response = post(mounted)
    assert response.status_code == 503
    assert response.json() == {'code': 'service_unavailable', 'detail': 'Project material service is unavailable.'}


def test_catalog_continuation_requires_unchanged_source(mounted):
    first = post(mounted).json()
    mounted[1]['page']['revision'] += 1
    mounted[1]['calls'].clear()
    response = post(mounted, dict(op='catalog', cursor=first['next_cursor'],
        expected_revision=first['source']['revision'], expected_digest=first['source']['digest']))
    assert response.status_code == 409
    assert mounted[1]['calls'] == ['page']


def test_catalog_rechecks_source_after_listing(mounted):
    state = mounted[1]

    def mutate(name):
        if name == 'list':
            state['page']['revision'] += 1

    state['hook'] = mutate
    assert post(mounted).status_code == 409


def test_saved_assignment_hash_mismatch_does_not_read_pixels(mounted):
    state = mounted[1]
    state['rows'][0]['sha256'] = 'e' * 64
    assert post(mounted, preview(state)).status_code == 409
    assert 'pixels' not in state['calls']


def test_response_size_is_bounded(mounted):
    mounted[1]['rows'][0]['name'] = 'x' * inspection.MAX_RESPONSE_BYTES
    assert post(mounted).status_code == 422


@pytest.mark.parametrize('tenant', [ORG,
    deps.TenantContext(ORG),
    deps.TenantContext(ORG, subject='auth0|reader', authority_resolved=True, verified_demo=True),
    deps.TenantContext(ORG, subject='auth0|someone-else', authority_resolved=True)])
def test_service_rejects_unverified_demo_or_wrong_subject(mounted, tenant):
    with pytest.raises(inspection.InspectionError):
        inspection.inspect(tenant, 's', 's', 't', {'op': 'catalog'})
    assert mounted[1]['calls'] == []
