"""Acquire one published CSV transform for a Malleable campaign release.

Campaign decisions reference the existing customization and async-job records.
No generated source, credentials, or caller-selected execution enters this API.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import uuid

import agent_policy
import campaign_capability_api as admission
import campaign_delivery_service as delivery
import campaign_web_tool_static as recipe
import customization_service as customization
import deps
import entitlements
import tool_loader
from customization_models import ChangeSetNotFoundError, ChangeState
from tool_validate import validate_params

TOOL_NAME = 'campaign-records-to-csv'
AUTHOR_DESCRIPTION = '''Create exactly one tenant Python tool named campaign-records-to-csv.
Use kind script, local_only true, capabilities ["drawing.read"], and a safe
tenant-relative Python entry. Define run(intake, params), ignoring intake.
The params JSON Schema is an object with only source_json (a string, minLength
1, maxLength 1048576), required ["source_json"], additionalProperties false.
Parse source_json as UTF-8 JSON: a nonempty array of at most 1000 nonempty flat
objects. Values are null, booleans, finite numbers, or strings of at most 2000
characters. Names are nonempty strings of at most 2000 characters. Reject
nested values and nonfinite numbers. Header union follows first occurrence
across records, at most 100 names. Missing or null cells are empty. Booleans
are lowercase true/false. Integers use str; integral floats use str(int(value));
other floats use repr. Prefix an apostrophe to TEXT cells and headers starting
with =, +, -, @, tab, or carriage return. Do not neutralize numeric negatives.
Quote EVERY header and cell with double quotes and double embedded quotes.
Join cells with comma, rows with CRLF, with a final CRLF. Output at most
1048576 UTF-8 bytes. Return exactly {"csv": csv_text}, a plain dictionary.
Use only Python standard-library json, math, and pure local computation.
No filesystem, network, subprocess, drawing changes, credentials, grants,
imports of app internals, or external effects. Reject invalid input explicitly.
'''


class AcquisitionError(ValueError):
    def __init__(self, state, reason, action):
        self.state, self.reason, self.action = state, reason, action
        super().__init__(reason)


def _refuse(reason='Published transform does not match its verified contract'):
    raise AcquisitionError('failed', reason, 'Review the existing transform and request one bounded correction')


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _key(version, phase):
    return f'acquisition-v{version}-{phase}'


def _payload(decisions, version, phase):
    rows = [d['payload'] for d in decisions if d.get('decision_key') == _key(version, phase)]
    if len(rows) > 1 or (rows and not isinstance(rows[0], dict)):
        _refuse('Acquisition references conflict')
    return rows[0] if rows else None


def _record(runtime, tenant, project, campaign, release, phase, payload):
    org, current_project, actor = runtime.authority(tenant, project)
    return runtime._store().record_decision(
        org, current_project, campaign, release['release_id'],
        decision_key=_key(release['contract_version'], phase),
        kind='capability_selection', payload=payload, decided_by=str(actor))


def _body(value):
    if hasattr(value, 'status_code'):
        try:
            body = json.loads(value.body)
        except (ValueError, TypeError):
            raise AcquisitionError('working', 'The authoring response is uncertain',
                                   'Retry this release to read the same authoring request') from None
        if value.status_code >= 400:
            if value.status_code in (401, 403, 409, 429):
                raise AcquisitionError('awaiting_user', 'Authoring or account authority needs attention',
                                       'Continue authoring from the project conversation or resolve its existing account action')
            if value.status_code < 500:
                raise AcquisitionError('failed', 'The authoring request was rejected',
                                       'Review the existing authoring request before one correction')
            raise AcquisitionError('working', 'The authoring service is unavailable',
                                   'Retry the same release after the authoring service recovers')
        value = body
    if not isinstance(value, dict):
        raise AcquisitionError('working', 'The authoring response is unavailable',
                               'Retry this release to read its existing authoring request')
    return value


def _pure_tool(tool, params):
    schema = tool.get('params')
    if (tool.get('name') != TOOL_NAME or tool.get('kind') != 'script'
            or tool.get('local_only') is not True or tool.get('aps_live') is True
            or tool.get('capabilities') != ['drawing.read']
            or any(tool.get(k) for k in ('grants', 'required_grants', 'network', 'network_access', 'permissions'))
            or not isinstance(schema, dict) or schema.get('type') != 'object'
            or schema.get('additionalProperties') is not False
            or schema.get('required') != ['source_json']
            or not isinstance(schema.get('properties'), dict)
            or set(schema['properties']) != {'source_json'}):
        _refuse()
    field = schema['properties']['source_json']
    if (not isinstance(field, dict) or field.get('type') != 'string'
            or validate_params(tool, params)
            or not validate_params(tool, {})
            or not validate_params(tool, dict(params, other=True))):
        _refuse()


def _publication(tenant_id, params):
    """Read the cumulative published registry, not the latest change's new tool."""
    winners = [(tool, source) for tool, source in deps.effective_tools_with_provenance(tenant_id)
               if tool.get('name') == TOOL_NAME]
    if not winners:
        return None
    if len(winners) != 1 or winners[0][1] != deps.TOOL_SOURCE_TENANT_REPO:
        _refuse()
    tool = winners[0][0]
    _pure_tool(tool, params)
    service = customization.CustomizationService.configured()
    try:
        pin = service.store.get_effective_catalog(tenant_id=tenant_id)
        change = service.store.get_change_set(tenant_id=tenant_id, change_set_id=pin.change_set_id)
    except (ChangeSetNotFoundError, AttributeError):
        _refuse()
    if (pin.tenant_id != tenant_id or change.tenant_id != tenant_id
            or change.change_set_id != pin.change_set_id or change.state != ChangeState.PUBLISHED
            or change.staged_commit != pin.catalog_commit or change.catalog_digest != pin.catalog_digest):
        _refuse()
    raw = customization._git_blob(customization._bare_repo(tenant_id), f'{pin.catalog_commit}:registry.json')
    if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024 or _hash(raw) != pin.catalog_digest:
        _refuse()
    registry = json.loads(raw)
    rows = [item for item in registry.get('tools', []) if isinstance(item, dict) and item.get('name') == TOOL_NAME]
    manifest = deps.catalog_tool_digest(tool)
    if len(rows) != 1 or deps.catalog_tool_digest(rows[0]) != manifest:
        _refuse()
    source_hash = tool_loader.published_tool_source_sha256(tool, tenant_id)
    if (not isinstance(source_hash, str) or not re.fullmatch('[0-9a-f]{64}', source_hash)
            or customization.effective_catalog_pin(tenant_id) != {
                'catalog_commit': pin.catalog_commit, 'effective_catalog_digest': pin.catalog_digest}):
        _refuse()
    return ({'change_set_id': pin.change_set_id, 'catalog_commit': pin.catalog_commit,
             'effective_catalog_digest': pin.catalog_digest, 'tool_name': TOOL_NAME,
             'tool_manifest_sha256': manifest, 'tool_source_sha256': source_hash}, tool)


def _run_authority(tenant, tool):
    import broker
    if not broker._authored_execution_enabled():
        raise AcquisitionError('awaiting_user', 'Authored tool execution is disabled',
                               'Enable the existing authored execution policy for this workspace')
    if broker.tenant_disabled(str(tenant)):
        raise AcquisitionError('awaiting_user', 'Workspace execution is disabled',
                               'Resolve the existing workspace account action')
    if broker._production_runtime() and not broker._sandbox_configured():
        raise AcquisitionError('awaiting_user', 'The production tool sandbox is unavailable',
                               'Restore the existing authorized sandbox provider connection')
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    required = entitlements.tool_required_capability(tool)
    if required != 'run_read' or not entitlements.entitlements_for(tier, roles, elevated).get(required, False):
        raise AcquisitionError('awaiting_user', 'The workspace cannot run this transform',
                               'Restore the existing run entitlement for this workspace')
    state = agent_policy.load_tenant_state(str(tenant))
    action = agent_policy.effective_action(agent_policy.load_policy(), 'run_read_tool',
                                          tier=tier, tenant_overlay=state['overlay'])
    if state['agent_disabled'] or action is None or not action.enabled:
        raise AcquisitionError('awaiting_user', 'Workspace policy has disabled transform execution',
                               'Resolve the existing workspace execution policy')
    if action.policy != 'auto':
        raise AcquisitionError('awaiting_user', 'Workspace policy requires approval for this transform',
                               'Approve the transform through the existing project conversation')


@contextmanager
def _capacity(org, project, campaign, release_id, version):
    """Share the release workspace lock with campaign claimers through submit."""
    _, _, _, db = admission._platform()
    from leaf_platform import campaign_release
    with db.cursor() as cur:
        campaign_release._org_lock(cur, {'org': org})
        cur.execute('SELECT release_id,contract_version,status FROM campaign_releases '
                    'WHERE org_id=%s AND project_id=%s AND campaign_id=%s AND release_id=%s',
                    (org, project, campaign, release_id))
        row = cur.fetchone()
        if (not row or row['status'] != 'active' or row['contract_version'] != version):
            yield False
            return
        cur.execute("SELECT count(*) AS n FROM campaign_releases WHERE org_id=%s AND status='active'", (org,))
        if cur.fetchone()['n'] != 1:
            yield False
            return
        cur.execute("SELECT (SELECT count(*) FROM campaign_task_attempts WHERE org_id=%s AND status='active') "
                    "+ (SELECT count(*) FROM async_jobs WHERE org_id=%s AND status IN ('submitted','running')) AS n",
                    (org, str(org)))
        yield cur.fetchone()['n'] < 3


def _read_job(row, context, params, key):
    import campaign_transform_job as transform
    import jobs
    if (not isinstance(row, dict) or any(str(row.get(k)) != context[k] for k in ('tenant_id', 'org_id', 'project_id'))
            or row.get('tool') != TOOL_NAME or not isinstance(row.get('execution_json'), dict)
            or row['execution_json'].get('completion_provenance') != context):
        _refuse('An existing invocation belongs to different work')
    transform.validate_context(context)
    job = jobs.get_job(str(row['job_id']))
    if (not isinstance(job, dict) or str(job.get('job_id')) != str(row['job_id'])
            or any(str(job.get(k)) != context[k] for k in ('tenant_id', 'org_id', 'project_id'))
            or job.get('tool') != TOOL_NAME or job.get('completion_provenance') != context
            or job.get('params') != params or job.get('idempotency_key') != key):
        _refuse('Invocation readback does not match the frozen input')
    return job


def _invoke(runtime, tenant, org, project, campaign, release, params, context, tool):
    import jobs
    if jobs.job_store_mode() != 'postgres':
        raise AcquisitionError('awaiting_user', 'Durable transform execution is unavailable',
                               'Connect the existing PostgreSQL job service')
    key = 'completion-transform:' + _hash(json.dumps(
        [context['release_id'], context['contract_version'], context['input_sha256']],
        separators=(',', ':')).encode())
    with admission._admission_lock(context['tenant_id'], str(org), str(project), key):
        prior = admission._lookup(context['tenant_id'], str(project), key)
        if prior is None:
            _run_authority(tenant, tool)
            with _capacity(org, project, campaign, release['release_id'], release['contract_version']) as available:
                if not available:
                    return {'state': 'working', 'reason': 'Workspace execution capacity is occupied',
                            'recommended_action': 'Wait for existing work to settle'}
                runtime.authority(tenant, project)
                try:
                    job_id = jobs.submit_job(
                        tenant_id=context['tenant_id'], tool=tool, params=params, dwg='', aps_live=False,
                        org_id=str(org), project_id=str(project), idempotency_key=key,
                        authority_mode='legacy_sqlite', completion_provenance=context)
                except Exception:
                    prior = admission._lookup(context['tenant_id'], str(project), key)
                    if prior is None:
                        raise AcquisitionError('working', 'Transform submission is uncertain',
                                               'Retry this release to read the same invocation') from None
                else:
                    prior = admission._lookup(context['tenant_id'], str(project), key)
                    if prior is None or str(prior['job_id']) != str(job_id):
                        raise AcquisitionError('working', 'Transform submission awaits durable readback',
                                               'Retry this release to read the same invocation')
        job = _read_job(prior, context, params, key)
    _record(runtime, tenant, project, campaign, release, 'invocation',
            {'job_id': job['job_id'], 'operation_key': key, 'context': context})
    if job['status'] in ('submitted', 'running'):
        return {'state': 'working', 'job_id': job['job_id'], 'reason': 'The published transform is running',
                'recommended_action': 'Wait for its existing job to finish'}
    if job['status'] != 'complete':
        raise AcquisitionError('failed', 'The published transform job failed',
                               'Inspect the existing job before one bounded correction')
    envelope = job.get('result')
    result = envelope.get('result') if isinstance(envelope, dict) else None
    if (not isinstance(envelope, dict) or envelope.get('ok') is not True
            or envelope.get('tool') != TOOL_NAME or not isinstance(result, dict)
            or set(result) != {'csv'} or not isinstance(result['csv'], str)):
        _refuse('The transform returned an invalid output')
    actual = result['csv'].encode('utf-8')
    if len(actual) > recipe.MAX_OUTPUT_BYTES or actual != recipe.expected_output(params['source_json'].encode('utf-8')):
        _refuse('The transform CSV does not match the promised result')
    metadata = delivery.validate_bytes('records.csv', actual)
    runtime.authority(tenant, project)
    _run_authority(tenant, tool)
    publication = _publication(context['tenant_id'], params)
    if publication is None or any(publication[0][k] != context[k] for k in publication[0]):
        _refuse('Transform publication changed before output acceptance')
    return {'state': 'complete', 'output_bytes': actual, 'metadata': metadata,
            'job_id': job['job_id'], 'publication': {k: context[k] for k in (
                'change_set_id', 'catalog_commit', 'effective_catalog_digest',
                'tool_name', 'tool_manifest_sha256', 'tool_source_sha256')}}


def advance(runtime, tenant, project_id, campaign_id, release, source_bytes, *,
            authority_session_id=None, authority_turn_id=None):
    """Advance one bounded acquisition, returning actual bytes only on completion."""
    org, project, actor = runtime.authority(tenant, project_id)
    retained = {}
    try:
        version = release['contract_version']
        if type(version) is not int or version < 1:
            _refuse('Release contract version is invalid')
        frozen = release['contract']['transform_recipe']
        if frozen.get('recipe_id') != recipe.RECIPE_ID or frozen.get('recipe_version') != recipe.RECIPE_VERSION:
            _refuse('The release requires a different transform')
        expected = recipe.expected_output(source_bytes)
        source_hash = _hash(source_bytes)
        if frozen['source_artifact']['sha256'] != source_hash:
            _refuse('The release input changed')
        expected_artifact = release['contract']['selected_artifact']
        if expected_artifact['sha256'] != _hash(expected) or expected_artifact['size_bytes'] != len(expected):
            _refuse('The release output contract changed')
        snapshot = runtime._store().get_release(org, project, campaign_id, release['release_id'])
        current = snapshot['release']
        if (not current or current['contract_version'] != version or current['contract'] != release['contract']):
            _refuse('The current release differs from the acquisition')
        if current['status'] != 'active':
            return {'state': 'working', 'reason': 'The release is not active',
                    'recommended_action': 'Resume the release when its workspace slot is available'}
        decisions = snapshot.get('decisions', [])
        change_ref = _payload(decisions, version, 'changeset')
        if change_ref:
            retained['change_set_id'] = change_ref['change_set_id']
        if not isinstance(tenant, deps.TenantContext) or not tenant.subject:
            raise AcquisitionError('awaiting_user', 'Acquisition requires the current account actor',
                                   'Continue this release from its authenticated project conversation')
        params = {'source_json': source_bytes.decode('utf-8')}
        _record(runtime, tenant, project, campaign_id, release, 'intent',
                {'tool_name': TOOL_NAME, 'input_sha256': source_hash, 'recipe_id': recipe.RECIPE_ID,
                 'recipe_version': recipe.RECIPE_VERSION})
        publication = _publication(str(tenant), params)
        if publication is None:
            if _payload(decisions, version, 'publication'):
                _refuse('The previously published transform is no longer available')
            from routers import author
            if change_ref is None:
                if not authority_session_id or not authority_turn_id:
                    raise AcquisitionError('awaiting_user', 'Authoring requires an active project conversation',
                                           'Continue this release from the project conversation to author the missing CSV tool')
                runtime.authority(tenant, project)
                response = _body(author.stage(
                    author.StageRequest(description=AUTHOR_DESCRIPTION, mode='build',
                        idempotency_key='completion-author:' + _hash(f"{release['release_id']}:{version}:{source_hash}".encode())),
                    tenant=tenant, authority_session_id=authority_session_id, authority_turn_id=authority_turn_id))
                change_id = response.get('change_set_id')
                if not isinstance(change_id, str) or str(uuid.UUID(change_id)) != change_id:
                    raise AcquisitionError('working', 'Authoring admission awaits a usable reference',
                                           'Retry this release with the same active conversation')
                change_ref = {'change_set_id': change_id}
                retained.update(change_ref)
                _record(runtime, tenant, project, campaign_id, release, 'changeset', change_ref)
            else:
                response = _body(author.stage_status(change_ref['change_set_id'], tenant=tenant))
            if response.get('status') in ('queued', 'running'):
                return dict(retained, state='working', reason='The missing transform is being authored',
                            recommended_action='Wait for the existing authoring request')
            if response.get('status') != 'staged':
                raise AcquisitionError('failed', 'The existing authoring request failed',
                                       'Review that authoring request before one bounded correction')
            runtime.authority(tenant, project)
            published = _body(author.request_publication(
                author.PublicationRequest(change_set_id=change_ref['change_set_id']), tenant=tenant))
            if published.get('status') in ('awaiting_approval', 'denied'):
                raise AcquisitionError('awaiting_user', 'Transform publication ' + published['status'].replace('_', ' '),
                                       'Resolve the existing tool publication request in the decision inbox')
            if published.get('status') != 'published':
                return dict(retained, state='working', reason='Transform publication is in progress',
                            recommended_action='Wait for the existing publication request')
            publication = _publication(str(tenant), params)
            if publication is None:
                _refuse('Published transform is absent from the effective catalog')
        published, tool = publication
        prior_publication = _payload(decisions, version, 'publication')
        if prior_publication is not None and prior_publication != published:
            _refuse('The selected transform publication changed')
        _record(runtime, tenant, project, campaign_id, release, 'publication', published)
        import campaign_transform_job as transform
        context = transform.validate_context({
            'schema': 'leaf.campaign-transform.v1', 'capability': 'campaign.records-to-csv',
            'recipe_id': recipe.RECIPE_ID, 'recipe_version': recipe.RECIPE_VERSION,
            'tenant_id': str(tenant), 'org_id': str(org), 'project_id': str(project),
            'campaign_id': str(campaign_id), 'release_id': str(release['release_id']),
            'contract_version': version, 'binding_id': str(actor), 'input_sha256': source_hash, **published})
        return _invoke(runtime, tenant, org, project, campaign_id, release, params, context, tool)
    except AcquisitionError as exc:
        return dict(retained, state=exc.state, reason=exc.reason, recommended_action=exc.action)
    except (agent_policy.PolicyError, entitlements.EntitlementsError):
        return dict(retained, state='awaiting_user', reason='Workspace execution policy is unavailable',
                    recommended_action='Restore the existing workspace policy before continuing')
    except customization.CustomizationServiceError:
        return dict(retained, state='working', reason='Published capability authority is unavailable',
                    recommended_action='Retry this release after the existing catalog service recovers')
    except (ValueError, KeyError, TypeError, UnicodeError):
        return dict(retained, state='failed', reason='Acquisition evidence or input is invalid',
                    recommended_action='Review this release and its existing capability evidence')
