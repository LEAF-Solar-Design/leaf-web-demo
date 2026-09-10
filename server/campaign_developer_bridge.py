"""Authenticated browser-walk adapter for the existing durable campaign ledger."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from campaign_bridge import BridgeError
from developer_jobs_transport import DeveloperJobsTransport, TransportError

OPS = frozenset({'walk_doctor', 'walk_prepare', 'walk_read', 'walk_request', 'walk_receipt'})
LIMIT = 128 * 1024
CONFIG = {'version', 'client_version', 'enabled', 'endpoint', 'region', 'role_arn',
          'registry_digest', 'profiles', 'jobs'}
JOB = {'org_id', 'project_id', 'repository_id', 'job_name', 'environment', 'commit_sha',
       'source_bucket', 'source_prefix', 'acceptance_bucket', 'acceptance_prefix',
       'acceptance_producer', 'runtime_seconds', 'reservation_microusd',
       'browser_startup_verified', 'profile_id'}
MANIFEST = {'version', 'profile_id', 'profile_revision', 'profile_digest', 'campaign_id',
            'task_id', 'parent_attempt_id', 'parent_attempt_fence', 'max_cost_microusd',
            'max_runtime_seconds'}
REQUEST = {'org_id', 'project_id', 'repository_id', 'campaign_id', 'task_id',
           'parent_attempt_id', 'parent_attempt_fence', 'commit_sha', 'source',
           'job_name', 'environment', 'idempotency_key'}
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED'}


def _require(condition, status=409):
    if not condition:
        raise BridgeError(status)


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(_raw(value)).hexdigest()


def _hex(value, size=64):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', value) is not None


def _integer(value, low=1, high=10**15):
    return type(value) is int and low <= value <= high


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and value.strip() == value and '\x00' not in value


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 400)
        result[key] = value
    return result


def _json(raw):
    _require(isinstance(raw, bytes) and 0 < len(raw) <= LIMIT, 503)
    value = json.loads(raw, object_pairs_hook=_pairs)
    _raw(value)  # Reject NaN, Infinity and exponent overflow.
    _require(isinstance(value, dict), 503)
    return value


def _uuid(value):
    _require(isinstance(value, str) and str(uuid.UUID(value)) == value, 400)
    return value


def _ref(value):
    _require(isinstance(value, dict) and set(value) == {'bucket', 'key', 'version_id', 'sha256'})
    _require(all(_text(item) for item in value.values()) and _hex(value['sha256'])
             and value['version_id'].lower() not in {'null', 'latest'})
    return dict(value)


def _operation(request):
    return _sha([[request[k] for k in ('org_id', 'project_id', 'repository_id')], request['idempotency_key']])


def installation():
    path = os.environ.get('LEAF_WALK_INSTALLATION_FILE')
    if not path:
        return None
    try:
        with Path(path).open('rb') as handle:
            value = _json(handle.read(LIMIT + 1))
        _require(set(value) == CONFIG and type(value['version']) is int and value['version'] == 1, 503)
        _require(value['client_version'] == 1 and type(value['client_version']) is int
                 and type(value['enabled']) is bool and _hex(value['registry_digest'])
                 and isinstance(value['profiles'], dict) and isinstance(value['jobs'], list)
                 and len(value['jobs']) <= 256, 503)
        for job in value['jobs']:
            _require(isinstance(job, dict) and set(job) == JOB, 503)
            _require(all(_text(job[k]) for k in JOB - {'runtime_seconds', 'reservation_microusd', 'browser_startup_verified'})
                     and _hex(job['commit_sha'], 40) and _integer(job['runtime_seconds'], 1, 3600)
                     and _integer(job['reservation_microusd']) and type(job['browser_startup_verified']) is bool, 503)
            for prefix in ('source_prefix', 'acceptance_prefix'):
                _require(job[prefix].endswith('/') and not job[prefix].startswith('/')
                         and '..' not in job[prefix].split('/') and '\\' not in job[prefix], 503)
        return value
    except FileNotFoundError:
        return None
    except Exception:
        raise BridgeError(503) from None


def _resolve(enrollment_id, campaign_id, task_id, parent_id, fence, active, subject):
    import campaign_bridge
    import campaign_worker_service
    enrollment, execution, _ = campaign_worker_service._platform()
    with execution._cursor() as cur:
        scope = campaign_bridge._scope(cur, enrollment, enrollment_id, subject)
        _require(str(scope['campaign']) == campaign_id, 403)
        if task_id is None:
            cur.execute('SELECT * FROM campaign_developer_allocations WHERE ' + execution.SCOPE, scope)
            return scope, cur.fetchone()
        cur.execute('SELECT *,deadline_at<=clock_timestamp() AS overdue FROM campaign_task_attempts WHERE '
                    + execution.SCOPE + ' AND attempt_id=%(attempt)s AND worker_id=%(worker)s',
                    dict(scope, attempt=uuid.UUID(parent_id), worker='enrollment-' + enrollment_id))
        attempt = cur.fetchone()
        cur.execute('SELECT * FROM campaign_tasks WHERE ' + execution.SCOPE + ' AND task_id=%(task)s',
                    dict(scope, task=uuid.UUID(task_id)))
        task = cur.fetchone()
        _require(attempt is not None and task is not None and str(attempt['task_id']) == task_id
                 and task['capability'] == 'browser.walk' and attempt['stage'] == 'implementation'
                 and attempt['worker_id'] == 'enrollment-' + enrollment_id and attempt['fence'] == fence, 403)
        if active:
            _require(attempt['status'] == 'active' and not attempt['overdue']
                     and task['status'] == 'claimed' and task['current_stage'] == 'implementation'
                     and task['fence'] == fence)
        return scope, None


def _clients(config):
    import boto3
    from botocore.config import Config
    bounded = Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1})
    session = boto3.Session(region_name=config['region'])
    # Assume once and share the same workload identity for object and API access.
    if config['role_arn'] is not None:
        credentials = session.client('sts', config=bounded).assume_role(
            RoleArn=config['role_arn'], RoleSessionName='campaign-browser-walk')['Credentials']
        session = boto3.Session(aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'], aws_session_token=credentials['SessionToken'],
            region_name=config['region'])
    return session.client('s3', config=bounded), DeveloperJobsTransport(
        config['endpoint'], region=config['region'], boto3_session=session)


class WalkBridge:
    """Injectable adapters are for tests; production authority comes from enrollment."""

    def __init__(self, config, ledger, *, resolve=_resolve, clients=_clients):
        self.config, self.ledger, self.resolve, self.clients = config, ledger, resolve, clients
        self._io = None

    def _aws(self):
        if self._io is None:
            self._io = self.clients(self.config)
        return self._io

    def _job(self, scope, *, profile_id=None, request=None):
        _require(self.config is not None, 503)
        matches = [job for job in self.config['jobs'] if job['org_id'] == str(scope['org'])
                   and job['project_id'] == str(scope['project'])
                   and (profile_id is None or job['profile_id'] == profile_id)
                   and (request is None or all(job[k] == request[k] for k in
                        ('org_id', 'project_id', 'repository_id', 'job_name', 'environment', 'commit_sha')))]
        _require(len(matches) == 1)
        return matches[0]

    def _scope(self, body, subject, frozen=None, active=False):
        intent = body.get('manifest', frozen or {})
        campaign = body.get('campaign_id', intent.get('campaign_id'))
        task = body.get('task_id', intent.get('task_id'))
        return self.resolve(body['enrollment_id'], campaign, task,
            intent.get('parent_attempt_id'), intent.get('parent_attempt_fence'), active, subject)

    def _load(self, body, subject):
        # Scope-only enrollment resolution precedes the ledger lookup.
        scope, _ = self.resolve(body['enrollment_id'], body['campaign_id'], None, None, None, False, subject)
        row = self.ledger.read(scope['org'], scope['project'], scope['campaign'], body['task_id'])
        _require(isinstance(row, dict) and isinstance(row.get('request'), dict), 409)
        request = row['request']
        _require(set(request) == REQUEST and request['campaign_id'] == body['campaign_id']
                 and request['task_id'] == body['task_id'] and request['org_id'] == str(scope['org'])
                 and request['project_id'] == str(scope['project']))
        self._scope(body, subject, frozen=request)
        return scope, row, self._job(scope, request=request)

    def _get(self, reference, maximum=LIMIT):
        reference = _ref(reference)
        obj = self._aws()[0].get_object(Bucket=reference['bucket'], Key=reference['key'],
                                        VersionId=reference['version_id'])
        stream = obj['Body']
        try:
            _require(obj.get('VersionId') == reference['version_id']
                     and _integer(obj.get('ContentLength'), 1, maximum))
            data = stream.read(maximum + 1)
            _require(isinstance(data, bytes) and len(data) == obj['ContentLength']
                     and hashlib.sha256(data).hexdigest() == reference['sha256'])
            return data
        finally:
            stream.close()

    def _source(self, job, data):
        digest = hashlib.sha256(data).hexdigest()
        key = job['source_prefix'] + digest + '.json'
        s3 = self._aws()[0]
        try:
            result = s3.put_object(Bucket=job['source_bucket'], Key=key, Body=data,
                IfNoneMatch='*', Metadata={'sha256': digest}, ContentType='application/json')
            version = result.get('VersionId')
        except Exception:
            # A conflict or lost response must resolve the exact existing bytes.
            head = s3.head_object(Bucket=job['source_bucket'], Key=key)
            version = head.get('VersionId')
        reference = _ref(dict(bucket=job['source_bucket'], key=key, version_id=version, sha256=digest))
        _require(self._get(reference) == data)
        return reference

    def doctor(self, body, subject):
        scope, allocation = self._scope(body, subject)
        state = 'ready'
        if self.config is None:
            state = 'installation_missing'
        elif not self.config['enabled']:
            state = 'execution_disabled'
        else:
            jobs = [j for j in self.config['jobs'] if j['org_id'] == str(scope['org'])
                    and j['project_id'] == str(scope['project'])]
            if not jobs:
                state = 'registration_missing'
            elif not any(j['browser_startup_verified'] for j in jobs):
                state = 'browser_unavailable'
            elif (allocation is None or allocation['limit_microusd'] - allocation['spent_microusd']
                  - allocation['reserved_microusd'] < min(j['reservation_microusd'] for j in jobs)):
                state = 'insufficient_allocation'
        return {'state': state}

    def prepare(self, body, subject):
        _require(self.config is not None and self.config['enabled'], 503)
        manifest = body['manifest']
        _require(isinstance(manifest, dict) and set(manifest) == MANIFEST, 400)
        for field in ('campaign_id', 'task_id', 'parent_attempt_id'):
            _uuid(manifest[field])
        _require(type(manifest['version']) is int and manifest['version'] == 1
                 and _integer(manifest['parent_attempt_fence']) and _integer(manifest['profile_revision'])
                 and _text(manifest['profile_id']) and _hex(manifest['profile_digest']), 400)
        scope, _ = self.resolve(body['enrollment_id'], manifest['campaign_id'], None, None, None, False, subject)
        job = self._job(scope, profile_id=manifest['profile_id'])
        profile = self.config['profiles'].get(manifest['profile_id'])
        _require(isinstance(profile, dict) and type(profile.get('version')) is int and profile['version'] == 1
                 and profile.get('profile_id') == manifest['profile_id']
                 and type(profile.get('profile_revision')) is int and profile['profile_revision'] == manifest['profile_revision']
                 and _sha(profile) == manifest['profile_digest'], 409)
        _require(job['browser_startup_verified'] and _integer(manifest['max_runtime_seconds'], 1,
                 min(job['runtime_seconds'], profile['max_runtime_seconds']))
                 and _integer(manifest['max_cost_microusd'], job['reservation_microusd'], profile['max_cost_microusd']), 409)
        row = self.ledger.read(scope['org'], scope['project'], scope['campaign'], manifest['task_id'])
        self._scope(body, subject, active=row is None)
        data = _raw({'version': 1, 'manifest': manifest, 'profile': profile})
        _require(len(data) <= LIMIT, 413)
        base = {k: job[k] for k in ('org_id', 'project_id', 'repository_id', 'job_name', 'environment', 'commit_sha')}
        base.update({k: manifest[k] for k in ('campaign_id', 'task_id', 'parent_attempt_id', 'parent_attempt_fence')})
        base['idempotency_key'] = 'walk-v1:' + _sha(manifest)
        if row is not None:
            frozen = row['request']
            expected = {'bucket': job['source_bucket'], 'key': job['source_prefix'] + hashlib.sha256(data).hexdigest() + '.json',
                        'sha256': hashlib.sha256(data).hexdigest()}
            _require(all(frozen.get(k) == v for k, v in base.items())
                     and all(frozen['source'].get(k) == v for k, v in expected.items()))
            _ref(frozen['source'])
            return frozen
        source = self._source(job, data)
        row = self.ledger.prepare(scope['org'], scope['project'], scope['campaign'],
            task_id=manifest['task_id'], parent_attempt_id=manifest['parent_attempt_id'],
            parent_attempt_fence=manifest['parent_attempt_fence'], request={**base, 'source': source},
            reservation_microusd=job['reservation_microusd'])
        return row['request']

    def _body(self, row, action, **extra):
        request = row['request']
        if action == 'submit':
            return {'action': action, **request}
        return {'action': action, **{k: request[k] for k in ('org_id', 'project_id', 'repository_id')},
                'operation_id': _operation(request), **extra}

    def _result(self, row, result, *, new_submission=False):
        _require(isinstance(result, dict) and result.get('operation_id') == _operation(row['request'])
                 and result.get('request') == row['request']
                 and _hex(result.get('registry_digest'))
                 and _integer(result.get('current_attempt'), 1, 99999999)
                 and isinstance(result.get('attempts'), list))
        if new_submission:
            _require(result['registry_digest'] == self.config['registry_digest'])
        # The authenticated admission producer stores the operation registry
        # generation in every attempt binding. A later global registry is not
        # the authority for this already-frozen request and job generation.
        for attempt in result['attempts']:
            _require(isinstance(attempt, dict))
            binding = attempt.get('binding')
            _require(isinstance(binding, dict)
                     and attempt.get('operation_id') == result['operation_id']
                     and _text(attempt.get('attempt_id'))
                     and re.fullmatch(re.escape(result['operation_id'][:48]) + r'-[0-9]{8}', attempt['attempt_id'])
                     and binding.get('operation_id') == result['operation_id']
                     and binding.get('attempt_id') == attempt['attempt_id']
                     and binding.get('registry_digest') == result['registry_digest']
                     and all(binding.get(k) == row['request'][k] for k in REQUEST - {'idempotency_key'}))
        return result

    def _request(self, row, action, **extra):
        return self._result(row, self._aws()[1].request(self._body(row, action, **extra)),
                            new_submission=action == 'submit')

    def _current(self, row, result):
        expected = _operation(row['request'])[:48] + '-%08d' % result['current_attempt']
        cursor_set = set()
        original = result['current_attempt']
        registry = result['registry_digest']
        for _ in range(100):
            _require(isinstance(result.get('attempts'), list))
            for attempt in result['attempts']:
                if isinstance(attempt, dict) and attempt.get('attempt_id') == expected:
                    _require(attempt.get('operation_id') == _operation(row['request']))
                    return attempt
            cursor = result.get('next_cursor')
            _require(_text(cursor) and cursor not in cursor_set)
            cursor_set.add(cursor)
            result = self._request(row, 'inspect', cursor=cursor)
            _require(result['current_attempt'] == original and result['registry_digest'] == registry)
        raise BridgeError(409)

    def _terminal(self, scope, row, job, attempt):
        request = row['request']
        binding = attempt.get('binding')
        _require(isinstance(binding, dict) and attempt.get('state') in TERMINAL
                 and attempt.get('resources_reconciled') is True
                 and binding.get('operation_id') == _operation(request)
                 and binding.get('attempt_id') == attempt['attempt_id']
                 and _hex(binding.get('registry_digest'))
                 and all(binding.get(k) == request[k] for k in REQUEST - {'idempotency_key'}))
        reserved = binding.get('reservation', {}).get('amount_microusd')
        _require(reserved == row['reservation_microusd'] and _integer(reserved))
        reference = _ref(attempt.get('evidence'))
        _require(reference['bucket'] == job['acceptance_bucket'] and reference['key'].startswith(job['acceptance_prefix']))
        evidence = _json(self._get(reference))
        locks = binding.get('resource_locks')
        _require(isinstance(locks, list) and all(_text(key) for key in locks)
                 and evidence.get('binding') == binding and evidence.get('producer') == job['acceptance_producer']
                 and evidence.get('status') == attempt['state'] and evidence.get('resources_reconciled') is True
                 and evidence.get('workers_terminal') is True
                 and evidence.get('resource_outcomes') == [{'key': key, 'state': 'reconciled'} for key in locks])
        basis = evidence.get('cost_basis')
        if basis == 'actual':
            amount = evidence.get('actual_cost_microusd')
            _require(_integer(amount, 0, reserved) and evidence.get('billed_cost_verified', True) is True)
        elif basis == 'reserved_maximum_pending_billing':
            _require(evidence.get('billed_cost_verified') is False and evidence.get('actual_cost_microusd') is None)
            if 'estimated_cost_microusd' in evidence:
                _require(_integer(evidence['estimated_cost_microusd'], 0, reserved))
            amount = reserved
        else:
            raise BridgeError(409)
        media = evidence.get('media_ref')
        safe_evidence = {'terminal_ref': reference}
        if media is not None:
            safe_evidence['media_ref'] = _ref(media)
        if row.get('settled_at') is not None and row['developer_attempt_id'] == attempt['attempt_id']:
            _require(row.get('cost_microusd') == amount and row.get('receipt_ref') == reference)
        else:
            self.ledger.settle(scope['org'], scope['project'], scope['campaign'], operation_id=_operation(request),
                developer_attempt_id=attempt['attempt_id'], cost_microusd=amount, receipt_ref=reference,
                resources_reconciled=True)
        return basis, safe_evidence

    def request(self, body, subject):
        scope, row, job = self._load(body, subject)
        _require(body['operation_id'] == _operation(row['request']))
        action = body['action']
        if action in {'submit', 'retry'}:
            _require(self.config['enabled'], 503)
        extra = {k: body[k] for k in ('cursor', 'expected_attempt_id') if k in body}
        if action == 'retry':
            result = self._request(row, 'inspect')
            attempt = self._current(row, result)
            expected = body['expected_attempt_id']
            if (row.get('expected_attempt_id') == expected
                    and attempt['attempt_id'] == row['developer_attempt_id']):
                self.ledger.record_admission(scope['org'], scope['project'], scope['campaign'],
                    operation_id=body['operation_id'], developer_attempt_id=attempt['attempt_id'])
                return result
            _require(attempt['attempt_id'] == expected and attempt['state'] in {'FAILED', 'CANCELLED'})
            self._terminal(scope, row, job, attempt)
            row = self.ledger.reserve_retry(scope['org'], scope['project'], scope['campaign'],
                operation_id=body['operation_id'], expected_attempt_id=expected,
                reservation_microusd=job['reservation_microusd'])
        try:
            result = self._request(row, action, **extra)
        except TransportError as error:
            if action == 'inspect' and error.status == 404:
                return {'error': 'operation_not_found'}
            raise
        current = _operation(row['request'])[:48] + '-%08d' % result['current_attempt']
        # Retry reservation can precede an interrupted POST. Inspection must
        # expose the old remote attempt so the same expected-attempt retry can
        # recover; it must not mark the reserved next attempt admitted.
        if action == 'inspect' and current == row.get('expected_attempt_id'):
            return result
        _require(current == row['developer_attempt_id'])
        self.ledger.record_admission(scope['org'], scope['project'], scope['campaign'],
            operation_id=body['operation_id'], developer_attempt_id=current)
        return result

    def receipt(self, body, subject):
        scope, row, job = self._load(body, subject)
        result = self._request(row, 'inspect')
        attempt = self._current(row, result)
        summary = dict(campaign_id=body['campaign_id'], task_id=body['task_id'],
            operation_id=_operation(row['request']), execution=attempt['state'], upload='pending',
            acceptance='pending', accounting='reserved', cleanup='unverified', evidence={})
        if attempt['attempt_id'] != row['developer_attempt_id']:
            _require(attempt['attempt_id'] == row.get('expected_attempt_id'))
            return summary
        if attempt['state'] in TERMINAL:
            basis, evidence = self._terminal(scope, row, job, attempt)
            summary.update(accounting=basis, cleanup='verified', evidence=evidence)
        return summary

    def handle(self, op, body, subject):
        fields = {'walk_doctor': {'enrollment_id', 'campaign_id'},
                  'walk_prepare': {'enrollment_id', 'manifest'},
                  'walk_read': {'enrollment_id', 'campaign_id', 'task_id'},
                  'walk_receipt': {'enrollment_id', 'campaign_id', 'task_id'},
                  'walk_request': {'enrollment_id', 'campaign_id', 'task_id', 'action', 'operation_id'}}
        _require(op in fields and isinstance(body, dict), 400)
        required = fields[op].copy()
        if op == 'walk_request':
            _require(body.get('action') in {'submit', 'inspect', 'recover', 'cancel', 'retry'}, 400)
            if body['action'] == 'retry':
                required.add('expected_attempt_id')
            if body['action'] == 'inspect' and 'cursor' in body:
                required.add('cursor')
        _require(set(body) == required, 400)
        for field in ('enrollment_id', 'campaign_id', 'task_id'):
            if field in body:
                _uuid(body[field])
        if op == 'walk_request':
            _require(_hex(body['operation_id']), 400)
            for field in ('cursor', 'expected_attempt_id'):
                if field in body:
                    _require(_text(body[field]), 400)
        if op == 'walk_read':
            result = self._load(body, subject)[1]['request']
        else:
            result = getattr(self, op.removeprefix('walk_'))(body, subject)
        return {'ok': True, 'result': result}


def handle(op, body, subject):
    try:
        from leaf_platform import campaign_developer_execution as ledger
        return WalkBridge(installation(), ledger).handle(op, body, subject)
    except BridgeError:
        raise
    except TransportError as error:
        raise BridgeError(403 if error.status in {401, 403} else 503) from None
    except (TimeoutError, ConnectionError, OSError):
        raise BridgeError(503) from None
    except Exception as error:
        from leaf_platform.campaigns import CampaignConflict, CampaignError, CampaignUnavailable
        if isinstance(error, CampaignConflict):
            status = 409
        elif isinstance(error, CampaignUnavailable):
            status = 503
        elif isinstance(error, CampaignError):
            status = 403 if error.code in {'worker_forbidden', 'project_unavailable'} else 400
        elif isinstance(error, (ValueError, TypeError, KeyError)):
            status = 400
        else:
            status = 503
        raise BridgeError(status) from None
