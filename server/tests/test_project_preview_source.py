"""Dormant custody proof with real filesystem bytes and controlled DB authority.

The page reader, exporter and iOS producer are real. The DB transaction boundary
and membership checks are controlled here; this is no DB or simulator proof.
"""
import copy
import hashlib
import io
import json
from types import SimpleNamespace
import uuid
import zipfile

import pytest

import platform_link

platform_link._ensure_platform_package()
from leaf_platform import project_lifecycle as lifecycle, project_web_page as pages
from leaf_platform.web_page_template import document_digest
import project_material_images as images
import project_preview_source as source
import write_loop
import store


@pytest.fixture
def custody(tmp_path, monkeypatch):
    org, project, actor, material_id = map(str, (uuid.uuid4() for _ in range(4)))
    backend = store.FilesystemBackend(str(tmp_path))
    preview = b'controlled portfolio preview bytes'
    material = {'material_id': material_id, 'sha256': hashlib.sha256(b'original').hexdigest(),
                'preview_sha256': hashlib.sha256(preview).hexdigest()}
    # Same minimal one-photo portfolio shape as platform/tests/test_project_web_page.py.
    document = {
        'schema': 'leaf.portfolio.v1', 'name': 'Jessica Sargent', 'headline': 'Selected work',
        'introduction': '', 'about': '', 'contact': {'email': '', 'location': ''},
        'resume': [], 'theme': 'light', 'categories': [
            {'slug': 'cakes', 'title': 'Cakes', 'description': '', 'hero': 0,
             'images': [{**material, 'title': 'Cake', 'alt': 'Chocolate cake'}]}],
    }
    page = {'revision': 3, 'digest': document_digest(document), 'document': document}
    state = SimpleNamespace(org=org, project=project, actor=actor, backend=backend,
                            page=page, active=True, exports=[], reads=[], storage=[], roles=[])

    def authorize(*args, write=False):
        assert args == (org, project, actor)
        assert write is False
        if not state.active:
            raise lifecycle.LifecycleForbidden('project access denied')
        state.roles.append(args)
        return 'read_only'

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, args):
            assert args[:2] == (org, project)
            if 'FROM project_web_page_revisions' in sql:
                assert args[2:] == (3, 3)
                self.row = copy.deepcopy(state.page)
            else:
                assert 'FROM project_materials' in sql
                assert args[2] == material_id
                self.row = material.copy()

        def fetchone(self):
            return self.row

    monkeypatch.setattr(pages, 'run_transaction', lambda operation: operation(SimpleNamespace(cursor=Cursor)))
    monkeypatch.setattr(pages, '_authorize', lambda cur, *args, **kwargs: authorize(*args, **kwargs))
    monkeypatch.setattr(lifecycle, 'require_project_role', authorize)
    real_read, real_export = pages.read, pages.export_source

    def read(*args, **kwargs):
        state.reads.append((args, kwargs))
        return real_read(*args, **kwargs)

    def export(*args, **kwargs):
        state.exports.append((args, kwargs))
        result = real_export(*args, **kwargs)
        state.exported = result
        return result

    monkeypatch.setattr(pages, 'read', read)
    monkeypatch.setattr(pages, 'export_source', export)

    def select_backend(tenant):
        assert tenant == org
        state.storage.append(tenant)
        return backend

    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', select_backend)
    backend.put_if_absent_or_verify(images.object_key(org, project, material['preview_sha256'], 'preview'), preview)
    state.prepare = lambda: source.prepare_source(org, project, actor, revision=3)
    state.read = lambda descriptor: source.read_source(org, project, actor, descriptor)
    return state


def test_exact_real_export_persisted_manifest_and_idempotent_capture(custody):
    c = custody
    descriptor = c.prepare()
    exported_page, raw = c.exported
    assert c.exports == [((c.org, c.project, c.actor), {'revision': 3, 'target': 'ios'})]
    assert all(call == ((c.org, c.project, c.actor), {'revision': 3}) for call in c.reads)
    assert descriptor == {
        'schema': 'leaf.project-preview-source.v1', 'target': 'ios-simulator',
        'org_id': c.org, 'project_id': c.project, 'revision': 3,
        'document_sha256': exported_page['digest'],
        'archive_sha256': hashlib.sha256(raw).hexdigest(), 'byte_size': len(raw),
    }
    scope = f'tenants/{c.org}/projects/{c.project}/previews/ios/source'
    archive_key = f"{scope}/{descriptor['archive_sha256']}"
    manifest_key = f"{scope}/manifests/3/{descriptor['document_sha256']}.json"
    assert c.backend.get(archive_key) == raw
    persisted = c.backend.get(manifest_key)
    assert json.loads(persisted) == descriptor
    assert persisted == json.dumps(descriptor, sort_keys=True, separators=(',', ':')).encode()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert json.loads(archive.read('Portfolio/PortfolioContent/portfolio.json')) == c.page['document']
        manifest = json.loads(archive.read('source-manifest.json'))
        assert manifest['revision'] == 3
        assert manifest['document_sha256'] == descriptor['document_sha256']
    assert c.prepare() == descriptor
    assert c.read(descriptor) == raw


def test_read_never_regenerates_source(custody, monkeypatch):
    c = custody
    descriptor = c.prepare()
    raw = c.exported[1]

    def forbidden(*args, **kwargs):
        pytest.fail('read must never export or rebuild')

    monkeypatch.setattr(pages, 'export_source', forbidden)
    from leaf_platform import portfolio_ios_template
    monkeypatch.setattr(portfolio_ios_template, 'source_archive', forbidden)
    assert c.read(descriptor) == raw


@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('bad', ['../escape', 'AABBCCDD-1111-4111-8111-111111111111',
                                 '11111111111141118111111111111111', None, 1])
def test_invalid_scope_fails_before_export_or_storage(custody, index, bad):
    c = custody
    ids = [c.org, c.project, c.actor]
    ids[index] = bad
    with pytest.raises(ValueError):
        source.prepare_source(*ids, revision=3)
    with pytest.raises(ValueError):
        source.read_source(*ids, {})
    assert not c.storage and not c.exports and not c.reads and not c.roles


@pytest.mark.parametrize('revision', [True, False, 0, -1, 1.0, '3', None, 2147483648])
def test_invalid_revision_fails_before_authority_or_storage(custody, revision):
    c = custody
    with pytest.raises(ValueError):
        source.prepare_source(c.org, c.project, c.actor, revision=revision)
    assert not c.storage and not c.exports and not c.roles


@pytest.mark.parametrize('changes', [
    {'org_id': str(uuid.uuid4())}, {'project_id': str(uuid.uuid4())},
    {'org_id': '../tenant'}, {'revision': True}, {'revision': 0}, {'revision': '3'},
    {'document_sha256': 'A' * 64}, {'document_sha256': '../digest'},
    {'archive_sha256': 'a' * 63}, {'archive_sha256': None},
    {'byte_size': True}, {'byte_size': 0}, {'byte_size': source.MAX_ARCHIVE_BYTES + 1},
    {'schema': 'other'}, {'target': 'ios'}, {'path': '/private'},
    {'signed_url': 'private'}, {'token': 'private'}, {'status': 'accepted'},
    {'provider_account': 'private'},
])
def test_closed_descriptor_rejects_bad_fields_before_storage(custody, changes):
    c = custody
    descriptor = c.prepare()
    c.storage.clear()
    c.reads.clear()
    c.roles.clear()
    with pytest.raises(ValueError):
        c.read({**descriptor, **changes})
    assert not c.storage and not c.reads and not c.roles


def test_missing_descriptor_field_and_wrong_request_scope_fail_before_storage(custody):
    c = custody
    descriptor = c.prepare()
    c.storage.clear()
    for field in descriptor:
        invalid = descriptor.copy()
        del invalid[field]
        with pytest.raises(ValueError):
            c.read(invalid)
    for org, project in [(str(uuid.uuid4()), c.project), (c.org, str(uuid.uuid4()))]:
        with pytest.raises(ValueError):
            source.read_source(org, project, c.actor, descriptor)
    assert not c.storage


@pytest.mark.parametrize('part', ['manifest', 'archive'])
@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'truncated', 'oversize'])
def test_missing_or_damaged_persisted_objects_fail_closed(custody, monkeypatch, part, damage):
    c = custody
    descriptor = c.prepare()
    archive_key, manifest_key = source._keys(descriptor)
    key = manifest_key if part == 'manifest' else archive_key
    if damage == 'missing':
        from pathlib import Path
        (Path(c.backend.root) / key).unlink()
    else:
        original = c.backend.get(key)
        if damage == 'oversize':
            # Exercise the bound without allocating an 80 MiB test object.
            monkeypatch.setattr(source, 'MAX_ARCHIVE_BYTES' if part == 'archive' else 'MAX_MANIFEST_BYTES',
                                len(original) - 1)
            if part == 'archive':
                descriptor['byte_size'] -= 1
                c.backend.put(manifest_key, source._manifest(descriptor))
            damaged = original
        else:
            damaged = b'x' * len(original) if damage == 'corrupt' else original[:-1]
        c.backend.put(key, damaged)
    with pytest.raises(source.SourceStoreUnavailable, match=r'^Preview source storage is unavailable\.$'):
        c.read(descriptor)


def test_descriptor_cannot_manufacture_binding_and_manifest_conflicts_are_immutable(custody):
    c = custody
    descriptor = c.prepare()
    forged = {**descriptor, 'archive_sha256': hashlib.sha256(b'forged').hexdigest(), 'byte_size': 6}
    archive_key, manifest_key = source._keys(forged)
    c.backend.put_if_absent_or_verify(archive_key, b'forged')
    with pytest.raises(source.SourceStoreUnavailable):
        c.read(forged)
    conflicting = source._manifest(forged)
    c.backend.put(manifest_key, conflicting)  # simulate corrupt/out-of-band storage, never the public API
    with pytest.raises(source.SourceStoreUnavailable):
        c.prepare()
    assert c.backend.get(manifest_key) == conflicting
    with pytest.raises(source.SourceStoreUnavailable):
        c.read(descriptor)


def test_missing_manifest_is_not_a_grant(custody):
    c = custody
    descriptor = {
        'schema': source.SCHEMA, 'target': 'ios-simulator', 'org_id': c.org,
        'project_id': c.project, 'revision': 3, 'document_sha256': c.page['digest'],
        'archive_sha256': hashlib.sha256(b'forged').hexdigest(), 'byte_size': 6,
    }
    c.backend.put_if_absent_or_verify(source._keys(descriptor)[0], b'forged')
    with pytest.raises(source.SourceStoreUnavailable):
        c.read(descriptor)
    assert not c.exports


@pytest.mark.parametrize('changes', [{'revision': 4}, {'digest': 'a' * 64},
                                    {'digest': 'A' * 64}, {'revision': True}])
def test_wrong_saved_page_fails_before_storage(custody, changes):
    c = custody
    descriptor = c.prepare()
    c.storage.clear()
    c.page.update(changes)
    with pytest.raises((ValueError, lifecycle.LifecycleConflict)):
        c.read(descriptor)
    assert not c.storage


@pytest.mark.parametrize('change', ['revision', 'digest', 'empty', 'oversize'])
def test_invalid_export_is_not_published(custody, monkeypatch, change):
    c = custody
    page, raw = copy.deepcopy(c.page), b'archive'
    if change == 'revision':
        page['revision'] = 4
    elif change == 'digest':
        page['digest'] = 'a' * 64
    elif change == 'empty':
        raw = b''
    else:
        monkeypatch.setattr(source, 'MAX_ARCHIVE_BYTES', len(raw) - 1)
    monkeypatch.setattr(pages, 'export_source', lambda *args, **kwargs: (page, raw))
    with pytest.raises(ValueError):
        c.prepare()
    assert not c.storage


@pytest.mark.parametrize('operation', ['prepare', 'read'])
def test_revoked_member_cannot_use_descriptor(custody, operation):
    c = custody
    descriptor = c.prepare()
    c.storage.clear()
    c.active = False
    with pytest.raises(lifecycle.LifecycleForbidden, match='project access denied'):
        c.prepare() if operation == 'prepare' else c.read(descriptor)
    assert not c.storage


@pytest.mark.parametrize('operation,method,part', [
    ('prepare', 'put_if_absent_or_verify', 'archive'),
    ('prepare', 'put_if_absent_or_verify', 'manifest'),
    ('prepare', 'get', 'archive'), ('prepare', 'get', 'manifest'),
    ('read', 'get', 'archive'), ('read', 'get', 'manifest'),
])
def test_revocation_during_custody_io_prevents_success(custody, monkeypatch, operation, method, part):
    c = custody
    descriptor = c.prepare()
    archive_key, manifest_key = source._keys(descriptor)
    target = archive_key if part == 'archive' else manifest_key
    original = getattr(c.backend, method)

    def revoke(key, *args):
        result = original(key, *args)
        if key == target:
            c.active = False
        return result

    monkeypatch.setattr(c.backend, method, revoke)
    with pytest.raises(lifecycle.LifecycleForbidden):
        c.prepare() if operation == 'prepare' else c.read(descriptor)
    assert not c.active


@pytest.mark.parametrize('method', ['put_if_absent_or_verify', 'get'])
def test_storage_errors_have_stable_public_text_and_do_not_cleanup(custody, monkeypatch, method):
    c = custody
    descriptor = c.prepare()
    archive_key, manifest_key = source._keys(descriptor)
    original = getattr(c.backend, method)

    def fail(key, *args):
        if key == manifest_key:
            raise OSError('/private/path?token=secret')
        return original(key, *args)

    monkeypatch.setattr(c.backend, method, fail)
    with pytest.raises(source.SourceStoreUnavailable) as error:
        c.prepare()
    assert str(error.value) == 'Preview source storage is unavailable.'
    assert error.value.__suppress_context__ is True
    assert c.backend.get(archive_key) == c.exported[1]


@pytest.mark.parametrize('error_type', [lifecycle.LifecycleForbidden, lifecycle.LifecycleUnavailable])
def test_private_errors_are_preserved_even_at_storage_boundary(custody, monkeypatch, error_type):
    c = custody
    descriptor = c.prepare()
    private = error_type('private authority error')

    def fail(key):
        raise private

    monkeypatch.setattr(c.backend, 'get', fail)
    with pytest.raises(error_type) as error:
        c.read(descriptor)
    assert error.value is private


def test_archive_precedes_manifest_and_readback_is_required(custody, monkeypatch):
    c = custody
    descriptor = c.prepare()
    archive_key, manifest_key = source._keys(descriptor)
    events = []
    original_put, original_get = c.backend.put_if_absent_or_verify, c.backend.get

    def put(key, raw):
        events.append(('put', key))
        return original_put(key, raw)

    def get(key):
        if key in (archive_key, manifest_key):
            events.append(('get', key))
        return original_get(key)

    monkeypatch.setattr(c.backend, 'put_if_absent_or_verify', put)
    monkeypatch.setattr(c.backend, 'get', get)
    c.prepare()
    assert events == [('put', archive_key), ('put', manifest_key),
                      ('get', manifest_key), ('get', archive_key)]
    monkeypatch.setattr(c.backend, 'get', lambda key: b'corrupt persisted bytes')
    # Bypass export's material read so this failure is the custody readback.
    monkeypatch.setattr(pages, 'export_source', lambda *args, **kwargs: c.exported)
    with pytest.raises(source.SourceStoreUnavailable):
        c.prepare()
