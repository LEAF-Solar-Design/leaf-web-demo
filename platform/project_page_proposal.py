"""Review-only, saved-source portfolio proposals. This module never saves a page."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from time import monotonic
from uuid import uuid4

import requests

from . import project_lifecycle as lifecycle, project_web_page
from .web_page_template import document_digest, validate_document

MAX_INPUT_BYTES = 32 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
OPERATION_SECONDS = 40
CONNECT_SECONDS = 3
READ_SECONDS = 35
_USAGE_KEYS = {'input_tokens', 'output_tokens', 'cache_creation_tokens',
               'cache_read_tokens', 'cost_tokens'}
logger = logging.getLogger(__name__)
_DIAGNOSTICS = {
    'configuration': ('unavailable',),
    'transport': ('upstream_status', 'timeout', 'connection_error', 'request_error',
                  'deadline', 'content_encoding', 'response_size'),
    'response': ('invalid_bytes', 'invalid_json', 'invalid_schema', 'source_mismatch', 'invalid_edits'),
    'document': ('invalid_document',),
}


def _diagnostic(stage, code, upstream_status):
    # Return our own literals, never caller-owned strings or string subclasses.
    safe_stage, safe_code = 'response', 'invalid_schema'
    if type(stage) is str and type(code) is str:
        for known_stage, codes in _DIAGNOSTICS.items():
            if stage == known_stage:
                for known_code in codes:
                    if code == known_code:
                        safe_stage, safe_code = known_stage, known_code
    safe_status = upstream_status if type(upstream_status) is int and 100 <= upstream_status <= 599 else 0
    return safe_stage, safe_code, safe_status


class ProposalError(Exception):
    """Only fixed public messages; never retain provider bodies or exceptions."""

    def __init__(self, status, *, stage='response', code='invalid_schema', upstream_status=0):
        self.status = status
        self.stage, self.code, self.upstream_status = _diagnostic(stage, code, upstream_status)
        super().__init__({413: 'Portfolio proposal request is too large.',
                          422: 'Invalid portfolio proposal request.',
                          429: 'Portfolio proposal service is temporarily rate limited.',
                          502: 'Invalid portfolio proposal response.',
                          503: 'Portfolio proposal service is unavailable.'}[status])


def _closed(value, keys):
    return type(value) is dict and set(value) == set(keys.split())


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _json(raw, status):
    def reject_constant(_):
        raise ValueError('nonfinite number')

    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProposalError(status) from None


def parse_request(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise ProposalError(413)
    body = _json(raw, 422)
    if (not _closed(body, 'expected_revision expected_digest instruction')
            or type(body['expected_revision']) is not int
            or not 1 <= body['expected_revision'] <= 2147483647
            or type(body['expected_digest']) is not str
            or re.fullmatch('[0-9a-f]{64}', body['expected_digest']) is None
            or type(body['instruction']) is not str
            or not 1 <= len(body['instruction']) <= 4000 or not body['instruction'].strip()):
        raise ProposalError(422)
    # Reject escaped lone surrogates before encoding the trusted internal envelope.
    try:
        body['instruction'].encode('utf-8')
    except UnicodeError:
        raise ProposalError(422) from None
    return body


def _catalog(document):
    """Map opaque counters directly to copied containers, never execute id paths."""
    fields, targets = [], {}

    def add(container, key, limit):
        field_id = f'f{len(fields) + 1:04d}'
        fields.append({'id': field_id, 'value': container[key], 'max_length': limit})
        targets[field_id] = (container, key, limit)

    for key, limit in [('name', 120), ('headline', 240), ('introduction', 2400), ('about', 8000)]:
        add(document, key, limit)
    for key, limit in [('email', 254), ('location', 160)]:
        add(document['contact'], key, limit)
    for category in document['categories']:
        add(category, 'title', 120)
        add(category, 'description', 1200)
        for image in category['images']:
            add(image, 'title', 200)
            add(image, 'alt', 400)
    for section in document['resume']:
        add(section, 'heading', 120)
        for entry in section['entries']:
            for key, limit in [('title', 200), ('subtitle', 240), ('dates', 120), ('description', 2400)]:
                add(entry, key, limit)
    if len(fields) > 1024:
        raise ProposalError(422)
    return fields, targets


def _transport(payload):
    """One non-retrying request; the response is closed even on invalid output.

    The monotonic budget starts before connect and is checked after headers and
    between streamed bytes, including trickling streams. Blocking body reads
    have a 35-second idle read-timeout granularity; requests has no hard total
    deadline for DNS or header reads. No further body read starts after expiry.
    """
    import broker_client

    base = os.environ.get('LEAF_AUTHOR_HARNESS_URL', '').strip().rstrip('/')
    headers = broker_client.harness_headers()
    if not base or not headers.get('X-Harness-Secret'):
        raise ProposalError(503, stage='configuration', code='unavailable')
    headers = {**headers, 'X-Tenant-Id': payload['tenant_id'], 'Content-Type': 'application/json',
               'Accept-Encoding': 'identity'}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ProposalError(422)
    deadline = monotonic() + OPERATION_SECONDS
    response = None
    try:
        response = requests.post(base + '/internal/portfolio/proposals', data=encoded,
                                 headers=headers, timeout=(CONNECT_SECONDS, READ_SECONDS),
                                 allow_redirects=False, stream=True)
        if response.status_code == 429:
            raise ProposalError(429, stage='transport', code='upstream_status', upstream_status=429)
        if response.status_code != 200:
            raise ProposalError(502, stage='transport', code='upstream_status',
                                upstream_status=response.status_code)
        if monotonic() >= deadline:
            raise ProposalError(502, stage='transport', code='deadline', upstream_status=response.status_code)
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise ProposalError(502, stage='transport', code='content_encoding',
                                upstream_status=response.status_code)
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=1):
            if monotonic() >= deadline:
                raise ProposalError(502, stage='transport', code='deadline', upstream_status=response.status_code)
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ProposalError(502, stage='transport', code='response_size', upstream_status=response.status_code)
            raw.extend(chunk)
        if monotonic() >= deadline:
            raise ProposalError(502, stage='transport', code='deadline', upstream_status=response.status_code)
        return bytes(raw)
    except requests.Timeout:
        raise ProposalError(502, stage='transport', code='timeout',
                            upstream_status=response.status_code if response is not None else 0) from None
    except requests.ConnectionError:
        raise ProposalError(502, stage='transport', code='connection_error',
                            upstream_status=response.status_code if response is not None else 0) from None
    except requests.RequestException:
        raise ProposalError(502, stage='transport', code='request_error',
                            upstream_status=response.status_code if response is not None else 0) from None
    finally:
        if response is not None:
            response.close()


def _result(raw, source, targets):
    if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
        raise ProposalError(502, code='invalid_bytes')
    try:
        result = _json(raw, 502)
    except ProposalError:
        raise ProposalError(502, code='invalid_json') from None
    if (not _closed(result, 'source model edits usage')
            or not _closed(result['source'], 'project_id revision digest64hex')
            or type(result['source']['revision']) is not int
            or type(result['source']['project_id']) is not str
            or type(result['source']['digest64hex']) is not str
            or type(result['model']) is not str
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', result['model']) is None
            or type(result['usage']) is not dict or not set(result['usage']) <= _USAGE_KEYS
            or any(type(v) is not int or not 0 <= v <= 9007199254740991
                   for v in result['usage'].values())
            or type(result['edits']) is not list or len(result['edits']) > 32):
        raise ProposalError(502, code='invalid_schema')
    if result['source'] != source:
        raise ProposalError(502, code='source_mismatch')
    seen = set()
    for edit in result['edits']:
        if (not _closed(edit, 'id value') or type(edit['id']) is not str
                or edit['id'] not in targets or edit['id'] in seen
                or type(edit['value']) is not str or len(edit['value']) > targets[edit['id']][2]):
            raise ProposalError(502, code='invalid_edits')
        seen.add(edit['id'])
    return result


def propose(org_id, project_id, actor_id, *, expected_revision, expected_digest, instruction):
    correlation_id = str(uuid4())
    # Also enforce the closed input contract for direct service callers.
    body = {'expected_revision': expected_revision, 'expected_digest': expected_digest, 'instruction': instruction}
    try:
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise ProposalError(422) from None
    parse_request(raw)
    lifecycle.require_project_role(org_id, project_id, actor_id, write=True)
    page = project_web_page.read(org_id, project_id, actor_id)
    if page is None or page['document'].get('schema') != 'leaf.portfolio.v1':
        raise ProposalError(422)
    if page['revision'] != expected_revision or page['digest'] != expected_digest:
        raise lifecycle.LifecycleConflict('Portfolio proposal source changed.')
    document = copy.deepcopy(page['document'])
    fields, targets = _catalog(document)
    source = {'project_id': str(project_id), 'revision': page['revision'], 'digest64hex': page['digest']}
    payload = {'tenant_id': str(org_id), 'source': source, 'instruction': instruction, 'fields': fields}
    if len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_REQUEST_BYTES:
        raise ProposalError(422)
    # All page transactions have completed before the synchronous network call.
    try:
        result = _result(_transport(payload), source, targets)
        for edit in result['edits']:
            container, key, _ = targets[edit['id']]
            container[key] = edit['value']
        try:
            document = validate_document(document)
            digest = document_digest(document)
        except (ValueError, UnicodeError):
            raise ProposalError(502, stage='document', code='invalid_document') from None
    except ProposalError as error:
        stage, code, upstream_status = _diagnostic(error.stage, error.code, error.upstream_status)
        try:
            logger.warning('portfolio_proposal_failed correlation_id=%s stage=%s code=%s upstream_status=%s',
                           correlation_id, stage, code, upstream_status)
        except Exception:
            # A failing logging handler must not change the fixed service result.
            pass
        raise
    lifecycle.require_project_role(org_id, project_id, actor_id, write=True)
    latest = project_web_page.read(org_id, project_id, actor_id)
    if latest is None or latest['revision'] != page['revision'] or latest['digest'] != page['digest']:
        raise lifecycle.LifecycleConflict('Portfolio proposal source changed.')
    return {'source': {'project_id': str(project_id), 'revision': page['revision'], 'digest': page['digest']},
            'proposed_document': document, 'proposed_digest': digest,
            'edits': result['edits'], 'model': result['model'], 'usage': result['usage']}
