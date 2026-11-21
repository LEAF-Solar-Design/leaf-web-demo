"""Read-only recovery preparation for independent, pinned corpus nodes.

This is not execution admission. A future executor must obtain confirmation of
the current plan digest and create a new document/run with this source lineage.
"""
from __future__ import annotations

import copy

import jobs
import lab_artifacts as ledger
import lab_corpus_executor as corpus
import lab_run_store as store
import lab_tool_manifests
import lab_tool_nodes
import platform_link
from lab_canonical import graph_digest
from lab_evaluate import GraphRefused, admit_graph
from lab_doc_validators import validate_graph

SCHEMA = 'leaf.lab-corpus-recovery-plan.v1'


def lookup_job(tenant, project, intent):
    """Bounded exact intent lookup; never use job lists or Lab event payloads."""
    with platform_link.platform_db().cursor() as cur:
        cur.execute('SELECT job_id,status,attempt,corpus_cancel_json,corpus_cleanup_json '
                    'FROM async_jobs WHERE tenant_id=%s AND org_id=%s AND project_id=%s '
                    'AND idempotency_key=%s ORDER BY job_id LIMIT 2',
                    (tenant, tenant, project, intent))
        matches = cur.fetchall()
    if len(matches) != 1:
        return {'reason_category': 'missing_job' if not matches else 'duplicate_jobs'}
    matched = matches[0]
    job = jobs.get_job(str(matched['job_id']))
    if (job is None or job.get('job_id') != str(matched['job_id'])
            or job.get('status') != matched['status'] or job.get('attempt') != matched['attempt']):
        return {'reason_category': 'job_receipt_changed'}
    return {'job': job, 'cancel': matched['corpus_cancel_json'],
            'cleanup': matched['corpus_cleanup_json']}


def _disposition(job, receipt, source):
    status = job.get('status')
    if status == 'complete':
        # Verdict fail is a completed evaluation, never a failed execution.
        return 'retained', 'completed'
    cancel, cleanup = receipt.get('cancel'), receipt.get('cleanup')
    if cancel is not None:
        if (not isinstance(cleanup, dict) or cleanup.get('cancel') != cancel
                or cleanup.get('attempt') != job.get('attempt')
                or not (cleanup.get('reaped') is True or cleanup.get('not_started') is True)):
            return 'unresolved', 'cleanup_unconfirmed'
        return 'unresolved', 'cancelled'
    if job.get('progress') == 'closed':
        return 'unresolved', 'cleanup_unconfirmed'
    if status in {'submitted', 'queued', 'running'}:
        return 'unresolved', 'job_nonterminal'
    if status in {'cancelled', 'canceled'}:
        return 'unresolved', 'cancelled'
    if status != 'failed':
        return 'unresolved', 'unknown_status'
    if source['state'] == 'cancelled' or source.get('cancel_requested_at') is not None:
        return 'unresolved', 'source_cancelled'
    error = job.get('error')
    error = error if isinstance(error, dict) else {}
    code = error.get('error_code')
    if code in {'BAD_PARAMS', 'INVALID_INPUT', 'VALIDATION_ERROR', 'BAD_REQUEST'}:
        return 'unresolved', 'invalid_input'
    if code in {'FORBIDDEN', 'UNAUTHORIZED', 'UNAUTHENTICATED', 'ACCESS_REVOKED',
                'TENANT_DISABLED', 'GRANT_REQUIRED', 'ENTITLEMENT_REQUIRED'}:
        return 'unresolved', 'access_revoked'
    if code in {'CANCELLED', 'CANCELED'}:
        return 'unresolved', 'cancelled'
    if code == 'TIMEOUT':
        return 'selected', 'timeout'
    if code == 'RESOURCE_EXHAUSTED':
        return 'selected', 'resource_exhausted'
    reason = error.get('corpus_failure')
    if reason == 'lease_or_budget':
        return 'selected', 'lease_or_budget'
    if reason in {'runtime_unavailable', 'capability_unavailable', 'kernel_isolation_unavailable'}:
        return 'unresolved', 'runtime_unavailable'
    if reason in {'package_identity', 'module_identity', 'material_custody',
                  'result_identity', 'terminal_identity', 'privileged_runtime', 'bounded_result_absent'}:
        return 'unresolved', 'verification_required'
    # Legacy INTERNAL messages cannot establish resource exhaustion, even if
    # their raw exception text mentions memory, timeout, credentials or paths.
    return 'unresolved', 'unknown_failure'


def prepare(tenant, run_id, *, authorize):
    """Resolve immutable source and authoritative jobs, then recheck live access."""
    source = store.get(tenant, run_id)
    if source['tenant_id'] != tenant or source['run_id'] != run_id:
        raise GraphRefused('lab_recovery_source_mismatch')
    authority = store.corpus_authority(source)
    if authority is None:
        raise GraphRefused('lab_recovery_corpus_authority_required')
    authorize(source)
    if not lab_tool_nodes.enabled():
        raise GraphRefused('lab_corpus_unavailable')
    if source['state'] not in {'finished', 'failed', 'cancelled'}:
        raise GraphRefused('lab_recovery_terminal_required')
    artifact = ledger.get(tenant, source['doc_id'], source['graph_revision'])
    graph = artifact['graph']
    if (artifact['tenant_id'] != tenant or artifact['doc_id'] != source['doc_id']
            or artifact['revision'] != source['graph_revision']
            or artifact['sha256'] != source['graph_sha256']
            or graph_digest(graph) != source['graph_sha256']):
        raise GraphRefused('lab_recovery_digest_mismatch')
    validate_graph(graph)
    if (not graph['blocks'] or graph['pipes']
            or any(block['registry']['id'] != 'corpus-evaluator' for block in graph['blocks'])):
        raise GraphRefused('lab_recovery_independent_corpus_required')
    manifest, _ = lab_tool_manifests.corpus_capability()
    admit_graph(graph, {'corpus-evaluator@1.0.0': manifest})
    if any(block['settings']['project_id'] != authority['project_id'] for block in graph['blocks']):
        raise GraphRefused('lab_recovery_project_mismatch')

    blocks, selected, retained, unresolved = [], [], [], []
    for block in graph['blocks']:
        intent = f'lab:{run_id}:{block["id"]}:corpus:v1'
        receipt = lookup_job(tenant, authority['project_id'], intent)
        job = receipt.get('job')
        item = {'block_id': block['id'], 'idempotency_key': intent,
                'job_id': None, 'status': None, 'receipt_sha256': None}
        if job is None:
            disposition, reason = 'unresolved', receipt['reason_category']
        elif (str(job.get('tenant_id')) != tenant or str(job.get('org_id')) != tenant
                or str(job.get('project_id')) != authority['project_id']
                or job.get('idempotency_key') != intent or job.get('tool') != corpus.OPERATION):
            # Never project any identifier, status or receipt from foreign jobs.
            disposition, reason = 'unresolved', 'job_binding_mismatch'
        else:
            disposition, reason = _disposition(job, receipt, source)
            item.update(job_id=job['job_id'], status=job['status'],
                receipt_sha256=graph_digest({
                    'job': {key: job.get(key) for key in (
                        'job_id', 'tenant_id', 'org_id', 'project_id', 'tool', 'idempotency_key',
                        'status', 'progress', 'attempt', 'params', 'result', 'error', 'provenance',
                        'lab_corpus', 'authority_mode')},
                    'cancel': receipt.get('cancel'), 'cleanup': receipt.get('cleanup')}))
        item.update(disposition=disposition, reason_category=reason)
        blocks.append(item)
        {'selected': selected, 'retained': retained, 'unresolved': unresolved}[disposition].append(block['id'])

    draft = None
    if selected and not unresolved:
        draft = copy.deepcopy(dict(graph,
            blocks=[block for block in graph['blocks'] if block['id'] in selected],
            outputs={name: endpoint for name, endpoint in graph['outputs'].items()
                     if endpoint['node'] in selected}))
    plan = {'schema': SCHEMA, 'source': {key: source[key] for key in (
                'run_id', 'doc_id', 'graph_revision', 'graph_sha256', 'state')},
            'blocks': blocks, 'selected_failed_block_ids': selected,
            'retained_completed_block_ids': retained, 'unresolved_block_ids': unresolved,
            'draft_graph': draft, 'execution_allowed': False,
            'confirmation_required': True}
    plan['digest'] = graph_digest(plan)
    authorize(source)
    return plan
