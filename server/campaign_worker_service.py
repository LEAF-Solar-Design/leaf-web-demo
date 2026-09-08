"""Enrolled planning-task entry for the campaign workflow, with no dispatch."""
from __future__ import annotations

import hashlib
import os

import project_repository_source


def _platform():
    # The router installs the non-stdlib package alias before this lazy import.
    from leaf_platform import campaign_enrollment, campaign_execution, campaign_plan
    return campaign_enrollment, campaign_execution, campaign_plan


_PLAN_FIELDS = ('task_key', 'source_sha', 'owned_paths', 'verify_command', 'declared_artifacts', 'spec')
_ATTEMPT_FIELDS = ('attempt_id', 'fence', 'stage', 'deadline_at')


def _active(cur, execution, scope, worker_id):
    cur.execute('SELECT * FROM campaign_task_attempts WHERE ' + execution.SCOPE +
                " AND worker_id=%(worker)s AND status='active' AND deadline_at>clock_timestamp() "
                'ORDER BY claimed_at, attempt_id LIMIT 1', dict(scope, worker=worker_id))
    row = cur.fetchone()
    if row is None:
        return None
    public = execution._public_attempt(row)
    return {key: public[key] for key in _ATTEMPT_FIELDS}


def pending_for_enrollment(cur, execution, scope):
    bindings = execution.pending_remote_bindings(scope['org'], scope['project'], scope['campaign'])
    cur.execute('SELECT attempt_id FROM campaign_task_attempts WHERE ' + execution.SCOPE +
                ' AND worker_id=%(worker)s',
                dict(scope, worker='enrollment-' + str(scope['enrollment_id'])))
    attempts = {str(row['attempt_id']) for row in cur.fetchall()}
    return [binding for binding in bindings if binding['machine_id'] == scope['machine_id']
            and str(binding['attempt_id']) in attempts]


def _planning_wait(cur, execution, scope):
    if os.environ.get('LEAF_CAMPAIGN_BRIDGE') != 'on':
        return None
    cur.execute('SELECT task_id, status, current_stage FROM campaign_tasks WHERE ' + execution.SCOPE +
                " AND task_key='campaign-plan' FOR UPDATE", scope)
    task = cur.fetchone()
    if task and task['current_stage'] == 'build_test' and task['status'] not in ('failed', 'succeeded'):
        from leaf_platform.campaign_plan_adoption import saved_plan_source
        return {'kind': 'awaiting_plan_validation', 'task_id': str(task['task_id']),
                'plan_source': saved_plan_source(cur, scope, task['task_id'])}
    return None


def _product_next(cur, execution, scope, worker_id, lease):
    if os.environ.get('LEAF_CAMPAIGN_BRIDGE') != 'on':
        return None
    from leaf_platform import campaign_product_publication as publication
    cur.execute('SELECT status FROM campaign_tasks WHERE ' + execution.SCOPE +
                " AND task_key='campaign-plan'", scope)
    planning = cur.fetchone()
    if not planning or planning['status'] != 'succeeded':
        return None
    publication.principal(cur, scope)
    cur.execute('SELECT * FROM campaign_tasks WHERE ' + execution.SCOPE +
                " AND kind='task' AND task_key<>'campaign-plan' ORDER BY created_at,task_id", scope)
    tasks = [task for task in cur.fetchall() if publication.product_task(task)]
    for task in tasks:
        if task['current_stage'] in ('build_test', 'publication') and task['status'] != 'succeeded':
            _, _, _, source = publication.saved_context(cur, scope, task['task_id'])
            return dict(kind='awaiting_product_publication', product_source=source)
    for task in tasks:
        if task['current_stage'] != 'implementation' or task['status'] not in ('claimed', 'pending'):
            continue
        cur.execute('SELECT * FROM campaign_task_attempts WHERE ' + execution.SCOPE +
                    " AND task_id=%(task)s AND worker_id=%(worker)s AND status='active' "
                    'AND deadline_at>clock_timestamp()', dict(scope, task=task['task_id'], worker=worker_id))
        active = cur.fetchone()
        if active:
            return dict(kind='active', task=execution._row(task),
                        attempt={k: execution._public_attempt(active)[k] for k in _ATTEMPT_FIELDS})
        claimed = execution._claim_task_cursor(cur, scope, worker_id=worker_id, lease=lease,
                                               task_key=task['task_key'])
        if claimed:
            return dict(kind='claimed', task=execution._row(task),
                        attempt={k: claimed[k] for k in (*_ATTEMPT_FIELDS, 'attempt_token')})
    return dict(kind='idle')


def next_work(enrollment_id, subject, *, lease_seconds=900):
    enrollment, execution, plan = _platform()
    if os.environ.get('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', '') != 'on':
        raise execution.CampaignUnavailable('producer_disabled', 'Campaign first-task producer is disabled')
    lease = execution._integer(lease_seconds, 30, 3600)
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
        execution._lock(cur, 'campaign-next:' + str(scope['enrollment_id']))
        bindings = pending_for_enrollment(cur, execution, scope)
        waiting = _planning_wait(cur, execution, scope) if not bindings else None
    worker_id = 'enrollment-' + str(scope['enrollment_id'])
    response = dict(ok=True, enrollment_id=str(scope['enrollment_id']), scope={
        'org_id': str(scope['org']), 'project_id': str(scope['project']),
        'campaign_id': str(scope['campaign']), 'machine_id': scope['machine_id']})
    if bindings:
        return dict(response, kind='recover', pending_remote_bindings=bindings)
    from leaf_platform import campaign_release
    completion = campaign_release.release_snapshot(scope['org'], scope['project'], scope['campaign'])
    release = completion.get('release')
    if release and release['status'] in ('paused', 'cancelled', 'finished', 'queued', 'needs_approach'):
        return dict(response, kind='release_wait', completion=completion)
    if waiting:
        return dict(response, **waiting)
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
        execution._lock(cur, 'campaign-next:' + str(scope['enrollment_id']))
        product = _product_next(cur, execution, scope, worker_id, lease)
    if product is not None:
        return dict(response, **product)
    with execution._cursor() as cur:
        active = _active(cur, execution, scope, worker_id)
    if active:
        return dict(response, kind='active', attempt=active)

    # Validate size before any source or task mutation. No DB transaction spans P1.
    plan.first_task_spec(scope, '0' * 40, scope['prompt'])
    tenant_id = scope['tenant_id']
    if not project_repository_source._uuid(tenant_id):
        raise project_repository_source.SourceConflict('source authority conflicts')
    source = project_repository_source.initialize_project_source(
        tenant_id, str(scope['org']), str(scope['project']), scope['prompt'])
    if (not isinstance(source, dict)
            or not project_repository_source._sha(source.get('source_commit'), 40)
            or not project_repository_source._sha(source.get('source_tree'), 40)
            or source.get('seed_digest') != hashlib.sha256(scope['prompt'].encode('utf-8')).hexdigest()):
        raise project_repository_source.SourceConflict('source seed conflicts')
    # Recheck after P1: revoked authority cannot create even a pending task.
    # submit_task commits on its own connection while this enrollment lock lives.
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
        task = plan.ensure_first_task(scope, source['source_commit'], scope['prompt'])
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
        execution._lock(cur, 'campaign-next:' + str(scope['enrollment_id']))
        bindings = pending_for_enrollment(cur, execution, scope)
        if bindings:
            return dict(response, kind='recover', pending_remote_bindings=bindings)
        waiting = _planning_wait(cur, execution, scope)
        if waiting:
            return dict(response, **waiting)
        active = _active(cur, execution, scope, worker_id)
        if active:
            return dict(response, kind='active', attempt=active)
        claimed = execution._claim_task_cursor(
            cur, scope, worker_id=worker_id, lease=lease, task_key='campaign-plan')
    result = dict(response, kind='claimed' if claimed else 'idle',
                  plan_task={key: task[key] for key in _PLAN_FIELDS},
                  source={key: source[key] for key in ('source_commit', 'source_tree', 'seed_digest')})
    if claimed:
        result['attempt'] = {key: claimed[key] for key in (*_ATTEMPT_FIELDS, 'attempt_token')}
    return result
