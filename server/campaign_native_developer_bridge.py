"""Registered native-change adapter for authenticated campaign workers.

The registered workflow can plan and apply. Execution mode belongs to trusted
worker routes, not this adapter. The worker obtains operation/attempt-bound
review material; admission_request and the native executor verify saved-plan,
source, ownership and approved apply authority. This broker neither accepts nor
produces that authority. Parent task completion remains with its owner.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from campaign_bridge import BridgeError
from developer_jobs_transport import DeveloperJobsTransport, TransportError

LIMIT = 128 * 1024
OPS = frozenset({'native_claim', 'native_prepare', 'native_read', 'native_request', 'native_receipt'})
REQUEST = {'org_id', 'project_id', 'repository_id', 'campaign_id', 'task_id',
           'parent_attempt_id', 'parent_attempt_fence', 'commit_sha', 'source',
           'job_name', 'environment', 'idempotency_key'}
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED'}
CONFIG = {'version', 'enabled', 'endpoint', 'region', 'role_arn', 'registry_digest', 'jobs'}
JOB = {'org_id', 'project_id', 'repository_id', 'job_name', 'environment', 'commit_sha',
       'source_bucket', 'source_prefix', 'acceptance_bucket', 'acceptance_prefix',
       'acceptance_producer', 'runtime_seconds', 'reservation_microusd', 'executor_digest'}

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


def _prefix(value):
    return (_text(value) and value.endswith('/') and not value.startswith('/')
            and all(part not in {'', '.', '..'} for part in value[:-1].split('/')))


def _validate_installation(value):
    _require(isinstance(value, dict) and set(value) == CONFIG, 503)
    _require(type(value['version']) is int and value['version'] == 1
             and type(value['enabled']) is bool and _hex(value['registry_digest']), 503)
    DeveloperJobsTransport(value['endpoint'], region=value['region'], role_arn=value['role_arn'],
                           boto3_session=object())
    _require(isinstance(value['jobs'], list) and 1 <= len(value['jobs']) <= 16, 503)
    seen = set()
    for job in value['jobs']:
        _require(isinstance(job, dict) and set(job) == JOB, 503)
        _uuid(job['org_id']); _uuid(job['project_id'])
        _require(all(_text(job[k]) for k in JOB - {'runtime_seconds', 'reservation_microusd'})
                 and job['environment'] == 'staging'
                 and _hex(job['commit_sha'], 40) and _hex(job['executor_digest'])
                 and _integer(job['runtime_seconds'], 1, 3600)
                 and _integer(job['reservation_microusd'])
                 and _prefix(job['source_prefix']) and _prefix(job['acceptance_prefix']), 503)
        key = (job['org_id'], job['project_id'])
        _require(key not in seen, 503)
        seen.add(key)
    return value


def installation():
    path = os.environ.get('LEAF_NATIVE_DEVELOPER_INSTALLATION_FILE')
    _require(_text(path), 503)
    with Path(path).open('rb') as stream:
        return _validate_installation(_json(stream.read(LIMIT + 1)))


def _resolve(enrollment_id, campaign_id, task_id, parent_id, fence, active, subject):
    import campaign_bridge
    import campaign_worker_service
    enrollment, execution, _ = campaign_worker_service._platform()
    with execution._cursor() as cur:
        scope = campaign_bridge._scope(cur, enrollment, enrollment_id, subject)
        _require(str(scope['campaign']) == campaign_id, 403)
        cur.execute('SELECT * FROM campaign_tasks WHERE ' + execution.SCOPE + ' AND task_id=%(task)s',
                    dict(scope, task=uuid.UUID(task_id)))
        task = cur.fetchone()
        _require(task is not None and task['kind'] == 'task'
                 and task['capability'] == 'terraform.native', 403)
        if parent_id is not None:
            cur.execute('SELECT *,deadline_at<=clock_timestamp() AS overdue FROM campaign_task_attempts WHERE '
                        + execution.SCOPE + ' AND attempt_id=%(attempt)s AND worker_id=%(worker)s',
                        dict(scope, attempt=uuid.UUID(parent_id), worker='enrollment-' + enrollment_id))
            attempt = cur.fetchone()
            _require(attempt is not None and str(attempt['task_id']) == task_id
                     and attempt['stage'] == 'implementation' and attempt['fence'] == fence, 403)
            if active:
                _require(attempt['status'] == 'active' and not attempt['overdue']
                         and task['status'] == 'claimed' and task['current_stage'] == 'implementation'
                         and task['fence'] == fence)
        return scope, task


def _clients(config):
    import boto3
    from botocore.config import Config
    bounded = Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1})
    session = boto3.Session(region_name=config['region'])
    # Assume once and share the same workload identity for object and API access.
    if config['role_arn'] is not None:
        credentials = session.client('sts', config=bounded).assume_role(
            RoleArn=config['role_arn'], RoleSessionName='campaign-native-change')['Credentials']
        session = boto3.Session(aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'], aws_session_token=credentials['SessionToken'],
            region_name=config['region'])
    return session.client('s3', config=bounded), DeveloperJobsTransport(
        config['endpoint'], region=config['region'], boto3_session=session)


class NativeBridge:
    def __init__(self, config, ledger, *, resolve=_resolve, clients=_clients):
        self.config = _validate_installation(config)
        self.ledger, self.resolve, self.clients = ledger, resolve, clients
        self._io = None

    def _aws(self):
        if self._io is None:
            self._io = self.clients(self.config)
        return self._io

    def _job(self, scope):
        jobs = [j for j in self.config['jobs'] if j['org_id'] == str(scope['org'])
                and j['project_id'] == str(scope['project'])]
        _require(len(jobs) == 1, 403)
        return jobs[0]

    def claim(self, body, subject):
        _require(self.config['enabled'], 503)
        import campaign_bridge
        import campaign_worker_service
        enrollment, execution, _ = campaign_worker_service._platform()
        with execution._cursor() as cur:
            scope = campaign_bridge._scope(cur, enrollment, body['enrollment_id'], subject)
            _require(str(scope['campaign']) == body['campaign_id'], 403)
            job = self._job(scope)
            cur.execute('SELECT * FROM campaign_tasks WHERE ' + execution.SCOPE
                        + ' AND task_key=%(key)s FOR UPDATE', dict(scope, key=body['task_key']))
            task = cur.fetchone()
            _require(task is not None and task['kind'] == 'task'
                     and task['capability'] == 'terraform.native'
                     and task['current_stage'] == 'implementation'
                     and task['source_sha'] == job['commit_sha'], 403)
            result = execution._claim_task_cursor(cur, scope,
                worker_id='enrollment-' + body['enrollment_id'], lease=900, task_key=body['task_key'])
            if result is None:
                return None
            return {k: result[k] for k in ('task_id', 'attempt_id', 'fence', 'stage', 'deadline_at')}

    def _get(self, reference, maximum=LIMIT):
        ref = _ref(reference)
        obj = self._aws()[0].get_object(Bucket=ref['bucket'], Key=ref['key'], VersionId=ref['version_id'])
        stream = obj['Body']
        try:
            _require(obj.get('VersionId') == ref['version_id']
                     and _integer(obj.get('ContentLength'), 1, maximum))
            data = stream.read(maximum + 1)
            _require(isinstance(data, bytes) and len(data) == obj['ContentLength']
                     and hashlib.sha256(data).hexdigest() == ref['sha256'])
            return data
        finally:
            stream.close()

    def prepare(self, body, subject):
        _require(self.config['enabled'], 503)
        scope, task = self.resolve(body['enrollment_id'], body['campaign_id'], body['task_id'],
            body['parent_attempt_id'], body['parent_attempt_fence'], True, subject)
        job = self._job(scope)
        source = _ref(body['source'])
        _require(source['bucket'] == job['source_bucket'] and source['key'].startswith(job['source_prefix']))
        manifest = _json(self._get(source))
        _require(set(manifest) == {'schema_version', 'commit', 'archive', 'bundle'}
                 and type(manifest['schema_version']) is int and manifest['schema_version'] == 1
                 and manifest['commit'] == task['source_sha'] == job['commit_sha'])
        for name in ('archive', 'bundle'):
            ref = _ref(manifest[name])
            _require(ref['bucket'] == job['source_bucket'] and ref['key'].startswith(job['source_prefix']))
        request = {k: job[k] for k in ('org_id', 'project_id', 'repository_id', 'commit_sha', 'job_name', 'environment')}
        request.update({k: body[k] for k in ('campaign_id', 'task_id', 'parent_attempt_id', 'parent_attempt_fence')})
        request['source'] = source
        # Preserve the original key format for prepared-request recovery. This
        # historical label never selected or restricted the worker execution mode.
        request['idempotency_key'] = 'native-plan-v1:' + _sha(request)
        row = self.ledger.prepare(scope['org'], scope['project'], scope['campaign'],
            task_id=body['task_id'], parent_attempt_id=body['parent_attempt_id'],
            parent_attempt_fence=body['parent_attempt_fence'], request=request,
            reservation_microusd=job['reservation_microusd'])
        return row['request']

    def _load(self, body, subject):
        scope, task = self.resolve(body['enrollment_id'], body['campaign_id'], body['task_id'],
                                   None, None, False, subject)
        row = self.ledger.read(scope['org'], scope['project'], scope['campaign'], body['task_id'])
        _require(isinstance(row, dict) and isinstance(row.get('request'), dict))
        request = row['request']
        _require(set(request) == REQUEST and request['campaign_id'] == body['campaign_id']
                 and request['task_id'] == body['task_id'])
        self.resolve(body['enrollment_id'], body['campaign_id'], body['task_id'],
                     request['parent_attempt_id'], request['parent_attempt_fence'], False, subject)
        job = self._job(scope)
        _require(all(request[k] == job[k] for k in
                     ('org_id', 'project_id', 'repository_id', 'commit_sha', 'job_name', 'environment')))
        _require(request['commit_sha'] == task['source_sha'])
        source = _ref(request['source'])
        _require(source['bucket'] == job['source_bucket'] and source['key'].startswith(job['source_prefix']))
        return scope, row, job

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
        _require(binding.get('executor_digest') == job['executor_digest']
                 and binding.get('runtime_seconds') == job['runtime_seconds'])
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
        safe_evidence = {'terminal_ref': reference}
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
            frozen = row['request']
            self.resolve(body['enrollment_id'], body['campaign_id'], body['task_id'],
                frozen['parent_attempt_id'], frozen['parent_attempt_fence'], True, subject)
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
        common = {'enrollment_id', 'campaign_id', 'task_id'}
        fields = {'native_claim': {'enrollment_id', 'campaign_id', 'task_key'},
                  'native_prepare': common | {'parent_attempt_id', 'parent_attempt_fence', 'source'},
                  'native_read': common, 'native_receipt': common,
                  'native_request': common | {'action', 'operation_id'}}
        _require(op in fields and isinstance(body, dict), 400)
        required = fields[op].copy()
        if op == 'native_request':
            _require(body.get('action') in {'submit', 'inspect', 'cancel', 'retry', 'recover'}, 400)
            if body['action'] == 'retry':
                required.add('expected_attempt_id')
            if body['action'] == 'inspect' and 'cursor' in body:
                required.add('cursor')
        _require(set(body) == required and len(_raw(body)) <= LIMIT, 400)
        for key in ('enrollment_id', 'campaign_id', 'task_id', 'parent_attempt_id'):
            if key in body:
                _uuid(body[key])
        if 'parent_attempt_fence' in body:
            _require(_integer(body['parent_attempt_fence']), 400)
        for key in ('task_key', 'expected_attempt_id', 'cursor'):
            if key in body:
                _require(_text(body[key]), 400)
        if 'operation_id' in body:
            _require(_hex(body['operation_id']), 400)
        result = (self._load(body, subject)[1]['request'] if op == 'native_read'
                  else getattr(self, op.removeprefix('native_'))(body, subject))
        return {'ok': True, 'result': result}


def handle(op, body, subject):
    try:
        from leaf_platform import campaign_developer_execution as ledger
        return NativeBridge(installation(), ledger).handle(op, body, subject)
    except BridgeError:
        raise
    except TransportError as error:
        raise BridgeError(403 if error.status in {401, 403} else 503) from None
    except (ValueError, TypeError, KeyError):
        raise BridgeError(400) from None
    except Exception as error:
        from leaf_platform.campaigns import CampaignConflict, CampaignError
        status = 409 if isinstance(error, CampaignConflict) else 403 if isinstance(error, CampaignError) else 503
        raise BridgeError(status) from None
