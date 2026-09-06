"""Published ReciPDF capability invocations beneath the existing async job authority."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import uuid

from psycopg.types.json import Jsonb

from . import campaign_enrollment as enrollment
from .campaigns import _principal
from .campaign_execution import (
    CampaignError, CampaignConflict, CampaignUnavailable, _scope, _uuid,
    _cursor, _check, _lock, _event, _task,
)

PUBLICATION = ('change_set_id', 'catalog_commit', 'effective_catalog_digest',
               'tool_name', 'tool_manifest_sha256', 'tool_source_sha256')
IDS = ('org_id', 'project_id', 'campaign_id', 'enrollment_id', 'link_id')
CONSTANTS = {'schema': 'leaf.campaign-capability.v1',
             'capability': 'campaign.host-enrollment',
             'tool_name': 'campaign-host-enrollment', 'profile_selector': 'campaign-default-v1'}
CONTEXT_KEYS = set(IDS) | set(PUBLICATION) | set(CONSTANTS) | {'tenant_id'}
STAGES = ('apply', 'activate', 'readback')
REASONS = ('verified', 'already_applied', 'lifecycle_handoff_required',
           'profile_unavailable', 'config_conflict', 'validation_failed',
           'activation_failed', 'job_cancelled')


def _invalid():
    raise CampaignError('invalid_request', 'Invalid capability request')


def _conflict():
    raise CampaignConflict('capability_conflict', 'Capability proof conflicts with durable state')


def _closed(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        _invalid()


def _hex(value, length=64, prefix=''):
    return isinstance(value, str) and re.fullmatch(prefix + r'[0-9a-f]{' + str(length) + '}', value) is not None


def _token(value, maximum):
    return (isinstance(value, str) and 1 <= len(value) <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _publication(value):
    _closed(value, PUBLICATION)
    if (not _token(value['change_set_id'], 200)
            or not _hex(value['catalog_commit'], 40)
            or not _hex(value['effective_catalog_digest'])
            or value['tool_name'] != CONSTANTS['tool_name']
            or not _hex(value['tool_manifest_sha256'], prefix='sha256:')
            or not _hex(value['tool_source_sha256'])):
        _invalid()


def _context(value):
    _closed(value, CONTEXT_KEYS)
    _publication({key: value[key] for key in PUBLICATION})
    for key in IDS:
        if not isinstance(value[key], str) or str(_uuid(value[key])) != value[key]:
            _invalid()
    if not _token(value['tenant_id'], 32768) or any(value[k] != v for k, v in CONSTANTS.items()):
        _invalid()
    return {**_scope(value['org_id'], value['project_id']), 'campaign': _uuid(value['campaign_id'])}


def _load(cur, scope, enrollment_id, *, enabled=True, live=True):
    _check(cur, scope)
    cur.execute('SELECT * FROM projects WHERE org_id=%(org)s AND project_id=%(project)s FOR SHARE', scope)
    project = cur.fetchone()
    if project is None or (live and (project['status'] != 'active' or project['deleted_at'] is not None)):
        _conflict()
    row = enrollment._enrollment(cur, scope, enrollment_id)
    link = enrollment._link(cur, scope, row['enrollment_id'])
    if enabled and row['state'] != 'enabled':
        _conflict()
    cur.execute('SELECT tenant_id FROM campaigns WHERE campaign_id=%(campaign)s '
                'AND org_id=%(org)s AND project_id=%(project)s FOR SHARE', scope)
    tenant = cur.fetchone()['tenant_id']
    return row, link, tenant


def _machine_lock_for_enrollment(cur, enrollment_id):
    # All operation readers and writers enter the same machine lock before
    # enrollment/job locks, including the poller's SKIP LOCKED selection.
    cur.execute('SELECT machine_id FROM campaign_host_enrollments WHERE enrollment_id=%s',
                (_uuid(enrollment_id),))
    row = cur.fetchone()
    if row is None:
        _conflict()
    _lock(cur, 'campaign-host-machine:' + row['machine_id'])


def _persisted_context(row, link, tenant):
    if link['state'] not in ('published', 'invoked_once', 'completed'):
        _conflict()
    value = {**CONSTANTS, **{key: str(row[key]) for key in IDS if key != 'link_id'},
             'link_id': str(link['link_id']), 'tenant_id': tenant,
             **{key: link[key] for key in PUBLICATION}}
    _context(value)
    if (link['publication_id'] != value['change_set_id']
            or link['effective_catalog_id'] != value['effective_catalog_digest']
            or link['published_at'] is None or link['capability'] != value['capability']):
        _conflict()
    return value


def bind_publication(org_id, project_id, campaign_id, enrollment_id, principal_id, *, publication):
    _publication(publication)
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    with _cursor() as cur:
        _principal(cur, scope, _uuid(principal_id))
        row, link, _ = _load(cur, scope, enrollment_id, enabled=False)
        if link['state'] != 'pending_link' or link['publication_id'] is not None:
            if (any(link[key] != publication[key] for key in PUBLICATION)
                    or link['publication_id'] != publication['change_set_id']
                    or link['effective_catalog_id'] != publication['effective_catalog_digest']
                    or link['published_at'] is None):
                _conflict()
            return enrollment._public(row, link, True)
        if (any(link[key] is not None and link[key] != publication[key] for key in PUBLICATION)
                or (link['effective_catalog_id'] is not None
                    and link['effective_catalog_id'] != publication['effective_catalog_digest'])):
            _conflict()
        cur.execute('UPDATE campaign_capability_links SET ' +
                    ', '.join(key + '=%(' + key + ')s' for key in PUBLICATION) +
                    ", publication_id=%(change_set_id)s, effective_catalog_id=%(effective_catalog_digest)s, "
                    "published_at=NOW(), state='published' WHERE link_id=%(link)s RETURNING *",
                    {**publication, 'link': link['link_id']})
        link = cur.fetchone()
        _event(cur, scope, _task(cur, scope, link['task_id']), 'capability_link_recorded',
               payload={'state': 'published', 'link_id': str(link['link_id'])})
        return enrollment._public(row, link)


def invocation_context(org_id, project_id, campaign_id, enrollment_id, principal_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    with _cursor() as cur:
        _principal(cur, scope, _uuid(principal_id))
        return _persisted_context(*_load(cur, scope, enrollment_id))


def _job(cur, job_id, context):
    cur.execute('SELECT * FROM async_jobs WHERE job_id=%s FOR UPDATE', (str(job_id),))
    job = cur.fetchone()
    if (job is None or any(job[key] != context[key] for key in ('tenant_id', 'org_id', 'project_id'))
            or job['tool'] != context['tool_name']
            or not isinstance(job['execution_json'], dict)
            or job['execution_json'].get('capability_provenance') != context):
        _conflict()
    return job


def _live_job(job):
    return job['status'] in ('submitted', 'running') and job['progress'] != 'closed'


def _operation(cur, job_id):
    cur.execute('SELECT *, lease_expires_at > clock_timestamp() AS lease_live '
                'FROM campaign_host_operations WHERE job_id=%s FOR UPDATE', (_uuid(job_id),))
    return cur.fetchone()


def _completed(row):
    return [stage for stage in STAGES if row['stage_evidence'].get(stage, {}).get('outcome') == 'succeeded']


def _public(row, replayed=False):
    result = {key: row[key] for key in ('operation_id', 'job_id', 'enrollment_id', 'link_id',
              'machine_id', 'input_sha256', 'profile_selector', 'attempt', 'fence',
              'lease_expires_at', 'stage', 'outcome', 'stage_evidence')}
    for key, value in result.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif hasattr(value, 'isoformat'):
            result[key] = value.isoformat()
    return {**result, 'completed_stages': _completed(row), 'replayed': replayed}


def _invocation(cur, job_id, context):
    cur.execute('SELECT * FROM campaign_capability_invocations WHERE job_id=%s', (_uuid(job_id),))
    row = cur.fetchone()
    if row is None or row['context'] != context or row['context_sha256'] != _sha(context):
        _conflict()
    return row


def ensure_operation(job_id, context):
    scope = _context(context)
    job_id = _uuid(job_id)
    with _cursor() as cur:
        _machine_lock_for_enrollment(cur, context['enrollment_id'])
        row, link, tenant = _load(cur, scope, context['enrollment_id'])
        if _persisted_context(row, link, tenant) != context:
            _conflict()
        job = _job(cur, job_id, context)
        if not _live_job(job):
            _conflict()
        existing = _operation(cur, job_id)
        if existing:
            _invocation(cur, job_id, context)
            return _public(existing, True)
        values = {**context, 'job': job_id, 'context': Jsonb(context), 'sha': _sha(context)}
        cur.execute('INSERT INTO campaign_capability_invocations '
                    '(job_id, org_id, project_id, campaign_id, link_id, enrollment_id, tenant_id, context, context_sha256) '
                    'VALUES (%(job)s,%(org_id)s,%(project_id)s,%(campaign_id)s,%(link_id)s,%(enrollment_id)s,'
                    '%(tenant_id)s,%(context)s,%(sha)s)', values)
        cur.execute('INSERT INTO campaign_host_operations '
                    '(operation_id, job_id, org_id, project_id, campaign_id, link_id, enrollment_id, tenant_id, '
                    'machine_id, service_subject, input_sha256, profile_selector) VALUES '
                    '(%(operation)s,%(job)s,%(org_id)s,%(project_id)s,%(campaign_id)s,%(link_id)s,%(enrollment_id)s,'
                    '%(tenant_id)s,%(machine)s,%(subject)s,%(input)s,%(profile_selector)s) RETURNING *',
                    {**values, 'operation': uuid.uuid4(), 'machine': row['machine_id'],
                     'subject': row['service_subject'], 'input': _sha({
                         'schema': 'leaf.campaign-host-operation.v1', 'job_id': str(job_id), 'context': context})})
        return _public(cur.fetchone())


def read_operation(job_id, context):
    scope = _context(context)
    with _cursor() as cur:
        _machine_lock_for_enrollment(cur, context['enrollment_id'])
        if _persisted_context(*_load(cur, scope, context['enrollment_id'], enabled=False, live=False)) != context:
            _conflict()
        _job(cur, job_id, context)
        _invocation(cur, job_id, context)
        row = _operation(cur, job_id)
        if row is None:
            _conflict()
        return _public(row)


def _machine(subject):
    configured = os.environ.get('LEAF_CAMPAIGN_WORKER_SUBJECT', '')
    if not configured or subject != configured:
        raise CampaignError('worker_forbidden', 'Campaign worker is not authorized')
    machines = enrollment.allowed_machines()
    machine = os.environ.get('LEAF_CAMPAIGN_HOST_MACHINE_ID', '')
    if not machine and len(machines) == 1:
        machine = machines[0]
    if machine not in machines or not _token(machine, 200):
        raise CampaignUnavailable('worker_unavailable', 'Campaign host machine is not configured')
    return machine


def _claim_value(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', value) is None:
        _invalid()
    return hashlib.sha256(value.encode()).hexdigest()


def _host_scope(cur, operation_id, machine, subject):
    # Resolve identifiers without taking the operation lock ahead of enrollment/job.
    cur.execute('SELECT * FROM campaign_host_operations WHERE operation_id=%s', (_uuid(operation_id),))
    op = cur.fetchone()
    if op is None or op['machine_id'] != machine or op['service_subject'] != subject:
        _conflict()
    scope = {**_scope(op['org_id'], op['project_id']), 'campaign': op['campaign_id']}
    row, link, tenant = _load(cur, scope, op['enrollment_id'], enabled=False, live=False)
    if row['machine_id'] != machine or row['service_subject'] != subject:
        _conflict()
    context = _persisted_context(row, link, tenant)
    _invocation(cur, op['job_id'], context)
    job = _job(cur, op['job_id'], context)
    op = _operation(cur, op['job_id'])
    if op['input_sha256'] != _sha({'schema': 'leaf.campaign-host-operation.v1',
                                  'job_id': str(op['job_id']), 'context': context}):
        _conflict()
    cur.execute('SELECT status, deleted_at FROM projects WHERE org_id=%s AND project_id=%s',
                (op['org_id'], op['project_id']))
    project = cur.fetchone()
    live = (row['state'] == 'enabled' and project['status'] == 'active'
            and project['deleted_at'] is None and _live_job(job))
    return op, live


def claim_host_operation(subject, body):
    if not isinstance(body, dict) or (body and set(body) != {'operation_id', 'claim'}):
        _invalid()
    machine = _machine(subject)
    if body and (not isinstance(body['operation_id'], str)
                 or str(_uuid(body['operation_id'])) != body['operation_id']):
        _invalid()
    claim_hash = _claim_value(body['claim']) if body else None
    with _cursor() as cur:
        _lock(cur, 'campaign-host-machine:' + machine)
        if body:
            op, live = _host_scope(cur, body['operation_id'], machine, subject)
            if (not live or not op['lease_live'] or op['outcome'] is not None
                    or not hmac.compare_digest(op['claim_sha256'] or '', claim_hash)):
                _conflict()
            claim = body['claim']
        else:
            cur.execute('SELECT operation_id FROM campaign_host_operations WHERE machine_id=%s '
                        'AND outcome IS NULL AND lease_expires_at > clock_timestamp() LIMIT 1', (machine,))
            if cur.fetchone():
                return {'ok': True, 'kind': 'idle'}
            # The machine admission lock serializes host writers. Candidate row locks
            # are skipped so a busy reader/count transaction cannot stall polling.
            cur.execute('SELECT o.operation_id FROM campaign_host_operations o '
                        'JOIN campaign_host_enrollments e ON e.enrollment_id=o.enrollment_id '
                        'JOIN projects p ON p.org_id=o.org_id AND p.project_id=o.project_id '
                        'JOIN async_jobs j ON j.job_id=o.job_id::text '
                        "WHERE o.machine_id=%s AND o.service_subject=%s AND o.outcome IS NULL "
                        'AND (o.lease_expires_at IS NULL OR o.lease_expires_at <= clock_timestamp()) '
                        "AND e.state='enabled' AND p.status='active' AND p.deleted_at IS NULL "
                        "AND j.status IN ('submitted','running') AND j.progress IS DISTINCT FROM 'closed' "
                        'ORDER BY o.created_at, o.operation_id LIMIT 1 FOR UPDATE OF o SKIP LOCKED',
                        (machine, subject))
            candidate = cur.fetchone()
            if candidate is None:
                return {'ok': True, 'kind': 'idle'}
            op, live = _host_scope(cur, candidate['operation_id'], machine, subject)
            if not live:
                _conflict()
            claim = secrets.token_urlsafe(32)
            claim_hash = _claim_value(claim)
            cur.execute('UPDATE campaign_host_operations SET attempt=attempt+1, fence=fence+1 '
                        'WHERE operation_id=%s', (op['operation_id'],))
        cur.execute("UPDATE campaign_host_operations SET claim_sha256=%s, "
                    "lease_expires_at=clock_timestamp()+interval '300 seconds', updated_at=clock_timestamp() "
                    'WHERE operation_id=%s RETURNING *', (claim_hash, op['operation_id']))
        public = _public(cur.fetchone())
        for key in ('outcome', 'stage_evidence', 'replayed'):
            public.pop(key)
        return {'ok': True, 'kind': 'claimed', 'operation': {**public, 'claim': claim}}


def settle_host_operation(subject, body):
    _closed(body, ('operation_id', 'attempt', 'fence', 'claim', 'input_sha256', 'stage', 'outcome', 'evidence'))
    machine = _machine(subject)
    if not isinstance(body['operation_id'], str) or str(_uuid(body['operation_id'])) != body['operation_id']:
        _invalid()
    claim_hash = _claim_value(body['claim'])
    if (any(type(body[key]) is not int or not 1 <= body[key] <= 9007199254740991 for key in ('attempt', 'fence'))
            or not _hex(body['input_sha256']) or body['stage'] not in STAGES
            or body['outcome'] not in ('succeeded', 'failed', 'held')):
        _invalid()
    evidence = body['evidence']
    _closed(evidence, ('config_identity_before', 'config_identity_after', 'readback_sha256', 'reason'))
    if (any(evidence[key] is not None and not _hex(evidence[key]) for key in
            ('config_identity_before', 'config_identity_after', 'readback_sha256'))
            or evidence['reason'] not in REASONS
            or (body['outcome'] == 'succeeded' and (evidence['config_identity_after'] is None
                or evidence['readback_sha256'] is None or evidence['reason'] not in ('verified', 'already_applied')))):
        _invalid()
    with _cursor() as cur:
        _lock(cur, 'campaign-host-machine:' + machine)
        op, live = _host_scope(cur, body['operation_id'], machine, subject)
        if (any(op[key] != body[key] for key in ('attempt', 'fence', 'input_sha256'))
                or not hmac.compare_digest(op['claim_sha256'] or '', claim_hash)):
            _conflict()
        stage = body['stage']
        proof = {'outcome': body['outcome'], 'evidence': evidence}
        prior = op['stage_evidence'].get(stage)
        if prior is not None:
            if prior != proof:
                _conflict()
            replayed = True
        else:
            if (not op['lease_live'] or op['outcome'] is not None or op['stage'] != stage
                    or _completed(op) != list(STAGES[:STAGES.index(stage)])
                    or (not live and not (stage == 'readback' and _completed(op) == ['apply', 'activate']))):
                _conflict()
            proofs = {**op['stage_evidence'], stage: proof}
            terminal = body['outcome'] != 'succeeded' or stage == 'readback'
            next_stage = stage if terminal else STAGES[STAGES.index(stage) + 1]
            cur.execute('UPDATE campaign_host_operations SET stage=%s, outcome=%s, stage_evidence=%s, '
                        'lease_expires_at=CASE WHEN %s THEN clock_timestamp() ELSE lease_expires_at END, '
                        'updated_at=clock_timestamp() WHERE operation_id=%s RETURNING *',
                        (next_stage, body['outcome'] if terminal else None, Jsonb(proofs), terminal, op['operation_id']))
            op = cur.fetchone()
            replayed = False
        return {'ok': True, 'operation_id': str(op['operation_id']), 'stage': stage,
                'outcome': body['outcome'], 'replayed': replayed, 'completed_stages': _completed(op)}


def count_invocation(org_id, project_id, campaign_id, enrollment_id, *, job_id, receipt):
    """Count a trusted build receipt extended with org/project, capability_provenance
    and host_readback (the exact closed readback evidence object). The producer's
    digest covers these additions and all its original fields.
    """
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    job_id = _uuid(job_id)
    if not isinstance(receipt, dict):
        _invalid()
    try:
        encoded = _canonical(receipt)
        receipt_digest = _sha({k: v for k, v in receipt.items() if k != 'digest'})
    except (TypeError, ValueError, OverflowError):
        _invalid()
    if len(encoded) > 16 * 1024:
        _invalid()
    required = {'schema', 'job_id', 'tenant_id', 'tool', 'status', 'attempt', 'execution_path',
                'fallback', 'created_at', 'finished_at', 'elapsed_ms', 'error_code', 'source_sha',
                'written_at', 'digest', 'org_id', 'project_id', 'capability_provenance', 'host_readback'}
    if (not required <= set(receipt) or receipt['schema'] != 'leaf.build-receipt.v1'
            or receipt['job_id'] != str(job_id) or receipt['status'] != 'complete'
            or not _hex(receipt['source_sha'], 40) or not _hex(receipt['digest'])
            or receipt['digest'] != receipt_digest
            or type(receipt['attempt']) is not int or receipt['attempt'] < 0
            or type(receipt['fallback']) is not bool
            or not isinstance(receipt['finished_at'], (int, float))
            or isinstance(receipt['finished_at'], bool) or receipt['finished_at'] <= 0
            or not isinstance(receipt['written_at'], (int, float))
            or isinstance(receipt['written_at'], bool) or receipt['written_at'] <= 0
            or receipt['error_code'] is not None):
        _conflict()
    identity = receipt.get('receipt_id', receipt['digest'])
    if not _token(identity, 200):
        _invalid()
    with _cursor() as cur:
        _machine_lock_for_enrollment(cur, enrollment_id)
        row, link, tenant = _load(cur, scope, enrollment_id)
        context = _persisted_context(row, link, tenant)
        job = _job(cur, job_id, context)
        invocation = _invocation(cur, job_id, context)
        op = _operation(cur, job_id)
        if (job['status'] != 'complete' or job['progress'] == 'closed'
                or receipt['capability_provenance'] != context
                or any(receipt[k] != context[k] for k in ('org_id', 'project_id', 'tenant_id'))
                or receipt['tool'] != context['tool_name'] or receipt['attempt'] != job['attempt']
                or receipt['created_at'] != job['created_at'] or receipt['finished_at'] != job['finished_at']
                or receipt['elapsed_ms'] != job['elapsed_ms']
                or op is None or op['outcome'] != 'succeeded' or _completed(op) != list(STAGES)
                or op['input_sha256'] != _sha({'schema': 'leaf.campaign-host-operation.v1',
                                              'job_id': str(job_id), 'context': context})
                or receipt['host_readback'] != op['stage_evidence']['readback']['evidence']):
            _conflict()
        if invocation['counted_at'] is not None:
            if invocation['counted_receipt_digest'] != receipt['digest'] or invocation['counted_receipt_id'] != identity:
                _conflict()
            return enrollment._public(row, enrollment._link(cur, scope, row['enrollment_id']), True)
        cur.execute('SELECT job_id FROM campaign_capability_invocations '
                    'WHERE link_id=%s AND counted_receipt_id=%s', (link['link_id'], identity))
        if cur.fetchone() or identity in (link['first_invocation_receipt_id'], link['second_invocation_receipt_id']):
            _conflict()
        cur.execute('UPDATE campaign_capability_invocations SET counted_receipt_digest=%s, '
                    'counted_receipt_id=%s, counted_at=clock_timestamp() WHERE job_id=%s',
                    (receipt['digest'], identity, job_id))
        if link['state'] != 'completed':
            first = link['first_invocation_receipt_id'] is None
            column = 'first_invocation_receipt_id' if first else 'second_invocation_receipt_id'
            cur.execute('UPDATE campaign_capability_links SET ' + column + '=%s, state=%s '
                        'WHERE link_id=%s', (identity, 'invoked_once' if first else 'completed', link['link_id']))
        link = enrollment._link(cur, scope, row['enrollment_id'])
        _event(cur, scope, _task(cur, scope, link['task_id']), 'capability_link_recorded',
               payload={'state': link['state'], 'job_id': str(job_id)})
        return enrollment._public(row, link)
