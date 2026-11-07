import asyncio
import io
from pathlib import Path
import sys
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from leaf_platform import api, project_lifecycle, project_materials, store
import project_material_images as images
import write_loop


def raw_photo(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (16, 8), color).save(out, format='PNG')
    return out.getvalue()


def headers(org, binding):
    return {'X-Org-Id': str(org), 'X-Actor-Binding-Id': str(binding)}


@pytest.fixture
def material_project(client, make_org, monkeypatch, tmp_path):
    import store as blobs
    org = make_org()
    actor = store.create_identity_binding(org.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    created = project_lifecycle.create_blank_project(org.org_id, actor.binding_id, name='Photos', idempotency_key=str(uuid.uuid4()))
    project = uuid.UUID(created['project']['project_id'])
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: blobs.FilesystemBackend(str(tmp_path)))
    return org.org_id, project, actor.binding_id


def test_upload_replay_list_content_and_pagination(client, material_project):
    org, project, actor = material_project
    url = f'/api/projects/{project}/materials'
    auth = headers(org, actor)
    raw = raw_photo()
    first = client.post(url + '/images?filename=cake.heic', content=raw, headers={**auth, 'Content-Type': 'text/plain'})
    assert first.status_code == 201, first.text
    material = first.json()['material']
    assert material['media_type'] == 'image/png'
    again = client.post(url + '/images?filename=other.png', content=raw, headers=auth)
    assert again.json()['material'] == material
    second = client.post(url + '/images?filename=bread.png', content=raw_photo('blue'), headers=auth)
    assert second.status_code == 201
    page = client.get(url + '?limit=1', headers=auth).json()
    assert len(page['materials']) == 1 and page['next_cursor']
    rest = client.get(url + '?cursor=' + page['next_cursor'], headers=auth).json()
    assert len(rest['materials']) == 1 and rest['next_cursor'] is None
    content = url + '/' + material['material_id'] + '/content'
    original = client.get(content + '?variant=original', headers=auth)
    assert original.content == raw
    assert original.headers['content-type'] == 'image/png'
    assert original.headers['cache-control'] == 'private, no-store'
    assert original.headers['x-content-type-options'] == 'nosniff'
    preview = client.get(content, headers=auth)
    assert preview.headers['content-type'] == 'image/jpeg'
    assert Image.open(io.BytesIO(preview.content)).size == (16, 8)


def test_other_tenant_and_uninvited_actor_cannot_read_or_upload(client, material_project, make_org):
    org, project, actor = material_project
    stranger = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='owner')
    other = make_org()
    other_actor = store.create_identity_binding(other.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    url = f'/api/projects/{project}/materials'
    created = client.post(url + '/images?filename=photo.png', content=raw_photo(), headers=headers(org, actor))
    material_id = created.json()['material']['material_id']
    for auth, expected in [(headers(org, stranger.binding_id), 403), (headers(other.org_id, other_actor.binding_id), 404)]:
        url = f'/api/projects/{project}/materials'
        assert client.get(url, headers=auth).status_code == expected
        assert client.post(url + '/images?filename=x.png', content=raw_photo(), headers=auth).status_code == expected
        assert client.get(url + '/' + material_id + '/content', headers=auth).status_code == expected


def test_read_only_member_can_view_but_cannot_upload(client, material_project):
    org, project, owner = material_project
    reader = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='editor')
    project_lifecycle.invite_project_member(org, project, owner, member_binding_id=reader.binding_id,
                                          role='read_only', idempotency_key=str(uuid.uuid4()))
    url = f'/api/projects/{project}/materials'
    assert client.get(url, headers=headers(org, reader.binding_id)).status_code == 200
    assert client.post(url + '/images?filename=x.png', content=raw_photo(), headers=headers(org, reader.binding_id)).status_code == 403


def test_revocation_during_content_read_refuses_bytes(client, material_project, monkeypatch):
    org, project, actor = material_project
    url = f'/api/projects/{project}/materials'
    created = client.post(url + '/images?filename=photo.png', content=raw_photo(), headers=headers(org, actor))
    material_id = created.json()['material']['material_id']
    original = images.read_content
    def revoke(*args):
        raw = original(*args)
        from leaf_platform.db import run_transaction
        def operation(conn):
            with conn.cursor() as cur:
                cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=NOW() WHERE org_id=%s AND project_id=%s AND binding_id=%s", (org, project, actor))
        run_transaction(operation)
        return raw
    monkeypatch.setattr(images, 'read_content', revoke)
    response = client.get(url + '/' + material_id + '/content', headers=headers(org, actor))
    assert response.status_code == 403
    assert response.headers['content-type'].startswith('application/json')


def test_revocation_during_decode_prevents_commit(client, material_project, monkeypatch):
    org, project, actor = material_project
    original = images.prepare_and_store
    def revoke(*args):
        result = original(*args)
        from leaf_platform.db import run_transaction
        def operation(conn):
            with conn.cursor() as cur:
                cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=NOW() WHERE org_id=%s AND project_id=%s AND binding_id=%s", (org, project, actor))
        run_transaction(operation)
        return result
    monkeypatch.setattr(images, 'prepare_and_store', revoke)
    response = client.post(f'/api/projects/{project}/materials/images?filename=x.png', content=raw_photo(), headers=headers(org, actor))
    assert response.status_code == 403
    from leaf_platform.db import cursor
    with cursor() as cur:
        cur.execute('SELECT count(*) AS count FROM project_materials WHERE org_id=%s AND project_id=%s', (org, project))
        assert cur.fetchone()['count'] == 0


def test_auth_precedes_body_and_chunked_limit_is_cumulative(material_project, monkeypatch):
    org, project, actor = material_project
    count = 0
    async def receive():
        nonlocal count
        count += 1
        return {'type': 'http.request', 'body': b'x' * 10, 'more_body': count < 3}
    def request():
        return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []}, receive)
    stranger = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='owner')
    with pytest.raises(HTTPException) as refused:
        asyncio.run(api.import_project_image(project, request(), 'x.png', api._LifecycleActor(org, stranger.binding_id)))
    assert refused.value.status_code == 403 and count == 0
    monkeypatch.setattr(images, 'MAX_BYTES', 15)
    with pytest.raises(HTTPException) as too_big:
        asyncio.run(api.import_project_image(project, request(), 'x.png', api._LifecycleActor(org, actor)))
    assert too_big.value.status_code == 413 and count == 2
