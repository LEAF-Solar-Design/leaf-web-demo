"""Project-authorized original references for the protected browser author port."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import uuid

import requests

import broker_client
import deps
import platform_link
import project_reference_materials as originals
import project_repository_source as source
from project_repository_edit_contract import parse_staged_receipt, receipt_digest_matches

MATERIAL_LIMIT = 8 * 1024 * 1024
WIRE_LIMIT = 12 * 1024 * 1024
_AUTHORITY = {'tenant_id', 'organization_id', 'project_id', 'repo_key'}
_ERRORS = {
    'invalid_request': (422, 'Invalid browser author request.'),
    'access_denied': (403, 'Project author access denied.'),
    'unavailable': (404, 'Project reference or source is unavailable.'),
    'source_changed': (409, 'Project reference or source changed.'),
    'cancelled': (409, 'Project author request was cancelled.'),
    'material_too_large': (413, 'Text and JSON references must total at most 8 MiB.'),
    'unsupported_material': (422, 'Browser author accepts original UTF-8 text and JSON; video is not consumed.'),
    'author_unavailable': (503, 'Project author service is unavailable.'),
}


class AuthoringError(ValueError):
    def __init__(self, code):
        self.code = code
        self.status, message = _ERRORS[code]
        super().__init__(message)


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value or not uuid.UUID(value).int:
        raise ValueError()
    return value


def _sha(value, size=64):
    return type(value) is str and re.fullmatch('[0-9a-f]{' + str(size) + '}', value) is not None


def _request(value):
    required = {'edit_id', 'requirements', 'idempotency_key', 'source_operation'}
    try:
        if type(value) is not dict or not required <= value.keys() or value.keys() - required - {'material_refs', 'expected_base_commit'}:
            raise ValueError()
        body = copy.deepcopy(value)
        _uuid(body['edit_id'])
        requirements = body['requirements']
        if type(requirements) is not str or not requirements.strip() or '\0' in requirements or len(requirements.encode('utf-8')) > 16384:
            raise ValueError()
        key = body['idempotency_key']
        if type(key) is not str or not 1 <= len(key) <= 200 or any(ord(c) < 32 for c in key):
            raise ValueError()
        operation = body['source_operation']
        base = body.get('expected_base_commit')
        if operation not in ('create', 'edit') or (base is not None and not _sha(base, 40)) or (operation == 'edit' and base is None):
            raise ValueError()
        refs = body.setdefault('material_refs', [])
        if type(refs) is not list or len(refs) > 8:
            raise ValueError()
        seen = set()
        for ref in refs:
            if type(ref) is not dict or set(ref) != {'material_id', 'sha256'} or not _sha(ref['sha256']):
                raise ValueError()
            material_id = _uuid(ref['material_id'])
            if material_id in seen:
                raise ValueError()
            seen.add(material_id)
        return body
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise AuthoringError('invalid_request') from None


def _scope(tenant, project_id):
    try:
        if not isinstance(tenant, deps.TenantContext):
            raise ValueError()
        if not isinstance(project_id, (str, uuid.UUID)):
            raise ValueError()
        project = _uuid(str(project_id))
        binding = platform_link.resolve_caller_binding(tenant)
        org = _uuid(platform_link.require_project_access(tenant, project, write=True, binding=binding))
        actor = _uuid(str(binding.binding_id))
        if str(binding.platform_tenant_id) != org or str(tenant) != org:
            raise ValueError()
        return org, project, actor
    except Exception:
        raise AuthoringError('access_denied') from None


def _repo(scope):
    try:
        authority = platform_link.platform_store().resolve_project_repository_authority(scope[0], scope[0], scope[1])
        if (type(authority) is not dict or set(authority) != _AUTHORITY
                or any(_uuid(value) != value for value in authority.values())
                or (authority['tenant_id'], authority['organization_id'], authority['project_id']) != (scope[0], scope[0], scope[1])):
            raise ValueError()
        return dict(authority)
    except Exception:
        raise AuthoringError('unavailable') from None


def _result(result, body):
    try:
        if type(result) is not dict or set(result) != {'witness', 'receipt', 'receiptDigest', 'version'}:
            raise ValueError()
        receipt = parse_staged_receipt(result['receipt'])
        if not receipt_digest_matches(receipt, result['receiptDigest']):
            raise ValueError()
        actual = receipt.to_mapping()
        for key in (*_AUTHORITY, 'edit_id', 'actor_binding_id', 'instruction_digest', 'idempotency_key'):
            if actual[key] != body[key]:
                raise ValueError()
        witness = result['witness']
        expected = {'base_commit': body['expected_base_commit'], 'staged_head_commit': receipt.staged_head_commit,
                    'staged_tree': receipt.staged_tree, 'private_ref': 'refs/leaf/changes/' + body['edit_id']}
        if (receipt.base_commit != body['expected_base_commit'] or receipt.operation != 'edit'
                or witness != expected or type(result['version']) is not int or result['version'] < 1):
            raise ValueError()
        return copy.deepcopy(result)
    except (ValueError, TypeError, KeyError):
        raise AuthoringError('source_changed') from None


def author(tenant, project_id, request, *, cancelled=None):
    """Stage complete immutable text/JSON assets; publication stays caller-owned."""
    request = _request(request)
    scope = _scope(tenant, project_id)

    def recheck():
        if cancelled is not None and cancelled():
            raise AuthoringError('cancelled')
        if _scope(tenant, project_id) != scope:
            raise AuthoringError('access_denied')

    platform_link._ensure_platform_package()
    from leaf_platform import project_reference_materials as metadata

    materials, total = [], 0
    for ref in request['material_refs']:
        recheck()
        try:
            row = metadata.get_material(*scope, ref['material_id'])
        except Exception:
            raise AuthoringError('unavailable') from None
        recheck()
        if (not isinstance(row, dict) or row.get('material_id') != ref['material_id']
                or row.get('org_id') != scope[0] or row.get('project_id') != scope[1]):
            raise AuthoringError('unavailable')
        if row.get('sha256') != ref['sha256']:
            raise AuthoringError('source_changed')
        if row.get('media_type') not in ('text/plain', 'application/json'):
            raise AuthoringError('unsupported_material')
        size = row.get('byte_size')
        if type(size) is not int or not 0 < size <= MATERIAL_LIMIT or total + size > MATERIAL_LIMIT:
            raise AuthoringError('material_too_large')
        total += size
        materials.append(copy.deepcopy(row))
    wire_material = []
    for row in materials:
        recheck()
        try:
            raw = originals.read_content(scope[0], scope[1], row)
        except Exception:
            raise AuthoringError('unavailable') from None
        recheck()
        if type(raw) is not bytes or len(raw) != row['byte_size'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise AuthoringError('source_changed')
        suffix = '.json' if row['media_type'] == 'application/json' else '.txt'
        wire_material.append({'path': row['material_id'] + suffix,
            'key': 'reference:' + scope[1] + ':' + row['material_id'] + ':' + row['sha256'],
            'sha256': row['sha256'], 'size_bytes': len(raw), 'product_b64': base64.b64encode(raw).decode('ascii')})
    recheck()
    if request['source_operation'] == 'edit':
        _repo(scope)  # Never initialize a missing edit target.
    try:
        initialized = source.initialize_project_source(scope[0], scope[0], scope[1], request['requirements'])
    except source.SourceConflict:
        raise AuthoringError('source_changed') from None
    except Exception:
        raise AuthoringError('author_unavailable') from None
    recheck()
    authority = _repo(scope)
    base = initialized.get('source_commit') if isinstance(initialized, dict) else None
    if not _sha(base, 40) or (request.get('expected_base_commit') is not None and request['expected_base_commit'] != base):
        raise AuthoringError('source_changed')
    body = dict(authority, edit_id=request['edit_id'], actor_binding_id=scope[2], expected_base_commit=base,
        instruction_digest=hashlib.sha256(request['requirements'].encode('utf-8')).hexdigest(),
        idempotency_key=request['idempotency_key'], commit_message='Author browser project',
        requirements=request['requirements'], material=wire_material,
        source_profile='web-next-static-v1', source_operation=request['source_operation'])
    encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > WIRE_LIMIT:
        raise AuthoringError('material_too_large')
    url = os.environ.get('LEAF_AUTHOR_HARNESS_URL', '').strip().rstrip('/')
    headers = broker_client.harness_headers()
    if not url or not headers.get('X-Harness-Secret'):
        raise AuthoringError('author_unavailable')
    headers = dict(headers, **{'X-Tenant-Id': scope[0], 'Content-Type': 'application/json'})
    recheck()
    if _repo(scope) != authority:
        raise AuthoringError('source_changed')
    response = None
    try:
        response = requests.post(url + '/internal/project-repository-source/author', data=encoded,
                                 headers=headers, timeout=(10, 240), allow_redirects=False)
        recheck()
        if _repo(scope) != authority:
            raise AuthoringError('source_changed')
        if response.status_code == 409:
            raise AuthoringError('source_changed')
        if response.status_code != 200:
            raise AuthoringError('author_unavailable')
        return _result(response.json(object_pairs_hook=source._closed_pairs), body)
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, AuthoringError):
            raise
        raise AuthoringError('author_unavailable') from None
    finally:
        if response is not None:
            response.close()
