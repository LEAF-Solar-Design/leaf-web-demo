"""Review-only assignments of authorized original-photo metadata; never saves."""
from __future__ import annotations

import copy
import json
import re

import platform_link
import project_material_inspection as inspection

MAX_INPUT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
_MESSAGES = {
    403: ('access_denied', 'Photo assignment access denied.'),
    404: ('unavailable', 'Photo assignment source is unavailable.'),
    409: ('source_changed', 'Photo assignment source changed.'),
    413: ('request_too_large', 'Photo assignment request is too large.'),
    422: ('invalid_request', 'Invalid photo assignment request.'),
    503: ('service_unavailable', 'Photo assignment service is unavailable.'),
}


class ProposalError(ValueError):
    def __init__(self, status):
        self.status = status
        self.code, message = _MESSAGES[status]
        super().__init__(message)


def parse_request(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise ProposalError(413)
    platform_link._ensure_platform_package()
    from leaf_platform.portfolio_page_template import text

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError()

    try:
        body = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=invalid_constant)
        if type(body) is not dict or set(body) != {'expected_revision', 'expected_digest', 'operations'}:
            raise ValueError()
        if (not inspection._pair(body['expected_revision'], body['expected_digest'])
                or body['expected_revision'] == 0):
            raise ValueError()
        operations = body['operations']
        if type(operations) is not list or not 1 <= len(operations) <= 200:
            raise ValueError()
        seen = set()
        for op in operations:
            if type(op) is not dict or set(op) != {
                    'op', 'material_id', 'sha256', 'preview_sha256', 'gallery_slug', 'title', 'alt'}:
                raise ValueError()
            if op['op'] != 'assign':
                raise ValueError()
            inspection._uuid(op['material_id'])
            if op['material_id'] in seen:
                raise ValueError()
            seen.add(op['material_id'])
            if not all(inspection._digest(op[key]) for key in ('sha256', 'preview_sha256')):
                raise ValueError()
            if type(op['gallery_slug']) is not str or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', op['gallery_slug']):
                raise ValueError()
            text(op['title'], 0, 200)
            text(op['alt'], 1, 400)
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProposalError(422) from None


def authority(tenant, session_id, authority_session_id, turn_id):
    try:
        scope = inspection.authority(tenant, session_id, authority_session_id, turn_id)
    except inspection.InspectionError as exc:
        raise ProposalError(exc.status) from None
    platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle
    project_lifecycle.require_project_role(*scope[:3], write=True)
    return scope


def propose(tenant, session_id, authority_session_id, turn_id, body):
    try:
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProposalError(422) from None
    body = parse_request(raw)
    scope = authority(tenant, session_id, authority_session_id, turn_id)
    from leaf_platform import project_materials, project_web_page
    from leaf_platform.portfolio_page_template import validate_document
    from leaf_platform.web_page_template import document_digest

    def recheck():
        if authority(tenant, session_id, authority_session_id, turn_id) != scope:
            raise ProposalError(403)

    def read(call):
        recheck()
        value = copy.deepcopy(call())
        recheck()
        return value

    def page_source():
        page = read(lambda: project_web_page.read(*scope[:3]))
        if page is None:
            raise ProposalError(404)
        try:
            document = validate_document(page['document'])
            if (not inspection._pair(page['revision'], page['digest']) or page['revision'] == 0
                    or document_digest(document) != page['digest']):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ProposalError(409) from None
        return {'project_id': str(scope[1]), 'revision': page['revision'], 'digest': page['digest']}, document

    source, document = page_source()
    if (source['revision'], source['digest']) != (body['expected_revision'], body['expected_digest']):
        raise ProposalError(409)
    candidate = copy.deepcopy(document)
    galleries = {gallery['slug']: gallery for gallery in candidate['categories']}
    assigned = {photo['material_id'] for gallery in candidate['categories'] for photo in gallery['images']}
    changes, witnesses = [], {}
    for op in body['operations']:
        gallery = galleries.get(op['gallery_slug'])
        if gallery is None or op['material_id'] in assigned:
            raise ProposalError(422)
        row = read(lambda: project_materials.get_material(*scope[:3], op['material_id']))
        if (row['material_id'] != op['material_id']
                or any(row[key] != op[key] for key in ('sha256', 'preview_sha256'))):
            raise ProposalError(409)
        if type(row['name']) is not str or not 1 <= len(row['name']) <= 1024:
            raise ProposalError(503)
        witnesses[row['material_id']] = row
        photo = {key: row[key] for key in ('material_id', 'sha256', 'preview_sha256')}
        photo.update(title=op['title'], alt=op['alt'])
        changes.append(dict(op='assign', material_id=row['material_id'], material_name=row['name'],
                            gallery_slug=gallery['slug'], gallery_title=gallery['title'],
                            index=len(gallery['images']), title=op['title'], alt=op['alt']))
        gallery['images'].append(photo)
    try:
        candidate = validate_document(candidate)
        digest = document_digest(candidate)
    except (ValueError, TypeError):
        raise ProposalError(422) from None
    for mid, witness in witnesses.items():
        if read(lambda: project_materials.get_material(*scope[:3], mid)) != witness:
            raise ProposalError(409)
    if page_source()[0] != source:
        raise ProposalError(409)
    result = dict(schema='leaf.project-photo-assignment-proposal.v1', source=source,
                  proposed_document=candidate, proposed_digest=digest, changes=changes)
    recheck()
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_RESPONSE_BYTES:
        raise ProposalError(422)
    recheck()
    return result
