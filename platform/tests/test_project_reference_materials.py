"""Registered platform routes against the existing ephemeral PostgreSQL rail."""
import asyncio
import hashlib
import io
from pathlib import Path
import sys
import uuid

import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from leaf_platform import api, db, project_lifecycle, project_reference_materials as metadata, store
import project_reference_materials as originals
import write_loop


def headers(org, actor, mime=None):
    result = {'X-Org-Id': str(org), 'X-Actor-Binding-Id': str(actor)}
    if mime:
        result['Content-Type'] = mime
    return result


@pytest.fixture
def reference_project(client, make_org, monkeypatch, tmp_path):
    import store as blobs
    org = make_org()
    actor = store.create_identity_binding(org.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    created = project_lifecycle.create_blank_project(org.org_id, actor.binding_id, name='Shared originals',
                                                   idempotency_key=str(uuid.uuid4()))
    project = uuid.UUID(created['project']['project_id'])
    backend = blobs.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    return org.org_id, project, actor.binding_id, backend


def upload(client, identity, raw=b'Original lesson\r\n', name='lesson.txt', mime='text/plain'):
    org, project, actor, _ = identity
    return client.post(f'/api/projects/{project}/reference-files?filename={name}', content=raw,
                       headers=headers(org, actor, mime))


def test_registered_upload_list_reopen_download_and_dedup(client, reference_project):
    org, project, actor, _ = reference_project
    raw = b'Original lesson\r\n'
    first = upload(client, reference_project)
    assert first.status_code == 201, first.text
    material = first.json()['material']
    assert material['org_id'] == str(org) and material['project_id'] == str(project)
    assert material['actor_binding_id'] == str(actor)
    assert material['sha256'] == hashlib.sha256(raw).hexdigest() and material['byte_size'] == len(raw)
    assert material['name'] == 'lesson.txt' and material['media_type'] == 'text/plain'
    assert material['storage_status'] == 'uploaded' and material['preview_status'] == 'unsupported'
    assert material['model_status'] == 'not_consumed'
    assert not {'width', 'height', 'preview_sha256'} & material.keys()
    assert upload(client, reference_project, name='renamed.txt').json()['material'] == material
    # JSON is original reference content, never a portfolio document import.
    second = upload(client, reference_project, b'{"lessons":[1]}', 'lesson.json', 'application/json')
    assert second.status_code == 201
    url = f'/api/projects/{project}/reference-files'
    auth = headers(org, actor)
    page = client.get(url + '?limit=1', headers=auth).json()
    assert len(page['materials']) == 1 and page['next_cursor']
    rest = client.get(url + '?cursor=' + page['next_cursor'], headers=auth).json()
    assert len(rest['materials']) == 1 and rest['next_cursor'] is None
    assert {row['material_id'] for row in page['materials'] + rest['materials']} == {material['material_id'], second.json()['material']['material_id']}
    response = client.get(url + '/' + material['material_id'] + '/content', headers=auth)
    assert response.content == raw
    assert response.headers['x-content-sha256'] == material['sha256']
    assert response.headers['content-type'].startswith('text/plain')
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert 'attachment;' in response.headers['content-disposition'] and 'lesson.txt' in response.headers['content-disposition']
    assert client.get(url + '/' + str(uuid.uuid4()) + '/content', headers=auth).status_code == 404


def test_tenant_project_foreign_actor_and_inactive_identity_are_denied(client, reference_project, make_org):
    org, project, owner, _ = reference_project
    created = upload(client, reference_project).json()['material']
    stranger = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='owner')
    other_org = make_org()
    other_actor = store.create_identity_binding(other_org.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    other = project_lifecycle.create_blank_project(org, owner, name='Other project', idempotency_key=str(uuid.uuid4()))
    foreign_project = other['project']['project_id']
    url = f'/api/projects/{project}/reference-files'
    for auth, status in [(headers(org, stranger.binding_id), 403),
                         (headers(other_org.org_id, other_actor.binding_id), 404),
                         (headers(org, other_actor.binding_id), 403)]:
        assert client.get(url, headers=auth).status_code == status
        assert client.post(url + '?filename=x.txt', content=b'text', headers={**auth, 'Content-Type': 'text/plain'}).status_code == status
        assert client.get(url + '/' + created['material_id'] + '/content', headers=auth).status_code == status
    # Membership in both projects does not let an ID select the other project's bytes.
    assert client.get(f'/api/projects/{foreign_project}/reference-files', headers=headers(org, owner)).json()['materials'] == []
    assert client.get(f'/api/projects/{foreign_project}/reference-files/{created["material_id"]}/content', headers=headers(org, owner)).status_code == 404
    with db.transaction() as conn:
        conn.execute("UPDATE identity_bindings SET status='revoked', revoked_at=NOW() WHERE binding_id=%s", (owner,))
    assert client.get(url, headers=headers(org, owner)).status_code == 403


@pytest.mark.parametrize('role,can_write', [('editor', True), ('reviewer', False), ('read_only', False)])
def test_existing_role_rules_and_active_membership(client, reference_project, role, can_write):
    org, project, owner, backend = reference_project
    member = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='editor')
    project_lifecycle.invite_project_member(org, project, owner, member_binding_id=member.binding_id,
                                          role=role, idempotency_key=str(uuid.uuid4()))
    material = upload(client, reference_project).json()['material']
    auth = headers(org, member.binding_id)
    url = f'/api/projects/{project}/reference-files'
    assert client.get(url, headers=auth).status_code == 200
    assert client.get(url + '/' + material['material_id'] + '/content', headers=auth).content == b'Original lesson\r\n'
    assert upload(client, (org, project, member.binding_id, backend)).status_code == (201 if can_write else 403)
    with db.transaction() as conn:
        conn.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=NOW() WHERE org_id=%s AND project_id=%s AND binding_id=%s",
                     (org, project, member.binding_id))
    assert client.get(url, headers=auth).status_code == 403


def test_before_body_authority_declared_and_streaming_limits(reference_project, monkeypatch):
    org, project, owner, _ = reference_project
    stranger = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='owner')
    reads = 0
    def request(chunks, length=None, mime='text/plain'):
        chunks = iter(chunks)
        async def receive():
            nonlocal reads
            reads += 1
            chunk, more = next(chunks)
            return {'type': 'http.request', 'body': chunk, 'more_body': more}
        request_headers = [(b'content-type', mime.encode())]
        if length is not None:
            request_headers.append((b'content-length', length.encode()))
        return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': request_headers}, receive)
    def refused(req, actor=owner, expected=413):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(api.import_project_reference(project, req, 'x.txt', api._LifecycleActor(org, actor)))
        assert exc.value.status_code == expected
    refused(request([(b'x', False)]), stranger.binding_id, 403)
    assert reads == 0
    refused(request([], str(originals.TEXT_MAX_BYTES + 1)))
    assert reads == 0
    for value in ['-1', 'garbage', '+10', '1.5']:
        refused(request([], value), expected=400)
        assert reads == 0
    refused(request([], mime='application/json'), expected=422)
    assert reads == 0
    monkeypatch.setattr(originals, 'TEXT_MAX_BYTES', 15)
    refused(request([(b'a' * 10, True), (b'b' * 10, True), (b'c', False)]))
    assert reads == 2
    refused(request([(b'ab', False)], '1'), expected=400)
    refused(request([(b'ab', False)], '3'), expected=400)


def test_store_failure_commit_failure_retry_and_integrity(client, reference_project, monkeypatch):
    org, project, actor, backend = reference_project
    class Offline:
        def put_if_absent_or_verify(self, key, raw):
            raise OSError('offline')
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: Offline())
    response = upload(client, reference_project)
    assert response.status_code == 503 and 'Original storage failed' in response.json()['detail']
    assert metadata.list_materials(org, project, actor)['materials'] == []
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    real_commit = metadata.commit
    def fail(*args):
        raise OSError('commit offline')
    monkeypatch.setattr(metadata, 'commit', fail)
    response = upload(client, reference_project)
    assert response.status_code == 503 and 'Original stored' in response.json()['detail']
    digest = hashlib.sha256(b'Original lesson\r\n').hexdigest()
    key = originals.object_key(org, project, digest)
    assert backend.get(key) == b'Original lesson\r\n'
    assert metadata.list_materials(org, project, actor)['materials'] == []
    monkeypatch.setattr(metadata, 'commit', real_commit)
    material = upload(client, reference_project).json()['material']
    assert upload(client, reference_project).json()['material'] == material
    assert len(metadata.list_materials(org, project, actor)['materials']) == 1
    class Corrupt:
        def get(self, key):
            return b'different content'
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: Corrupt())
    response = client.get(f'/api/projects/{project}/reference-files/{material["material_id"]}/content', headers=headers(org, actor))
    assert response.status_code == 503 and 'integrity' in response.json()['detail']
    assert backend.get(key) == b'Original lesson\r\n'


@pytest.mark.parametrize('boundary', ['commit', 'read'])
def test_revocation_during_storage_or_read_refuses_commit_or_bytes(client, reference_project, monkeypatch, boundary):
    org, project, actor, backend = reference_project
    material = upload(client, reference_project).json()['material']
    def revoke():
        with db.transaction() as conn:
            conn.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=NOW() WHERE org_id=%s AND project_id=%s AND binding_id=%s",
                         (org, project, actor))
    name = 'prepare_and_store' if boundary == 'commit' else 'read_content'
    original = getattr(originals, name)
    def during(*args):
        result = original(*args)
        revoke()
        return result
    monkeypatch.setattr(originals, name, during)
    if boundary == 'commit':
        response = upload(client, reference_project, b'new content')
        with db.cursor() as cur:
            cur.execute('SELECT count(*) AS count FROM project_reference_materials WHERE org_id=%s AND project_id=%s', (org, project))
            assert cur.fetchone()['count'] == 1
        assert backend.get(originals.object_key(org, project, hashlib.sha256(b'new content').hexdigest())) == b'new content'
    else:
        response = client.get(f'/api/projects/{project}/reference-files/{material["material_id"]}/content', headers=headers(org, actor))
    assert response.status_code == 403


def test_real_migration_readiness_reapplication_and_photo_compatibility(client, reference_project):
    org, project, actor, _ = reference_project
    material = upload(client, reference_project).json()['material']
    db.apply_migration()
    assert metadata.get_material(org, project, actor, uuid.UUID(material['material_id'])) == material
    status = db.schema_status()
    assert status['ok'], status
    assert any(row['name'] == '0096_project_reference_materials.sql' for row in db.migration_manifest())
    output = io.BytesIO()
    Image.new('RGB', (4, 2), 'red').save(output, format='PNG')
    photo_url = f'/api/projects/{project}/materials'
    photo = client.post(photo_url + '/images?filename=photo.png', content=output.getvalue(), headers=headers(org, actor))
    assert photo.status_code == 201, photo.text
    saved = photo.json()['material']
    assert saved['width'] == 4 and saved['height'] == 2 and saved['preview_media_type'] == 'image/jpeg'
    assert client.get(photo_url + '/' + saved['material_id'] + '/content?variant=original', headers=headers(org, actor)).content == output.getvalue()
    assert len(client.get(photo_url, headers=headers(org, actor)).json()['materials']) == 1
    assert metadata.list_materials(org, project, actor)['materials'] == [material]
