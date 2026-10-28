"""Project-scoped remote preview protocol; importing installs no worker binding.

Receipts are authenticated worker reports, not independent remote attestation or
proof of all app interactions. Immutable orphan evidence never grants acceptance.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import os
import re
import struct
import time
import uuid

import native_preview_job as native
from leaf_platform import project_lifecycle as lifecycle

MAX_PNG = 16 * 1024 * 1024
MAX_RECEIPT = 16 * 1024
MAX_ENVELOPE = 4 * ((MAX_PNG + 2) // 3) + MAX_RECEIPT + 1024
RECEIPT_SCHEMA = 'leaf.native-preview-worker-receipt.v1'
_SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_OWNER = object()


class ProtocolError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def invalid():
    raise ProtocolError(422, 'Invalid native preview request.')


def conflict():
    raise ProtocolError(409, 'Native preview attempt is no longer current.')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            invalid()
        result[key] = value
    return result


def parse(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda value: invalid())
    except (ValueError, UnicodeError, RecursionError):
        invalid()


def validate_binding(value):
    if type(value) is not dict or set(value) != {'org_id', 'project_id', 'actor_id', 'worker_id'}:
        invalid()
    for key in ('org_id', 'project_id', 'actor_id'):
        native.source._uuid(value[key])
    if type(value['worker_id']) is not str or not _SAFE.fullmatch(value['worker_id']):
        invalid()
    return value.copy()


def bindings():
    raw = os.environ.get('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS', '')
    if not raw:
        raise ProtocolError(503, 'Native preview workers are unavailable.')
    try:
        if len(raw.encode('utf-8')) > 65536:
            invalid()
        values = parse(raw)
        if type(values) is not list or not 1 <= len(values) <= 128:
            invalid()
        result = [validate_binding(value) for value in values]
        # One allocator identity per project, and no reused worker identifiers.
        if (len({(v['org_id'], v['project_id']) for v in result}) != len(result)
                or len({v['worker_id'] for v in result}) != len(result)):
            invalid()
        return result
    except (ValueError, TypeError, OverflowError):
        raise ProtocolError(503, 'Native preview workers are unavailable.') from None


def binding_for(org, project, actor=None):
    for binding in bindings():
        if binding['org_id'] == org and binding['project_id'] == project:
            if actor is not None and actor != binding['actor_id']:
                raise ProtocolError(403, 'Native preview worker access denied.')
            return binding
    raise ProtocolError(503 if actor is None else 403, 'Native preview worker is unavailable.')


def authorize(org, project, actor, *, write):
    org, project, actor = map(native.source._uuid, (org, project, actor))
    lifecycle.require_project_role(org, project, actor, write=write)
    return org, project, actor


def ready():
    import jobs
    if jobs.job_store_mode() != 'postgres':
        raise ProtocolError(503, 'Native preview storage is unavailable.')


def submit(org, project, actor, body, key):
    import jobs
    org, project, actor = authorize(org, project, actor, write=True)
    ready()
    binding = binding_for(org, project)
    lifecycle.require_project_role(org, project, binding['actor_id'], write=True)
    if type(body) is not dict or set(body) != {'revision'}:
        invalid()
    revision = native.source._revision(body['revision'])
    if (type(key) is not str or not 1 <= len(key) <= 200
            or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        invalid()
    budget = jobs.job_max_s()
    if not math.isfinite(budget) or not 0 < budget <= 3600:
        raise ProtocolError(503, 'Native preview budget is unavailable.')
    descriptor = native.source.prepare_source(org, project, actor, revision=revision)
    # Publication can block: check current authority and configuration again.
    authorize(org, project, actor, write=True)
    if binding_for(org, project) != binding:
        conflict()
    lifecycle.require_project_role(org, project, binding['actor_id'], write=True)
    context = native.validate_context({
        'schema': native.REMOTE_SCHEMA, 'tenant_id': org, 'org_id': org,
        'project_id': project, 'actor_id': actor, 'source': descriptor,
        'dispatch_kind': 'remote-worker', 'worker_binding': binding, 'budget_s': budget})
    try:
        jid = jobs._submit_job(org, {}, {}, '', False, org, project,
                               idempotency_key=key, native_preview=context)
    except ValueError as exc:
        if 'different run input' in str(exc):
            raise ProtocolError(409, 'Idempotency key conflicts with an existing preview.') from None
        raise
    return status(org, project, actor, jid)


def record(org, project, jid):
    import jobs
    ready()
    native.source._uuid(jid)
    rec = jobs.get_job(jid)
    context = (rec or {}).get('native_preview')
    if (not rec or rec.get('tenant_id') != org or rec.get('org_id') != org
            or rec.get('project_id') != project or not context
            or context.get('schema') != native.REMOTE_SCHEMA):
        raise ProtocolError(404, 'Native preview not found.')
    context = native.validate_context(context)
    if any(context[k] != v for k, v in {'org_id': org, 'project_id': project, 'tenant_id': org}.items()):
        raise ProtocolError(404, 'Native preview not found.')
    return rec, context


def _current_binding(context, actor):
    binding = binding_for(context['org_id'], context['project_id'], actor)
    if binding != context['worker_binding']:
        conflict()
    authorize(context['org_id'], context['project_id'], actor, write=True)
    authorize(context['org_id'], context['project_id'], context['actor_id'], write=True)


def deadline(rec, context):
    start = rec.get('started_at')
    if type(start) not in (int, float) or not math.isfinite(start):
        conflict()
    return start + context['budget_s']


def identity(body, *, extra=()):
    if type(body) is not dict or set(body) != {'lease_owner', 'attempt'} | set(extra):
        invalid()
    owner, attempt = body['lease_owner'], body['attempt']
    native.source._uuid(owner)
    if type(attempt) is not int or attempt < 1:
        invalid()
    return owner, attempt


def live(org, project, actor, jid, owner, attempt):
    rec, context = record(org, project, jid)
    _current_binding(context, actor)
    lease = rec.get('lease') or {}
    now = time.time()
    if (rec['status'] != 'running' or rec.get('progress') == 'closed'
            or rec.get('attempt') != attempt or lease.get('owner') != owner
            or type(lease.get('expires_at')) not in (int, float)
            or not math.isfinite(lease['expires_at']) or lease['expires_at'] <= now
            or deadline(rec, context) <= now):
        conflict()
    return rec, context


def claim(org, project, actor, body):
    import jobs
    org, project, actor = authorize(org, project, actor, write=True)
    if type(body) is not dict or body:
        invalid()
    ready()
    binding = binding_for(org, project, actor)
    for candidate in jobs._pg_store.native_remote_candidates(binding, time.time(), jobs.max_attempts()):
        jid = candidate['job_id']
        rec, context = record(org, project, jid)
        _current_binding(context, actor)
        if rec['attempt'] >= jobs.max_attempts():
            continue
        if rec.get('started_at') is not None and deadline(rec, context) <= time.time():
            continue
        native.read_source(context)
        _current_binding(context, actor)
        owner = str(uuid.uuid4())
        attempt = jobs.claim_lease(jid, owner)
        if attempt is None:
            continue
        rec, context = live(org, project, actor, jid, owner, attempt)
        return {'job': {'job_id': jid, 'lease_owner': owner, 'attempt': attempt,
                        'descriptor': copy.deepcopy(context['source']),
                        'deadline': deadline(rec, context)}}
    return {'job': None}


def source_bytes(org, project, actor, jid, body):
    org, project, actor = authorize(org, project, actor, write=True)
    owner, attempt = identity(body)
    _, context = live(org, project, actor, jid, owner, attempt)
    raw = native.read_source(context)
    live(org, project, actor, jid, owner, attempt)
    return raw


def heartbeat(org, project, actor, jid, body):
    import jobs
    org, project, actor = authorize(org, project, actor, write=True)
    owner, attempt = identity(body)
    rec, context = live(org, project, actor, jid, owner, attempt)
    native.read_source(context)
    rec, context = live(org, project, actor, jid, owner, attempt)
    now = time.time()
    if not jobs._pg_store.heartbeat(jid, owner, now,
            min(now + jobs.lease_duration_s(), deadline(rec, context)), None, attempt=attempt):
        conflict()
    rec, context = live(org, project, actor, jid, owner, attempt)
    return {'job_id': jid, 'lease_owner': owner, 'attempt': attempt,
            'deadline': deadline(rec, context)}


def png(raw):
    if type(raw) is not bytes:
        invalid()
    if len(raw) > MAX_PNG:
        raise ProtocolError(413, 'Native preview evidence is too large.')
    if (len(raw) < 33 or raw[:8] != b'\x89PNG\r\n\x1a\n'
            or raw[8:16] != b'\x00\x00\x00\x0dIHDR'):
        invalid()
    width, height = struct.unpack('>II', raw[16:24])
    if not 0 < width <= 16384 or not 0 < height <= 16384 or width * height > 67108864:
        invalid()
    if binascii.crc32(raw[12:29]) & 0xffffffff != struct.unpack('>I', raw[29:33])[0]:
        invalid()
    return raw


def receipt(value, jid, owner, attempt, context):
    expected = {'schema': RECEIPT_SCHEMA, 'job_id': jid, 'lease_owner': owner,
                'attempt': attempt, 'archive_sha256': context['source']['archive_sha256'],
                'worker_binding': context['worker_binding'], 'target': 'ios-simulator',
                'build_exit_code': 0, 'install_exit_code': 0, 'launch_exit_code': 0,
                'cleanup_complete': True, 'allocator_release_confirmed': True}
    extra = {'simulator_udid', 'simulator_runtime', 'bundle_id', 'allocator_ticket', 'screenshot_sha256'}
    if (type(value) is not dict or set(value) != set(expected) | extra
            or any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())):
        invalid()
    if len(canonical(value)) > MAX_RECEIPT:
        raise ProtocolError(413, 'Native preview receipt is too large.')
    native.source._uuid(value['simulator_udid'])
    native.source._digest(value['screenshot_sha256'])
    validate_binding(value['worker_binding'])
    for key in ('simulator_runtime', 'bundle_id', 'allocator_ticket'):
        if type(value[key]) is not str or not _SAFE.fullmatch(value[key]):
            invalid()
    return copy.deepcopy(value)


def _keys(context, jid, attempt, receipt_digest, screenshot_digest):
    prefix = (f"tenants/{context['org_id']}/projects/{context['project_id']}"
              f'/previews/ios/jobs/{jid}/attempts/{attempt}')
    return f'{prefix}/receipts/{receipt_digest}.json', f'{prefix}/screenshots/{screenshot_digest}.png'


def _backend(context):
    import write_loop
    return write_loop.upload_backend_for_tenant(context['org_id'])


def read_evidence(context, jid, result):
    result = native.validate_result(context, jid, result['worker_id'], result['attempt'], result)
    if result['receipt']['kind'] != 'simulator':
        invalid()
    receipt_key, _ = _keys(context, jid, result['attempt'], result['receipt']['sha256'], 'unused')
    backend = _backend(context)
    raw = backend.get(receipt_key)
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_RECEIPT
            or hashlib.sha256(raw).hexdigest() != result['receipt']['sha256']):
        invalid()
    value = receipt(parse(raw), jid, result['worker_id'], result['attempt'], context)
    if canonical(value) != raw:
        invalid()
    _, screenshot_key = _keys(context, jid, result['attempt'], result['receipt']['sha256'],
                              value['screenshot_sha256'])
    image = png(backend.get(screenshot_key))
    if hashlib.sha256(image).hexdigest() != value['screenshot_sha256']:
        invalid()
    return image


def verify_terminal(jid, owner, attempt, result):
    """Private adapter seam: recheck custody, evidence and fences after reads."""
    import jobs
    durable = jobs._pg_store.durable_context(jid)
    context = native.record_context(durable['execution'], durable)
    org, project = context['org_id'], context['project_id']
    actor = context['worker_binding']['actor_id']
    native.validate_result(context, jid, owner, attempt, result)
    live(org, project, actor, jid, owner, attempt)
    read_evidence(context, jid, result)
    native.read_source(context)
    live(org, project, actor, jid, owner, attempt)


def complete(org, project, actor, jid, body):
    import jobs
    org, project, actor = authorize(org, project, actor, write=True)
    if type(body) is not dict or type(body.get('outcome')) is not str:
        invalid()
    failure = body['outcome'] == 'failed'
    if not failure and body['outcome'] != 'complete':
        invalid()
    owner, attempt = identity(body, extra=('outcome',) if failure else ('outcome', 'receipt', 'screenshot_base64'))
    _, context = live(org, project, actor, jid, owner, attempt)
    provenance = {'attempt': attempt, 'execution_path': 'native-worker'}
    if failure:
        native.read_source(context)
        live(org, project, actor, jid, owner, attempt)
        outcome = jobs.complete_callback(jid, 'failed', worker_id=owner, provenance=provenance,
            error={'error_code': 'INTERNAL', 'message': 'Native preview worker reported failure.', 'retryable': False},
            _native_owner=_OWNER)
    else:
        value = receipt(body['receipt'], jid, owner, attempt, context)
        encoded = body['screenshot_base64']
        if type(encoded) is not str:
            invalid()
        if len(encoded) > 4 * ((MAX_PNG + 2) // 3):
            raise ProtocolError(413, 'Native preview evidence is too large.')
        try:
            image = png(base64.b64decode(encoded, validate=True))
        except ProtocolError:
            raise
        except (ValueError, binascii.Error):
            invalid()
        if hashlib.sha256(image).hexdigest() != value['screenshot_sha256']:
            invalid()
        raw = canonical(value)
        digest = hashlib.sha256(raw).hexdigest()
        rkey, skey = _keys(context, jid, attempt, digest, value['screenshot_sha256'])
        backend = _backend(context)
        backend.put_if_absent_or_verify(skey, image)
        backend.put_if_absent_or_verify(rkey, raw)
        result = {'schema': native.RESULT_SCHEMA, 'target': 'ios-simulator',
                  'job_id': jid, 'worker_id': owner, 'attempt': attempt,
                  'archive_sha256': context['source']['archive_sha256'],
                  'receipt': {'kind': 'simulator', 'receipt_id': str(uuid.uuid4()), 'sha256': digest}}
        verify_terminal(jid, owner, attempt, result)
        outcome = jobs.complete_callback(jid, 'complete', worker_id=owner,
            provenance=provenance, result_env={'ok': True, 'result': result}, _native_owner=_OWNER)
    if outcome != 'applied':
        conflict()
    return {'job_id': jid, 'status': 'failed' if failure else 'complete'}


def status(org, project, actor, jid):
    org, project, actor = authorize(org, project, actor, write=False)
    rec, context = record(org, project, jid)
    public = {'job_id': jid, 'status': 'queued' if rec['status'] == 'submitted' else rec['status'],
              'revision': context['source']['revision'],
              'document_sha256': context['source']['document_sha256'],
              'archive_sha256': context['source']['archive_sha256']}
    if rec.get('progress') == 'closed':
        public['status'] = 'cancelled'
    if public['status'] == 'complete':
        result = rec['result']['result']
        _accepted(rec, context, jid, result)
        read_evidence(context, jid, result)
        public['receipt'] = copy.deepcopy(result['receipt'])
        public['evidence_basis'] = 'authenticated-worker-report'
    authorize(org, project, actor, write=False)
    return public


def screenshot(org, project, actor, jid):
    org, project, actor = authorize(org, project, actor, write=False)
    rec, context = record(org, project, jid)
    if rec['status'] != 'complete' or rec.get('progress') == 'closed':
        raise ProtocolError(404, 'Native preview screenshot not found.')
    _accepted(rec, context, jid, rec['result']['result'])
    image = read_evidence(context, jid, rec['result']['result'])
    authorize(org, project, actor, write=False)
    return image


def _accepted(rec, context, jid, result):
    if (rec['result'].get('ok') is not True
            or rec.get('provenance') != {'attempt': rec['attempt'], 'execution_path': 'native-worker'}):
        invalid()
    native.validate_result(context, jid, result['worker_id'], rec['attempt'], result)
