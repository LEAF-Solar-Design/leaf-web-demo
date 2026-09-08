"""Trusted mounted coordinator ingress for the campaign planning ledger."""
from __future__ import annotations

import base64
import hashlib
import os
import uuid

import campaign_worker_service
import project_repository_source as source_service


_FIELDS = {
    'next': {'enrollment_id'},
    'recover': {'enrollment_id'},
    'product': {'enrollment_id', 'task_id', 'attempt_id', 'fence', 'result_fingerprint', 'output'},
    'plan': {'enrollment_id', 'task_id', 'attempt_id', 'fence', 'result_fingerprint',
             'plan_sha256', 'plan_size_bytes', 'plan_b64'},
    'export': {'enrollment_id', 'attempt_id', 'fence'},
    'bind': {'enrollment_id', 'attempt_id', 'fence', 'run_id', 'registration_id',
             'root_request_id', 'gateway_project_id', 'source_ref', 'packet_digest',
             'budget_class', 'reservation_micro_usd'},
    'admit': {'enrollment_id', 'attempt_id', 'leaf_id', 'run_id', 'submission_digest'},
    'settle': {'enrollment_id', 'attempt_id', 'fence', 'verdict', 'outcome', 'result', 'artifact_ref'},
}


class BridgeError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__('Campaign bridge request failed')


def _validate(op, body):
    if not isinstance(op, str) or op not in _FIELDS or not isinstance(body, dict):
        raise BridgeError(400)
    optional = {'reservation_id'} if op == 'admit' else set()
    if not _FIELDS[op] <= set(body) or set(body) - _FIELDS[op] - optional:
        raise BridgeError(400)
    body = dict(body)
    for key, value in body.items():
        if key in ('enrollment_id', 'attempt_id', 'task_id'):
            if not isinstance(value, str) or len(value) != 36:
                raise BridgeError(400)
            try:
                if op == 'product' and str(uuid.UUID(value)) != value:
                    raise ValueError('noncanonical id')
                body[key] = str(uuid.UUID(value))
            except ValueError:
                raise BridgeError(400) from None
        elif key in ('fence', 'reservation_micro_usd', 'plan_size_bytes'):
            minimum = 1 if key == 'reservation_micro_usd' else 0
            if type(value) is not int or not minimum <= value <= 9223372036854775807:
                raise BridgeError(400)
        elif key in ('verdict', 'result', 'output'):
            if not isinstance(value, dict):
                raise BridgeError(400)
        elif key in ('artifact_ref', 'reservation_id') and value is None:
            continue
        elif not isinstance(value, str) or not value or '\x00' in value:
            raise BridgeError(400)
    if op == 'settle' and body['outcome'] not in ('succeeded', 'failed'):
        raise BridgeError(400)
    if op == 'plan':
        if (not 1 <= body['plan_size_bytes'] <= 262144
                or len(body['plan_b64']) > 349528):
            raise BridgeError(413)
        for key in ('result_fingerprint', 'plan_sha256'):
            value = body[key]
            if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
                raise BridgeError(400)
    if op == 'product':
        import campaign_product_execution as product
        try:
            if (any(body[k] != str(uuid.UUID(body[k])) for k in ('enrollment_id', 'task_id', 'attempt_id'))
                    or not product.counter(body['fence'], 1)
                    or not source_service._sha(body['result_fingerprint'], 64)):
                raise ValueError('invalid product identity')
            product.validate_output(body['output'])
        except (ValueError, TypeError, KeyError):
            raise BridgeError(400) from None
    return body


def _configured():
    if (os.environ.get('LEAF_CAMPAIGN_BRIDGE') != 'on'
            or not os.environ.get('LEAF_CAMPAIGN_WORKER_SUBJECT', '').strip()
            or not any(item.strip() for item in
                       os.environ.get('LEAF_CAMPAIGN_ALLOWED_MACHINES', '').split(','))):
        raise BridgeError(503)


def _scope(cur, enrollment, enrollment_id, subject):
    scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
    if scope['machine_id'] not in enrollment.allowed_machines():
        raise enrollment.CampaignError('worker_forbidden', 'Campaign bridge request failed')
    return scope


def _attempt(cur, execution, scope, body, *, active=False):
    # Do not retain task locks while invoking a ledger method on its own connection.
    cur.execute('SELECT * , deadline_at<=clock_timestamp() AS overdue '
                'FROM campaign_task_attempts WHERE ' + execution.SCOPE +
                ' AND attempt_id=%(attempt)s AND worker_id=%(worker)s',
                dict(scope, attempt=uuid.UUID(body['attempt_id']),
                     worker='enrollment-' + str(scope['enrollment_id'])))
    attempt = cur.fetchone()
    if attempt is None:
        raise execution.CampaignError('worker_forbidden', 'Campaign bridge request failed')
    cur.execute('SELECT * FROM campaign_tasks WHERE ' + execution.SCOPE +
                ' AND task_id=%(task)s', dict(scope, task=attempt['task_id']))
    task = cur.fetchone()
    if active:
        task, attempt = execution._remote_attempt(cur, scope, uuid.UUID(body['attempt_id']))
    from leaf_platform.campaign_product_publication import product_task, principal
    planning = task is not None and task['task_key'] == 'campaign-plan' and task['capability'] == 'campaign.plan'
    if (task is None or not (planning or product_task(task)) or attempt['stage'] != 'implementation'
            or attempt['worker_id'] != 'enrollment-' + str(scope['enrollment_id'])):
        raise execution.CampaignError('worker_forbidden', 'Campaign bridge request failed')
    if not planning:
        principal(cur, scope)
    if active and (attempt['status'] != 'active' or attempt['overdue']
                   or task['status'] != 'claimed' or task['current_stage'] != 'implementation'
                   or body['fence'] != attempt['fence'] or body['fence'] != task['fence']):
        raise execution.CampaignConflict('stale_attempt', 'Campaign bridge request failed')
    return task, attempt


def _export(enrollment, execution, body, subject):
    with execution._cursor() as cur:
        scope = _scope(cur, enrollment, body['enrollment_id'], subject)
        task, attempt = _attempt(cur, execution, scope, body, active=True)
    if task['task_key'] != 'campaign-plan':
        import campaign_product_execution
        return campaign_product_execution.export(enrollment, execution, body, subject, scope, task, attempt)
    # Source and harness I/O must never span a ledger transaction or read lock.
    identity = (scope['tenant_id'], str(scope['org']), str(scope['project']))
    seed = source_service.initialize_project_source(*identity, scope['prompt'])
    if (not isinstance(seed, dict) or seed.get('source_commit') != task['source_sha']
            or not source_service._sha(seed.get('source_commit'), 40)
            or not source_service._sha(seed.get('source_tree'), 40)
            or seed.get('seed_digest') != hashlib.sha256(scope['prompt'].encode('utf-8')).hexdigest()):
        raise BridgeError(409)
    authority = source_service.platform_link.platform_store().resolve_project_repository_authority(*identity)
    if (not isinstance(authority, dict) or set(authority) != source_service._AUTHORITY
            or not all(source_service._uuid(value) for value in authority.values())
            or tuple(authority[key] for key in ('tenant_id', 'organization_id', 'project_id')) != identity):
        raise BridgeError(409)
    bundle = source_service.export_project_source_bundle(*identity, seed['source_commit'], seed['source_tree'])
    if not isinstance(bundle, dict):
        raise BridgeError(503)
    raw = bundle.get('bundle')
    if (not isinstance(raw, bytes) or not 1 <= len(raw) <= 262144
            or type(bundle.get('size_bytes')) is not int or bundle['size_bytes'] != len(raw)
            or bundle.get('bundle_sha256') != hashlib.sha256(raw).hexdigest()
            or bundle.get('source_commit') != seed['source_commit']
            or bundle.get('source_tree') != seed['source_tree']):
        raise BridgeError(409)
    with execution._cursor() as cur:
        current = _scope(cur, enrollment, body['enrollment_id'], subject)
        current_task, current_attempt = _attempt(cur, execution, current, body, active=True)
        if current != scope or current_task != task or current_attempt != attempt:
            raise execution.CampaignConflict('source_conflict', 'Campaign bridge request failed')
        return dict(ok=True, enrollment_id=body['enrollment_id'], scope={
            'org_id': str(scope['org']), 'project_id': str(scope['project']),
            'campaign_id': str(scope['campaign']), 'machine_id': scope['machine_id']},
            attempt={key: str(attempt[key]) if key == 'attempt_id' else attempt[key]
                     for key in ('attempt_id', 'fence', 'stage')},
            task={key: task[key] for key in campaign_worker_service._PLAN_FIELDS},
            source={**{key: seed[key] for key in ('source_commit', 'source_tree', 'seed_digest')},
                    'repository_key': authority['repo_key'], 'bundle_sha256': bundle['bundle_sha256'],
                    'size_bytes': len(raw), 'bundle_b64': base64.b64encode(raw).decode('ascii')})


def handle(op, body, subject):
    """Validate closed requests, resolve persisted authority, and call the ledger."""
    if op in ('release', 'deliver'):
        fields = {'enrollment_id'} if op == 'release' else {'enrollment_id', 'release_id'}
        if not isinstance(body, dict) or set(body) != fields:
            raise BridgeError(400)
        try:
            import uuid
            for value in body.values():
                uuid.UUID(value)
            enrollment, execution, _ = campaign_worker_service._platform()
            with execution._cursor() as cur:
                scope = enrollment.resolve_worker_scope(cur, body['enrollment_id'], subject)
            from leaf_platform import campaign_release
            if op == 'deliver':
                completion = campaign_release.get_release(scope['org'], scope['project'],
                                                          scope['campaign'], body['release_id'])
                import campaign_release_service as releases
                actor = releases._WorkerActor(body['enrollment_id'], subject)
                try:
                    actor.resolve(scope['project'])
                except (PermissionError, releases.platform_link.ProjectSessionForbidden):
                    return {'ok': True, 'completion': completion, 'next_action': {
                        'available': False, 'action': 'enable-with-current-project-actor',
                        'reason': 'Current enrollment actor binding is unavailable'}}
                return {'ok': True, 'completion': releases.advance(actor, scope['project'],
                    scope['campaign'], body['release_id'])}
            else:
                completion = campaign_release.release_snapshot(scope['org'], scope['project'], scope['campaign'])
            return {'ok': True, 'completion': completion, 'next_action': {
                'available': True, 'action': 'deliver',
                'reason': 'Delivery rechecks the enabled enrollment actor and project membership'}}
        except (ValueError, TypeError):
            raise BridgeError(400) from None
        except Exception:
            raise BridgeError(403) from None
    if op in ('host_op', 'host_settle', 'host_grant'):
        try:
            from leaf_platform import campaign_capabilities as capabilities
            operation = {'host_op': 'claim_host_operation', 'host_settle': 'settle_host_operation',
                         'host_grant': 'read_host_grant'}[op]
            return getattr(capabilities, operation)(subject, body)
        except Exception as exc:
            from leaf_platform.campaigns import CampaignError, CampaignConflict, CampaignUnavailable
            if isinstance(exc, CampaignConflict):
                status = 409
            elif isinstance(exc, CampaignUnavailable):
                status = 503
            elif isinstance(exc, CampaignError):
                status = 403 if exc.code in ('worker_forbidden', 'project_unavailable') else 400
            elif isinstance(exc, (ValueError, TypeError, KeyError)):
                status = 400
            else:
                status = 503
            raise BridgeError(status) from None
    body = _validate(op, body)
    _configured()
    try:
        enrollment, execution, _ = campaign_worker_service._platform()
    except Exception:
        raise BridgeError(503) from None
    try:
        if op == 'product':
            import campaign_product_execution
            return campaign_product_execution.publish(body, subject)
        if op == 'plan':
            from leaf_platform.campaign_plan_adoption import adopt_plan
            return adopt_plan(body['enrollment_id'], subject,
                              **{key: value for key, value in body.items() if key != 'enrollment_id'})
        if op == 'export':
            return _export(enrollment, execution, body, subject)
        if op == 'next':
            with execution._cursor() as cur:
                _scope(cur, enrollment, body['enrollment_id'], subject)
            result = campaign_worker_service.next_work(body['enrollment_id'], subject)
            if 'attempt' in result:
                result = dict(result, attempt={key: value for key, value in result['attempt'].items()
                                               if key != 'attempt_token'})
            return result
        with execution._cursor() as cur:
            scope = _scope(cur, enrollment, body['enrollment_id'], subject)
            if op == 'recover':
                return dict(ok=True, pending_remote_bindings=
                            campaign_worker_service.pending_for_enrollment(cur, execution, scope))
            _attempt(cur, execution, scope, body)
            args = (scope['org'], scope['project'], scope['campaign'], body['attempt_id'])
            kwargs = {key: value for key, value in body.items() if key not in ('enrollment_id', 'attempt_id')}
        if op == 'bind':
            return dict(ok=True, binding=execution.bind_remote_dispatch(
                *args, machine_id=scope['machine_id'], **kwargs))
        if op == 'admit':
            return dict(ok=True, binding=execution.record_remote_admission(*args, **kwargs))
        return dict(ok=True, receipt=execution.settle_remote_attempt(*args, **kwargs))
    except BridgeError:
        raise
    except Exception as exc:
        # No exception message, provider data, or dynamically supplied error code escapes.
        if isinstance(exc, source_service.SourceConflict):
            status = 409
        elif isinstance(exc, execution.CampaignConflict):
            status = 409
        elif isinstance(exc, execution.CampaignUnavailable):
            status = 503
        elif isinstance(exc, execution.CampaignError):
            status = (422 if exc.code == 'invalid_plan' else
                      403 if exc.code in ('worker_forbidden', 'project_unavailable') else 400)
        elif isinstance(exc, (ValueError, TypeError, KeyError)):
            status = 400
        else:
            status = 503
        raise BridgeError(status) from None
