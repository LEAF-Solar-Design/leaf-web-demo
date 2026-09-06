"""Authenticated ReciPDF publication binding and durable invocation projection."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import re
import uuid

import deps
import platform_link


class CapabilityError(Exception):
    def __init__(self, status=503, code='capability_unavailable'):
        self.status, self.code = status, code
        super().__init__('Campaign capability request failed')


def _safe(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CapabilityError:
            raise
        except platform_link.ProjectSessionForbidden:
            raise CapabilityError(403, 'forbidden') from None
        except (KeyError, IndexError):
            raise CapabilityError() from None
        except LookupError:
            raise CapabilityError(404, 'project_unavailable') from None
        except Exception:
            raise CapabilityError() from None
    return call


def _platform():
    platform_link._ensure_platform_package()
    from leaf_platform import campaigns, campaign_capabilities, campaign_enrollment, db
    return campaigns, campaign_capabilities, campaign_enrollment, db


def _scope(tenant, project_id, campaign_id):
    binding = platform_link.resolve_caller_binding(tenant)
    org = platform_link.require_project_access(tenant, project_id, write=True, binding=binding)
    if binding is None or str(binding.platform_tenant_id) != str(org):
        raise CapabilityError(403, 'forbidden')
    campaign = _platform()[0].get_campaign(org, project_id, campaign_id)
    if campaign is None:
        raise CapabilityError(404, 'project_unavailable')
    return str(org), str(binding.binding_id), campaign['tenant_id']


def _publication(tenant_id):
    from customization_models import ChangeState, ChangeSetNotFoundError
    from customization_service import CustomizationService, effective_catalog_pin
    from tool_loader import published_tool_source_sha256
    service = CustomizationService.configured()
    try:
        pin = service.store.get_effective_catalog(tenant_id=tenant_id)
    except ChangeSetNotFoundError:
        return None
    if pin is None:
        return None
    change = service.store.get_change_set(tenant_id=tenant_id, change_set_id=pin.change_set_id)
    if (pin.tenant_id != tenant_id or change.tenant_id != tenant_id
            or change.change_set_id != pin.change_set_id or change.state != ChangeState.PUBLISHED
            or change.staged_commit != pin.catalog_commit or change.catalog_digest != pin.catalog_digest):
        raise CapabilityError()
    candidate = service._staged_tool(change)
    if candidate.get('name') != 'campaign-host-enrollment':
        return None
    manifest = deps.catalog_tool_digest(candidate)
    winners = [(tool, source) for tool, source in deps.effective_tools_with_provenance(tenant_id)
               if tool.get('name') == 'campaign-host-enrollment']
    if (len(winners) != 1 or winners[0][1] != deps.TOOL_SOURCE_TENANT_REPO
            or deps.catalog_tool_digest(winners[0][0]) != manifest):
        raise CapabilityError()
    tool = winners[0][0]
    source_hash = published_tool_source_sha256(tool, tenant_id)
    publication = dict(change_set_id=pin.change_set_id, catalog_commit=pin.catalog_commit,
                       effective_catalog_digest=pin.catalog_digest, tool_name=tool['name'],
                       tool_manifest_sha256=manifest, tool_source_sha256=source_hash)
    _platform()[1]._publication(publication)
    if effective_catalog_pin(tenant_id) != {
            'catalog_commit': pin.catalog_commit, 'effective_catalog_digest': pin.catalog_digest}:
        raise CapabilityError()
    return publication, tool


@_safe
def list_capabilities(tenant, project_id, campaign_id):
    _, _, tenant_id = _scope(tenant, project_id, campaign_id)
    current = _publication(tenant_id)
    if current is None:
        return []
    publication, _ = current
    return [{**{key: publication[key] for key in ('change_set_id', 'tool_name', 'catalog_commit',
                                                 'effective_catalog_digest')},
             'label': 'Connect this build host'}]


@_safe
def bind_publication(tenant, project_id, campaign_id, enrollment_id, change_set_id):
    org, principal, tenant_id = _scope(tenant, project_id, campaign_id)
    current = _publication(tenant_id)
    if current is None or current[0]['change_set_id'] != change_set_id:
        raise CapabilityError(409, 'publication_conflict')
    capabilities = _platform()[1]
    try:
        return capabilities.bind_publication(org, project_id, campaign_id, enrollment_id, principal,
                                             publication=current[0])
    except capabilities.CampaignConflict:
        raise CapabilityError(409, 'publication_conflict') from None


def _stored_context(org, project, campaign, enrollment_id, tenant_id):
    _, capabilities, enrollment, _ = _platform()
    rows = enrollment.list_enrollments(org, project, campaign)
    row = next((row for row in rows if row['enrollment_id'] == enrollment_id), None)
    if row is None:
        raise CapabilityError(404, 'project_unavailable')
    link = row['capability_link']
    context = dict(capabilities.CONSTANTS, tenant_id=tenant_id, org_id=org, project_id=project,
                   campaign_id=campaign, enrollment_id=enrollment_id, link_id=link['link_id'])
    context.update({key: link.get(key) for key in capabilities.PUBLICATION})
    return context


def _key(campaign, enrollment, client_key):
    if (not isinstance(client_key, str) or not 1 <= len(client_key) <= 128
            or not client_key.isprintable()):
        raise CapabilityError(400, 'invalid_request')
    value = json.dumps([campaign, enrollment, client_key], separators=(',', ':'), ensure_ascii=True)
    return 'campaign-capability:' + hashlib.sha256(value.encode()).hexdigest()


def _lock_id(tenant, org, project, key):
    value = json.dumps(['campaign-capability-admission-v1', tenant, org, project, key],
                       separators=(',', ':'), ensure_ascii=True)
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], 'big', signed=True)


@contextmanager
def _admission_lock(tenant, org, project, key):
    pool = _platform()[3].get_pool()
    conn = None
    acquired = False
    lock_id = _lock_id(tenant, org, project, key)
    try:
        conn = pool.getconn(timeout=5)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '5s'")
            cur.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (lock_id,))
            acquired = cur.fetchone()['acquired']
        conn.commit()
        if not acquired:
            raise CapabilityError(409, 'invocation_pending')
        yield
    except CapabilityError:
        raise
    except Exception:
        raise CapabilityError(503, 'invocation_unknown') from None
    finally:
        if conn is not None:
            try:
                conn.rollback()
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute('SELECT pg_advisory_unlock(%s) AS released', (lock_id,))
                        if not cur.fetchone()['released']:
                            raise RuntimeError('lock cleanup failed')
                conn.execute('RESET statement_timeout')
                conn.commit()
            except Exception:
                conn.close()
                raise CapabilityError(503, 'invocation_unknown') from None
            finally:
                pool.putconn(conn)


def _lookup(tenant, project, key):
    with _platform()[3].cursor() as cur:
        cur.execute('SELECT job_id,tenant_id,org_id,project_id,tool,execution_json FROM async_jobs '
                    'WHERE tenant_id=%s AND project_id=%s AND idempotency_key=%s', (tenant, project, key))
        return cur.fetchone()


def _matching(row, context):
    return (isinstance(row, dict)
            and all(row.get(key) == context[key] for key in ('tenant_id', 'org_id', 'project_id'))
            and row.get('tool') == context['tool_name']
            and isinstance(row.get('execution_json'), dict)
            and row['execution_json'].get('capability_provenance') == context)


def _job_projection(job_id, context):
    import jobs
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError('Invalid job identity')
        _platform()[1]._context(context)
        row = jobs.get_job(job_id)
    except Exception:
        raise CapabilityError(503, 'invocation_unknown') from None
    if (row is None or row.get('job_id') != job_id
            or any(row.get(k) != context[k] for k in ('tenant_id', 'org_id', 'project_id'))
            or row.get('tool') != context['tool_name'] or row.get('capability_provenance') != context):
        raise CapabilityError(503, 'invocation_unknown')
    status = row.get('status')
    if status not in ('submitted', 'running', 'complete', 'failed'):
        raise CapabilityError(503, 'invocation_unknown')
    return dict(job_id=job_id, status=status, progress={
        'submitted': 'Queued', 'running': 'Working', 'complete': 'Complete', 'failed': 'Failed'}[status])


def _recover(row, context, expected_digest):
    if not _matching(row, context) or context['effective_catalog_digest'] != expected_digest:
        raise CapabilityError(409, 'idempotency_conflict')
    _platform()[1]._context(context)
    return _job_projection(str(row['job_id']), context)


@_safe
def invoke(tenant, project_id, campaign_id, enrollment_id, expected_digest, idempotency_key):
    key = _key(campaign_id, enrollment_id, idempotency_key)
    if not isinstance(expected_digest, str) or re.fullmatch('[0-9a-f]{64}', expected_digest) is None:
        raise CapabilityError(400, 'invalid_request')
    org, principal, tenant_id = _scope(tenant, project_id, campaign_id)
    with _admission_lock(tenant_id, org, project_id, key):
        prior = _lookup(tenant_id, project_id, key)
        context = _stored_context(org, project_id, campaign_id, enrollment_id, tenant_id)
        if prior is not None:
            return _recover(prior, context, expected_digest)
        capabilities = _platform()[1]
        context = capabilities.invocation_context(org, project_id, campaign_id, enrollment_id, principal)
        current = _publication(tenant_id)
        if (current is None or expected_digest != context['effective_catalog_digest']
                or current[0] != {key: context[key] for key in capabilities.PUBLICATION}):
            raise CapabilityError(409, 'catalog_drift')
        from tool_validate import validate_params
        if validate_params(current[1], {}):
            raise CapabilityError()
        import jobs
        try:
            job_id = jobs.submit_job(tenant_id=context['tenant_id'], tool=current[1], params={}, dwg='',
                                     aps_live=False, org_id=org, project_id=project_id,
                                     idempotency_key=key, authority_mode='legacy_sqlite',
                                     capability_provenance=context)
            durable = _lookup(tenant_id, project_id, key)
            if durable is None or durable['job_id'] != job_id:
                raise CapabilityError(503, 'invocation_unknown')
            return _recover(durable, context, expected_digest)
        except Exception:
            # Once submit was entered, no error can instruct the client to clear its key.
            try:
                durable = _lookup(tenant_id, project_id, key)
                if durable is not None:
                    return _recover(durable, context, expected_digest)
            except CapabilityError:
                raise
            except Exception:
                pass
            raise CapabilityError(503, 'invocation_unknown') from None


@_safe
def enrollment_snapshot(tenant, project_id, campaign_id):
    org, _, tenant_id = _scope(tenant, project_id, campaign_id)
    _, capabilities, enrollment, db = _platform()
    import build_receipts
    rows = enrollment.list_enrollments(org, project_id, campaign_id)
    result = []
    for original in rows:
        row = dict(original)
        eid = row['enrollment_id']
        context = _stored_context(org, project_id, campaign_id, eid, tenant_id)
        with db.cursor() as cur:
            cur.execute('SELECT job_id,tenant_id,org_id,project_id,tool,execution_json FROM async_jobs '
                        'WHERE tenant_id=%s AND org_id=%s AND project_id=%s AND tool=%s '
                        "AND execution_json->'capability_provenance'->>'campaign_id'=%s "
                        "AND execution_json->'capability_provenance'->>'enrollment_id'=%s "
                        "AND execution_json->'capability_provenance'->>'link_id'=%s "
                        'ORDER BY created_at DESC,job_id DESC LIMIT 100',
                        (tenant_id, org, project_id, context['tool_name'], campaign_id, eid, context['link_id']))
            recent = cur.fetchall()
        invocations = []
        for durable in recent:
            if not _matching(durable, context):
                continue
            try:
                item = _job_projection(str(durable['job_id']), context)
            except CapabilityError:
                continue
            item.update(receipt_available=False, counted=False, reason=None)
            if item['status'] == 'complete':
                try:
                    receipt = build_receipts.read_terminal_receipt(item['job_id'])
                    if receipt is None:
                        item['reason'] = 'Receipt unavailable'
                    else:
                        item['receipt_available'] = True
                        row = capabilities.count_invocation(org, project_id, campaign_id, eid,
                                                            job_id=item['job_id'], receipt=receipt)
                        row = next(r for r in enrollment.list_enrollments(org, project_id, campaign_id)
                                   if r['enrollment_id'] == eid)
                except Exception:
                    item['reason'] = 'Receipt could not be counted'
            elif item['status'] == 'failed':
                item['reason'] = 'Invocation failed'
            invocations.append(item)
        counted = set(row['capability_link'].get('counted_job_ids', []))
        for item in invocations:
            item['counted'] = item['job_id'] in counted
        result.append(dict(row, invocations=invocations, completed_uses=min(2, len(counted))))
    return dict(enrollments=result, allowed_machines=enrollment.allowed_machines())
