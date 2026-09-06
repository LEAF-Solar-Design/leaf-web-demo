"""Tenant-authorized completion runtime for the Malleable campaign engine."""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import uuid
from urllib.parse import quote

import platform_link
import campaign_delivery_service as delivery
import campaign_capability_resolver as capabilities

STAGES = ('implementation', 'publication', 'deployment', 'user_verification', 'delivery')
PRODUCERS = dict(zip(STAGES, ('task_ledger', 'deployment_adapter', 'deployment_adapter',
                             'user_workflow', 'artifact_reader')))
_STORE = None
_LIFECYCLE = None


class _WorkerActor:
    """Internal enrollment handle, never constructed from tenant API fields."""
    def __init__(self, enrollment_id, subject):
        self.enrollment_id = enrollment_id
        self.subject = subject

    def resolve(self, project_id):
        from leaf_platform import campaign_enrollment, campaign_execution
        with campaign_execution._cursor() as cur:
            scope = campaign_enrollment.resolve_worker_scope(cur, self.enrollment_id, self.subject)
            if str(scope['project']) != str(project_id):
                raise LookupError('Project unavailable')
            cur.execute('SELECT e.enabled_by_binding_id AS principal FROM campaign_host_enrollments e '
                        'JOIN identity_bindings i ON i.binding_id=e.enabled_by_binding_id '
                        'AND i.platform_tenant_id=e.org_id AND i.status=\'active\' '
                        'WHERE e.enrollment_id=%s AND e.org_id=%s AND e.project_id=%s '
                        'AND e.campaign_id=%s AND e.state=\'enabled\'',
                        (self.enrollment_id, scope['org'], scope['project'], scope['campaign']))
            row = cur.fetchone()
            if not row or not row['principal']:
                raise platform_link.ProjectSessionForbidden('Enable enrollment with a current project actor')
            actor = row['principal']
        _lifecycle().require_project_role(scope['org'], scope['project'], actor, write=True)
        return scope['org'], scope['project'], actor


def set_store(store):
    global _STORE
    _STORE = store


def set_lifecycle(lifecycle):
    global _LIFECYCLE
    _LIFECYCLE = lifecycle


def _module(name):
    from routers import campaigns
    campaigns._store()
    return importlib.import_module('leaf_platform.' + name)


def _store():
    return _STORE if _STORE is not None else _module('campaign_release')


def _lifecycle():
    return _LIFECYCLE if _LIFECYCLE is not None else _module('project_lifecycle')


def authority(tenant, project_id):
    if isinstance(tenant, _WorkerActor):
        return tenant.resolve(project_id)
    project = uuid.UUID(str(project_id))
    org = platform_link.require_project_access(tenant, str(project), write=True)
    binding = platform_link.resolve_caller_binding(tenant)
    if binding is None:
        raise platform_link.ProjectSessionForbidden('Current principal binding is required')
    return uuid.UUID(str(org)), project, uuid.UUID(str(binding.binding_id))


def validate_finish(finish):
    if not isinstance(finish, dict) or set(finish) != {
            'delivery_profile', 'intended_user', 'workflow', 'artifact_refs'}:
        raise ValueError('Invalid finish fields')
    for key in ('intended_user', 'workflow'):
        if not isinstance(finish[key], str) or not 1 <= len(finish[key].strip()) <= 2000:
            raise ValueError('Invalid finish description')
    if not isinstance(finish['delivery_profile'], str) or not re.fullmatch('[a-z][a-z0-9_]{0,39}', finish['delivery_profile']):
        raise ValueError('Invalid delivery profile')
    refs = finish['artifact_refs']
    if not isinstance(refs, list) or len(refs) > 32:
        raise ValueError('Invalid artifact references')
    for path in refs:
        delivery.safe_path(path)
    return dict(finish)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def compile_finish(tenant, project_id, campaign_id, finish):
    finish = validate_finish(finish)
    org, project, actor = authority(tenant, project_id)
    from routers import campaigns
    campaign = campaigns._store().get_campaign(org, project, campaign_id)
    if campaign is None:
        raise LookupError('Campaign unavailable')
    artifact = None
    if finish['delivery_profile'] == 'cad_file':
        artifact = delivery.select_artifact(_lifecycle().project_snapshot(org, project, actor),
                                            finish['artifact_refs'])
    boundary = ('Deliver and verify the existing ' + artifact['format'] + ' artifact ' + artifact['path']
                if artifact else 'Produce and verify a ' + finish['delivery_profile'] + ' release')
    deferred = ['The original ambition beyond this existing artifact has not been implemented or validated.'] if artifact else []
    return {**finish, 'original_goal': campaign['prompt'], 'release_boundary': boundary,
            'deferred_items': deferred, 'selected_artifact': artifact,
            'request_digest': _digest(finish),
            'required_checks': [{'check_id': stage + '.verified', 'stage': stage,
                                 'description': description} for stage, description in zip(STAGES, (
                'Validate actual source file bytes and format',
                'Persist release copy with matching lifecycle receipt',
                'Retrieve the saved version through the authorized artifact reader',
                'Open the project and retrieve and validate the promised file',
                'Re-read recipient artifact bytes and provide replay and known limits'))]}


def snapshot(tenant, project_id, campaign_id, release_id=None):
    org, project, _ = authority(tenant, project_id)
    completion = (_store().release_snapshot(org, project, campaign_id) if release_id is None
                  else _store().get_release(org, project, campaign_id, release_id))
    release = completion.get('release')
    if release and release.get('status') == 'finished' and release['contract'].get('selected_artifact'):
        try:
            read_artifact(tenant, project_id, campaign_id, release['release_id'],
                          release['contract']['selected_artifact']['name'])
            completion = dict(completion, current_verification={'status': 'passed'})
        except (ValueError, UnicodeError):
            completion = dict(completion, deliverables=[], current_verification={
                'status': 'failed', 'reason': 'Saved artifact no longer matches the accepted version'})
    return completion


def list_releases(tenant, project_id, campaign_id):
    org, project, _ = authority(tenant, project_id)
    return _store().list_releases(org, project, campaign_id)


def create(tenant, project_id, campaign_id, finish, idempotency_key):
    finish = validate_finish(finish)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ValueError('Idempotency key required')
    org, project, actor = authority(tenant, project_id)
    # Reuse the frozen contract on a retry, even if the project has since changed.
    rows = _store().list_releases(org, project, campaign_id)
    request_key_digest = _digest(idempotency_key)
    existing = next((row for row in rows if row.get('idempotency_key') == idempotency_key
                     or row.get('contract', {}).get('request_key_digest') == request_key_digest), None)
    if existing:
        contract = existing['contract']
        if contract.get('request_digest') != _digest(finish):
            raise delivery.DeliveryConflict('Finish idempotency collision')
    else:
        contract = compile_finish(tenant, project_id, campaign_id, finish)
        contract['request_key_digest'] = request_key_digest
    row = _store().create_release(org, project, campaign_id, actor, contract=contract,
                                  delivery_profile=finish['delivery_profile'], idempotency_key=idempotency_key)
    rid = row['release_id']
    selection = capabilities.resolve(tenant, finish['delivery_profile'],
                                     existing_artifact=bool(contract.get('selected_artifact')))
    authority(tenant, project_id)
    _store().record_decision(org, project, campaign_id, rid, decision_key='capability-selection',
                             kind='capability_selection', payload=selection, decided_by=str(actor))
    if not selection['selected']:
        from routers import campaigns
        campaigns._store().ask_question(org, project, campaign_id, question_key='completion-capability-' + str(rid),
            prompt=selection['missing_capability'] + '. ' + selection['recommended_action'],
            options=[], blocks_dispatch=False)
    return advance(tenant, project_id, campaign_id, rid)


def transition(tenant, project_id, campaign_id, release_id, action):
    if action not in ('pause', 'resume', 'cancel'):
        raise ValueError('Unknown release action')
    org, project, actor = authority(tenant, project_id)
    _store().transition_release(org, project, campaign_id, release_id, actor, action=action)
    return snapshot(tenant, project_id, campaign_id, release_id)


def retry(tenant, project_id, campaign_id, release_id, stage):
    if stage not in STAGES:
        raise ValueError('Unknown stage')
    org, project, actor = authority(tenant, project_id)
    before = _store().get_release(org, project, campaign_id, release_id)
    _store().retry_stage(org, project, campaign_id, release_id, actor, stage=stage)
    failed = [s for s in before.get('stages', []) if s['stage'] == stage and s['status'] != 'passed']
    predecessor = failed[-1].get('operation_key') if failed else None
    if predecessor:
        authority(tenant, project_id)
        _store().record_decision(org, project, campaign_id, release_id,
            decision_key='retry-' + _digest([stage, predecessor]), kind='revision', decided_by=str(actor),
            payload={'retry_stage': stage, 'predecessor_operation_key': predecessor})
    return advance(tenant, project_id, campaign_id, release_id)


def _artifact(release):
    source = release['contract'].get('selected_artifact')
    if not source:
        raise delivery.DeliveryConflict('Validated project artifact is unavailable')
    return dict(source, path='releases/' + str(uuid.UUID(str(release['release_id']))) + '/' + source['name'])


def read_artifact(tenant, project_id, campaign_id, release_id, name):
    org, project, actor = authority(tenant, project_id)
    completion = _store().get_release(org, project, campaign_id, release_id)
    release = completion['release']
    artifact = _artifact(release)
    if name != artifact['name']:
        raise LookupError('Artifact unavailable')
    current = [s for s in completion.get('stages', []) if s.get('stage') == 'publication'
               and s.get('status') == 'passed'
               and s.get('contract_version', s.get('evidence', {}).get('contract_version')) == release['contract_version']]
    if not current:
        raise delivery.DeliveryConflict('Publication evidence unavailable')
    return delivery.read_verified(_lifecycle().project_snapshot(org, project, actor), artifact)


def advance(tenant, project_id, campaign_id, release_id):
    org, project, actor = authority(tenant, project_id)
    store = _store()
    for _ in range(5):
        completion = store.get_release(org, project, campaign_id, release_id)
        release = completion['release']
        if release['status'] != 'active':
            return completion
        contract = release['contract']
        required = contract.get('required_checks')
        if not required or {c['stage'] for c in required} != set(STAGES):
            raise delivery.DeliveryConflict('Required checks are incomplete')
        passed = {s['stage'] for s in completion.get('stages', []) if s['status'] == 'passed'
                  and s.get('contract_version', s.get('evidence', {}).get('contract_version')) == release['contract_version']}
        stage = next((s for s in STAGES if s not in passed), None)
        if stage is None:
            break
        source = contract.get('selected_artifact')
        revision = source['sha256'] if source else None
        observations = {}
        status = 'passed'
        retries = [d for d in completion.get('decisions', [])
                   if d.get('payload', {}).get('retry_stage') == stage]
        retry_key = retries[-1]['decision_key'] if retries else None
        operation = _digest([str(release_id), release['contract_version'], stage, revision, retry_key])
        try:
            if not source:
                raise delivery.DeliveryConflict('No executable delivery adapter or validated project artifact')
            artifact = _artifact(release)
            if stage == 'implementation':
                raw = delivery.file_bytes(_lifecycle().project_snapshot(org, project, actor), source['path'])
                observed = delivery.validate_bytes(source['path'], raw)
                if observed['sha256'] != revision or observed['size_bytes'] != source['size_bytes']:
                    raise delivery.DeliveryConflict('Source artifact changed')
                observations = {'artifact': observed, 'scope_summary': contract['release_boundary']}
            elif stage == 'publication':
                key = 'release-' + _digest([str(release_id), release['contract_version'], revision])
                authority(tenant, project_id)
                store.record_decision(org, project, campaign_id, release_id,
                    decision_key=key, kind='external_dependency', decided_by=str(actor),
                    payload={'operation': 'project_file_put', 'operation_key': key,
                             'path': artifact['path'], 'source_revision': revision})
                project_state = _lifecycle().project_snapshot(org, project, actor)
                receipt = delivery.receipt_for(project_state, artifact['path'], artifact['media_type'], revision)
                if receipt is None:
                    raw = delivery.file_bytes(project_state, source['path'])
                    observed = delivery.validate_bytes(source['path'], raw)
                    if observed['sha256'] != revision:
                        raise delivery.DeliveryConflict('Source artifact changed before publication')
                    authority(tenant, project_id)
                    result = _lifecycle().put_project_file(org, project, actor, path=artifact['path'],
                        media_type=artifact['media_type'], content=raw.decode('utf-8'), idempotency_key=key)
                    receipt = result['receipt']
                raw, observed = delivery.read_verified(_lifecycle().project_snapshot(org, project, actor), artifact)
                observations = {'artifact': observed, 'receipt': receipt}
            else:
                raw, observed = read_artifact(tenant, project_id, campaign_id, release_id, artifact['name'])
                url = '/api/campaigns/' + str(campaign_id) + '/releases/' + str(release_id) + '/artifacts/' + quote(artifact['name'], safe='') + '?project_id=' + str(project)
                observed['url'] = url
                observations = {'artifact': observed, 'observed_revision': observed['sha256'],
                    'retrieved': True, 'bytes_verified': True, 'content_valid': True,
                    'size_bytes': len(raw), 'workflow': contract['workflow'],
                    'workflow_verified': True, 'workflow_observations': ['Project access checked', 'Saved file retrieved', 'Actual file bytes parsed and digest checked'],
                    'replay_recipe': ['Open the authorized project', 'Download ' + url, 'Open the downloaded ' + observed['format'] + ' file'],
                    'known_limits': contract['deferred_items'], 'deliverables': [observed]}
        except (ValueError, UnicodeError) as exc:
            status = 'unavailable'
            observations = {'reason': str(exc), 'recommended_action': 'Restore a valid source artifact or provide the missing delivery adapter'}
        evidence = {**observations, 'contract_version': release['contract_version'],
                    'source_revision': revision,
                    'checks': [{'check_id': c['check_id'], 'status': status, 'evidence': observations}
                               for c in required if c['stage'] == stage]}
        authority(tenant, project_id)
        store.record_stage(org, project, campaign_id, release_id, stage=stage, status=status,
                           evidence=evidence, producer=PRODUCERS[stage], operation_key=operation)
        if status != 'passed':
            return store.get_release(org, project, campaign_id, release_id)
    authority(tenant, project_id)
    store.finish_release(org, project, campaign_id, release_id)
    return store.get_release(org, project, campaign_id, release_id)
