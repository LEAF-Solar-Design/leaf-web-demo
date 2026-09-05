"""Campaign execution ledger for trusted service adapters.

Evidence records completion; it does not grant provider or tenant authority.
All mutators lock the task before its attempts. Claimers skip locked tasks.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid

from psycopg.types.json import Jsonb

from .campaigns import (
    CampaignError, CampaignConflict, CampaignUnavailable, _scope, _uuid,
    _text, _secret, _fingerprint, _lock, _row, _cursor, _campaign, _missing,
)

STAGES = ('implementation', 'build_test', 'publication', 'deployment', 'verification', 'cleanup')
OUTWARD = frozenset(('publication', 'deployment', 'cleanup'))
SCOPE = 'org_id=%(org)s AND project_id=%(project)s AND campaign_id=%(campaign)s'


def _check(cur, scope):
    if _campaign(cur, scope) is None:
        _missing()


def _invalid(message='invalid execution request'):
    raise CampaignError('invalid_request', message)


def _conflict(code):
    raise CampaignConflict(code, code.replace('_', ' '))


def _strings(value, maximum, item_max=1024):
    _secret(value)
    if not isinstance(value, list) or len(value) > maximum:
        _invalid()
    for item in value:
        _text(item, 'list entry', item_max)
    return value


def _integer(value, low, high):
    _secret(value)
    try:
        return max(low, min(high, int(value)))
    except (ValueError, TypeError, OverflowError):
        _invalid()


def _event(cur, scope, task, event_type, attempt=None, payload=None):
    cur.execute(
        'INSERT INTO campaign_events (event_id, org_id, project_id, campaign_id, '
        'task_id, attempt_id, fence, event_type, payload) VALUES '
        '(%(id)s, %(org)s, %(project)s, %(campaign)s, %(task)s, %(attempt)s, '
        '%(fence)s, %(type)s, %(payload)s)',
        {**scope, 'id': uuid.uuid4(), 'task': task['task_id'],
         'attempt': attempt['attempt_id'] if attempt else None,
         'fence': attempt['fence'] if attempt else task['fence'],
         'type': event_type, 'payload': Jsonb(payload or {})})


def _task(cur, scope, task_id):
    cur.execute('SELECT * FROM campaign_tasks WHERE ' + SCOPE +
                ' AND task_id=%(task)s FOR UPDATE', {**scope, 'task': task_id})
    task = cur.fetchone()
    if task is None:
        _missing()
    return task


def submit_task(org_id, project_id, campaign_id, *, task_key, title, spec,
                capability, stages, owned_paths, source_sha, verify_command,
                declared_artifacts, depends_on, idempotency_key, kind='task',
                parent_task_id=None):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    for value, name, maximum in ((task_key, 'task_key', 128), (title, 'title', 200),
                                  (spec, 'spec', 16384), (capability, 'capability', 64),
                                  (source_sha, 'source_sha', 40),
                                  (verify_command, 'verify_command', 4096),
                                  (idempotency_key, 'idempotency_key', 128)):
        _text(value, name, maximum)
    _secret(kind)
    if (not re.fullmatch(r'[A-Za-z0-9._-]+', task_key)
            or not re.fullmatch(r'[a-z][a-z0-9._-]*', capability)
            or not re.fullmatch(r'[0-9a-f]{40}', source_sha)
            or kind not in ('task', 'capability')):
        _invalid()
    _strings(stages, 6, 32)
    if not stages or stages != [stage for stage in STAGES if stage in stages]:
        _invalid('stages must follow canonical order')
    _strings(owned_paths, 64)
    _strings(declared_artifacts, 32)
    _strings(depends_on, 128, 128)
    if len(set(depends_on)) != len(depends_on) or task_key in depends_on:
        _invalid('invalid dependencies')
    parent = _uuid(parent_task_id) if parent_task_id is not None else None
    payload = dict(task_key=task_key, title=title, spec=spec, capability=capability,
                   stages=stages, owned_paths=owned_paths, source_sha=source_sha,
                   verify_command=verify_command, declared_artifacts=declared_artifacts,
                   depends_on=sorted(depends_on), kind=kind,
                   parent_task_id=str(parent) if parent else None)
    fingerprint = _fingerprint('leaf.campaign.task.v1', payload)
    with _cursor() as cur:
        _check(cur, scope)
        # A shared submission lock also serializes distinct idempotency keys
        # naming the same task, without locking execution or dispatch.
        _lock(cur, f"campaign-submit:{scope['campaign']}")
        _lock(cur, f"{scope['campaign']}:{idempotency_key}")
        cur.execute('SELECT * FROM campaign_tasks WHERE ' + SCOPE +
                    ' AND (idempotency_key=%(key)s OR task_key=%(task_key)s)',
                    {**scope, 'key': idempotency_key, 'task_key': task_key})
        existing = cur.fetchall()
        if existing:
            if len(existing) != 1 or existing[0]['payload_fingerprint'] != fingerprint:
                _conflict('task_conflict')
            return _row(existing[0], replayed=True)
        if parent:
            cur.execute('SELECT task_id FROM campaign_tasks WHERE ' + SCOPE +
                        ' AND task_id=%(parent)s', {**scope, 'parent': parent})
            if cur.fetchone() is None:
                _invalid('parent task must belong to this campaign')
        cur.execute('SELECT task_id, task_key FROM campaign_tasks WHERE ' + SCOPE +
                    ' AND task_key=ANY(%(keys)s)', {**scope, 'keys': depends_on})
        dependencies = cur.fetchall()
        if len(dependencies) != len(depends_on):
            _invalid('dependency must already exist in this campaign')
        cur.execute(
            'INSERT INTO campaign_tasks (task_id, org_id, project_id, campaign_id, '
            'task_key, kind, parent_task_id, title, spec, capability, stages, owned_paths, '
            'source_sha, verify_command, declared_artifacts, idempotency_key, '
            'payload_fingerprint, current_stage) VALUES (%(id)s, %(org)s, %(project)s, '
            '%(campaign)s, %(task_key)s, %(kind)s, %(parent)s, %(title)s, %(spec)s, '
            '%(capability)s, %(stages)s, %(owned_paths)s, %(source_sha)s, %(verify_command)s, '
            '%(declared_artifacts)s, %(key)s, %(fingerprint)s, %(stage)s) RETURNING *',
            {**scope, **payload, 'id': uuid.uuid4(), 'parent': parent,
             'stages': Jsonb(stages), 'owned_paths': Jsonb(owned_paths),
             'declared_artifacts': Jsonb(declared_artifacts), 'key': idempotency_key,
             'fingerprint': fingerprint, 'stage': stages[0]})
        task = cur.fetchone()
        for dependency in dependencies:
            cur.execute('INSERT INTO campaign_task_dependencies '
                        '(org_id, project_id, campaign_id, task_id, depends_on_task_id) '
                        'VALUES (%(org)s, %(project)s, %(campaign)s, %(task)s, %(dependency)s)',
                        {**scope, 'task': task['task_id'], 'dependency': dependency['task_id']})
        _event(cur, scope, task, 'task_submitted')
        return _row(task)


def link_question(org_id, project_id, campaign_id, task_id, question_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    task_id, question_id = _uuid(task_id), _uuid(question_id)
    with _cursor() as cur:
        _check(cur, scope)
        task = _task(cur, scope, task_id)
        cur.execute('SELECT question_id FROM campaign_questions WHERE ' + SCOPE +
                    ' AND question_id=%(question)s', {**scope, 'question': question_id})
        if cur.fetchone() is None:
            _missing()
        params = {**scope, 'task': task_id, 'question': question_id}
        cur.execute('INSERT INTO campaign_task_questions '
                    '(org_id, project_id, campaign_id, task_id, question_id) VALUES '
                    '(%(org)s, %(project)s, %(campaign)s, %(task)s, %(question)s) '
                    'ON CONFLICT DO NOTHING RETURNING *', params)
        row = cur.fetchone()
        if row:
            _event(cur, scope, task, 'question_linked', payload={'question_id': str(question_id)})
            return _row(row)
        cur.execute('SELECT * FROM campaign_task_questions WHERE ' + SCOPE +
                    ' AND task_id=%(task)s AND question_id=%(question)s', params)
        return _row(cur.fetchone(), replayed=True)


def _operation_key(scope, task):
    if task['current_stage'] not in OUTWARD:
        return None
    return _fingerprint('leaf.campaign.operation.v1', {
        'campaign': str(scope['campaign']), 'task': str(task['task_id']),
        'stage': task['current_stage']})


def _values(outcome, result, artifact_ref, outward_operation_key,
            resource_identity, rollback_identity, verified):
    _secret(outcome)
    _secret(result)
    if outcome not in ('succeeded', 'failed', 'unknown') or not isinstance(result, dict):
        _invalid()
    if not isinstance(verified, bool):
        _invalid()
    for value, maximum in ((artifact_ref, 1024), (outward_operation_key, 256),
                           (resource_identity, 1024), (rollback_identity, 1024)):
        if value is not None:
            _text(value, 'receipt reference', maximum)
    values = dict(outcome=outcome, result=result, artifact_ref=artifact_ref,
                  outward_operation_key=outward_operation_key,
                  resource_identity=resource_identity, rollback_identity=rollback_identity,
                  verified=verified)
    try:
        encoded = json.dumps(result, allow_nan=False).encode()
        if len(encoded) > 65536:
            _invalid('result too large')
        values['result_fingerprint'] = _fingerprint('leaf.campaign.receipt.v1', values)
    except (ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, CampaignError):
            raise
        _invalid('result must be JSON')
    if outcome == 'unknown' and not outward_operation_key:
        _invalid('unknown outcome requires an operation key')
    return values


def _evidence(task, values):
    if values['outcome'] != 'succeeded':
        return
    stage, result = task['current_stage'], values['result']
    artifact, key = values['artifact_ref'], values['outward_operation_key']
    sufficient = {
        'implementation': bool(artifact),
        'build_test': bool(artifact) and type(result.get('exit_code')) is int
                      and result['exit_code'] == 0
                      and result.get('verify_command') == task['verify_command'],
        'publication': bool(artifact and key),
        'deployment': bool(values['resource_identity'] and values['rollback_identity'] and key),
        'verification': values['verified'] is True and bool(result.get('observed')),
        'cleanup': bool(values['resource_identity'] and key),
    }[stage]
    if not sufficient:
        raise CampaignError('insufficient_evidence', 'stage evidence is incomplete')


def _receipt(cur, scope, task, attempt, values, reconciles=None):
    cur.execute(
        'INSERT INTO campaign_stage_receipts (receipt_id, org_id, project_id, campaign_id, '
        'task_id, attempt_id, stage, fence, outcome, result, result_fingerprint, artifact_ref, '
        'outward_operation_key, resource_identity, rollback_identity, verified, reconciles_receipt_id) '
        'VALUES (%(id)s, %(org)s, %(project)s, %(campaign)s, %(task)s, %(attempt)s, %(stage)s, '
        '%(fence)s, %(outcome)s, %(result)s, %(result_fingerprint)s, %(artifact_ref)s, '
        '%(outward_operation_key)s, %(resource_identity)s, %(rollback_identity)s, %(verified)s, '
        '%(reconciles)s) RETURNING *',
        {**scope, **values, 'id': uuid.uuid4(), 'task': task['task_id'],
         'attempt': attempt['attempt_id'], 'stage': attempt['stage'], 'fence': attempt['fence'],
         'result': Jsonb(values['result']), 'reconciles': reconciles})
    return cur.fetchone()


def _advance(cur, scope, task, outcome):
    stage = task['current_stage']
    status = {'failed': 'failed', 'unknown': 'reconcile_required', 'succeeded': 'succeeded'}[outcome]
    if outcome == 'succeeded':
        index = task['stages'].index(stage) + 1
        if index < len(task['stages']):
            stage, status = task['stages'][index], 'pending'
    cur.execute('UPDATE campaign_tasks SET status=%(status)s, current_stage=%(stage)s, '
                'updated_at=NOW() WHERE ' + SCOPE + ' AND task_id=%(task)s',
                {**scope, 'task': task['task_id'], 'status': status, 'stage': stage})


def claim_task(org_id, project_id, campaign_id, *, worker_id, lease_seconds,
               budget_reservation_ref=None):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    _text(worker_id, 'worker_id', 128)
    lease = _integer(lease_seconds, 30, 3600)
    if budget_reservation_ref is not None:
        _text(budget_reservation_ref, 'budget_reservation_ref', 256)
    with _cursor() as cur:
        _check(cur, scope)
        # Never update attempts before locking their task. Concurrent expiry
        # cannot overwrite a newly claimed task or deadlock with settlement.
        cur.execute('SELECT * FROM campaign_tasks WHERE ' + SCOPE +
                    " AND status='claimed' ORDER BY task_id FOR UPDATE SKIP LOCKED", scope)
        for task in cur.fetchall():
            params = {**scope, 'task': task['task_id']}
            cur.execute("UPDATE campaign_task_attempts SET status='expired' WHERE " + SCOPE +
                        " AND task_id=%(task)s AND status='active' AND deadline_at<=clock_timestamp() RETURNING *",
                        params)
            attempt = cur.fetchone()
            if attempt is None:
                continue
            _event(cur, scope, task, 'attempt_expired', attempt)
            if attempt['stage'] in OUTWARD:
                values = _values('unknown', {'reason': 'lease_expired'}, None,
                                 attempt['outward_operation_key'], None, None, False)
                _receipt(cur, scope, task, attempt, values)
                _advance(cur, scope, task, 'unknown')
                _event(cur, scope, task, 'outcome_unknown', attempt,
                       {'outward_operation_key': attempt['outward_operation_key']})
            else:
                cur.execute("UPDATE campaign_tasks SET status='pending', updated_at=NOW() WHERE " +
                            SCOPE + ' AND task_id=%(task)s', params)
        cur.execute(
            'SELECT t.* FROM campaign_tasks t WHERE t.org_id=%(org)s AND t.project_id=%(project)s '
            "AND t.campaign_id=%(campaign)s AND t.status='pending' AND NOT EXISTS ("
            'SELECT 1 FROM campaign_task_dependencies d JOIN campaign_tasks p '
            'ON p.task_id=d.depends_on_task_id AND p.org_id=d.org_id AND p.project_id=d.project_id '
            'AND p.campaign_id=d.campaign_id WHERE d.task_id=t.task_id AND d.org_id=t.org_id '
            "AND d.project_id=t.project_id AND d.campaign_id=t.campaign_id AND p.status<>'succeeded') "
            'AND NOT EXISTS (SELECT 1 FROM campaign_task_questions l JOIN campaign_questions q '
            'ON q.question_id=l.question_id AND q.org_id=l.org_id AND q.project_id=l.project_id '
            'AND q.campaign_id=l.campaign_id WHERE l.task_id=t.task_id AND l.org_id=t.org_id '
            'AND l.project_id=t.project_id AND l.campaign_id=t.campaign_id '
            "AND q.status='open' AND q.blocks_dispatch) "
            'ORDER BY t.created_at, t.task_id LIMIT 1 FOR UPDATE OF t SKIP LOCKED', scope)
        task = cur.fetchone()
        if task is None:
            return None
        cur.execute("UPDATE campaign_tasks SET fence=fence+1, status='claimed', updated_at=NOW() WHERE " +
                    SCOPE + ' AND task_id=%(task)s RETURNING *', {**scope, 'task': task['task_id']})
        task = cur.fetchone()
        token = secrets.token_hex(32)
        cur.execute(
            'INSERT INTO campaign_task_attempts (attempt_id, task_id, org_id, project_id, campaign_id, '
            'fence, attempt_token_hash, worker_id, stage, deadline_at, status, '
            'budget_reservation_ref, outward_operation_key) VALUES (%(id)s, %(task)s, %(org)s, '
            '%(project)s, %(campaign)s, %(fence)s, %(hash)s, %(worker)s, %(stage)s, '
            "clock_timestamp()+make_interval(secs => %(lease)s), 'active', %(budget)s, %(key)s) RETURNING *",
            {**scope, 'id': uuid.uuid4(), 'task': task['task_id'], 'fence': task['fence'],
             'hash': hashlib.sha256(token.encode()).hexdigest(), 'worker': worker_id,
             'stage': task['current_stage'], 'lease': lease, 'budget': budget_reservation_ref,
             'key': _operation_key(scope, task)})
        attempt = cur.fetchone()
        _event(cur, scope, task, 'attempt_claimed', attempt)
        result = _public_attempt(attempt)
        result['attempt_token'] = token
        result.update({key: _row(task)[key] for key in (
            'task_key', 'kind', 'parent_task_id', 'spec', 'owned_paths', 'source_sha',
            'verify_command', 'declared_artifacts')})
        return result


def _public_attempt(attempt):
    return _row({key: value for key, value in attempt.items() if key != 'attempt_token_hash'})


def settle_attempt(org_id, project_id, campaign_id, attempt_id, *, attempt_token,
                   fence, outcome, result, artifact_ref=None, outward_operation_key=None,
                   resource_identity=None, rollback_identity=None, verified=False):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    attempt_id = _uuid(attempt_id)
    _text(attempt_token, 'attempt_token', 64)
    _secret(fence)
    values = _values(outcome, result, artifact_ref, outward_operation_key,
                     resource_identity, rollback_identity, verified)
    with _cursor() as cur:
        _check(cur, scope)
        params = {**scope, 'attempt': attempt_id}
        cur.execute('SELECT task_id FROM campaign_task_attempts WHERE ' + SCOPE +
                    ' AND attempt_id=%(attempt)s', params)
        pointer = cur.fetchone()
        if pointer is None:
            _missing()
        task = _task(cur, scope, pointer['task_id'])
        _lock(cur, attempt_id)
        cur.execute('SELECT *, deadline_at<=clock_timestamp() AS overdue FROM campaign_task_attempts WHERE ' +
                    SCOPE + ' AND attempt_id=%(attempt)s FOR UPDATE', params)
        attempt = cur.fetchone()
        if (not hmac.compare_digest(hashlib.sha256(attempt_token.encode()).hexdigest(),
                                    attempt['attempt_token_hash']) or fence != attempt['fence']):
            _conflict('stale_attempt')
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                    ' AND attempt_id=%(attempt)s', params)
        receipt = cur.fetchone()
        # Expiry receipts are recovery history, never worker-accepted results.
        if attempt['status'] == 'expired':
            _conflict('stale_attempt')
        if receipt:
            if receipt['result_fingerprint'] != values['result_fingerprint']:
                _conflict('settlement_conflict')
            return _row(receipt, replayed=True)
        if attempt['status'] != 'active' or attempt['overdue'] or fence != task['fence']:
            _conflict('stale_attempt')
        if attempt['outward_operation_key'] != outward_operation_key:
            _conflict('reconcile_identity_mismatch')
        _evidence(task, values)
        receipt = _receipt(cur, scope, task, attempt, values)
        cur.execute("UPDATE campaign_task_attempts SET status='settled', settled_at=NOW() WHERE " +
                    SCOPE + ' AND attempt_id=%(attempt)s', params)
        _advance(cur, scope, task, outcome)
        _event(cur, scope, task, {'succeeded': 'stage_succeeded', 'failed': 'stage_failed',
                                'unknown': 'outcome_unknown'}[outcome], attempt)
        return _row(receipt)


def reconcile_outward(org_id, project_id, campaign_id, task_id, *, outward_operation_key,
                      outcome, result, artifact_ref=None, resource_identity=None,
                      rollback_identity=None, verified=False):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    task_id = _uuid(task_id)
    values = _values(outcome, result, artifact_ref, outward_operation_key,
                     resource_identity, rollback_identity, verified)
    if outcome == 'unknown':
        _invalid()
    with _cursor() as cur:
        _check(cur, scope)
        task = _task(cur, scope, task_id)
        params = {**scope, 'task': task_id, 'key': outward_operation_key}
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                    " AND task_id=%(task)s AND outcome='unknown' AND outward_operation_key=%(key)s "
                    'ORDER BY fence DESC LIMIT 1', params)
        unknown = cur.fetchone()
        if unknown is None:
            _conflict('reconcile_identity_mismatch')
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                    ' AND reconciles_receipt_id=%(unknown)s', {**scope, 'unknown': unknown['receipt_id']})
        existing = cur.fetchone()
        if existing:
            if existing['result_fingerprint'] != values['result_fingerprint']:
                _conflict('settlement_conflict')
            return _row(existing, replayed=True)
        if (task['status'] != 'reconcile_required' or task['current_stage'] != unknown['stage']
                or task['fence'] != unknown['fence']):
            _conflict('reconcile_identity_mismatch')
        _evidence(task, values)
        cur.execute('UPDATE campaign_tasks SET fence=fence+1, updated_at=NOW() WHERE ' + SCOPE +
                    ' AND task_id=%(task)s RETURNING *', params)
        task = cur.fetchone()
        cur.execute(
            'INSERT INTO campaign_task_attempts (attempt_id, task_id, org_id, project_id, campaign_id, '
            'fence, attempt_token_hash, worker_id, stage, deadline_at, settled_at, status, outward_operation_key) '
            'VALUES (%(id)s, %(task)s, %(org)s, %(project)s, %(campaign)s, %(fence)s, %(hash)s, '
            "'reconciliation', %(stage)s, NOW(), NOW(), 'settled', %(key)s) RETURNING *",
            {**params, 'id': uuid.uuid4(), 'fence': task['fence'],
             'hash': hashlib.sha256(secrets.token_bytes(32)).hexdigest(), 'stage': unknown['stage']})
        attempt = cur.fetchone()
        receipt = _receipt(cur, scope, task, attempt, values, unknown['receipt_id'])
        _advance(cur, scope, task, outcome)
        _event(cur, scope, task, 'reconciled', attempt,
               {'reconciles_receipt_id': str(unknown['receipt_id'])})
        return _row(receipt)


def retry_task(org_id, project_id, campaign_id, task_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    task_id = _uuid(task_id)
    with _cursor() as cur:
        _check(cur, scope)
        task = _task(cur, scope, task_id)
        if task['status'] == 'reconcile_required':
            _conflict('reconcile_required')
        if task['status'] != 'failed':
            _conflict('task_conflict')
        cur.execute("UPDATE campaign_tasks SET status='pending', updated_at=NOW() WHERE " + SCOPE +
                    ' AND task_id=%(task)s RETURNING *', {**scope, 'task': task_id})
        task = cur.fetchone()
        _event(cur, scope, task, 'task_retried')
        return _row(task)


def read_execution(org_id, project_id, campaign_id, *, limit=200):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    limit = _integer(limit, 1, 200)
    with _cursor() as cur:
        _check(cur, scope)
        cur.execute('SELECT * FROM campaign_tasks WHERE ' + SCOPE +
                    ' ORDER BY created_at, task_id LIMIT %(limit)s', {**scope, 'limit': limit})
        tasks = [_row(row) for row in cur.fetchall()]
        for task in tasks:
            params = {**scope, 'task': _uuid(task['task_id'])}
            cur.execute('SELECT p.task_key FROM campaign_task_dependencies d JOIN campaign_tasks p '
                        'ON p.task_id=d.depends_on_task_id AND p.org_id=d.org_id '
                        'AND p.project_id=d.project_id AND p.campaign_id=d.campaign_id '
                        'WHERE d.org_id=%(org)s AND d.project_id=%(project)s '
                        'AND d.campaign_id=%(campaign)s AND d.task_id=%(task)s ORDER BY p.task_key', params)
            task['depends_on'] = [row['task_key'] for row in cur.fetchall()]
            cur.execute('SELECT q.question_id FROM campaign_task_questions l JOIN campaign_questions q '
                        'ON q.question_id=l.question_id AND q.org_id=l.org_id '
                        'AND q.project_id=l.project_id AND q.campaign_id=l.campaign_id '
                        'WHERE l.org_id=%(org)s AND l.project_id=%(project)s '
                        'AND l.campaign_id=%(campaign)s AND l.task_id=%(task)s '
                        "AND q.status='open' AND q.blocks_dispatch ORDER BY q.question_id", params)
            task['blocked_by_questions'] = [str(row['question_id']) for row in cur.fetchall()]
            cur.execute('SELECT attempt_id, task_id, fence, worker_id, stage, claimed_at, deadline_at, '
                        'settled_at, status, budget_reservation_ref, outward_operation_key '
                        'FROM campaign_task_attempts WHERE ' + SCOPE +
                        " AND task_id=%(task)s AND status='active'", params)
            task['active_attempt'] = _row(cur.fetchone())
        cur.execute('SELECT * FROM campaign_questions WHERE ' + SCOPE +
                    " AND status='open' AND blocks_dispatch ORDER BY created_at, question_id", scope)
        questions = [_row(row) for row in cur.fetchall()]
        for question in questions:
            cur.execute('SELECT task_id FROM campaign_task_questions WHERE ' + SCOPE +
                        ' AND question_id=%(question)s ORDER BY task_id',
                        {**scope, 'question': _uuid(question['question_id'])})
            question['task_ids'] = [str(row['task_id']) for row in cur.fetchall()]
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                    ' ORDER BY created_at DESC, receipt_id DESC LIMIT %(limit)s', {**scope, 'limit': limit})
        receipts = [_row(row) for row in cur.fetchall()]
        cur.execute('SELECT * FROM campaign_events WHERE ' + SCOPE +
                    ' ORDER BY seq DESC LIMIT %(limit)s', {**scope, 'limit': limit})
        return {'tasks': tasks, 'pending_questions': questions, 'receipts': receipts,
                'events': [_row(row) for row in cur.fetchall()]}
