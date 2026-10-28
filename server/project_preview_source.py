"""Dormant immutable iOS source custody; no build, job, or simulator proof.

Descriptors identify saved source. They never grant access or supply store keys.
"""
from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID

from leaf_platform import project_lifecycle, project_web_page
from leaf_platform.portfolio_ios_template import MAX_ARCHIVE_BYTES


SCHEMA = 'leaf.project-preview-source.v1'
MAX_MANIFEST_BYTES = 2048
_FIELDS = frozenset(('schema', 'target', 'org_id', 'project_id', 'revision',
                     'document_sha256', 'archive_sha256', 'byte_size'))
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_PRIVATE_ERRORS = (project_lifecycle.LifecycleForbidden,
                   project_lifecycle.LifecycleUnavailable,
                   project_lifecycle.LifecycleConflict)


class SourceStoreUnavailable(RuntimeError):
    """Persisted source could not be published or verified."""


def _uuid(value):
    if isinstance(value, UUID):
        return str(value)
    if type(value) is not str:
        raise ValueError('Invalid source scope.')
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ValueError('Invalid source scope.') from None
    if canonical != value:
        raise ValueError('Invalid source scope.')
    return canonical


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError('Invalid source revision.')
    return value


def _digest(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise ValueError('Invalid source digest.')
    return value


def _descriptor(value, org_id, project_id):
    if type(value) is not dict or set(value) != _FIELDS:
        raise ValueError('Invalid source descriptor.')
    value = value.copy()
    if (type(value['schema']) is not str or value['schema'] != SCHEMA
            or type(value['target']) is not str or value['target'] != 'ios-simulator'
            or type(value['org_id']) is not str or type(value['project_id']) is not str
            or _uuid(value['org_id']) != org_id or _uuid(value['project_id']) != project_id):
        raise ValueError('Invalid source descriptor scope.')
    _revision(value['revision'])
    _digest(value['document_sha256'])
    _digest(value['archive_sha256'])
    if type(value['byte_size']) is not int or not 0 < value['byte_size'] <= MAX_ARCHIVE_BYTES:
        raise ValueError('Invalid source size.')
    return value


def _keys(descriptor):
    scope = (f"tenants/{descriptor['org_id']}/projects/{descriptor['project_id']}"
             '/previews/ios/source')
    return (f"{scope}/{descriptor['archive_sha256']}",
            f"{scope}/manifests/{descriptor['revision']}/{descriptor['document_sha256']}.json")


def _manifest(descriptor):
    return json.dumps(descriptor, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _page_identity(page, revision):
    if type(page) is not dict or type(page.get('revision')) is not int or page['revision'] != revision:
        raise ValueError('Saved source revision does not match.')
    return _digest(page.get('digest'))


def _read_persisted(backend, descriptor):
    archive_key, manifest_key = _keys(descriptor)
    manifest = backend.get(manifest_key)
    # Requiring the exact canonical serialization also rejects duplicate JSON
    # fields, unknown fields, and bool/int equality tricks without parsing them.
    if (type(manifest) is not bytes or not 0 < len(manifest) <= MAX_MANIFEST_BYTES
            or manifest != _manifest(descriptor)):
        raise ValueError('Source manifest integrity failure.')
    raw = backend.get(archive_key)
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES
            or len(raw) != descriptor['byte_size']
            or hashlib.sha256(raw).hexdigest() != descriptor['archive_sha256']):
        raise ValueError('Source archive integrity failure.')
    return raw


def prepare_source(org_id, project_id, actor_id, *, revision):
    """Capture the exact authorized saved revision through the existing exporter."""
    org_id, project_id, actor_id = map(_uuid, (org_id, project_id, actor_id))
    revision = _revision(revision)
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    saved = project_web_page.read(org_id, project_id, actor_id, revision=revision)
    digest = _page_identity(saved, revision)
    page, raw = project_web_page.export_source(org_id, project_id, actor_id,
                                              revision=revision, target='ios')
    if _page_identity(page, revision) != digest:
        raise ValueError('Exported source does not match the saved page.')
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
        raise ValueError('Invalid source archive size.')
    descriptor = _descriptor({
        'schema': SCHEMA, 'target': 'ios-simulator', 'org_id': org_id,
        'project_id': project_id, 'revision': revision, 'document_sha256': digest,
        'archive_sha256': hashlib.sha256(raw).hexdigest(), 'byte_size': len(raw),
    }, org_id, project_id)
    import write_loop
    try:
        backend = write_loop.upload_backend_for_tenant(org_id)
        archive_key, manifest_key = _keys(descriptor)
        backend.put_if_absent_or_verify(archive_key, raw)
        # Never remove an archive if manifest publication loses a conflict.
        backend.put_if_absent_or_verify(manifest_key, _manifest(descriptor))
        _read_persisted(backend, descriptor)
    except _PRIVATE_ERRORS:
        raise
    except Exception:
        raise SourceStoreUnavailable('Preview source storage is unavailable.') from None
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    return descriptor


def read_source(org_id, project_id, actor_id, descriptor):
    """Read verified persisted source under current saved-page authority."""
    org_id, project_id, actor_id = map(_uuid, (org_id, project_id, actor_id))
    descriptor = _descriptor(descriptor, org_id, project_id)
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    page = project_web_page.read(org_id, project_id, actor_id, revision=descriptor['revision'])
    if _page_identity(page, descriptor['revision']) != descriptor['document_sha256']:
        raise ValueError('Saved source digest does not match.')
    import write_loop
    try:
        backend = write_loop.upload_backend_for_tenant(org_id)
        raw = _read_persisted(backend, descriptor)
    except _PRIVATE_ERRORS:
        raise
    except Exception:
        raise SourceStoreUnavailable('Preview source storage is unavailable.') from None
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    return raw
