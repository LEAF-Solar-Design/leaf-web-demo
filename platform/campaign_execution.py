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

from .campaign_release import admits_claim

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


def _task_payload(*, task_key, title, spec, capability, stages, owned_paths,
                  source_sha, verify_command, declared_artifacts, depends_on, kind,
                  parent_task_id=None):
    return dict(task_key=task_key, title=title, spec=spec, capability=capability,
                stages=stages, owned_paths=owned_paths, source_sha=source_sha,
                verify_command=verify_command, declared_artifacts=declared_artifacts,
                depends_on=sorted(depends_on), kind=kind,
                parent_task_id=str(parent_task_id) if parent_task_id else None)


def submit_task(org_id, project_id, campaign_id, *, task_key, title, spec,
                capability, stages, owned_paths, source_sha, verify_command,
                declared_artifacts, depends_on, idempotency_key, kind='task',
                parent_task_id=None):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    with _cursor() as cur:
        return _submit_task_cursor(
            cur, scope, task_key=task_key, title=title, spec=spec, capability=capability,
            stages=stages, owned_paths=owned_paths, source_sha=source_sha,
            verify_command=verify_command, declared_artifacts=declared_artifacts,
            depends_on=depends_on, idempotency_key=idempotency_key, kind=kind,
            parent_task_id=parent_task_id)


def _submit_task_cursor(cur, scope, *, task_key, title, spec,
                capability, stages, owned_paths, source_sha, verify_command,
                declared_artifacts, depends_on, idempotency_key, kind='task',
                parent_task_id=None):
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
    payload = _task_payload(task_key=task_key, title=title, spec=spec, capability=capability,
                   stages=stages, owned_paths=owned_paths, source_sha=source_sha,
                   verify_command=verify_command, declared_artifacts=declared_artifacts,
                   depends_on=sorted(depends_on), kind=kind,
                   parent_task_id=parent)
    fingerprint = _fingerprint('leaf.campaign.task.v1', payload)
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
    with _cursor() as cur:
        return _link_question_cursor(cur, scope, task_id, question_id)


def _link_question_cursor(cur, scope, task_id, question_id):
    task_id, question_id = _uuid(task_id), _uuid(question_id)
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


def _evidence(task, values, *, system_host=False):
    if not system_host:
        _reject_host_settlement(task, values['outcome'])
    if values['outcome'] != 'succeeded':
        return
    stage, result = task['current_stage'], values['result']
    if task['task_key'] == 'campaign-plan' and stage == 'build_test':
        raise CampaignError('insufficient_evidence', 'semantic plan adoption is required')
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
        if status == 'succeeded' and task['kind'] == 'capability':
            cur.execute('SELECT state FROM campaign_capability_links WHERE ' + SCOPE +
                        ' AND task_id=%(task)s FOR UPDATE', {**scope, 'task': task['task_id']})
            link = cur.fetchone()
            if link is not None and link['state'] != 'completed':
                raise CampaignError('insufficient_evidence', 'capability lifecycle is incomplete')
    cur.execute('UPDATE campaign_tasks SET status=%(status)s, current_stage=%(stage)s, '
                'updated_at=NOW() WHERE ' + SCOPE + ' AND task_id=%(task)s',
                {**scope, 'task': task['task_id'], 'status': status, 'stage': stage})


def _system_host_task(task):
    return task['kind'] == 'capability' and task['capability'] == 'campaign.host-enrollment'


def _reject_host_settlement(task, outcome):
    if outcome == 'succeeded' and _system_host_task(task):
        raise CampaignError('insufficient_evidence', 'Host capability requires its internal lifecycle producer')


def _complete_host_capability(cur, scope, enrollment_id):
    """Private continuation of count_invocation's digest-verified transaction."""
    from . import campaign_capabilities as capabilities, campaign_enrollment as enrollment

    task = enrollment._host_task(cur, scope, enrollment_id)
    row = enrollment._enrollment(cur, scope, enrollment_id)
    link = enrollment._link(cur, scope, enrollment_id)
    if not _system_host_task(task) or link['task_id'] != task['task_id']:
        _conflict('reconcile_required')
    task = enrollment._repair_host_task(cur, scope, task, row['machine_id'])
    if link['state'] != 'completed':
        return None
    if row['state'] != 'enabled':
        _conflict('capability_conflict')
    cur.execute('SELECT tenant_id FROM campaigns WHERE ' + SCOPE, scope)
    context = capabilities._persisted_context(row, link, cur.fetchone()['tenant_id'])
    identities = [link['first_invocation_receipt_id'], link['second_invocation_receipt_id']]
    if not all(identities) or len(set(identities)) != 2:
        _conflict('capability_conflict')
    cur.execute('SELECT * FROM campaign_capability_invocations WHERE ' + SCOPE +
                ' AND enrollment_id=%(enrollment)s AND link_id=%(link)s '
                'AND counted_receipt_id=ANY(%(identities)s) AND counted_at IS NOT NULL',
                {**scope, 'enrollment': row['enrollment_id'], 'link': link['link_id'],
                 'identities': identities})
    invocations = cur.fetchall()
    if len(invocations) != 2 or len({str(item['job_id']) for item in invocations}) != 2:
        _conflict('capability_conflict')
    by_identity = {item['counted_receipt_id']: item for item in invocations}
    if set(by_identity) != set(identities):
        _conflict('capability_conflict')
    uses = []
    for identity in identities:
        invocation = by_identity[identity]
        job_id = invocation['job_id']
        if (invocation['context'] != context
                or invocation['context_sha256'] != capabilities._sha(context)
                or invocation['tenant_id'] != context['tenant_id']
                or not capabilities._hex(invocation['counted_receipt_digest'])):
            _conflict('capability_conflict')
        job = capabilities._job(cur, job_id, context)
        op = capabilities._operation(cur, job_id)
        if (job['status'] != 'complete' or job['progress'] == 'closed'
                or job['finished_at'] is None or op is None
                or op['outcome'] != 'succeeded' or op['stage'] != 'readback'
                or capabilities._completed(op) != list(capabilities.STAGES)
                or any(str(op[key]) != context[key] for key in capabilities.IDS)
                or op['tenant_id'] != context['tenant_id']
                or op['machine_id'] != row['machine_id']
                or op['service_subject'] != row['service_subject']
                or op['profile_selector'] != context['profile_selector']
                or op['input_sha256'] != capabilities._sha({
                    'schema': 'leaf.campaign-host-operation.v1',
                    'job_id': str(job_id), 'context': context})):
            _conflict('capability_conflict')
        for stage in capabilities.STAGES:
            proof = op['stage_evidence'][stage]
            evidence = proof.get('evidence')
            if (not isinstance(evidence, dict)
                    or set(evidence) != {'config_identity_before', 'config_identity_after',
                                         'readback_sha256', 'reason'}
                    or not capabilities._hex(evidence['config_identity_after'])
                    or not capabilities._hex(evidence['readback_sha256'])
                    or (evidence['config_identity_before'] is not None
                        and not capabilities._hex(evidence['config_identity_before']))
                    or evidence['reason'] not in ('verified', 'already_applied')):
                _conflict('capability_conflict')
        uses.append(dict(
            job_id=str(job_id), receipt_id=identity,
            receipt_digest=invocation['counted_receipt_digest'],
            context_sha256=invocation['context_sha256'],
            operation_id=str(op['operation_id']), input_sha256=op['input_sha256'],
            readback_sha256=op['stage_evidence']['readback']['evidence']['readback_sha256'],
            host_readback_digest=capabilities._sha(op['stage_evidence']['readback']['evidence']),
            stage_proofs=op['stage_evidence']))
    observed = dict(
        publication_binding={
            **{key: link[key] for key in capabilities.PUBLICATION},
            'publication_id': link['publication_id'],
            'effective_catalog_id': link['effective_catalog_id'],
            'published_at': link['published_at'].isoformat(),
            'link_id': str(link['link_id']), 'enrollment_id': str(row['enrollment_id']),
            'task_id': str(task['task_id'])},
        invocations=uses)
    values = _values('succeeded', {'observed': observed},
                     'campaign-capability-link:' + str(link['link_id']), None, None, None, True)
    params = {**scope, 'task': task['task_id']}
    cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                ' AND task_id=%(task)s', params)
    receipts = cur.fetchall()
    if task['status'] == 'succeeded':
        if (len(receipts) != 1 or receipts[0]['stage'] != 'verification'
                or receipts[0]['outcome'] != 'succeeded' or not receipts[0]['verified']
                or receipts[0]['result_fingerprint'] != values['result_fingerprint']):
            _conflict('reconcile_required')
        cur.execute('SELECT * FROM campaign_task_attempts WHERE ' + SCOPE +
                    ' AND task_id=%(task)s', params)
        attempts = cur.fetchall()
        if (len(attempts) != 1 or attempts[0]['worker_id'] != 'capability-lifecycle'
                or attempts[0]['status'] != 'settled' or attempts[0]['settled_at'] is None
                or attempts[0]['attempt_id'] != receipts[0]['attempt_id']
                or attempts[0]['fence'] != task['fence']):
            _conflict('reconcile_required')
        return _row(receipts[0], replayed=True)
    if task['status'] != 'pending' or task['fence'] != 0 or receipts:
        _conflict('reconcile_required')
    for table in ('campaign_task_attempts', 'campaign_dispatch_bindings'):
        cur.execute('SELECT 1 FROM ' + table + ' WHERE ' + SCOPE +
                    ' AND task_id=%(task)s LIMIT 1', params)
        if cur.fetchone():
            _conflict('reconcile_required')
    _evidence(task, values, system_host=True)
    cur.execute('UPDATE campaign_tasks SET fence=fence+1, updated_at=NOW() WHERE ' + SCOPE +
                ' AND task_id=%(task)s RETURNING *', params)
    task = cur.fetchone()
    cur.execute(
        'INSERT INTO campaign_task_attempts (attempt_id, task_id, org_id, project_id, campaign_id, '
        'fence, attempt_token_hash, worker_id, stage, deadline_at, settled_at, status) '
        'VALUES (%(id)s, %(task)s, %(org)s, %(project)s, %(campaign)s, %(fence)s, %(hash)s, '
        "'capability-lifecycle', 'verification', NOW(), NOW(), 'settled') RETURNING *",
        {**params, 'id': uuid.uuid4(), 'fence': task['fence'],
         'hash': hashlib.sha256(secrets.token_bytes(32)).hexdigest()})
    attempt = cur.fetchone()
    receipt = _receipt(cur, scope, task, attempt, values)
    _advance(cur, scope, task, 'succeeded')
    _event(cur, scope, task, 'stage_succeeded', attempt)
    return _row(receipt)


def claim_task(org_id, project_id, campaign_id, *, worker_id, lease_seconds,
               budget_reservation_ref=None):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    _text(worker_id, 'worker_id', 128)
    lease = _integer(lease_seconds, 30, 3600)
    if budget_reservation_ref is not None:
        _text(budget_reservation_ref, 'budget_reservation_ref', 256)
    with _cursor() as cur:
        return _claim_task_cursor(cur, scope, worker_id=worker_id, lease=lease,
                                  budget_reservation_ref=budget_reservation_ref)


def _claim_task_cursor(cur, scope, *, worker_id, lease, task_key=None,
                       budget_reservation_ref=None):
    _check(cur, scope)
    if not admits_claim(cur, scope, worker_id):
        return None
    scope = dict(scope, task_key=task_key)
    task_filter = ' AND task_key=%(task_key)s' if task_key is not None else ''
    task_filter += " AND NOT (kind='capability' AND capability IN ('campaign.host-enrollment','campaign.native-release'))"
    # Never update attempts before locking their task. Concurrent expiry
    # cannot overwrite a newly claimed task or deadlock with settlement.
    cur.execute('SELECT * FROM campaign_tasks WHERE ' + SCOPE +
                " AND status='claimed'" + task_filter + ' ORDER BY task_id FOR UPDATE SKIP LOCKED', scope)
    for task in cur.fetchall():
        params = {**scope, 'task': task['task_id']}
        cur.execute("UPDATE campaign_task_attempts SET status='expired' WHERE " + SCOPE +
                    " AND task_id=%(task)s AND status='active' AND deadline_at<=clock_timestamp() RETURNING *",
                    params)
        attempt = cur.fetchone()
        if attempt is None:
            continue
        _event(cur, scope, task, 'attempt_expired', attempt)
        binding = _dispatch_binding(cur, scope, attempt)
        if binding or attempt['stage'] in OUTWARD:
            key = attempt['outward_operation_key']
            if binding:
                key = key or binding['request_id']
            values = _values('unknown', {'reason': 'lease_expired'}, None,
                             key, None, None, False)
            _receipt(cur, scope, task, attempt, values)
            _advance(cur, scope, task, 'unknown')
            _event(cur, scope, task, 'outcome_unknown', attempt,
                   {'outward_operation_key': key})
        else:
            cur.execute("UPDATE campaign_tasks SET status='pending', updated_at=NOW() WHERE " +
                        SCOPE + ' AND task_id=%(task)s', params)
    cur.execute(
        'SELECT t.* FROM campaign_tasks t WHERE t.org_id=%(org)s AND t.project_id=%(project)s '
        "AND t.campaign_id=%(campaign)s AND t.status='pending' "
        "AND NOT (t.kind='capability' AND t.capability IN ('campaign.host-enrollment','campaign.native-release')) AND NOT EXISTS ("
        'SELECT 1 FROM campaign_task_dependencies d JOIN campaign_tasks p '
        'ON p.task_id=d.depends_on_task_id AND p.org_id=d.org_id AND p.project_id=d.project_id '
        'AND p.campaign_id=d.campaign_id WHERE d.task_id=t.task_id AND d.org_id=t.org_id '
        "AND d.project_id=t.project_id AND d.campaign_id=t.campaign_id AND p.status<>'succeeded') "
        'AND NOT EXISTS (SELECT 1 FROM campaign_task_questions l JOIN campaign_questions q '
        'ON q.question_id=l.question_id AND q.org_id=l.org_id AND q.project_id=l.project_id '
        'AND q.campaign_id=l.campaign_id WHERE l.task_id=t.task_id AND l.org_id=t.org_id '
        'AND l.project_id=t.project_id AND l.campaign_id=t.campaign_id '
        "AND q.status='open' AND q.blocks_dispatch) "
        + ('AND t.task_key=%(task_key)s ' if task_key is not None else '') +
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
        _reject_host_settlement(task, outcome)
        _lock(cur, attempt_id)
        cur.execute('SELECT *, deadline_at<=clock_timestamp() AS overdue FROM campaign_task_attempts WHERE ' +
                    SCOPE + ' AND attempt_id=%(attempt)s FOR UPDATE', params)
        attempt = cur.fetchone()
        if (not hmac.compare_digest(hashlib.sha256(attempt_token.encode()).hexdigest(),
                                    attempt['attempt_token_hash']) or fence != attempt['fence']):
            _conflict('stale_attempt')
        if _dispatch_binding(cur, scope, attempt):
            _conflict('remote_reconciliation_required')
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
        _reject_host_settlement(task, outcome)
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                    " AND task_id=%(task)s AND outcome='unknown' AND outward_operation_key=%(key)s "
                    'ORDER BY fence DESC LIMIT 1', params)
        unknown = cur.fetchone()
        if unknown is None:
            _conflict('reconcile_identity_mismatch')
        if _dispatch_binding(cur, scope, unknown):
            _conflict('remote_reconciliation_required')
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


def _canonical_digest(value):
    # Matches fleet.gateway.delegation.canonical_digest, including UTF-8.
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, OverflowError, UnicodeError):
        _invalid('dispatch material must be JSON')


def _dispatch_binding(cur, scope, attempt):
    cur.execute('SELECT * FROM campaign_dispatch_bindings WHERE ' + SCOPE +
                ' AND task_id=%(task)s AND attempt_id=%(attempt)s AND fence=%(fence)s',
                {**scope, 'task': attempt['task_id'], 'attempt': attempt['attempt_id'],
                 'fence': attempt['fence']})
    return cur.fetchone()


def _remote_attempt(cur, scope, attempt_id):
    cur.execute('SELECT task_id FROM campaign_task_attempts WHERE ' + SCOPE +
                ' AND attempt_id=%(attempt)s', {**scope, 'attempt': attempt_id})
    pointer = cur.fetchone()
    if pointer is None:
        _missing()
    task = _task(cur, scope, pointer['task_id'])
    cur.execute('SELECT *, deadline_at<=clock_timestamp() AS overdue '
                'FROM campaign_task_attempts WHERE ' + SCOPE +
                ' AND task_id=%(task)s AND attempt_id=%(attempt)s FOR UPDATE',
                {**scope, 'task': task['task_id'], 'attempt': attempt_id})
    return task, cur.fetchone()


def _submission(binding):
    return dict(version=1, request_id=binding['request_id'],
                root_request_id=binding['root_request_id'], registration_id=binding['registration_id'],
                project_id=binding['gateway_project_id'], source_ref=binding['source_ref'],
                packet_digest=binding['packet_digest'], budget_class=binding['budget_class'],
                reservation_micro_usd=binding['reservation_micro_usd'])


def _public_binding(binding, *, replayed=False):
    return {**_row(binding, replayed=replayed), 'submission': _submission(binding)}


def _remote_integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 9223372036854775807:
        _invalid('dispatch number must be an integer in range')


def _attempt_source(cur, scope, attempt, *, result=False):
    table = 'campaign_attempt_result_sources' if result else 'campaign_attempt_input_sources'
    cur.execute('SELECT * FROM ' + table + ' WHERE ' + SCOPE +
                ' AND task_id=%(task)s AND attempt_id=%(attempt)s AND fence=%(fence)s',
                {**scope, 'task': attempt['task_id'], 'attempt': attempt['attempt_id'],
                 'fence': attempt['fence']})
    return cur.fetchone()


def _source_material(fence, repository_id, commit_sha, tree_sha):
    _remote_integer(fence)
    _text(repository_id, 'repository_id', 200)
    for name, value in (('commit_sha', commit_sha), ('tree_sha', tree_sha)):
        _text(value, name, 40)
        if not re.fullmatch(r'[0-9a-f]{40}', value):
            _invalid()
    return dict(repository_id=repository_id, commit_sha=commit_sha, tree_sha=tree_sha)


def bind_attempt_input_source(org_id, project_id, campaign_id, attempt_id, *, fence,
                              repository_id, commit_sha, tree_sha, bundle_sha256, bundle_bytes):
    """Freeze trusted adapter input without changing adopted task lineage."""
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    attempt_id = _uuid(attempt_id)
    material = _source_material(fence, repository_id, commit_sha, tree_sha)
    _text(bundle_sha256, 'bundle_sha256', 64)
    if not re.fullmatch(r'[0-9a-f]{64}', bundle_sha256):
        _invalid()
    _remote_integer(bundle_bytes, minimum=1)
    material.update(bundle_sha256=bundle_sha256, bundle_bytes=bundle_bytes)
    with _cursor() as cur:
        _check(cur, scope)
        task, attempt = _remote_attempt(cur, scope, attempt_id)
        fingerprint = _fingerprint('leaf.campaign.attempt.input-source.v1',
                                   {**material, 'attempt': str(attempt_id), 'fence': fence})
        existing = _attempt_source(cur, scope, attempt)
        if existing:
            if existing['source_fingerprint'] != fingerprint:
                _conflict('input_source_conflict')
            return _row(existing, replayed=True)
        if (attempt['status'] != 'active' or attempt['overdue']
                or fence != attempt['fence'] or fence != task['fence']):
            _conflict('stale_attempt')
        if _dispatch_binding(cur, scope, attempt):
            _conflict('input_source_conflict')
        cur.execute(
            'INSERT INTO campaign_attempt_input_sources (attempt_id, task_id, org_id, '
            'project_id, campaign_id, fence, repository_id, commit_sha, tree_sha, '
            'bundle_sha256, bundle_bytes, source_fingerprint) VALUES (%(attempt)s, %(task)s, '
            '%(org)s, %(project)s, %(campaign)s, %(fence)s, %(repository_id)s, %(commit_sha)s, '
            '%(tree_sha)s, %(bundle_sha256)s, %(bundle_bytes)s, %(fingerprint)s) RETURNING *',
            {**material, 'org': attempt['org_id'], 'project': attempt['project_id'],
             'campaign': attempt['campaign_id'], 'task': attempt['task_id'],
             'attempt': attempt['attempt_id'], 'fence': attempt['fence'],
             'fingerprint': fingerprint})
        return _row(cur.fetchone())


def record_attempt_result_source(org_id, project_id, campaign_id, attempt_id, *, fence,
                                 repository_id, commit_sha, tree_sha, publication_receipt):
    """Preserve sanitized L1 publication readback; L1 owns authority and CAS verification."""
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    attempt_id = _uuid(attempt_id)
    material = _source_material(fence, repository_id, commit_sha, tree_sha)
    _secret(publication_receipt)
    if not isinstance(publication_receipt, dict) or not publication_receipt:
        _invalid('publication receipt must be a nonempty object')
    digest = _canonical_digest(publication_receipt)
    if len(json.dumps(publication_receipt, ensure_ascii=False, allow_nan=False).encode()) > 65536:
        _invalid('publication receipt too large')
    material.update(publication_receipt=publication_receipt, publication_receipt_sha256=digest)
    with _cursor() as cur:
        _check(cur, scope)
        _, attempt = _remote_attempt(cur, scope, attempt_id)
        fingerprint = _fingerprint('leaf.campaign.attempt.result-source.v1',
                                   {**material, 'attempt': str(attempt_id), 'fence': fence})
        existing = _attempt_source(cur, scope, attempt, result=True)
        if existing:
            if existing['result_fingerprint'] != fingerprint:
                _conflict('result_source_conflict')
            return _row(existing, replayed=True)
        if fence != attempt['fence']:
            _conflict('stale_attempt')
        input_source = _attempt_source(cur, scope, attempt)
        if input_source is None or repository_id != input_source['repository_id']:
            _conflict('result_source_identity_mismatch')
        cur.execute(
            'INSERT INTO campaign_attempt_result_sources (attempt_id, task_id, org_id, '
            'project_id, campaign_id, fence, repository_id, commit_sha, tree_sha, '
            'publication_receipt, publication_receipt_sha256, result_fingerprint) VALUES '
            '(%(attempt)s, %(task)s, %(org)s, %(project)s, %(campaign)s, %(fence)s, '
            '%(repository_id)s, %(commit_sha)s, %(tree_sha)s, %(publication_receipt)s, '
            '%(publication_receipt_sha256)s, %(fingerprint)s) RETURNING *',
            {**material, 'publication_receipt': Jsonb(publication_receipt),
             'org': attempt['org_id'], 'project': attempt['project_id'],
             'campaign': attempt['campaign_id'], 'task': attempt['task_id'],
             'attempt': attempt['attempt_id'], 'fence': attempt['fence'],
             'fingerprint': fingerprint})
        return _row(cur.fetchone())


def read_attempt_sources(org_id, project_id, campaign_id, attempt_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    attempt_id = _uuid(attempt_id)
    with _cursor() as cur:
        _check(cur, scope)
        _, attempt = _remote_attempt(cur, scope, attempt_id)
        return {'input_source': _row(_attempt_source(cur, scope, attempt)),
                'result_source': _row(_attempt_source(cur, scope, attempt, result=True))}


def bind_remote_dispatch(org_id, project_id, campaign_id, attempt_id, *, fence,
                         machine_id, run_id, registration_id, root_request_id,
                         gateway_project_id, source_ref, packet_digest, budget_class,
                         reservation_micro_usd):
    """Freeze a server adapter's request before submit; no spending authority is granted."""
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    attempt_id = _uuid(attempt_id)
    _remote_integer(fence)
    _remote_integer(reservation_micro_usd, minimum=1)
    material = dict(machine_id=machine_id, run_id=run_id, registration_id=registration_id,
                    root_request_id=root_request_id, gateway_project_id=gateway_project_id,
                    source_ref=source_ref, packet_digest=packet_digest, budget_class=budget_class,
                    reservation_micro_usd=reservation_micro_usd)
    for name, maximum in (('machine_id', 200), ('run_id', 128), ('registration_id', 128),
                           ('root_request_id', 200), ('gateway_project_id', 200),
                           ('source_ref', 40), ('packet_digest', 64), ('budget_class', 8)):
        _text(material[name], name, maximum)
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', run_id)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', registration_id)
            or not re.fullmatch(r'[0-9a-f]{40}', source_ref)
            or not re.fullmatch(r'[0-9a-f]{64}', packet_digest)
            or budget_class not in ('explicit', 'daily')):
        _invalid()
    with _cursor() as cur:
        _check(cur, scope)
        task, attempt = _remote_attempt(cur, scope, attempt_id)
        identity = dict(campaign=str(scope['campaign']), task=str(task['task_id']),
                        attempt=str(attempt_id), fence=fence, stage=attempt['stage'])
        request_id = 'cd-' + _fingerprint('leaf.campaign.dispatch.v1', identity)[:48]
        leaf_id = 'vmc-' + _canonical_digest([machine_id, request_id])[:48]
        material.update(request_id=request_id, leaf_id=leaf_id)
        submission_material = {**_submission(material), 'machine_id': machine_id,
                               'leaf_id': leaf_id, 'run_id': run_id}
        digest = _canonical_digest(submission_material)
        fingerprint = _fingerprint('leaf.campaign.dispatch.binding.v1',
                                   {**identity, **submission_material})
        binding = _dispatch_binding(cur, scope, attempt)
        if binding:
            if binding['binding_fingerprint'] != fingerprint:
                _conflict('dispatch_conflict')
            return _public_binding(binding, replayed=True)
        if (attempt['status'] != 'active' or attempt['overdue']
                or fence != attempt['fence'] or fence != task['fence']):
            _conflict('stale_attempt')
        input_source = _attempt_source(cur, scope, attempt)
        if source_ref != (input_source['commit_sha'] if input_source else task['source_sha']):
            _conflict('dispatch_identity_mismatch')
        cur.execute(
            'INSERT INTO campaign_dispatch_bindings (attempt_id, task_id, org_id, project_id, '
            'campaign_id, fence, stage, request_id, machine_id, run_id, leaf_id, registration_id, '
            'root_request_id, gateway_project_id, source_ref, packet_digest, budget_class, '
            'reservation_micro_usd, submission_digest, binding_fingerprint) VALUES '
            '(%(attempt)s, %(task)s, %(org)s, %(project)s, %(campaign)s, %(fence)s, %(stage)s, '
            '%(request_id)s, %(machine_id)s, %(run_id)s, %(leaf_id)s, %(registration_id)s, '
            '%(root_request_id)s, %(gateway_project_id)s, %(source_ref)s, %(packet_digest)s, '
            '%(budget_class)s, %(reservation_micro_usd)s, %(digest)s, %(fingerprint)s) RETURNING *',
            {**scope, **material, 'attempt': attempt_id, 'task': task['task_id'], 'fence': fence,
             'stage': attempt['stage'], 'digest': digest, 'fingerprint': fingerprint})
        binding = cur.fetchone()
        _event(cur, scope, task, 'remote_bound', attempt, {'request_id': request_id})
        return _public_binding(binding)


def record_remote_admission(org_id, project_id, campaign_id, attempt_id, *,
                            leaf_id, run_id, submission_digest, reservation_id=None):
    """Record trusted gateway readback, including after the coordinator lease expires."""
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    attempt_id = _uuid(attempt_id)
    if reservation_id is not None:
        _text(reservation_id, 'reservation_id', 128)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', reservation_id):
            _invalid()
    with _cursor() as cur:
        _check(cur, scope)
        task, attempt = _remote_attempt(cur, scope, attempt_id)
        binding = _dispatch_binding(cur, scope, attempt)
        if (binding is None or leaf_id != binding['leaf_id'] or run_id != binding['run_id']
                or submission_digest != binding['submission_digest']):
            _conflict('dispatch_identity_mismatch')
        if reservation_id is not None and binding['reservation_id'] not in (None, reservation_id):
            _conflict('dispatch_conflict')
        if binding['state'] == 'settled':
            if reservation_id is not None and reservation_id != binding['reservation_id']:
                _conflict('dispatch_conflict')
            return _public_binding(binding, replayed=True)
        if binding['state'] == 'admitted' and (reservation_id is None or binding['reservation_id'] == reservation_id):
            return _public_binding(binding, replayed=True)
        cur.execute("UPDATE campaign_dispatch_bindings SET state='admitted', "
                    'admitted_at=COALESCE(admitted_at, NOW()), '
                    'reservation_id=COALESCE(reservation_id, %(reservation)s) WHERE ' + SCOPE +
                    ' AND task_id=%(task)s AND attempt_id=%(attempt)s AND fence=%(fence)s RETURNING *',
                    {**scope, 'task': task['task_id'], 'attempt': attempt_id,
                     'fence': attempt['fence'], 'reservation': reservation_id})
        binding = cur.fetchone()
        _event(cur, scope, task, 'remote_admitted', attempt, {'leaf_id': leaf_id})
        return _public_binding(binding)


def settle_remote_attempt(org_id, project_id, campaign_id, attempt_id, *, fence, verdict,
                          outcome, result, artifact_ref=None, resource_identity=None,
                          rollback_identity=None, verified=False):
    """Settle only from a trusted adapter that verified gateway and immutable producer evidence.

    Identity matching here is not signature verification or billing authority.
    These methods are server-only and must never be exposed as browser ingress.
    """
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    attempt_id = _uuid(attempt_id)
    _remote_integer(fence)
    if (not isinstance(verdict, dict) or type(verdict.get('fencing_token')) is not int
            or not 0 <= verdict['fencing_token'] <= 9223372036854775807):
        _conflict('dispatch_identity_mismatch')
    # This validated counter is not a credential. Keep secret rejection for
    # every other verdict field, including nested material.
    _secret({key: value for key, value in verdict.items() if key != 'fencing_token'})
    verdict_fingerprint = _canonical_digest(verdict)
    if outcome == 'unknown':
        _invalid('remote settlement requires a terminal outcome')
    with _cursor() as cur:
        _check(cur, scope)
        task, attempt = _remote_attempt(cur, scope, attempt_id)
        binding = _dispatch_binding(cur, scope, attempt)
        _reject_host_settlement(task, outcome)
        if (binding is None or verdict.get('run_id') != binding['run_id']
                or verdict.get('leaf_id') != binding['leaf_id']):
            _conflict('dispatch_identity_mismatch')
        if fence != binding['fence']:
            _conflict('stale_attempt')
        key = attempt['outward_operation_key'] or binding['request_id']
        values = _values(outcome, result, artifact_ref, key,
                         resource_identity, rollback_identity, verified)
        params = {**scope, 'task': task['task_id'], 'attempt': attempt_id, 'fence': fence}
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                    ' AND task_id=%(task)s AND attempt_id=%(attempt)s AND fence=%(fence)s', params)
        receipt = cur.fetchone()
        if binding['state'] == 'settled':
            if attempt['status'] == 'expired' and receipt:
                cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + SCOPE +
                            ' AND task_id=%(task)s AND reconciles_receipt_id=%(unknown)s',
                            {**params, 'unknown': receipt['receipt_id']})
                receipt = cur.fetchone()
            if (receipt is None or receipt['result_fingerprint'] != values['result_fingerprint']
                    or binding['verdict_fingerprint'] != verdict_fingerprint
                    or binding['remote_fencing_token'] != verdict['fencing_token']):
                _conflict('settlement_conflict')
            return _row(receipt, replayed=True)
        if binding['state'] != 'admitted':
            _conflict('dispatch_identity_mismatch')
        if (fence != task['fence'] or task['current_stage'] != attempt['stage']
                or attempt['status'] not in ('active', 'expired')):
            _conflict('stale_attempt')
        _evidence(task, values)
        if attempt['status'] == 'active' and attempt['overdue']:
            cur.execute("UPDATE campaign_task_attempts SET status='expired' WHERE " + SCOPE +
                        ' AND task_id=%(task)s AND attempt_id=%(attempt)s AND fence=%(fence)s', params)
            _event(cur, scope, task, 'attempt_expired', attempt)
            receipt = _receipt(cur, scope, task, attempt,
                               _values('unknown', {'reason': 'lease_expired'}, None, key, None, None, False))
            _advance(cur, scope, task, 'unknown')
            _event(cur, scope, task, 'outcome_unknown', attempt, {'outward_operation_key': key})
            attempt['status'], task['status'] = 'expired', 'reconcile_required'
        if attempt['status'] == 'expired':
            if (task['status'] != 'reconcile_required' or receipt is None
                    or receipt['outcome'] != 'unknown' or receipt['outward_operation_key'] != key):
                _conflict('stale_attempt')
            unknown = receipt
            cur.execute('SELECT receipt_id FROM campaign_stage_receipts WHERE ' + SCOPE +
                        ' AND task_id=%(task)s AND reconciles_receipt_id=%(unknown)s',
                        {**params, 'unknown': unknown['receipt_id']})
            if cur.fetchone():
                _conflict('stale_attempt')
            cur.execute('UPDATE campaign_tasks SET fence=fence+1, updated_at=NOW() WHERE ' + SCOPE +
                        ' AND task_id=%(task)s RETURNING *', params)
            task = cur.fetchone()
            cur.execute(
                'INSERT INTO campaign_task_attempts (attempt_id, task_id, org_id, project_id, campaign_id, '
                'fence, attempt_token_hash, worker_id, stage, deadline_at, settled_at, status, outward_operation_key) '
                'VALUES (%(id)s, %(task)s, %(org)s, %(project)s, %(campaign)s, %(next_fence)s, %(hash)s, '
                "'reconciliation', %(stage)s, NOW(), NOW(), 'settled', %(key)s) RETURNING *",
                {**params, 'id': uuid.uuid4(), 'next_fence': task['fence'], 'key': key,
                 'hash': hashlib.sha256(secrets.token_bytes(32)).hexdigest(), 'stage': attempt['stage']})
            reconciliation = cur.fetchone()
            receipt = _receipt(cur, scope, task, reconciliation, values, unknown['receipt_id'])
            _event(cur, scope, task, 'reconciled', reconciliation,
                   {'reconciles_receipt_id': str(unknown['receipt_id'])})
        else:
            if receipt is not None:
                _conflict('stale_attempt')
            receipt = _receipt(cur, scope, task, attempt, values)
            cur.execute("UPDATE campaign_task_attempts SET status='settled', settled_at=NOW() WHERE " + SCOPE +
                        ' AND task_id=%(task)s AND attempt_id=%(attempt)s AND fence=%(fence)s', params)
            _event(cur, scope, task, {'succeeded': 'stage_succeeded', 'failed': 'stage_failed'}[outcome], attempt)
        _advance(cur, scope, task, outcome)
        cur.execute("UPDATE campaign_dispatch_bindings SET state='settled', settled_at=NOW(), "
                    'remote_fencing_token=%(remote_fence)s, verdict_fingerprint=%(verdict)s WHERE ' + SCOPE +
                    ' AND task_id=%(task)s AND attempt_id=%(attempt)s AND fence=%(fence)s',
                    {**params, 'remote_fence': verdict['fencing_token'], 'verdict': verdict_fingerprint})
        _event(cur, scope, task, 'remote_settled', attempt, {'receipt_id': str(receipt['receipt_id'])})
        return _row(receipt)


def pending_remote_bindings(org_id, project_id, campaign_id):
    """Return frozen request bodies for restart recovery without coordinator memory."""
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    with _cursor() as cur:
        _check(cur, scope)
        cur.execute(
            'SELECT b.* FROM campaign_dispatch_bindings b JOIN campaign_task_attempts a '
            'ON a.org_id=b.org_id AND a.project_id=b.project_id AND a.campaign_id=b.campaign_id '
            'AND a.task_id=b.task_id AND a.attempt_id=b.attempt_id AND a.fence=b.fence '
            'JOIN campaign_tasks t ON t.org_id=b.org_id AND t.project_id=b.project_id '
            'AND t.campaign_id=b.campaign_id AND t.task_id=b.task_id AND t.fence=b.fence '
            'WHERE b.org_id=%(org)s AND b.project_id=%(project)s AND b.campaign_id=%(campaign)s '
            "AND b.state<>'settled' AND (a.status='active' OR t.status='reconcile_required') "
            'ORDER BY b.created_at, b.attempt_id', scope)
        return [_public_binding(row) for row in cur.fetchall()]


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
