"""Browser source transactions over the shared intent and repository owners."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import requests

import broker_client
import browser_project_authoring as authoring
import deps
import platform_link
import project_material_inspection as inspection
import session_store
import turn_runner
from project_repository_edit_contract import (
    CONFIRMATION_CONTRACT, parse_confirmation, parse_staged_receipt,
    receipt_digest_matches, require_confirmation_binding,
)

WEB_PROFILE = authoring.WEB_PROFILE
NATIVE_PROFILE = authoring.NATIVE_PROFILE


def _profile(value):
    if value not in (WEB_PROFILE, NATIVE_PROFILE):
        raise FlowError(422)
    return value


MAX_INPUT_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
AUTHORITY_FIELDS = ('tenant_id', 'organization_id', 'project_id', 'repo_key')
_ERRORS = {
    403: ('access_denied', 'Project source access denied.'),
    404: ('unavailable', 'Project source is unavailable.'),
    409: ('source_changed', 'Project source changed or its outcome is unknown.'),
    413: ('request_too_large', 'Project source request is too large.'),
    422: ('invalid_request', 'Invalid project source request.'),
    503: ('service_unavailable', 'Project source service is unavailable or busy.'),
}


class FlowError(ValueError):
    def __init__(self, status):
        self.status = status
        self.code, message = _ERRORS[status]
        super().__init__(message)


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value or not uuid.UUID(value).int:
        raise ValueError()
    return value


def _sha(value, size=64):
    return type(value) is str and re.fullmatch('[0-9a-f]{' + str(size) + '}', value) is not None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def parse_request(raw, action, *, source_profile=WEB_PROFILE):
    _profile(source_profile)
    if len(raw) > MAX_INPUT_BYTES:
        raise FlowError(413)
    try:
        body = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_nonfinite)
        if type(body) is not dict:
            raise ValueError()
        if action == 'request':
            fields = {'source_request_id', 'requirements', 'source_operation', 'material_refs'}
            if not fields <= body.keys() or body.keys() - fields - {'expected_base_commit'}:
                raise ValueError()
            _uuid(body['source_request_id'])
            # Reuse the adapter's exact requirements/reference validation.
            authoring._request(dict(body_to_author(body), idempotency_key='validate'), source_profile=source_profile)
        else:
            fields = {'receipt_digest', 'expected_version'}
            if action == 'confirm':
                fields.add('confirmation_id')
                _uuid(body.get('confirmation_id'))
            elif action not in ('recover', 'reject'):
                raise ValueError()
            if (set(body) != fields or not _sha(body['receipt_digest'])
                    or type(body['expected_version']) is not int or not 1 <= body['expected_version'] <= 2147483647):
                raise ValueError()
        return body
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise FlowError(422) from None


def body_to_author(body):
    return {('edit_id' if key == 'source_request_id' else key): value for key, value in body.items()}


def _store():
    platform_link._ensure_platform_package()
    from leaf_platform import repository_edit_store
    return repository_edit_store


def scope(tenant, project_id, *, write=False):
    try:
        _uuid(project_id)
        if (not isinstance(tenant, deps.TenantContext) or tenant.verified_demo
                or not tenant.subject or not tenant.authority_resolved):
            raise ValueError()
        binding = platform_link.resolve_caller_binding(tenant)
        org = platform_link.require_project_access(tenant, project_id, write=write, binding=binding)
        if org != str(tenant) or str(binding.platform_tenant_id) != org:
            raise ValueError()
        return (_uuid(org), project_id, _uuid(str(binding.binding_id)))
    except Exception:
        raise FlowError(403) from None


def repository(current):
    try:
        row = platform_link.platform_store().resolve_project_repository_authority(current[0], current[0], current[1])
        if row is None:
            return None
        if type(row) is not dict or set(row) != set(AUTHORITY_FIELDS):
            raise ValueError()
        for value in row.values():
            _uuid(value)
        if tuple(row[key] for key in AUTHORITY_FIELDS[:3]) != (current[0], current[0], current[1]):
            raise ValueError()
        return row
    except Exception:
        raise FlowError(503) from None


def _read(authority, edit_id):
    return _store().get_edit(*(authority[key] for key in AUTHORITY_FIELDS), edit_id)


def _receipt(row, authority):
    try:
        receipt = parse_staged_receipt(row['receipt_json'])
        if not receipt_digest_matches(receipt, row['receipt_digest']):
            raise ValueError()
        frozen = receipt.to_mapping()
        fields = (*AUTHORITY_FIELDS, 'edit_id', 'actor_binding_id', 'writer_lease_id',
                  'writer_lease_generation', 'base_commit', 'staged_head_commit', 'staged_tree',
                  'instruction_digest', 'idempotency_key', 'operation')
        if any(row[key] != frozen[key] for key in fields):
            raise ValueError()
        if any(frozen[key] != authority[key] for key in AUTHORITY_FIELDS):
            raise ValueError()
        if type(row['version']) is not int or row['version'] < 1:
            raise ValueError()
        if row['state'] == 'published' and (
                row['observed_main_commit'] != receipt.staged_head_commit
                or row['observed_main_tree'] != receipt.staged_tree
                or row['observed_private_ref_commit'] != receipt.staged_head_commit):
            raise ValueError()
        return receipt
    except (ValueError, TypeError, KeyError):
        raise FlowError(409) from None


def representation(row, authority, *, source_profile=WEB_PROFILE):
    receipt = _receipt(row, authority)
    if row['state'] not in ('staged', 'awaiting_confirmation', 'publishing', 'published',
                            'rejected', 'conflicted', 'failed', 'superseded', 'rolled_back'):
        raise FlowError(409)
    def timestamp(key):
        value = row[key]
        if isinstance(value, datetime):
            return value.isoformat()
        if type(value) is not str:
            raise FlowError(503)
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value
    return dict(schema=('leaf.native-project-source.v1' if source_profile == NATIVE_PROFILE else 'leaf.browser-project-source.v1'), project_id=authority['project_id'],
                source_request_id=row['edit_id'], state=row['state'], version=row['version'],
                receipt_digest=row['receipt_digest'], base_commit=receipt.base_commit,
                source_commit=receipt.staged_head_commit, source_tree=receipt.staged_tree,
                changed_paths=list(receipt.changed_paths), created_at=timestamp('created_at'),
                updated_at=timestamp('updated_at'))


def read(tenant, project_id, edit_id=None, *, source_profile=WEB_PROFILE):
    _profile(source_profile)
    current = scope(tenant, project_id)
    authority = repository(current)
    if edit_id is not None:
        try:
            _uuid(edit_id)
        except (ValueError, TypeError):
            raise FlowError(404) from None
        row = _read(authority, edit_id) if authority else None
        if row is None:
            raise FlowError(404)
        _membership(row, authority, current, source_profile)
        result = representation(row, authority, source_profile=source_profile)
    else:
        rows = _store().list_edits(*(authority[key] for key in AUTHORITY_FIELDS), limit=20) if authority else []
        if len(rows) > 20:
            raise FlowError(503)
        result = []
        for row in rows:
            if row.get('idempotency_key', '').startswith('native-source:') != (source_profile == NATIVE_PROFILE):
                continue
            _membership(row, authority, current, source_profile)
            result.append(representation(row, authority, source_profile=source_profile))
    if scope(tenant, project_id) != current or repository(current) != authority:
        raise FlowError(403)
    return result


def _hint(action, edit_id, body, *, source_profile=WEB_PROFILE):
    _profile(source_profile)
    value = dict(source='author_panel', purpose='stage_authority', browser_action=action,
                 source_request_id=edit_id)
    if source_profile == NATIVE_PROFILE:
        value['source_profile'] = source_profile
    if action in ('create', 'edit'):
        value['material_refs'] = body['material_refs']
        if 'expected_base_commit' in body:
            value['expected_base_commit'] = body['expected_base_commit']
    else:
        value.update(body)
    return value


def authority(tenant, project_id, session_id, turn_id, action, edit_id, body, *, source_profile=WEB_PROFILE):
    _profile(source_profile)
    current = scope(tenant, project_id, write=True)
    try:
        held = inspection.authority(tenant, session_id, session_id, turn_id)
        if tuple(map(str, held[:3])) != current:
            raise ValueError()
        started = session_store.turn_started_data(session_id, turn_id, current[0])
        if (not started or started.get('stage_authority') is not True
                or started.get('classifier_hint') != _hint(action, edit_id, body, source_profile=source_profile)
                or (action in ('create', 'edit') and started.get('text') != body['requirements'])):
            raise ValueError()
        return current, held[3]
    except Exception:
        raise FlowError(403) from None


def _claim(session_id, turn_id, current, action, edit_id, digest, recheck, *, source_profile=WEB_PROFILE, body=None):
    """Snapshot FIRST, then a provably complete bounded current-turn tail."""
    _profile(source_profile)
    marker = dict(action=action, source_request_id=edit_id, payload_digest=digest)
    if source_profile == NATIVE_PROFILE:
        marker.update(source_profile=source_profile, request=body, authority=list(current))
    for _ in range(3):
        recheck()
        snapshot = session_store.get_session(session_id)
        if not snapshot or snapshot.get('active_turn_id') != turn_id:
            raise FlowError(403)
        pinned = snapshot['last_seq']
        events = session_store.recent_events(session_id, 64)
        # No missing marker in a truncated or discontinuous tail grants dispatch.
        starts = [i for i, event in enumerate(events) if event['turn_id'] == turn_id
                  and event['type'] == 'turn_started' and event['seq'] <= pinned]
        if len(starts) != 1:
            raise FlowError(503)
        tail = [event for event in events[starts[0]:] if event['seq'] <= pinned]
        if not tail or [event['seq'] for event in tail] != list(range(tail[0]['seq'], pinned + 1)):
            raise FlowError(503)
        markers = [event['data'] for event in tail if event['turn_id'] == turn_id
                   and event['type'] == 'browser_source_dispatch']
        if markers:
            if len(markers) != 1 or markers[0] != marker:
                raise FlowError(409)
            return False
        try:
            session_store.append_event(session_id, turn_id, 'browser_source_dispatch', marker,
                                       expected_seq=pinned + 1)
            return True
        except RuntimeError:
            # Includes dual-write migration modes: never bypass public routing.
            continue
    raise FlowError(503)


def _request_digest(body, source_profile):
    value = dict(source_profile=source_profile, payload=body) if source_profile == NATIVE_PROFILE else body
    return hashlib.sha256(canonical(value)).hexdigest()


def _request_key(body, source_profile, session_id=None, turn_id=None):
    if source_profile == WEB_PROFILE:
        return 'browser-source:' + _request_digest(body, source_profile)
    try:
        _uuid(session_id)
        _uuid(turn_id)
    except (ValueError, TypeError):
        raise FlowError(422) from None
    return 'native-source:' + session_id + ':' + turn_id + ':' + _request_digest(body, source_profile)


def _membership(row, bound, current, source_profile):
    """Bind a profile-less producer receipt to its original durable request/turn."""
    key = row.get('idempotency_key', '')
    if source_profile == WEB_PROFILE:
        if key.startswith('native-source:'):
            raise FlowError(409)
        return
    try:
        namespace, sid, tid, digest = key.split(':')
        if namespace != 'native-source' or not _sha(digest):
            raise ValueError()
        _uuid(sid)
        _uuid(tid)
        snapshot = session_store.get_session(sid)
        pinned = snapshot['last_seq']
        if type(pinned) is not int or not 0 < pinned <= 10000:
            raise ValueError()
        events = [e for e in session_store.events_after(sid, 0, 10000) if e['seq'] <= pinned]
        if [e['seq'] for e in events] != list(range(1, pinned + 1)):
            raise ValueError()
        markers = [e['data'] for e in events if e['turn_id'] == tid and e['type'] == 'browser_source_dispatch']
        if len(markers) != 1:
            raise ValueError()
        marker = markers[0]
        body = parse_request(canonical(marker['request']), 'request', source_profile=NATIVE_PROFILE)
        operation = body['source_operation']
        started = session_store.turn_started_data(sid, tid, current[0])
        if (not started or started.get('stage_authority') is not True
                or started.get('text') != body['requirements']
                or started.get('classifier_hint') != _hint(operation, row['edit_id'], body, source_profile=NATIVE_PROFILE)
                or marker.get('source_profile') != NATIVE_PROFILE or marker.get('authority') != list(current)
                or marker.get('action') != operation or marker.get('source_request_id') != row['edit_id']
                or marker.get('payload_digest') != hashlib.sha256(canonical(dict(
                    source_profile=NATIVE_PROFILE, action=operation, source_request_id=row['edit_id'], payload=body))).hexdigest()
                or key != _request_key(body, NATIVE_PROFILE, sid, tid)):
            raise ValueError()
        _same_request(row, bound, current, body, key)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise FlowError(409) from None


def _same_request(row, authority, current, body, key):
    receipt = _receipt(row, authority)
    if (receipt.edit_id != body['source_request_id'] or receipt.actor_binding_id != current[2]
            or receipt.instruction_digest != hashlib.sha256(body['requirements'].encode('utf-8')).hexdigest()
            or receipt.idempotency_key != key
            or ('expected_base_commit' in body and receipt.base_commit != body['expected_base_commit'])):
        raise FlowError(409)
    return receipt


def _stage(tenant, project_id, current, body, recheck, session_id, turn_id, *, source_profile=WEB_PROFILE):
    key = _request_key(body, source_profile, session_id, turn_id)
    bound = repository(current)
    row = _read(bound, body['source_request_id']) if bound else None
    if row is not None:
        _membership(row, bound, current, source_profile)
        if source_profile == NATIVE_PROFILE:
            key = row['idempotency_key']
            if key.rsplit(':', 1)[-1] != _request_digest(body, source_profile):
                raise FlowError(409)
        _same_request(row, bound, current, body, key)
    else:
        # A previous turn can have lost the author response before its row was
        # visible. Missing state is never proof that another editor is safe.
        snapshot = session_store.get_session(session_id)
        pinned = snapshot['last_seq']
        if pinned > 10000:
            raise FlowError(503)
        events = session_store.events_after(session_id, 0, 10000)
        prefix = [event for event in events if event['seq'] <= pinned]
        if [event['seq'] for event in prefix] != list(range(1, pinned + 1)):
            raise FlowError(503)
        if any(event['type'] == 'browser_source_dispatch'
               and event['turn_id'] != turn_id
               and event['data'].get('source_request_id') == body['source_request_id'] for event in prefix):
            raise FlowError(409)
        recheck()
        kwargs = dict(cancelled=lambda: _cancelled(recheck))
        dispatch_author = authoring.author
        if source_profile == NATIVE_PROFILE:
            from native_project_authoring import author as dispatch_author
        result = dispatch_author(tenant, project_id, dict(body_to_author(body), idempotency_key=key), **kwargs)
        recheck()
        bound = repository(current)
        row = _read(bound, body['source_request_id']) if bound else None
        if row is None:
            raise FlowError(409)
        receipt = _same_request(row, bound, current, body, key)
        if (result['receipt'] != receipt.to_mapping() or result['receiptDigest'] != row['receipt_digest']
                or result['version'] != row['version']):
            raise FlowError(409)
    recheck()
    if repository(current) != bound:
        raise FlowError(409)
    if row['state'] == 'staged':
        _store().await_confirmation(row['edit_id'], expected_version=row['version'], transition_key=key + ':review')
        row = _read(bound, row['edit_id'])
    return row, bound


def _cancelled(recheck):
    try:
        recheck()
        return False
    except FlowError:
        return True


def _confirmation(row, receipt, actor, confirmation_id):
    now = datetime.now(timezone.utc)
    frozen = receipt.to_mapping()
    fields = (*AUTHORITY_FIELDS, 'edit_id', 'writer_lease_id', 'writer_lease_generation', 'staged_tree')
    value = {key: frozen[key] for key in fields}
    value.update(contract=CONFIRMATION_CONTRACT, confirmation_id=confirmation_id,
                 receipt_digest=row['receipt_digest'], approver_binding_id=actor,
                 issued_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat())
    confirmation = parse_confirmation(value)
    require_confirmation_binding(confirmation, receipt)
    return confirmation


def _validate_publication(result, row, receipt, *, recover, prior_version):
    """Metadata is evidence only when it agrees with fresh durable settlement."""
    try:
        expected = {'observation', 'settlement', 'compareAndSwap'} if recover else {'matrix', 'observation', 'settlement'}
        if type(result) is not dict or set(result) != expected:
            raise ValueError()
        settlement = result['settlement']
        if (set(settlement) != {'contract', 'action', 'edit_id', 'state', 'version'}
                or settlement['contract'] != 'leaf.project-repository-edit-coordination.v1'
                or settlement['action'] != ('recover_publish' if recover else 'settle_publish')
                or any(settlement[key] != row[key] for key in ('edit_id', 'state', 'version'))
                or row['state'] != 'published'):
            raise ValueError()
        observation = result['observation']
        if recover:
            if (type(result['compareAndSwap']) is not bool or set(observation) != {
                    'private_ref_commit', 'main_commit', 'main_tree'}
                    or observation != dict(private_ref_commit=receipt.staged_head_commit,
                        main_commit=receipt.staged_head_commit, main_tree=receipt.staged_tree)):
                raise ValueError()
        else:
            matrix = result['matrix']
            fixed = dict(contract='leaf.project-repository-edit-coordination.v1', action='authorize_publish',
                         edit_id=receipt.edit_id, state='publishing', version=prior_version + 1,
                         receipt_digest=row['receipt_digest'], expected_main_commit=receipt.base_commit,
                         staged_head_commit=receipt.staged_head_commit, staged_tree=receipt.staged_tree,
                         private_ref='refs/leaf/changes/' + receipt.edit_id)
            if set(matrix) != set(fixed) | {'publish_lease_id', 'publish_lease_generation'}:
                raise ValueError()
            if any(matrix[key] != value for key, value in fixed.items()):
                raise ValueError()
            _uuid(matrix['publish_lease_id'])
            if (type(matrix['publish_lease_generation']) is not int
                    or matrix['publish_lease_generation'] <= receipt.writer_witness.writer_lease_generation):
                raise ValueError()
            if (set(observation) != {'private_ref', 'private_ref_commit', 'before_main_commit',
                                     'after_main_commit', 'after_main_tree', 'compare_and_swap'}
                    or observation['private_ref'] != 'refs/leaf/changes/' + receipt.edit_id
                    or type(observation['compare_and_swap']) is not bool
                    or observation['before_main_commit'] not in (receipt.base_commit, receipt.staged_head_commit)
                    or observation['private_ref_commit'] != receipt.staged_head_commit
                    or observation['after_main_commit'] != receipt.staged_head_commit
                    or observation['after_main_tree'] != receipt.staged_tree):
                raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise FlowError(409) from None


def _publish(current, bound, row, body, action, recheck, *, source_profile=WEB_PROFILE):
    receipt = _receipt(row, bound)
    if row['receipt_digest'] != body['receipt_digest']:
        raise FlowError(409)
    if row['state'] == 'published':
        return row
    if row['version'] != body['expected_version']:
        raise FlowError(409)
    recover = action == 'recover' or row['state'] == 'publishing'
    if recover and (row['state'] != 'publishing' or not row['confirmation_id']):
        raise FlowError(409)
    if not recover and row['state'] != 'awaiting_confirmation':
        raise FlowError(409)
    transition = dict(action=action, edit_id=row['edit_id'], **body)
    if source_profile == NATIVE_PROFILE:
        transition['source_profile'] = source_profile
    key = ('native-source:' if source_profile == NATIVE_PROFILE else 'browser-source:') + hashlib.sha256(canonical(transition)).hexdigest()
    dispatch = dict(bound, edit_id=row['edit_id'], actor_binding_id=current[2],
                    expected_version=row['version'], transition_key=key)
    if recover:
        dispatch.update(expected_main_commit=receipt.base_commit, staged_head_commit=receipt.staged_head_commit,
                        staged_tree=receipt.staged_tree, reason_code=('native_source_recovery' if source_profile == NATIVE_PROFILE else 'browser_source_recovery'))
    else:
        recheck()
        _store().put_confirmation(_confirmation(row, receipt, current[2], body['confirmation_id']),
                                  expected_edit_version=row['version'], transition_key=key)
        dispatch.update(confirmation_id=body['confirmation_id'], receipt_digest=row['receipt_digest'])
    url = os.environ.get('LEAF_AUTHOR_HARNESS_URL', '').strip().rstrip('/')
    headers = broker_client.harness_headers()
    if not url or not headers.get('X-Harness-Secret'):
        raise FlowError(503)
    headers = dict(headers, **{'X-Tenant-Id': current[0], 'Content-Type': 'application/json'})
    recheck()
    if repository(current) != bound:
        raise FlowError(409)
    response = None
    result = None
    try:
        response = requests.post(url + '/internal/project-repository-source/' + ('recover' if recover else 'publish'),
                                 data=canonical(dispatch), headers=headers, timeout=(10, 240), allow_redirects=False)
        if response.status_code == 200 and len(response.content) <= MAX_RESPONSE_BYTES:
            result = json.loads(response.content.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (requests.RequestException, ValueError, UnicodeError, RecursionError):
        pass
    finally:
        if response is not None:
            response.close()
    recheck()
    if repository(current) != bound:
        raise FlowError(409)
    fresh = _read(bound, row['edit_id'])
    _receipt(fresh, bound)
    if result is not None:
        _validate_publication(result, fresh, receipt, recover=recover, prior_version=row['version'])
    # A lost response can be reconciled from exact durable Git observations.
    # An unsettled transaction remains publishing and is recovered explicitly.
    if fresh['state'] not in ('published', 'publishing', 'conflicted'):
        raise FlowError(409)
    return fresh


def mutate(tenant, project_id, session_id, turn_id, action, edit_id, body, *, source_profile=WEB_PROFILE):
    body = parse_request(canonical(body), action, source_profile=source_profile)
    if action == 'request':
        edit_id, action = body['source_request_id'], body['source_operation']
    try:
        _uuid(edit_id)
    except (ValueError, TypeError):
        raise FlowError(422) from None
    held = authority(tenant, project_id, session_id, turn_id, action, edit_id, body, source_profile=source_profile)
    current, subject = held

    def recheck():
        if authority(tenant, project_id, session_id, turn_id, action, edit_id, body, source_profile=source_profile) != held:
            raise FlowError(403)

    claim_body = dict(action=action, source_request_id=edit_id, payload=body)
    if source_profile == NATIVE_PROFILE:
        claim_body['source_profile'] = source_profile
        bound = repository(current)
        prior = _read(bound, edit_id) if bound else None
        if prior is not None:
            _membership(prior, bound, current, source_profile)
        if action in ('create', 'edit') and prior is None:
            try:
                authoring.validate_native_materials(tenant, project_id, dict(body_to_author(body), idempotency_key='validate'), cancelled=lambda: _cancelled(recheck))
            except authoring.AuthoringError as exc:
                raise FlowError(exc.status if exc.status in _ERRORS else 503) from None
    digest = hashlib.sha256(canonical(claim_body)).hexdigest()
    won = _claim(session_id, turn_id, current, action, edit_id, digest, recheck, source_profile=source_profile, body=body)
    if not won:
        # Only the marker owner may emit an outcome or end the held turn.
        recheck()
        if action in ('create', 'edit'):
            bound = repository(current)
            row = _read(bound, edit_id) if bound else None
            if row is not None:
                _membership(row, bound, current, source_profile)
                key = row['idempotency_key'] if source_profile == NATIVE_PROFILE else _request_key(body, source_profile)
                if source_profile == NATIVE_PROFILE and key.rsplit(':', 1)[-1] != _request_digest(body, source_profile):
                    raise FlowError(409)
                _same_request(row, bound, current, body, key)
                result = representation(row, bound, source_profile=source_profile)
                recheck()
                if repository(current) != bound:
                    raise FlowError(409)
                return result
        raise FlowError(409)
    success = False
    try:
        recheck()
        if action in ('create', 'edit'):
            row, bound = _stage(tenant, project_id, current, body, recheck, session_id, turn_id, source_profile=source_profile)
        else:
            bound = repository(current)
            row = _read(bound, edit_id) if bound else None
            if row is None:
                raise FlowError(404)
            _membership(row, bound, current, source_profile)
            _receipt(row, bound)
            recheck()
            if action == 'reject':
                row = _store().reject_edit(*(bound[key] for key in AUTHORITY_FIELDS), edit_id,
                    receipt_digest=body['receipt_digest'], expected_version=body['expected_version'],
                    transition_key=('native-reject:' if source_profile == NATIVE_PROFILE else 'browser-reject:') + digest)
            else:
                row = _publish(current, bound, row, body, action, recheck, source_profile=source_profile)
        recheck()
        if repository(current) != bound:
            raise FlowError(409)
        result = representation(row, bound, source_profile=source_profile)
        session_store.append_event(session_id, turn_id, 'browser_source_result', dict(
            source_request_id=edit_id, state=row['state'], version=row['version'],
            text='Source ready for review.' if row['state'] == 'awaiting_confirmation' else 'Private source state updated.'))
        success = True
        return result
    except authoring.AuthoringError as exc:
        raise FlowError(exc.status if exc.status in _ERRORS else 503) from None
    except FlowError:
        raise
    except Exception:
        raise FlowError(503) from None
    finally:
        # Fresh subject/turn checks prevent an old completion touching a replacement.
        try:
            recheck()
            if not success:
                session_store.append_event(session_id, turn_id, 'browser_source_error', dict(
                    source_request_id=edit_id, text='Source outcome unavailable. Review the saved source state before retrying.'))
            recheck()
            turn_runner.finish_stage_authority(current[0], session_id, turn_id, subject,
                stop_reason='browser_source_complete' if success else 'browser_source_unavailable')
        except Exception:
            pass
