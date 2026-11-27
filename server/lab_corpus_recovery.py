"""Review and confirm recovery lineage for independent, pinned corpus nodes."""
from __future__ import annotations

import copy
import re

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
CLEANUP_SCHEMA = 'leaf.lab-corpus-cleanup-evidence.v1'
SCHEMA_V2 = 'leaf.lab-corpus-recovery-plan.v2'


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


def _cleanup_evidence(job, receipt, run_id, block_id, *, stable):
    evidence = {'schema': CLEANUP_SCHEMA, 'job_id': job['job_id'] if job else None,
                'attempt': None, 'state': 'unconfirmed', 'cancel': None, 'receipt': None}
    if not job or not stable:
        return evidence
    attempt = job.get('attempt')
    if type(attempt) is not int or attempt < 0:
        return evidence
    evidence['attempt'] = attempt
    tag = receipt.get('cancel')
    if (not isinstance(tag, dict)
            or set(tag) != {'run_id', 'block_id', 'attempt', 'fence', 'reason'}
            or tag['run_id'] != run_id or tag['block_id'] != block_id
            or type(tag['attempt']) is not int or tag['attempt'] != attempt
            or type(tag['fence']) is not int or tag['fence'] <= 0
            or tag['reason'] not in ('deadline', 'cancel')):
        return evidence
    evidence['cancel'] = dict(tag)
    cleanup = receipt.get('cleanup')
    # The native reap producer includes worker_id. It is never public evidence.
    if (not isinstance(cleanup, dict)
            or not {'attempt', 'cancel'} <= set(cleanup)
            or set(cleanup) - {'attempt', 'cancel', 'reaped', 'not_started', 'worker_id'}
            or type(cleanup['attempt']) is not int or cleanup['attempt'] != attempt
            or not isinstance(cleanup['cancel'], dict)
            or graph_digest(cleanup['cancel']) != graph_digest(tag)
            or any(type(cleanup[key]) is not bool for key in ('reaped', 'not_started') if key in cleanup)):
        return evidence
    reaped, not_started = cleanup.get('reaped', False), cleanup.get('not_started', False)
    if reaped == not_started:
        return evidence
    evidence['receipt'] = {'attempt': attempt, 'cancel': dict(tag),
                           'reaped': reaped, 'not_started': not_started}
    evidence['state'] = 'confirmed_reaped' if reaped else 'confirmed_not_started'
    return evidence


def _pinned_graph(tenant, source):
    artifact = ledger.get(tenant, source['doc_id'], source['graph_revision'])
    graph = artifact['graph']
    if (artifact['tenant_id'] != tenant or artifact['doc_id'] != source['doc_id']
            or artifact['revision'] != source['graph_revision']
            or artifact['sha256'] != source['graph_sha256']
            or graph_digest(graph) != source['graph_sha256']):
        raise GraphRefused('lab_recovery_digest_mismatch')
    validate_graph(graph)
    return graph


def _matches_intent(job, tenant, project, intent):
    return (str(job.get('tenant_id')) == tenant and str(job.get('org_id')) == tenant
            and str(job.get('project_id')) == project and job.get('idempotency_key') == intent
            and job.get('tool') == corpus.OPERATION)


def inspect_jobs(tenant, run_id, *, authorize):
    """Read status and pinned input lineage for each corpus block on this exact run."""
    source = store.get(tenant, run_id)
    if source['tenant_id'] != tenant or source['run_id'] != run_id:
        raise GraphRefused('lab_recovery_source_mismatch')
    authority = store.corpus_authority(source)
    if authority is None:
        return {'corpus': False}
    authorize(source)
    graph = _pinned_graph(tenant, source)
    blocks = [block for block in graph['blocks'] if block['registry']['id'] == 'corpus-evaluator']
    project = authority['project_id']
    if not blocks or any(block['settings']['project_id'] != project for block in blocks):
        raise GraphRefused('lab_recovery_project_mismatch')
    statuses = {'submitted': 'queued', 'queued': 'queued', 'running': 'running',
                'failed': 'failed', 'complete': 'completed'}
    intents = {f'lab:{run_id}:{block["id"]}:corpus:v1': block for block in blocks}
    # One snapshot of just the exact intents. The project idempotency index
    # makes each intent unique; treat a duplicate as unavailable if encountered.
    with platform_link.platform_db().cursor() as cur:
        cur.execute('SELECT job_id, tenant_id, org_id, project_id, tool, idempotency_key, status, attempt '
                    'FROM async_jobs WHERE tenant_id=%s AND org_id=%s AND project_id=%s '
                    'AND idempotency_key = ANY(%s)', (tenant, tenant, project, list(intents)))
        jobs_by_intent = {}
        for job in cur.fetchall():
            intent = job['idempotency_key']
            jobs_by_intent[intent] = job if intent not in jobs_by_intent else None
    items = []
    for intent, block in intents.items():
        settings = block['settings']
        job = jobs_by_intent.get(intent)
        status, job_id, attempt = 'unavailable', None, None
        if intent not in jobs_by_intent and source['state'] in {'queued', 'running'}:
            status = 'not submitted'
        if job is not None and _matches_intent(job, tenant, project, intent):
            job_status, job_attempt = statuses.get(job.get('status')), job.get('attempt')
            if job_status is not None and type(job_attempt) is int and job_attempt >= 0:
                status, job_id, attempt = job_status, job['job_id'], job_attempt
        items.append({'block_id': block['id'], 'status': status, 'job_id': job_id, 'attempt': attempt,
                      'inputs': {'corpus_sha256': settings['corpus']['sha256'],
                                 'suite_sha256': settings['suite']['sha256'],
                                 'settings_sha256': graph_digest(settings)}})
    authorize(source)
    return {'corpus': True, 'source': {key: source[key] for key in
            ('run_id', 'graph_revision', 'graph_sha256', 'input_digest')}, 'blocks': items}


def prepare(tenant, run_id, *, authorize, include_cleanup=False):
    """Resolve immutable source and authoritative jobs, then recheck live access."""
    plan, _, _, _, _ = _prepare(tenant, run_id, authorize=authorize, include_cleanup=include_cleanup)
    return plan


def confirmation_lineage(tenant, run_id, reviewed_digest, *, authorize_write, include_cleanup=False):
    """Recheck the reviewed plan and return the server-owned inputs for admission."""
    if not _is_digest(reviewed_digest):
        raise GraphRefused('lab_recovery_confirmation_digest_invalid')
    plan, source, authority, graph, bound_jobs = _prepare(
        tenant, run_id, authorize=authorize_write, include_cleanup=include_cleanup)
    if plan['digest'] != reviewed_digest:
        raise GraphRefused('lab_recovery_plan_changed')
    if plan['draft_graph'] is None:
        raise GraphRefused('lab_recovery_plan_ineligible')

    selected, retained = [], []
    for block, item in zip(graph['blocks'], plan['blocks']):
        binding = _producer_binding(authority, block, bound_jobs[block['id']])
        producer = {**item, **binding}
        (selected if item['disposition'] == 'selected' else retained).append(producer)
    authorize_write(source)
    return {'source': {**plan['source'], 'input_digest': source['input_digest']},
            'project_id': authority['project_id'], 'reviewed_digest': reviewed_digest,
            'draft_graph': plan['draft_graph'], 'selected': selected, 'retained': retained}


def _is_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _producer_binding(authority, block, job):
    settings = block['settings']
    attempt = job.get('attempt')
    try:
        context = corpus.validate_context(job.get('lab_corpus'))
        policy = corpus.compile_policy(authority, block['id'], settings)
    except ValueError:
        raise GraphRefused('lab_recovery_producer_binding_invalid') from None
    if (type(attempt) is not int or attempt < 0 or context.get('policy') != policy
            or context['actor_id'] != authority['actor_id']
            or any(context[key] != settings[key] for key in ('project_id', 'corpus', 'suite'))):
        raise GraphRefused('lab_recovery_producer_binding_invalid')
    binding = {'attempt': attempt, 'source_policy': policy,
               'corpus_sha256': settings['corpus']['sha256'],
               'settings_sha256': graph_digest(settings)}
    if job['status'] == 'complete':
        envelope = job.get('result')
        result = envelope.get('result') if isinstance(envelope, dict) else None
        if (not isinstance(result, dict) or result.get('schema') != corpus.RESULT_SCHEMA
                or result.get('job_id') != job['job_id'] or result.get('context') != context
                or type(result.get('attempt')) is not int or result['attempt'] != attempt
                or not _is_digest(result.get('result_sha256'))
                or not _is_digest(result.get('evaluated_suite_sha256'))):
            raise GraphRefused('lab_recovery_producer_result_invalid')
        binding.update(result_sha256=result['result_sha256'],
                       evaluated_suite_sha256=result['evaluated_suite_sha256'])
    return binding


def _prepare(tenant, run_id, *, authorize, include_cleanup):
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
    graph = _pinned_graph(tenant, source)
    if (not graph['blocks'] or graph['pipes']
            or any(block['registry']['id'] != 'corpus-evaluator' for block in graph['blocks'])):
        raise GraphRefused('lab_recovery_independent_corpus_required')
    manifest, _ = lab_tool_manifests.corpus_capability()
    admit_graph(graph, {'corpus-evaluator@1.0.0': manifest})
    if any(block['settings']['project_id'] != authority['project_id'] for block in graph['blocks']):
        raise GraphRefused('lab_recovery_project_mismatch')

    blocks, selected, retained, unresolved = [], [], [], []
    bound_jobs = {}
    for block in graph['blocks']:
        intent = f'lab:{run_id}:{block["id"]}:corpus:v1'
        receipt = copy.deepcopy(lookup_job(tenant, authority['project_id'], intent))
        job = receipt.get('job')
        item = {'block_id': block['id'], 'idempotency_key': intent,
                'job_id': None, 'status': None, 'receipt_sha256': None}
        if job is None:
            disposition, reason = 'unresolved', receipt['reason_category']
        elif not _matches_intent(job, tenant, authority['project_id'], intent):
            # Never project any identifier, status or receipt from foreign jobs.
            disposition, reason = 'unresolved', 'job_binding_mismatch'
        else:
            disposition, reason = _disposition(job, receipt, source)
            bound_jobs[block['id']] = job
            item.update(job_id=job['job_id'], status=job['status'],
                receipt_sha256=graph_digest({
                    'job': {key: job.get(key) for key in (
                        'job_id', 'tenant_id', 'org_id', 'project_id', 'tool', 'idempotency_key',
                        'status', 'progress', 'attempt', 'params', 'result', 'error', 'provenance',
                        'lab_corpus', 'authority_mode')},
                    'cancel': receipt.get('cancel'), 'cleanup': receipt.get('cleanup')}))
        item.update(disposition=disposition, reason_category=reason)
        if include_cleanup:
            bound_job = job if item['job_id'] is not None else None
            stable = False
            if bound_job is not None:
                readback = lookup_job(tenant, authority['project_id'], intent)
                stable = (readback == receipt
                    and type(readback['job'].get('attempt')) is type(job.get('attempt'))
                    and graph_digest({key: readback.get(key) for key in ('cancel', 'cleanup')})
                        == graph_digest({key: receipt.get(key) for key in ('cancel', 'cleanup')}))
            item['cleanup_evidence'] = _cleanup_evidence(
                bound_job, receipt, run_id, block['id'], stable=stable)
        blocks.append(item)
        {'selected': selected, 'retained': retained, 'unresolved': unresolved}[disposition].append(block['id'])

    draft = None
    if selected and not unresolved:
        draft = copy.deepcopy(dict(graph,
            blocks=[block for block in graph['blocks'] if block['id'] in selected],
            outputs={name: endpoint for name, endpoint in graph['outputs'].items()
                     if endpoint['node'] in selected}))
    plan = {'schema': SCHEMA_V2 if include_cleanup else SCHEMA, 'source': {key: source[key] for key in (
                'run_id', 'doc_id', 'graph_revision', 'graph_sha256', 'state')},
            'blocks': blocks, 'selected_failed_block_ids': selected,
            'retained_completed_block_ids': retained, 'unresolved_block_ids': unresolved,
            'draft_graph': draft, 'execution_allowed': False,
            'confirmation_required': True}
    plan['digest'] = graph_digest(plan)
    authorize(source)
    return plan, source, authority, graph, bound_jobs
