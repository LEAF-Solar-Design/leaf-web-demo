"""Semantic build and source publication receipts for adopted campaign products."""
from __future__ import annotations

import hashlib
import secrets
import uuid

from . import campaign_enrollment as enrollment, campaign_execution as execution, campaigns

ACCEPTANCE = 'leaf.campaign.product-publication.v1'


def source_evidence(source):
    # L2's secret-shaped-field guard reserves token-named keys.
    return dict({k: v for k, v in source.items() if k != 'remote_fencing_token'},
                remote_fence=source['remote_fencing_token'])


def product_task(task):
    return (task['kind'] == 'task' and task['task_key'] != 'campaign-plan'
            and task['stages'] == ['implementation', 'build_test', 'publication']
            and task['parent_task_id'] is not None)


def principal(cur, scope):
    campaign = campaigns._campaign(cur, scope)
    if campaign is None or str(campaign['tenant_id']) != str(scope['tenant_id']):
        execution._conflict('source_authority_conflict')
    campaigns._principal(cur, scope, campaign['principal_id'])
    cur.execute("SELECT binding_id FROM identity_bindings WHERE platform_tenant_id=%(org)s "
                "AND binding_id=%(principal)s AND status='active' AND role IN ('owner','editor')",
                dict(scope, principal=campaign['principal_id']))
    if cur.fetchone() is None:
        raise execution.CampaignError('worker_forbidden', 'Campaign principal is not authorized')
    return str(campaign['principal_id'])


def saved_context(cur, scope, task_id):
    """Lock task before attempts; retain the original remote attempt after expiry."""
    task = execution._task(cur, scope, execution._uuid(task_id))
    if not product_task(task):
        execution._conflict('product_identity_mismatch')
    cur.execute('SELECT * FROM campaign_task_attempts WHERE ' + execution.SCOPE +
                " AND task_id=%(task)s AND stage='implementation' AND worker_id=%(worker)s "
                'ORDER BY fence DESC FOR UPDATE',
                dict(scope, task=task['task_id'], worker='enrollment-' + str(scope['enrollment_id'])))
    for attempt in cur.fetchall():
        binding = execution._dispatch_binding(cur, scope, attempt)
        source = execution._attempt_source(cur, scope, attempt)
        if (not binding or binding['state'] != 'settled' or not source
                or binding['machine_id'] != scope['machine_id']
                or binding['source_ref'] != source['commit_sha']):
            continue
        cur.execute('SELECT r.* FROM campaign_stage_receipts r WHERE r.org_id=%(org)s '
                    'AND r.project_id=%(project)s AND r.campaign_id=%(campaign)s '
                    "AND r.task_id=%(task)s AND r.stage='implementation' AND r.outcome='succeeded' "
                    'AND (r.attempt_id=%(attempt)s OR r.reconciles_receipt_id IN '
                    '(SELECT receipt_id FROM campaign_stage_receipts WHERE ' + execution.SCOPE +
                    ' AND task_id=%(task)s AND attempt_id=%(attempt)s))',
                    dict(scope, task=task['task_id'], attempt=attempt['attempt_id']))
        receipt = cur.fetchone()
        result = receipt['result'] if receipt else {}
        fingerprint = result.get('result_fingerprint')
        if (result.get('result_binding') != 'bound'
                or result.get('requested_source_sha') != source['commit_sha']
                or not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(c not in '0123456789abcdef' for c in fingerprint)
                or type(binding['remote_fencing_token']) is not int):
            continue
        wire = dict(task_id=str(task['task_id']), attempt_id=str(attempt['attempt_id']),
                    fence=attempt['fence'], leaf_id=binding['leaf_id'], run_id=binding['run_id'],
                    remote_fencing_token=binding['remote_fencing_token'],
                    result_fingerprint=fingerprint, payload_fingerprint=task['payload_fingerprint'],
                    source_ref=source['commit_sha'])
        return task, attempt, receipt, wire
    execution._conflict('product_identity_mismatch')


def _settle(cur, scope, task, implementation, stage, result, artifact, operation=None):
    params = dict(scope, task=task['task_id'], stage=stage)
    cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + execution.SCOPE +
                " AND task_id=%(task)s AND stage=%(stage)s AND outcome='succeeded'", params)
    existing = cur.fetchone()
    values = execution._values('succeeded', result, artifact, operation, None, None, True)
    if existing:
        if existing['result_fingerprint'] != values['result_fingerprint']:
            execution._conflict('settlement_conflict')
        return existing, True
    if (task['status'] != 'pending' or task['current_stage'] != stage
            or task['fence'] != implementation['fence']):
        execution._conflict('stale_attempt')
    execution._evidence(task, values)
    cur.execute('UPDATE campaign_tasks SET fence=fence+1, updated_at=NOW() WHERE ' +
                execution.SCOPE + ' AND task_id=%(task)s RETURNING *', params)
    task = cur.fetchone()
    cur.execute('INSERT INTO campaign_task_attempts '
                '(attempt_id,task_id,org_id,project_id,campaign_id,fence,attempt_token_hash,'
                'worker_id,stage,deadline_at,settled_at,status,outward_operation_key) VALUES '
                '(%(id)s,%(task)s,%(org)s,%(project)s,%(campaign)s,%(fence)s,%(hash)s,'
                "'product-publication',%(stage)s,NOW(),NOW(),'settled',%(operation)s) RETURNING *",
                dict(params, id=uuid.uuid4(), fence=task['fence'], operation=operation,
                     hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
    attempt = cur.fetchone()
    receipt = execution._receipt(cur, scope, task, attempt, values)
    execution._advance(cur, scope, task, 'succeeded')
    execution._event(cur, scope, task, 'stage_succeeded', attempt, result)
    return receipt, False


def settle_build(enrollment_id, subject, task_id, source, *, accept_sha256, artifact_ref):
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
        principal(cur, scope)
        task, attempt, implementation, saved = saved_context(cur, scope, task_id)
        if saved != source:
            execution._conflict('product_identity_mismatch')
        result = dict(acceptance=ACCEPTANCE, source_attempt_id=saved['attempt_id'],
                      source_receipt_id=str(implementation['receipt_id']),
                      product_fingerprint=saved['result_fingerprint'], accept_sha256=accept_sha256,
                      exit_code=0, verify_command=task['verify_command'])
        return _settle(cur, scope, task, implementation, 'build_test', result, artifact_ref)[0]


def settle_publication(enrollment_id, subject, task_id, source):
    """L2 committed source evidence can finish a missing semantic receipt on restart."""
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, enrollment_id, subject)
        principal(cur, scope)
        task, attempt, _, saved = saved_context(cur, scope, task_id)
        if saved != source:
            execution._conflict('product_identity_mismatch')
        readback = execution._attempt_source(cur, scope, attempt, result=True)
        if not readback:
            execution._conflict('publication_evidence_missing')
        proof = readback['publication_receipt']
        if (proof.get('acceptance') != ACCEPTANCE or proof.get('source') != source_evidence(saved)
                or proof.get('source_commit') != readback['commit_sha']
                or proof.get('source_tree') != readback['tree_sha']):
            execution._conflict('publication_evidence_mismatch')
        result = dict(acceptance=ACCEPTANCE, source_attempt_id=saved['attempt_id'],
                      product_fingerprint=saved['result_fingerprint'],
                      source_commit=readback['commit_sha'], source_tree=readback['tree_sha'],
                      publication_receipt_sha256=readback['publication_receipt_sha256'])
        cur.execute('SELECT * FROM campaign_stage_receipts WHERE ' + execution.SCOPE +
                    " AND task_id=%(task)s AND stage='build_test' AND outcome='succeeded'",
                    dict(scope, task=task['task_id']))
        build = cur.fetchone()
        if (not build or build['result'].get('acceptance') != ACCEPTANCE
                or build['result'].get('source_attempt_id') != saved['attempt_id']
                or build['result'].get('product_fingerprint') != saved['result_fingerprint']):
            execution._conflict('publication_evidence_mismatch')
        receipt, replayed = _settle(cur, scope, task, build, 'publication', result,
                                   proof['receipt_digest'], proof['edit_id'])
        return dict(ok=True, receipt=dict(receipt_id=str(receipt['receipt_id']),
                    task_id=str(task['task_id']), attempt_id=str(receipt['attempt_id']),
                    stage='publication', outcome='succeeded', replayed=replayed),
                    source=dict(source_commit=readback['commit_sha'], source_tree=readback['tree_sha']))
