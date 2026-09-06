"""Atomic semantic adoption for the enrolled campaign planning workflow."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import shlex
import uuid

from . import campaign_enrollment as enrollment
from . import campaign_execution as execution
from . import campaigns
from .campaign_plan import validate_plan

ACCEPTANCE = 'leaf.campaign.plan-adoption.v1'
MAX_BYTES = 262144
MAX_BASE64 = 349528
_DIGEST = re.compile(r'[0-9a-f]{64}')


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _counter(value, low=0, high=9223372036854775807):
    return type(value) is int and low <= value <= high


def _safe_key(value):
    return (isinstance(value, str) and 1 <= len(value) <= 1024
            and re.fullmatch(r'[A-Za-z0-9._/-]+', value) is not None
            and all(part not in ('', '.', '..') for part in value.split('/')))


def validate_request(*, task_id, attempt_id, fence, result_fingerprint,
                     plan_sha256, plan_size_bytes, plan_b64):
    """Bound closed wire values before any authority or artifact lookup."""
    for value in (task_id, attempt_id):
        if not isinstance(value, str) or len(value) != 36:
            execution._invalid()
        execution._uuid(value)
    if (not _counter(fence) or not _digest(result_fingerprint) or not _digest(plan_sha256)
            or not _counter(plan_size_bytes, 1, MAX_BYTES)
            or not isinstance(plan_b64, str) or not 1 <= len(plan_b64) <= MAX_BASE64):
        execution._invalid()


def _saved_context(cur, scope, task_id):
    # The task lock always precedes attempt locks, including restart reads.
    cur.execute('SELECT * FROM campaign_tasks WHERE ' + execution.SCOPE +
                ' AND task_id=%(task)s FOR UPDATE',
                dict(scope, task=execution._uuid(task_id)))
    task = cur.fetchone()
    if task is None or task['task_key'] != 'campaign-plan' or task['capability'] != 'campaign.plan':
        return None
    params = dict(scope, task=task['task_id'],
                  worker='enrollment-' + str(scope['enrollment_id']))
    cur.execute('SELECT * FROM campaign_task_attempts WHERE ' + execution.SCOPE +
                " AND task_id=%(task)s AND stage='implementation' AND status='settled' "
                'AND worker_id=%(worker)s ORDER BY fence DESC FOR UPDATE', params)
    attempts = cur.fetchall()
    for attempt in attempts:
        binding = execution._dispatch_binding(cur, scope, attempt)
        if (binding is None or binding['state'] != 'settled'
                or binding['machine_id'] != scope['machine_id']
                or binding['source_ref'] != task['source_sha']
                or not _counter(binding['remote_fencing_token'])
                or not isinstance(binding['leaf_id'], str) or not binding['leaf_id']
                or not isinstance(binding['run_id'], str) or not binding['run_id']):
            continue
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + execution.SCOPE +
                    " AND task_id=%(task)s AND stage='implementation' AND outcome='succeeded' "
                    'AND attempt_id=%(attempt)s AND fence=%(fence)s',
                    dict(params, attempt=attempt['attempt_id'], fence=attempt['fence']))
        receipt = cur.fetchone()
        if receipt is None:
            continue
        result = receipt['result']
        if not isinstance(result, dict):
            continue
        product = result.get('product')
        if (result.get('result_binding') != 'bound'
                or result.get('requested_source_sha') != task['source_sha']
                or not _digest(result.get('result_fingerprint'))
                or not isinstance(product, dict) or product.get('verified') is not True
                or product.get('path') != '.leaf/campaign-plan.json'
                or not _safe_key(product.get('key')) or not _digest(product.get('sha256'))
                or not _counter(product.get('size_bytes'), 1, MAX_BYTES)
                or receipt['artifact_ref'] != product['key']):
            continue
        source = dict(task_id=str(task['task_id']), attempt_id=str(attempt['attempt_id']),
                      fence=attempt['fence'], leaf_id=binding['leaf_id'], run_id=binding['run_id'],
                      remote_fencing_token=binding['remote_fencing_token'],
                      result_fingerprint=result['result_fingerprint'],
                      product={key: product[key] for key in ('path', 'key', 'sha256', 'size_bytes')})
        return task, receipt, source
    return None


def saved_plan_source(cur, scope, task_id):
    """Project a saved implementation result under the caller's enrollment lock."""
    context = _saved_context(cur, scope, task_id)
    return context[2] if context else None


def _response(receipt, *, replayed=False):
    result = receipt['result']
    return dict(ok=True, receipt=dict(receipt_id=str(receipt['receipt_id']),
                task_id=str(receipt['task_id']), stage='build_test', outcome='succeeded',
                replayed=replayed),
                adopted=dict(tasks=len(result['task_ids']),
                             capability_tasks=len(result['capability_task_ids']),
                             questions=len(result['question_ids'])))


def _capability(cur, scope, planning, name, digest):
    suffix = hashlib.sha256(name.encode()).hexdigest()[:24]
    key = 'CAP-' + suffix
    path = '.leaf/capabilities/' + suffix + '/capability.json'
    task = execution._submit_task_cursor(
        cur, scope, task_key=key, idempotency_key='plan:' + digest[:16] + ':' + key,
        kind='capability', title='Implement capability ' + name, capability=name,
        source_sha=planning['source_sha'], parent_task_id=planning['task_id'],
        stages=['implementation', 'build_test', 'publication', 'verification'],
        owned_paths=[path], declared_artifacts=['capability-contract'],
        verify_command=shlex.join(['python', '-m', 'json.tool', path]),
        depends_on=['campaign-plan'],
        spec=('Implement capability ' + name + ' for this project. Record its inputs, outputs, '
              'device/provider access, permissions, retry/recovery and guided physical actions '
              'when needed in ' + path + '. Resolve existing access and ask concrete necessary '
              'questions through the existing campaign client. Publish through canonical Mushy '
              'and demonstrate two distinct successful client invocations in this project. '
              'The pending capability link requires trusted lifecycle evidence before completion. '
              'This requirement grants no money, provider access or new permissions.'))
    params = dict(scope, task=execution._uuid(task['task_id']))
    cur.execute('SELECT * FROM campaign_capability_links WHERE ' + execution.SCOPE +
                ' AND task_id=%(task)s FOR UPDATE', params)
    existing = cur.fetchone()
    if existing:
        if (existing['enrollment_id'] != scope['enrollment_id']
                or existing['capability'] != name):
            execution._conflict('task_conflict')
    else:
        cur.execute('INSERT INTO campaign_capability_links '
                    '(link_id, org_id, project_id, campaign_id, task_id, enrollment_id, capability, state) '
                    'VALUES (%(id)s, %(org)s, %(project)s, %(campaign)s, %(task)s, %(enrollment)s, '
                    "%(capability)s, 'pending_link')",
                    dict(params, id=uuid.uuid4(), enrollment=scope['enrollment_id'], capability=name))
        execution._event(cur, scope, task, 'capability_link_recorded')
    return task


def _question(cur, scope, digest, owner, question, tasks):
    key = 'plan-' + campaigns._fingerprint('leaf.campaign.plan-question.v1',
        dict(plan=digest, task=owner, question=question['question_key']))
    row = campaigns._ask_question_cursor(
        cur, scope, question_key=key, prompt=question['prompt'],
        options=question.get('options'), asked_by='worker', blocks_dispatch=True)
    for task in tasks:
        execution._link_question_cursor(cur, scope, task['task_id'], row['question_id'])
    return row['question_id']


def adopt_plan(enrollment_id, subject, *, task_id, attempt_id, fence, result_fingerprint,
               plan_sha256, plan_size_bytes, plan_b64):
    """Validate real artifact bytes and commit children and semantic success once."""
    validate_request(task_id=task_id, attempt_id=attempt_id, fence=fence,
                     result_fingerprint=result_fingerprint, plan_sha256=plan_sha256,
                     plan_size_bytes=plan_size_bytes, plan_b64=plan_b64)
    if os.environ.get('LEAF_CAMPAIGN_BRIDGE') != 'on':
        raise execution.CampaignUnavailable('bridge_disabled', 'Campaign bridge is disabled')
    try:
        raw = base64.b64decode(plan_b64, validate=True)
    except (ValueError, binascii.Error):
        execution._conflict('plan_identity_mismatch')
    if (base64.b64encode(raw).decode('ascii') != plan_b64 or len(raw) != plan_size_bytes
            or hashlib.sha256(raw).hexdigest() != plan_sha256):
        execution._conflict('plan_identity_mismatch')
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
        if scope['machine_id'] not in enrollment.allowed_machines():
            raise execution.CampaignError('worker_forbidden', 'Campaign worker is not authorized')
        execution._lock(cur, 'campaign-next:' + str(scope['enrollment_id']))
        execution._lock(cur, 'campaign-submit:' + str(scope['campaign']))
        execution._lock(cur, 'campaign-adopt:' + str(scope['campaign']))
        context = _saved_context(cur, scope, task_id)
        if context is None:
            execution._conflict('plan_identity_mismatch')
        planning, implementation, source = context
        if (source['attempt_id'] != str(execution._uuid(attempt_id)) or source['fence'] != fence
                or source['result_fingerprint'] != result_fingerprint
                or source['product']['sha256'] != plan_sha256
                or source['product']['size_bytes'] != plan_size_bytes):
            execution._conflict('plan_identity_mismatch')
        params = dict(scope, task=planning['task_id'])
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + execution.SCOPE +
                    " AND task_id=%(task)s AND stage='build_test' AND outcome='succeeded' "
                    "AND result->>'acceptance'=%(acceptance)s",
                    dict(params, acceptance=ACCEPTANCE))
        existing = cur.fetchone()
        if existing:
            result = existing['result']
            if (result.get('plan_sha256') != plan_sha256
                    or result.get('plan_size_bytes') != plan_size_bytes
                    or result.get('result_fingerprint') != result_fingerprint
                    or result.get('source_receipt_id') != str(implementation['receipt_id'])
                    or result.get('source_attempt_id') != source['attempt_id']
                    or result.get('implementation_fence') != fence
                    or result.get('remote_fence') != source['remote_fencing_token']):
                execution._conflict('settlement_conflict')
            return _response(existing, replayed=True)
        if (planning['status'] != 'pending' or planning['current_stage'] != 'build_test'
                or planning['fence'] != fence):
            execution._conflict('stale_attempt')
        plan = validate_plan(raw, campaign_id=scope['campaign'],
                             prompt_digest=hashlib.sha256(scope['prompt'].encode('utf-8')).hexdigest(),
                             source_sha=planning['source_sha'])
        # The validator has already established an acyclic closed graph.
        capabilities = {name: _capability(cur, scope, planning, name, plan_sha256)
                        for name in sorted({name for item in plan['tasks']
                                            for name in item['capabilities_required']})}
        tasks, questions = {}, []
        pending = list(plan['tasks'])
        while pending:
            for item in pending[:]:
                if not all(key in tasks for key in item['depends_on']):
                    continue
                task = execution._submit_task_cursor(
                    cur, scope, **{key: item[key] for key in (
                        'task_key', 'title', 'spec', 'capability', 'owned_paths')},
                    stages=['implementation', 'build_test', 'publication'],
                    source_sha=planning['source_sha'], parent_task_id=planning['task_id'],
                    verify_command=shlex.join(item['verify_argv']), declared_artifacts=item['artifacts'],
                    depends_on=item['depends_on'] + ['campaign-plan'] +
                        [capabilities[name]['task_key'] for name in item['capabilities_required']],
                    idempotency_key='plan:' + plan_sha256[:16] + ':' + item['task_key'])
                tasks[item['task_key']] = task
                for question in item['questions']:
                    questions.append(_question(cur, scope, plan_sha256, item['task_key'], question, [task]))
                pending.remove(item)
        for question in plan['open_questions']:
            questions.append(_question(cur, scope, plan_sha256, None, question, list(tasks.values())))
        result = dict(acceptance=ACCEPTANCE, plan_sha256=plan_sha256, plan_size_bytes=plan_size_bytes,
                      source_receipt_id=str(implementation['receipt_id']), source_attempt_id=source['attempt_id'],
                      implementation_fence=fence, result_fingerprint=result_fingerprint,
                      remote_fence=source['remote_fencing_token'],
                      task_ids=[task['task_id'] for task in tasks.values()],
                      capability_task_ids=[task['task_id'] for task in capabilities.values()],
                      question_ids=questions)
        values = execution._values('succeeded', result, source['product']['key'], None, None, None, True)
        cur.execute('UPDATE campaign_tasks SET fence=fence+1, updated_at=NOW() WHERE ' + execution.SCOPE +
                    ' AND task_id=%(task)s RETURNING *', params)
        planning = cur.fetchone()
        cur.execute('INSERT INTO campaign_task_attempts '
                    '(attempt_id, task_id, org_id, project_id, campaign_id, fence, attempt_token_hash, '
                    'worker_id, stage, deadline_at, settled_at, status) VALUES '
                    '(%(id)s, %(task)s, %(org)s, %(project)s, %(campaign)s, %(fence)s, %(hash)s, '
                    "'semantic-adoption', 'build_test', NOW(), NOW(), 'settled') RETURNING *",
                    dict(params, id=uuid.uuid4(), fence=planning['fence'],
                         hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        attempt = cur.fetchone()
        receipt = execution._receipt(cur, scope, planning, attempt, values)
        execution._advance(cur, scope, planning, 'succeeded')
        execution._event(cur, scope, planning, 'stage_succeeded', attempt, result)
        return _response(receipt)
