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
                kind='capability', title='Implement campaign host enrollment',
                capability='campaign.host-enrollment', source_sha=source,
                spec=('Implement scoped host enrollment for this campaign and configured machine '
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
                stages=['implementation', 'build_test'],
                owned_paths=['platform/campaign_enrollment.py', 'server/routers/campaigns.py',
                             'web/src/campaigns/'],
                verify_command='python -m pytest -q tests/test_campaign_enrollment_store.py',
                declared_artifacts=['enrollment-implementation', 'postgres-authorization-replay-results'],
                depends_on=[])
        task = _task(cur, scope, _uuid(task['task_id']))
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
