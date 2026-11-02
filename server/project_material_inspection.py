"""Private, active-turn photo inspection. Request witnesses never grant access."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import uuid

import deps
import platform_link
import project_material_images
import session_store

MAX_INPUT_BYTES = 4096
MAX_IMAGE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1536 * 1024
_HEX = re.compile(r'[0-9a-f]{64}')
_PUBLIC = ('material_id', 'name', 'media_type', 'byte_size', 'sha256',
           'preview_sha256', 'width', 'height', 'created_at')
_MESSAGES = {
    403: ('access_denied', 'Project material access denied.'),
    404: ('unavailable', 'Project material session or photo is unavailable.'),
    409: ('source_changed', 'Project material source changed.'),
    413: ('request_too_large', 'Project material request is too large.'),
    422: ('invalid_request', 'Invalid project material request or preview.'),
    503: ('service_unavailable', 'Project material service is unavailable.'),
}


class InspectionError(ValueError):
    def __init__(self, status):
        self.status = status
        self.code, message = _MESSAGES[status]
        super().__init__(message)


def _uuid(value):
    if type(value) is not str:
        raise ValueError()
    parsed = uuid.UUID(value)
    if not parsed.int or str(parsed) != value:
        raise ValueError()
    return parsed


def _digest(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _pair(revision, digest):
    return (type(revision) is int and 0 <= revision <= 2147483647
            and ((revision == 0 and digest is None) or (revision > 0 and _digest(digest))))


def parse_request(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise InspectionError(413)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        body = json.loads(raw, object_pairs_hook=unique)
        if type(body) is not dict:
            raise ValueError()
        op = body.get('op')
        witnesses = {'expected_revision', 'expected_digest'}
        if op == 'catalog':
            if set(body) - {'op', 'cursor', *witnesses}:
                raise ValueError()
            if 'cursor' in body:
                _uuid(body['cursor'])
                if not witnesses <= body.keys():
                    raise ValueError()
        elif op == 'preview':
            if set(body) != {'op', 'material_id', 'expected_preview_sha256', *witnesses}:
                raise ValueError()
            _uuid(body['material_id'])
            if not _digest(body['expected_preview_sha256']):
                raise ValueError()
        else:
            raise ValueError()
        if witnesses & body.keys():
            if not witnesses <= body.keys() or not _pair(body['expected_revision'], body['expected_digest']):
                raise ValueError()
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InspectionError(422) from None


def authority(tenant, session_id, authority_session_id, turn_id):
    if (authority_session_id != session_id or type(turn_id) is not str or not turn_id.strip()
            or not isinstance(tenant, deps.TenantContext) or tenant.verified_demo
            or (not tenant.backedge and not tenant.authority_resolved)):
        raise InspectionError(403)
    actor = deps.stage_author_identity(tenant, session_id, turn_id)
    if (actor is None or not actor.subject or actor.verified_demo
            or not actor.authority_resolved):
        raise InspectionError(403)
    binding = platform_link.resolve_caller_binding(actor)
    if binding is None:
        raise InspectionError(403)
    session = platform_link.require_project_session_access(
        session_store.get_session(session_id), actor, write=False, binding=binding)
    if session is None or session.get('active_turn_id') != turn_id:
        raise InspectionError(404)
    try:
        org, project = _uuid(session.get('org_id')), _uuid(session.get('project_id'))
        binding_id = _uuid(str(binding.binding_id))
        if str(org) != str(actor) or str(binding.platform_tenant_id) != str(org):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise InspectionError(403) from None
    return org, project, binding_id, actor.subject


def inspect(tenant, session_id, authority_session_id, turn_id, body):
    """Derive all storage selectors from current session authority, never args."""
    # Also close the input boundary for non-HTTP callers.
    body = parse_request(json.dumps(body, allow_nan=False).encode())
    scope = authority(tenant, session_id, authority_session_id, turn_id)
    platform_link._ensure_platform_package()
    from leaf_platform import project_materials, project_web_page, project_page_proposal

    def recheck():
        if authority(tenant, session_id, authority_session_id, turn_id) != scope:
            raise InspectionError(403)

    def read(call):
        recheck()
        value = copy.deepcopy(call())
        recheck()
        return value

    def page_source():
        page = read(lambda: project_web_page.read(*scope[:3]))
        if page is None:
            return {'project_id': str(scope[1]), 'revision': 0, 'digest': None}, {}
        try:
            document = project_page_proposal.validate_document(page['document'])
            if (not _pair(page['revision'], page['digest']) or page['revision'] == 0
                    or project_page_proposal.document_digest(document) != page['digest']):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise InspectionError(409) from None
        assignments = {}
        if document.get('schema') == 'leaf.portfolio.v1':
            for gallery in document['categories']:
                for index, photo in enumerate(gallery['images']):
                    assignments.setdefault(photo['material_id'], []).append((
                        photo['sha256'], photo['preview_sha256'], {
                            'gallery_slug': gallery['slug'], 'gallery_title': gallery['title'],
                            'index': index, 'hero': gallery['hero'] == index,
                            'caption': photo['title'], 'alt': photo['alt'],
                        }))
        return {'project_id': str(scope[1]), 'revision': page['revision'], 'digest': page['digest']}, assignments

    source, assignments = page_source()
    if 'expected_revision' in body and (
            body['expected_revision'], body['expected_digest']) != (source['revision'], source['digest']):
        raise InspectionError(409)

    def assigned(material):
        result = []
        for original, preview, assignment in assignments.get(material['material_id'], []):
            if (original, preview) != (material['sha256'], material['preview_sha256']):
                raise InspectionError(409)
            result.append(assignment)
        return result

    result = {'org_id': str(scope[0]), 'project_id': str(scope[1]),
              'session_id': session_id, 'turn_id': turn_id, 'source': source,
              'assignment_basis': 'saved_project_page' if source['revision'] else 'no_saved_page'}
    if body['op'] == 'catalog':
        batch = read(lambda: project_materials.list_materials(
            *scope[:3], cursor=body.get('cursor'), limit=50))
        rows = batch['materials']
        if len(rows) > 50:
            raise InspectionError(503)
        if batch['next_cursor'] is not None:
            _uuid(batch['next_cursor'])
        materials = []
        for row in rows:
            public = {key: row[key] for key in _PUBLIC}
            if any(type(value) not in (str, int, type(None)) for value in public.values()):
                raise InspectionError(503)
            public['assignments'] = assigned(row)
            materials.append(public)
        result.update(schema='leaf.project-material-catalog.v1', materials=materials,
                      next_cursor=batch['next_cursor'], page_count=len(materials))
    else:
        material = read(lambda: project_materials.get_material(*scope[:3], body['material_id']))
        if (material['material_id'] != body['material_id']
                or material['preview_sha256'] != body['expected_preview_sha256']):
            raise InspectionError(409)
        if material['preview_media_type'] != 'image/jpeg':
            raise InspectionError(422)
        if not _digest(material['sha256']) or not _digest(material['preview_sha256']):
            raise InspectionError(409)
        labels = assigned(material)
        raw = read(lambda: project_material_images.read_content(*scope[:2], material, 'preview'))
        if (type(raw) is not bytes or not 0 < len(raw) <= MAX_IMAGE_BYTES
                or not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9')):
            raise InspectionError(422)
        if hashlib.sha256(raw).hexdigest() != material['preview_sha256']:
            raise InspectionError(409)
        current = read(lambda: project_materials.get_material(*scope[:3], body['material_id']))
        if current != material:
            raise InspectionError(409)
        result.update(schema='leaf.project-material-image.v1', material_id=material['material_id'],
                      original_sha256=material['sha256'], preview_sha256=material['preview_sha256'],
                      media_type='image/jpeg', sha256=material['preview_sha256'], byte_size=len(raw),
                      data=base64.b64encode(raw).decode('ascii'), assignments=labels)
    if page_source()[0] != source:
        raise InspectionError(409)
    recheck()
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
        raise InspectionError(422)
    return result
