"""Trusted source producer for accepted Leaf Studio project campaigns."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid

import requests

import broker_client
import platform_link

CONTRACT = 'leaf.project-repository-source-initializer.v1'
BUNDLE_CONTRACT = 'leaf.project-repository-source-bundle.v1'
MAX_BUNDLE_BYTES = 67108864
_AUTHORITY = {'tenant_id', 'organization_id', 'project_id', 'repo_key'}
_RESPONSE = {'contract', 'request_digest', 'source_commit', 'source_tree',
             'seed_digest', 'replayed', 'writer_lease_id', 'writer_lease_generation'}


class SourceUnavailable(Exception):
    """The trusted source service cannot supply a verified result."""


class SourceConflict(Exception):
    """The immutable project source conflicts with the admitted authority."""


def _uuid(value):
    return (isinstance(value, str) and
            re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', value)
            is not None and str(uuid.UUID(value)) == value)


def _sha(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def _closed_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate response field')
        result[key] = value
    return result


def export_project_source_bundle(tenant_id, organization_id, project_id, source_commit, source_tree):
    """Read exact private Git bytes using only an existing server-owned mapping."""
    if (not all(_uuid(value) for value in (tenant_id, organization_id, project_id)) or
            not all(_sha(value, 40) for value in (source_commit, source_tree))):
        raise SourceConflict('source authority conflicts')
    base_url = os.environ.get('LEAF_AUTHOR_HARNESS_URL', '').strip().rstrip('/')
    headers = broker_client.harness_headers()
    if not base_url or not headers.get('X-Harness-Secret'):
        raise SourceUnavailable('source is unavailable')
    try:
        authority = platform_link.platform_store().resolve_project_repository_authority(
            tenant_id, organization_id, project_id)
    except ValueError:
        raise SourceConflict('source authority conflicts') from None
    except Exception:
        raise SourceUnavailable('source is unavailable') from None
    if (not isinstance(authority, dict) or set(authority) != _AUTHORITY or
            not all(_uuid(value) for value in authority.values()) or
            (authority['tenant_id'], authority['organization_id'], authority['project_id']) !=
            (tenant_id, organization_id, project_id)):
        raise SourceConflict('source authority conflicts')
    body = dict(authority, source_commit=source_commit, source_tree=source_tree)
    request_digest = hashlib.sha256(json.dumps(
        dict(body, contract=BUNDLE_CONTRACT), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    headers = dict(headers, **{'X-Tenant-Id': tenant_id, 'Content-Type': 'application/json',
                              'Accept-Encoding': 'identity'})
    response = None
    try:
        response = requests.post(base_url + '/internal/project-repository-source/export',
                                 data=json.dumps(body, separators=(',', ':')).encode(),
                                 headers=headers, timeout=(10, 30), stream=True, allow_redirects=False)
        if response.status_code == 409:
            raise SourceConflict('source authority conflicts')
        if response.status_code != 200:
            raise SourceUnavailable('source is unavailable')
        values = response.headers
        length = values.get('Content-Length', '')
        lease_id = values.get('X-Leaf-Lease-Id')
        generation = values.get('X-Leaf-Lease-Generation', '')
        bundle_hash = values.get('X-Leaf-Bundle-Sha256')
        if (values.get('Content-Type') != 'application/octet-stream' or
                values.get('Content-Encoding', 'identity').lower() != 'identity' or
                re.fullmatch(r'[1-9][0-9]{0,7}', length) is None or
                not 1 <= int(length) <= MAX_BUNDLE_BYTES or
                values.get('X-Leaf-Source-Contract') != BUNDLE_CONTRACT or
                values.get('X-Leaf-Request-Digest') != request_digest or
                values.get('X-Leaf-Source-Commit') != source_commit or
                values.get('X-Leaf-Source-Tree') != source_tree or
                not _sha(bundle_hash, 64) or not _uuid(lease_id) or
                re.fullmatch(r'[1-9][0-9]*', generation) is None):
            raise SourceUnavailable('source is unavailable')
        bundle = bytearray()
        digest = hashlib.sha256()
        for chunk in response.iter_content(chunk_size=65536):
            if len(bundle) + len(chunk) > min(MAX_BUNDLE_BYTES, int(length)):
                raise SourceUnavailable('source is unavailable')
            bundle.extend(chunk)
            digest.update(chunk)
        if len(bundle) != int(length) or digest.hexdigest() != bundle_hash:
            raise SourceUnavailable('source is unavailable')
        return dict(bundle=bytes(bundle), source_commit=source_commit, source_tree=source_tree,
                    bundle_sha256=bundle_hash, size_bytes=len(bundle),
                    lease_id=lease_id, lease_generation=generation)
    except (requests.RequestException, ValueError, TypeError):
        raise SourceUnavailable('source is unavailable') from None
    finally:
        if response is not None:
            response.close()


def initialize_project_source(tenant_id, organization_id, project_id, seed_document):
    """Resolve the server-owned key, then call the harness outside the DB transaction."""
    if not all(_uuid(value) for value in (tenant_id, organization_id, project_id)):
        raise SourceConflict('source authority conflicts')
    try:
        encoded = seed_document.encode('utf-8')
        if not 1 <= len(seed_document) <= 32768 or len(encoded) > 131072 or '\x00' in seed_document:
            raise ValueError()
    except (AttributeError, UnicodeError, ValueError):
        raise SourceConflict('source seed conflicts') from None
    base_url = os.environ.get('LEAF_AUTHOR_HARNESS_URL', '').strip().rstrip('/')
    headers = broker_client.harness_headers()
    if not base_url or not headers.get('X-Harness-Secret'):
        raise SourceUnavailable('source is unavailable')
    try:
        store = platform_link.platform_store()
        authority = store.ensure_project_repository_authority(tenant_id, organization_id, project_id)
    except ValueError:
        raise SourceConflict('source authority conflicts') from None
    except Exception:
        raise SourceUnavailable('source is unavailable') from None
    if (not isinstance(authority, dict) or set(authority) != _AUTHORITY or
            not all(_uuid(value) for value in authority.values()) or
            (authority['tenant_id'], authority['organization_id'], authority['project_id']) !=
            (tenant_id, organization_id, project_id)):
        raise SourceConflict('source authority conflicts')
    seed_digest = hashlib.sha256(encoded).hexdigest()
    canonical = dict(authority, contract=CONTRACT, seed_digest=seed_digest)
    request_digest = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
    body = dict(authority, seed_digest=seed_digest, seed_document=seed_document)
    headers = dict(headers, **{'X-Tenant-Id': tenant_id, 'Content-Type': 'application/json'})
    try:
        # Explicit UTF-8 avoids requests' default ASCII escaping exceeding the raw body cap.
        response = requests.post(base_url + '/internal/project-repository-source/initialize',
                                 data=json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                                 headers=headers, timeout=10)
        if response.status_code == 409:
            raise SourceConflict('source authority conflicts')
        if response.status_code != 200:
            raise SourceUnavailable('source is unavailable')
        result = response.json(object_pairs_hook=_closed_pairs)
    except (requests.RequestException, ValueError):
        raise SourceUnavailable('source is unavailable') from None
    if (not isinstance(result, dict) or set(result) != _RESPONSE or
            result['contract'] != CONTRACT or result['request_digest'] != request_digest or
            not _sha(result['source_commit'], 40) or not _sha(result['source_tree'], 40) or
            not _sha(result['seed_digest'], 64) or type(result['replayed']) is not bool or
            not _uuid(result['writer_lease_id']) or
            not isinstance(result['writer_lease_generation'], str) or
            re.fullmatch(r'[1-9][0-9]*', result['writer_lease_generation']) is None or
            (not result['replayed'] and result['seed_digest'] != seed_digest)):
        raise SourceUnavailable('source is unavailable')
    return {key: result[key] for key in ('source_commit', 'source_tree', 'seed_digest', 'replayed')}
