"""Durable host enrollment for the campaign execution ledger.

Publication and invocation evidence is recorded by the trusted capability store.
Enrollment itself never claims capability completion.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid

from . import campaign_execution as execution
from .campaigns import _principal
from .campaign_execution import (
    CampaignError, CampaignConflict, CampaignUnavailable, _scope, _uuid, _text,
    _lock, _row, _cursor, _check, _missing, _event, _task, SCOPE,
)


def allowed_machines():
    return list(dict.fromkeys(value.strip() for value in
        os.environ.get('LEAF_CAMPAIGN_ALLOWED_MACHINES', '').split(',') if value.strip()))


def _public(row, link, replayed=False):
    result = _row({key: row[key] for key in (
        'enrollment_id', 'machine_id', 'state', 'created_at', 'enabled_at', 'revoked_at')},
        replayed=replayed)
    result.pop('dispatch', None)
    result['capability_link'] = {key: str(link[key]) for key in
                               ('link_id', 'task_id', 'capability', 'state')}
    for key in ('author_stage_id', 'change_set_id', 'publication_id', 'effective_catalog_id',
                'catalog_commit', 'effective_catalog_digest', 'tool_name', 'tool_manifest_sha256',
                'tool_source_sha256', 'published_at', 'first_invocation_receipt_id',
                'second_invocation_receipt_id'):
        value = link.get(key)
        result['capability_link'][key] = value.isoformat() if hasattr(value, 'isoformat') else value
    result['capability_link']['counted_job_ids'] = [str(value) for value in link.get('counted_job_ids', [])]
    return result


def _enrollment(cur, scope, enrollment_id):
    cur.execute('SELECT * FROM campaign_host_enrollments WHERE ' + SCOPE +
                ' AND enrollment_id=%(enrollment)s FOR UPDATE',
                {**scope, 'enrollment': _uuid(enrollment_id)})
    row = cur.fetchone()
    if row is None:
        _missing()
    return row


def _link(cur, scope, enrollment_id):
    cur.execute('SELECT l.*, ARRAY(SELECT i.job_id FROM campaign_capability_invocations i '
                'WHERE i.link_id=l.link_id AND i.org_id=l.org_id AND i.project_id=l.project_id '
                'AND i.campaign_id=l.campaign_id AND i.enrollment_id=l.enrollment_id '
                'AND i.counted_at IS NOT NULL ORDER BY i.counted_at, i.job_id) AS counted_job_ids '
                'FROM campaign_capability_links l '
                'JOIN campaign_tasks t ON t.task_id=l.task_id AND t.org_id=l.org_id '
                'AND t.project_id=l.project_id AND t.campaign_id=l.campaign_id '
                'WHERE l.org_id=%(org)s AND l.project_id=%(project)s '
                'AND l.campaign_id=%(campaign)s AND l.enrollment_id=%(enrollment)s',
                {**scope, 'enrollment': enrollment_id})
    row = cur.fetchone()
    if row is None:
        _missing()
    return row


def _host_contract(machine_id, *, legacy=False):
    if legacy:
        return {
            'title': 'Implement campaign host enrollment',
            'spec': ('Implement scoped host enrollment for this campaign and configured machine '
                      + machine_id + '. Inputs: campaign scope, server allowlist, authenticated '
                      'project writer, deployed source SHA and worker subject. Outputs: durable '
                      'enrollment and pending canonical capability link. Required host access: '
                      'the configured worker may read only its enabled campaign recovery ledger. '
                      'Permissions: project write for admission and enable/revoke; no provider, '
                      'publication or deployment permission. Retry with the same campaign and '
                      'machine; preserve revocation and original task source. Evidence: PostgreSQL '
                      'authorization and replay results, client controls, then canonical author '
                      'publication and two distinct successful invocations from the future lifecycle '
                      'producer. Until that producer exists the link remains pending_link.'),
            'stages': ['implementation', 'build_test'],
            'owned_paths': ['platform/campaign_enrollment.py', 'server/routers/campaigns.py',
                            'web/src/campaigns/'],
            'verify_command': 'python -m pytest -q tests/test_campaign_enrollment_store.py',
            'declared_artifacts': ['enrollment-implementation', 'postgres-authorization-replay-results']
        }
    return dict(
        title='Verify campaign host enrollment',
        spec=('Verify the stored campaign, enrollment and canonical publication binding for '
              + machine_id + '. Preserve the original deployed Leaf source as lineage. '
              'Inputs are the stored enrollment, publication and two distinct actual async job uses '
              'with matching immutable invocation context and exact digest-verified host readbacks. '
              'Only the server-side campaign_capabilities.count_invocation producer may settle this '
              'verification after both stored host operations succeeded at apply, activate and readback. '
              'Client receipts, worker summaries and link state alone cannot authorize completion. '
              'Exact receipt replay recovers missing settlement and otherwise returns the existing '
              'result without another attempt. No physical automation or live client use is claimed.'),
        stages=['verification'], owned_paths=[],
        verify_command='campaign_capabilities.count_invocation',
        declared_artifacts=['published-capability-binding', 'two-verified-invocation-receipts'])


def _host_task(cur, scope, enrollment_id):
    # Resolve without an enrollment lock, then follow execution's task-first order.
    link = _link(cur, scope, _uuid(enrollment_id))
    return _task(cur, scope, link['task_id'])


def _repair_host_task(cur, scope, task, machine_id):
    contract = _host_contract(machine_id)
    params = {**scope, 'task': task['task_id']}
    cur.execute('SELECT p.task_key FROM campaign_task_dependencies d JOIN campaign_tasks p '
                'ON p.task_id=d.depends_on_task_id AND p.org_id=d.org_id '
                'AND p.project_id=d.project_id AND p.campaign_id=d.campaign_id '
                'WHERE d.org_id=%(org)s AND d.project_id=%(project)s '
                'AND d.campaign_id=%(campaign)s AND d.task_id=%(task)s', params)
    dependencies = [row['task_key'] for row in cur.fetchall()]
    material = {key: task[key] for key in (
        'task_key', 'title', 'spec', 'capability', 'stages', 'owned_paths', 'source_sha',
        'verify_command', 'declared_artifacts', 'kind', 'parent_task_id')}
    fingerprint = execution._fingerprint(
        'leaf.campaign.task.v1', execution._task_payload(**material, depends_on=dependencies))
    if (task['kind'] != 'capability' or task['capability'] != 'campaign.host-enrollment'
            or task['task_key'] != 'host-enrollment-' + hashlib.sha256(machine_id.encode()).hexdigest()
            or task['idempotency_key'] != task['task_key']
            or task['parent_task_id'] is not None or dependencies
            or fingerprint != task['payload_fingerprint']):
        raise CampaignConflict('reconcile_required', 'Enrollment task contract requires reconciliation')
    if all(task[key] == value for key, value in contract.items()):
        if task['current_stage'] != 'verification':
            raise CampaignConflict('reconcile_required', 'Enrollment task stage requires reconciliation')
        return task
    legacy = _host_contract(machine_id, legacy=True)
    if (any(task[key] != value for key, value in legacy.items())
            or task['status'] != 'pending' or task['fence'] != 0
            or task['current_stage'] != 'implementation'):
        raise CampaignConflict('reconcile_required', 'Enrollment task contract requires reconciliation')
    for table in ('campaign_task_attempts', 'campaign_stage_receipts', 'campaign_dispatch_bindings'):
        cur.execute('SELECT 1 FROM ' + table + ' WHERE ' + SCOPE + ' AND task_id=%(task)s LIMIT 1', params)
        if cur.fetchone():
            raise CampaignConflict('reconcile_required', 'Enrollment task has execution history')
    new_fingerprint = execution._fingerprint(
        'leaf.campaign.task.v1',
        execution._task_payload(**{**material, **contract}, depends_on=dependencies))
    cur.execute('UPDATE campaign_tasks SET title=%(title)s, spec=%(spec)s, stages=%(stages)s, '
                'owned_paths=%(owned_paths)s, verify_command=%(verify_command)s, '
                'declared_artifacts=%(declared_artifacts)s, current_stage=\'verification\', '
                'payload_fingerprint=%(fingerprint)s WHERE ' + SCOPE +
                ' AND task_id=%(task)s RETURNING *',
                {**params, **contract, 'stages': execution.Jsonb(contract['stages']),
                 'owned_paths': execution.Jsonb(contract['owned_paths']),
                 'declared_artifacts': execution.Jsonb(contract['declared_artifacts']),
                 'fingerprint': new_fingerprint})
    task = cur.fetchone()
    _event(cur, scope, task, 'capability_link_recorded', payload={
        'old_fingerprint': fingerprint, 'new_fingerprint': new_fingerprint,
        'changed_fields': sorted([*contract, 'current_stage', 'payload_fingerprint']),
        'reason': 'Untouched generated host task now verifies the stored two-use lifecycle'})
    return task


def request_enrollment(org_id, project_id, campaign_id, principal_id, *, machine_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    principal = _uuid(principal_id)
    _text(machine_id, 'machine_id', 200)
    if machine_id not in allowed_machines():
        raise CampaignError('invalid_machine', 'Choose a configured machine')
    key = 'host-enrollment-' + hashlib.sha256(machine_id.encode()).hexdigest()
    with _cursor() as cur:
        _check(cur, scope)
        _principal(cur, scope, principal)
        # Separate from submit_task's locks: its own transaction commits first.
        # A crash after that commit is recovered by the deterministic task key.
        _lock(cur, f"enrollment:{scope['campaign']}:{machine_id}")
        cur.execute('SELECT * FROM campaign_host_enrollments WHERE ' + SCOPE +
                    ' AND machine_id=%(machine)s', {**scope, 'machine': machine_id})
        existing = cur.fetchone()
        if existing:
            task = _host_task(cur, scope, existing['enrollment_id'])
            _repair_host_task(cur, scope, task, machine_id)
            return _public(existing, _link(cur, scope, existing['enrollment_id']), True)
        subject = os.environ.get('LEAF_CAMPAIGN_WORKER_SUBJECT', '')
        if not subject or len(subject) > 200:
            raise CampaignUnavailable('worker_unavailable', 'Campaign worker is not configured')
        cur.execute('SELECT * FROM campaign_tasks WHERE ' + SCOPE +
                    ' AND task_key=%(key)s', {**scope, 'key': key})
        task = cur.fetchone()
        if task is None:
            source = os.environ.get('LEAF_SOURCE_SHA', '')
            if re.fullmatch(r'[0-9a-f]{40}', source) is None:
                raise CampaignUnavailable('source_unavailable', 'Deployed implementation source is unavailable')
            task = execution.submit_task(
                org_id, project_id, campaign_id, task_key=key, idempotency_key=key,
                kind='capability', capability='campaign.host-enrollment', source_sha=source,
                **_host_contract(machine_id), depends_on=[])
        task = _task(cur, scope, _uuid(task['task_id']))
        task = _repair_host_task(cur, scope, task, machine_id)
        if task['kind'] != 'capability' or task['capability'] != 'campaign.host-enrollment':
            raise CampaignConflict('task_conflict', 'Enrollment task identity conflicts')
        cur.execute('INSERT INTO campaign_host_enrollments '
                    '(enrollment_id, org_id, project_id, campaign_id, machine_id, service_subject, '
                    'enrolled_by_binding_id) VALUES (%(id)s, %(org)s, %(project)s, %(campaign)s, '
                    '%(machine)s, %(subject)s, %(principal)s) RETURNING *',
                    {**scope, 'id': uuid.uuid4(), 'machine': machine_id,
                     'subject': subject, 'principal': principal})
        row = cur.fetchone()
        cur.execute('INSERT INTO campaign_capability_links '
                    '(link_id, org_id, project_id, campaign_id, task_id, enrollment_id, capability) '
                    'VALUES (%(id)s, %(org)s, %(project)s, %(campaign)s, %(task)s, %(enrollment)s, '
                    '%(capability)s) RETURNING *',
                    {**scope, 'id': uuid.uuid4(), 'task': task['task_id'],
                     'enrollment': row['enrollment_id'], 'capability': task['capability']})
        link = cur.fetchone()
        _event(cur, scope, task, 'enrollment_requested')
        _event(cur, scope, task, 'capability_link_recorded')
        return _public(row, link)


def _transition(org_id, project_id, campaign_id, enrollment_id, principal_id, state):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    principal = _uuid(principal_id)
    with _cursor() as cur:
        _check(cur, scope)
        _principal(cur, scope, principal)
        _host_task(cur, scope, enrollment_id)
        row = _enrollment(cur, scope, enrollment_id)
        link = _link(cur, scope, row['enrollment_id'])
        if row['state'] == state:
            return _public(row, link, True)
        if row['state'] == 'revoked':
            raise CampaignConflict('enrollment_revoked', 'Revoked enrollment cannot be enabled')
        assignments = ("state='enabled', enabled_at=NOW(), enabled_by_binding_id=%(principal)s"
                       if state == 'enabled' else "state='revoked', revoked_at=NOW()")
        cur.execute('UPDATE campaign_host_enrollments SET ' + assignments + ' WHERE ' + SCOPE +
                    ' AND enrollment_id=%(enrollment)s RETURNING *',
                    {**scope, 'enrollment': row['enrollment_id'], 'principal': principal})
        row = cur.fetchone()
        _event(cur, scope, _task(cur, scope, link['task_id']), 'enrollment_' + state)
        return _public(row, link)


def enable_enrollment(org_id, project_id, campaign_id, enrollment_id, principal_id):
    return _transition(org_id, project_id, campaign_id, enrollment_id, principal_id, 'enabled')


def revoke_enrollment(org_id, project_id, campaign_id, enrollment_id, principal_id):
    return _transition(org_id, project_id, campaign_id, enrollment_id, principal_id, 'revoked')


def list_enrollments(org_id, project_id, campaign_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    with _cursor() as cur:
        _check(cur, scope)
        cur.execute('SELECT * FROM campaign_host_enrollments WHERE ' + SCOPE +
                    ' ORDER BY created_at, enrollment_id', scope)
        rows = cur.fetchall()
        return [_public(row, _link(cur, scope, row['enrollment_id'])) for row in rows]


def resolve_worker_scope(cur, enrollment_id, subject):
    """Resolve persisted authority while retaining the enrollment read lock."""
    configured = os.environ.get('LEAF_CAMPAIGN_WORKER_SUBJECT', '')
    if not configured or subject != configured:
        raise CampaignError('worker_forbidden', 'Campaign worker is not authorized')
    cur.execute('SELECT e.*, c.tenant_id, c.prompt FROM campaign_host_enrollments e JOIN campaigns c '
                'ON c.campaign_id=e.campaign_id AND c.org_id=e.org_id AND c.project_id=e.project_id '
                'JOIN projects p ON p.org_id=e.org_id AND p.project_id=e.project_id '
                "WHERE e.enrollment_id=%s AND e.service_subject=%s AND e.state='enabled' "
                "AND p.status='active' AND p.deleted_at IS NULL FOR SHARE OF e",
                (_uuid(enrollment_id), subject))
    row = cur.fetchone()
    if row is None:
        raise CampaignError('worker_forbidden', 'Campaign worker is not authorized')
    scope = {**_scope(row['org_id'], row['project_id']), 'campaign': row['campaign_id']}
    _link(cur, scope, row['enrollment_id'])
    return dict(scope, enrollment_id=row['enrollment_id'], machine_id=row['machine_id'],
                tenant_id=row['tenant_id'], prompt=row['prompt'])


def resolve_worker_enrollment(enrollment_id, subject):
    """Preserve the recovery list and read-lock lifetime."""
    with _cursor() as cur:
        scope = resolve_worker_scope(cur, enrollment_id, subject)
        bindings = execution.pending_remote_bindings(scope['org'], scope['project'], scope['campaign'])
        return [binding for binding in bindings if binding['machine_id'] == scope['machine_id']]
