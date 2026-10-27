"""Real database proof of the page editor's project and revision boundary."""
import io
import copy
import hashlib
import json
from pathlib import Path
import sys
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

# The exported page resolves the same private assets as the material API.
# Match that API's standalone platform test harness without collection-order reliance.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from leaf_platform import project_lifecycle, project_web_page, store


DOCUMENT = {'label': 'Baking portfolio', 'headline': 'Something sweet is on the way.',
            'description': 'A collection of bakes, made with care.', 'status': 'Coming soon',
            'theme': 'cream', 'alignment': 'center'}


def headers(org, actor):
    return {'X-Org-Id': str(org), 'X-Actor-Binding-Id': str(actor)}


@pytest.fixture
def page_project(make_org):
    org = make_org()
    actor = store.create_identity_binding(org.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    created = project_lifecycle.create_blank_project(org.org_id, actor.binding_id,
        name='Page', idempotency_key=str(uuid.uuid4()))
    return org.org_id, uuid.UUID(created['project']['project_id']), actor.binding_id


def test_save_retry_reload_exact_revision_export(client, page_project):
    org, project, actor = page_project
    url, auth = f'/api/projects/{project}/page', headers(org, actor)
    assert client.get(url, headers=auth).json() == {'page': None}
    body = {'expected_revision': 0, 'document': DOCUMENT}
    first = client.put(url, json=body, headers=auth)
    assert first.status_code == 200, first.text
    page = first.json()['page']
    assert page['revision'] == 1
    assert client.put(url, json=body, headers=auth).json()['page'] == page
    second = client.put(url, json={'expected_revision': 1, 'document': {**DOCUMENT, 'headline': 'Fresh from the oven'}}, headers=auth)
    assert second.json()['page']['revision'] == 2
    assert client.get(url, headers=auth).json() == second.json()
    assert client.put(url, json=body, headers=auth).status_code == 409
    exported = client.get(url + '/source?revision=1', headers=auth)
    assert exported.status_code == 200, exported.text
    assert exported.headers['x-page-digest'] == page['digest']
    assert exported.headers['cache-control'] == 'private, no-store'
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        # Package may have a root folder; resolve the canonical document by suffix.
        name = next(name for name in archive.namelist() if name == 'page.json' or name.endswith('/page.json'))
        assert json.loads(archive.read(name)) == DOCUMENT
    assert client.get(url + '/source?revision=3', headers=auth).status_code == 404


def test_scope_read_only_revocation_and_deleted_project(client, page_project, make_org):
    org, project, owner = page_project
    url = f'/api/projects/{project}/page'
    assert client.put(url, json={'expected_revision': 0, 'document': DOCUMENT}, headers=headers(org, owner)).status_code == 200
    outsider = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='owner')
    other = make_org()
    foreign = store.create_identity_binding(other.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    for auth, status in [(headers(org, outsider.binding_id), 403), (headers(other.org_id, foreign.binding_id), 404)]:
        assert client.get(url, headers=auth).status_code == status
        assert client.get(url + '/source?revision=1', headers=auth).status_code == status
        assert client.put(url, json={'expected_revision': 1, 'document': DOCUMENT}, headers=auth).status_code == status
    project_lifecycle.invite_project_member(org, project, owner, member_binding_id=outsider.binding_id,
        role='read_only', idempotency_key=str(uuid.uuid4()))
    auth = headers(org, outsider.binding_id)
    assert client.get(url + '/source?revision=1', headers=auth).status_code == 200
    assert client.put(url, json={'expected_revision': 1, 'document': DOCUMENT}, headers=auth).status_code == 403
    from leaf_platform.db import run_transaction
    def revoke(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=now() WHERE org_id=%s AND project_id=%s AND binding_id=%s", (org, project, outsider.binding_id))
    run_transaction(revoke)
    assert client.get(url + '/source?revision=1', headers=auth).status_code == 403
    store.soft_delete_project(org, project)
    assert client.get(url, headers=headers(org, owner)).status_code == 404


def test_concurrent_first_save_has_one_winner(page_project):
    org, project, actor = page_project
    def save(headline):
        try:
            return project_web_page.save(org, project, actor, expected_revision=0,
                document={**DOCUMENT, 'headline': headline})['revision']
        except project_lifecycle.LifecycleConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ['First draft', 'Second draft']))
    assert sorted(map(str, results)) == ['1', 'conflict']


@pytest.mark.parametrize('body', [
    {'expected_revision': True, 'document': DOCUMENT},
    {'expected_revision': 0, 'document': {**DOCUMENT, 'script': 'evil'}},
    {'expected_revision': 0, 'document': {**DOCUMENT, 'headline': 'x' * 241}},
    {'expected_revision': 0, 'document': {**DOCUMENT, 'theme': 'url(https://example.com)'}},
    {'expected_revision': 0, 'document': DOCUMENT, 'actor_id': str(uuid.uuid4())},
])
def test_rejects_invalid_input_without_saving(client, page_project, body):
    org, project, actor = page_project
    url, auth = f'/api/projects/{project}/page', headers(org, actor)
    assert client.put(url, json=body, headers=auth).status_code == 422
    assert client.get(url, headers=auth).json() == {'page': None}


def portfolio(material):
    return {'schema': 'leaf.portfolio.v1', 'name': 'Jessica Sargent', 'headline': 'Selected work',
            'introduction': '', 'about': '', 'contact': {'email': '', 'location': ''}, 'resume': [], 'theme': 'light',
            'categories': [{'slug': 'cakes', 'title': 'Cakes', 'description': '', 'hero': 0,
                'images': [{key: material[key] for key in ('material_id', 'sha256', 'preview_sha256')} | {'title': 'Cake', 'alt': 'Chocolate cake'}]}]}


def commit_photo(org, project, actor):
    from leaf_platform import project_materials
    return project_materials.commit(org, project, actor, {
        'name': 'cake.jpg', 'media_type': 'image/jpeg', 'byte_size': 10,
        'sha256': hashlib.sha256(b'original photo').hexdigest(), 'width': 100, 'height': 100,
        'preview_media_type': 'image/jpeg', 'preview_sha256': hashlib.sha256(b'preview photo').hexdigest(),
    })


def test_portfolio_rejects_foreign_missing_and_mismatched_photos(client, page_project):
    org, project, actor = page_project
    other = project_lifecycle.create_blank_project(org, actor, name='Other', idempotency_key=str(uuid.uuid4()))
    foreign = commit_photo(org, uuid.UUID(other['project']['project_id']), actor)
    local = commit_photo(org, project, actor)
    url, auth = f'/api/projects/{project}/page', headers(org, actor)
    bad = [portfolio(foreign)]
    for changes in [{'material_id': str(uuid.uuid4())}, {'sha256': '0' * 64}, {'preview_sha256': '0' * 64}]:
        bad.append(portfolio({**local, **changes}))
    for document in bad:
        response = client.put(url, json={'expected_revision': 0, 'document': document}, headers=auth)
        assert response.status_code == 422, response.text
        assert client.get(url, headers=auth).json() == {'page': None}


@pytest.mark.parametrize('target', ['web', 'ios'])
def test_portfolio_historical_export_retains_conflict_and_checks_materials(client, page_project, monkeypatch, target):
    import project_material_images as images
    org, project, actor = page_project
    material = commit_photo(org, project, actor)
    document = portfolio(material)
    url, auth = f'/api/projects/{project}/page', headers(org, actor)
    first = client.put(url, json={'expected_revision': 0, 'document': document}, headers=auth)
    assert first.status_code == 200, first.text
    assert client.put(url, json={'expected_revision': 0, 'document': document}, headers=auth).json() == first.json()
    changed = copy.deepcopy(document)
    changed['categories'][0]['images'][0]['title'] = 'New caption'
    assert client.put(url, json={'expected_revision': 1, 'document': changed}, headers=auth).json()['page']['revision'] == 2
    assert client.put(url, json={'expected_revision': 0, 'document': document}, headers=auth).status_code == 409
    reads = []
    def read_content(org_id, project_id, row, variant):
        reads.append((org_id, project_id, str(row['material_id']), variant))
        return b'preview photo'
    monkeypatch.setattr(images, 'read_content', read_content)
    response = client.get(url + f'/source?revision=1&target={target}', headers=auth)
    assert response.status_code == 200, response.text
    assert reads == [(org, project, material['material_id'], 'preview')]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        prefix = 'Portfolio/Resources/' if target == 'ios' else ''
        assert json.loads(archive.read(prefix + 'portfolio.json')) == document
        images_path = prefix + 'images/' if target == 'ios' else 'public/images/'
        assert archive.read(images_path + material['material_id'] + '.jpg') == b'preview photo'
        if target == 'ios':
            assert 'Portfolio.xcodeproj/project.pbxproj' in archive.namelist()
        assert json.loads(archive.read('source-manifest.json'))['revision'] == 1
    # Even an already-saved historical revision cannot export mismatched metadata.
    from leaf_platform.db import run_transaction
    def alter(conn):
        with conn.cursor() as cur:
            cur.execute('UPDATE project_materials SET preview_sha256=%s WHERE org_id=%s AND project_id=%s AND material_id=%s',
                        ('0' * 64, org, project, material['material_id']))
    run_transaction(alter)
    assert client.get(url + f'/source?revision=1&target={target}', headers=auth).status_code == 422


def test_page_raw_envelope_bound(client, page_project):
    org, project, actor = page_project
    response = client.put(f'/api/projects/{project}/page', content=b' ' * (2 * 1024 * 1024 + 1),
                          headers={**headers(org, actor), 'Content-Type': 'application/json'})
    assert response.status_code == 413


@pytest.mark.parametrize('target', ['web', 'ios'])
def test_portfolio_export_rechecks_revoked_membership_after_preview_reads(client, page_project, monkeypatch, target):
    import project_material_images as images
    from leaf_platform.db import run_transaction
    org, project, actor = page_project
    material = commit_photo(org, project, actor)
    url, auth = f'/api/projects/{project}/page', headers(org, actor)
    document = portfolio(material)
    # Reuse across galleries is permitted, but fetch each immutable asset once.
    document['categories'].append({**copy.deepcopy(document['categories'][0]), 'slug': 'selected', 'title': 'Selected'})
    assert client.put(url, json={'expected_revision': 0, 'document': document}, headers=auth).status_code == 200
    reads = []

    def read_content(org_id, project_id, row, variant):
        reads.append(str(row['material_id']))
        def revoke(conn):
            with conn.cursor() as cur:
                cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=now() "
                            'WHERE org_id=%s AND project_id=%s AND binding_id=%s', (org, project, actor))
        run_transaction(revoke)
        return b'preview photo'

    monkeypatch.setattr(images, 'read_content', read_content)
    response = client.get(url + f'/source?revision=1&target={target}', headers=auth)
    assert response.status_code == 403, response.text
    assert reads == [material['material_id']]
    assert not response.content.startswith(b'PK')
