"""Tenant-authorized completion runtime for the Malleable campaign engine."""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import quote

import platform_link
import campaign_delivery_service as delivery
import campaign_capability_resolver as capabilities
import campaign_web_release as web_release

STAGES = ('implementation', 'publication', 'deployment', 'user_verification', 'delivery')
PRODUCERS = dict(zip(STAGES, ('task_ledger', 'task_ledger', 'deployment_adapter',
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


def actor_for_release(row):
    """Re-resolve a persisted project actor without reconstructing JWT powers."""
    import deps
    campaigns = _module('campaigns')
    scope = campaigns._scope(row['org_id'], row['project_id'])
    principal = uuid.UUID(str(row['principal_id']))
    with campaigns._cursor() as cur:
        campaigns._principal(cur, scope, principal)
        cur.execute("SELECT external_subject FROM identity_bindings WHERE binding_id=%s "
                    "AND platform_tenant_id=%s AND external_authority='auth0' AND status='active'",
                    (principal, scope['org']))
        identity = cur.fetchone()
    if not identity:
        raise platform_link.ProjectSessionForbidden('Current project actor is unavailable')
    org, tier = deps.resolve_active_platform_tenant_authority(identity['external_subject'])
    if str(org) != str(scope['org']):
        raise platform_link.ProjectSessionForbidden('Project actor moved to another workspace')
    tenant = deps.TenantContext(str(org), org_id=str(org), tier=tier,
                              subject=identity['external_subject'], authority_resolved=True)
    resolved = authority(tenant, row['project_id'])
    if resolved[2] != principal:
        raise platform_link.ProjectSessionForbidden('Project actor binding changed')
    return tenant


def validate_finish(finish):
    required = {'delivery_profile', 'intended_user', 'workflow', 'artifact_refs'}
    if not isinstance(finish, dict) or not required <= set(finish) or set(finish) - required - {'deadline_at'}:
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
    if 'deadline_at' in finish:
        value = finish['deadline_at']
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError('Invalid deadline')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError('Deadline must use UTC')
        finish = dict(finish, deadline_at=parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'))
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
    web_recipe = None
    transform_recipe = None
    if finish['delivery_profile'] == 'cad_file':
        state = _lifecycle().project_snapshot(org, project, actor)
        refs = finish['artifact_refs']
        wants_csv = bool(re.search(r'\bcsv\b', finish['workflow'], re.I))
        if wants_csv:
            csv_refs = [p for p in (refs or [r['path'] for r in state.get('files', [])
                                            if not r['path'].startswith('releases/')]) if p.lower().endswith('.csv')]
            artifact = delivery.select_artifact(state, csv_refs) if csv_refs else None
            if artifact is None:
                _, transform_recipe = web_release.compile_recipe(state, refs)
                if transform_recipe:
                    raw = delivery.file_bytes(state, transform_recipe['source_artifact']['path'])
                    artifact = delivery.validate_bytes('records.csv', web_release.static.expected_output(raw))
        else:
            artifact = delivery.select_artifact(state, refs)
    elif finish['delivery_profile'] == 'web_tool':
        artifact, web_recipe = web_release.compile_recipe(
            _lifecycle().project_snapshot(org, project, actor), finish['artifact_refs'])
    boundary = ('Deliver and verify the existing ' + artifact['format'] + ' artifact ' + artifact['path']
                if artifact else 'Produce and verify a ' + finish['delivery_profile'] + ' release')
    deferred = ['The original ambition beyond this existing artifact has not been implemented or validated.'] if artifact else []
    # The row already owns delivery_profile as its own column; the contract must not duplicate it.
    public = {key: value for key, value in finish.items() if key != 'delivery_profile'}
    if artifact:
        public['workflow'] = ('Open the authorized project, retrieve ' + artifact['name'] +
                              ', and validate the downloaded ' + artifact['format'] + ' file.')
        public['artifact_refs'] = [artifact['path']]
    if web_recipe:
        public['workflow'] = web_release.WORKFLOW
        public['artifact_refs'] = [web_recipe['source_artifact']['path']]
        boundary = 'Deliver a working records-to-CSV web tool and its verified CSV output'
        deferred = ['The original ambition beyond this records-to-CSV workflow remains unproven.']
    if transform_recipe:
        public['workflow'] = 'Transform the selected JSON records with the published project tool, then download and open the verified CSV file.'
        public['artifact_refs'] = [transform_recipe['source_artifact']['path']]
        boundary = 'Deliver CSV records produced by a verified published tenant tool'
        deferred = ['The original ambition beyond this records-to-CSV file workflow remains unproven.']
    if finish['workflow'] != public['workflow']:
        deferred.append('Requested workflow beyond this release: ' + finish['workflow'][:900])
    deferred.extend('Deferred input: ' + path for path in finish['artifact_refs'] if path not in public['artifact_refs'])
    return {**public, 'original_goal': campaign['prompt'], 'release_boundary': boundary,
            'deferred_items': deferred, 'selected_artifact': artifact,
            **({'web_recipe': web_recipe} if web_recipe else {}),
            **({'transform_recipe': transform_recipe} if transform_recipe else {}),
            'priority_score': 90 if artifact and not (web_recipe or transform_recipe) else 60 if web_recipe else 40,
            'request_digest': _digest(finish),
            'required_checks': [{'check_id': stage + '.verified', 'stage': stage,
                                 'description': description} for stage, description in zip(STAGES, (
                'Validate actual source file bytes and format',
                'Persist release copy with matching lifecycle receipt',
                'Retrieve the saved version through the authorized artifact reader',
                'Open the project and retrieve and validate the promised file',
                'Re-read recipient artifact bytes and provide replay and known limits'))] +
                ([{'check_id': 'browser.download', 'stage': 'user_verification',
                   'description': 'Run the served converter and validate actual downloaded CSV bytes'}] if web_recipe else [])}


def snapshot(tenant, project_id, campaign_id, release_id=None):
    org, project, _ = authority(tenant, project_id)
    completion = (_store().release_snapshot(org, project, campaign_id) if release_id is None
                  else _store().get_release(org, project, campaign_id, release_id))
    release = completion.get('release')
    if release and release.get('status') == 'finished' and release['contract'].get('selected_artifact'):
        try:
            names = [release['contract']['selected_artifact']['name']]
            if release['contract'].get('web_recipe'):
                names.append('records.csv')
            for name in names:
                read_artifact(tenant, project_id, campaign_id, release['release_id'], name)
            completion = dict(completion, current_verification={'status': 'passed'})
        except (ValueError, UnicodeError):
            completion = dict(completion, deliverables=[], current_verification={
                'status': 'failed', 'reason': 'Saved artifact no longer matches the accepted version'})
    return completion


def list_releases(tenant, project_id, campaign_id):
    org, project, _ = authority(tenant, project_id)
    return _store().list_releases(org, project, campaign_id)


def create(tenant, project_id, campaign_id, finish, idempotency_key,
           authority_session_id=None, authority_turn_id=None):
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
    if existing:
        if row['status'] == 'waiting':
            _store().transition_release(org, project, campaign_id, rid, actor, action='resume')
        return advance(tenant, project_id, campaign_id, rid, authority_session_id, authority_turn_id)
    selection = capabilities.resolve(tenant, finish['delivery_profile'],
                                     existing_artifact=bool(contract.get('selected_artifact')),
                                     **({'transform_recipe': True} if contract.get('transform_recipe') else {}))
    authority(tenant, project_id)
    _store().record_decision(org, project, campaign_id, rid, decision_key='capability-selection',
                             kind='capability_selection', payload=selection, decided_by=str(actor))
    _store().record_decision(org, project, campaign_id, rid, decision_key='initial-release-scope',
        kind='scope', decided_by=str(actor), payload={'requested_workflow': finish['workflow'],
        'selected_workflow': contract['workflow'], 'release_boundary': contract['release_boundary'],
        'deferred_items': contract['deferred_items'],
        'reason': 'Reuse validated project material or a proven managed recipe for a useful bounded release'})
    if not selection['selected']:
        from routers import campaigns
        campaigns._store().ask_question(org, project, campaign_id, question_key='completion-capability-' + str(rid),
            prompt=selection['missing_capability'] + '. ' + selection['recommended_action'],
            options=[], blocks_dispatch=False)
    return advance(tenant, project_id, campaign_id, rid, authority_session_id, authority_turn_id)


def transition(tenant, project_id, campaign_id, release_id, action,
               authority_session_id=None, authority_turn_id=None):
    if action not in ('pause', 'resume', 'cancel'):
        raise ValueError('Unknown release action')
    org, project, actor = authority(tenant, project_id)
    _store().transition_release(org, project, campaign_id, release_id, actor, action=action)
    if action == 'resume':
        return advance(tenant, project_id, campaign_id, release_id, authority_session_id, authority_turn_id)
    return snapshot(tenant, project_id, campaign_id, release_id)


def resume_pending(tenant, project_id, campaign_id, release_id):
    """Worker continuation cannot override a later user pause or authority wait."""
    org, project, actor = authority(tenant, project_id)
    row = _store().transition_release(org, project, campaign_id, release_id, actor,
                                      action='resume', automatic=True)
    if row['status'] == 'active' and not row.get('replayed'):
        return advance(tenant, project_id, campaign_id, release_id)
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
    return dict(source, path=web_release.prefix(release) + source['name'])


def read_artifact(tenant, project_id, campaign_id, release_id, name):
    org, project, actor = authority(tenant, project_id)
    completion = _store().get_release(org, project, campaign_id, release_id)
    release = completion['release']
    artifact = _artifact(release)
    if not release['contract'].get('web_recipe') and name != artifact['name']:
        raise LookupError('Artifact unavailable')
    current = [s for s in completion.get('stages', []) if s.get('stage') == 'publication'
               and s.get('status') == 'passed'
               and s.get('contract_version', s.get('evidence', {}).get('contract_version')) == release['contract_version']]
    if not current:
        raise delivery.DeliveryConflict('Publication evidence unavailable')
    if release['contract'].get('web_recipe'):
        if name == 'records.csv' and not any(s['stage'] == 'user_verification' and s['status'] == 'passed'
                and s['contract_version'] == release['contract_version'] for s in completion.get('stages', [])):
            raise delivery.DeliveryConflict('Download verification evidence unavailable')
        return web_release.read(_lifecycle().project_snapshot(org, project, actor), release, name)
    return delivery.read_verified(_lifecycle().project_snapshot(org, project, actor), artifact)


def _transform_input(tenant, project_id, campaign_id, release):
    original = release['contract']['transform_recipe']['source_artifact']
    frozen = dict(original, path=web_release.prefix(release) + 'source.json', name='source.json')
    org, project, actor = authority(tenant, project_id)
    state = _lifecycle().project_snapshot(org, project, actor)
    if delivery.receipt_for(state, frozen['path'], frozen['media_type'], frozen['sha256']):
        return delivery.read_verified(state, frozen)[0]
    raw = delivery.file_bytes(state, original['path'])
    observed = delivery.validate_bytes(original['path'], raw)
    if any(observed[k] != original[k] for k in ('sha256', 'size_bytes', 'media_type')):
        raise delivery.DeliveryConflict('Source records changed before acquisition')
    web_release._put(__import__(__name__), tenant, project_id, campaign_id, release, frozen, raw)
    return raw


def _pending(tenant, project_id, campaign_id, release, result):
    org, project, actor = authority(tenant, project_id)
    reason = str(result.get('reason', 'Existing work is pending'))[:1024]
    action = str(result.get('recommended_action', 'Wait for the existing work'))[:1024]
    waiting_user = result['state'] == 'awaiting_user'
    kind = ('approval' if waiting_user and 'publication' in reason.lower() else 'authority') if waiting_user else (
        'job' if result.get('job_id') else 'authoring' if result.get('change_set_id') else 'capacity')
    progress = {'wait_kind': kind, 'reason': reason, 'recommended_action': action}
    progress.update({k: str(result[k]) for k in ('change_set_id', 'job_id') if result.get(k)})
    # Running execution retains the slot. External authoring and account actions yield it.
    _store().set_progress(org, project, campaign_id, release['release_id'], actor,
                          state='active' if kind == 'job' else 'waiting', next_action=progress)
    if waiting_user:
        from routers import campaigns
        campaigns._store().ask_question(org, project, campaign_id,
            question_key='completion-action-' + _digest([release['release_id'], release['contract_version'], kind]),
            prompt=reason + '. Recommended action: ' + action, options=[], blocks_dispatch=False)
    return _store().get_release(org, project, campaign_id, release['release_id'])


def _transform_implementation(tenant, project_id, campaign_id, release,
                               authority_session_id, authority_turn_id):
    import campaign_acquisition_service as acquisition
    source = _transform_input(tenant, project_id, campaign_id, release)
    if isinstance(tenant, _WorkerActor):
        tenant.resolve(project_id)
        tenant = actor_for_release(release)
    result = acquisition.advance(__import__(__name__), tenant, project_id, campaign_id,
        release, source, authority_session_id=authority_session_id, authority_turn_id=authority_turn_id)
    if result['state'] in ('working', 'awaiting_user'):
        return None, _pending(tenant, project_id, campaign_id, release, result)
    if result['state'] != 'complete':
        raise delivery.DeliveryConflict(result.get('reason', 'The authored transform failed'))
    raw = result['output_bytes']
    expected = web_release.static.expected_output(source)
    observed = delivery.validate_bytes('records.csv', raw)
    artifact = _artifact(release)
    if raw != expected or any(observed[k] != artifact[k] for k in ('sha256', 'size_bytes', 'media_type')):
        raise delivery.DeliveryConflict('The authored output does not match the release contract')
    receipt = web_release._put(__import__(__name__), tenant, project_id, campaign_id, release, artifact, raw)
    return {'artifact': observed, 'job_id': result['job_id'], 'publication': result['publication'],
            'input_sha256': hashlib.sha256(source).hexdigest(), 'receipt': receipt}, None


def advance(tenant, project_id, campaign_id, release_id,
            authority_session_id=None, authority_turn_id=None):
    org, project, _ = authority(tenant, project_id)
    store = _store()
    guard = getattr(store, 'execution_guard', None)
    # The fallback is for injected test stores. The canonical PG store owns the lock.
    with guard(org, project, campaign_id, release_id) if guard else nullcontext(True) as acquired:
        if not acquired:
            return store.get_release(org, project, campaign_id, release_id)
        return _advance(tenant, project_id, campaign_id, release_id, authority_session_id, authority_turn_id)


def _advance(tenant, project_id, campaign_id, release_id,
             authority_session_id=None, authority_turn_id=None):
    org, project, actor = authority(tenant, project_id)
    store = _store()
    for _ in range(5):
        completion = store.get_release(org, project, campaign_id, release_id)
        release = completion['release']
        if release['status'] != 'active':
            return snapshot(tenant, project_id, campaign_id, release_id) if release['status'] == 'finished' else completion
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
            url = ('/api/campaigns/' + str(campaign_id) + '/releases/' + str(release_id) + '/artifacts/' +
                   quote(artifact['name'], safe='') + '?project_id=' + str(project))
            if contract.get('web_recipe'):
                observations = web_release.run_stage(__import__(__name__), tenant, project_id,
                                                       campaign_id, completion, stage)
            elif contract.get('transform_recipe') and stage == 'implementation':
                observations, pending = _transform_implementation(tenant, project_id, campaign_id, release,
                                                                   authority_session_id, authority_turn_id)
                if pending is not None:
                    return pending
            elif contract.get('transform_recipe') and stage == 'publication':
                raw, observed = delivery.read_verified(_lifecycle().project_snapshot(org, project, actor), artifact)
                receipt = delivery.receipt_for(_lifecycle().project_snapshot(org, project, actor),
                                               artifact['path'], artifact['media_type'], revision)
                observations = {'artifact': observed, 'receipt': receipt}
            elif stage == 'implementation':
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
            elif stage == 'deployment':
                raw, observed = read_artifact(tenant, project_id, campaign_id, release_id, artifact['name'])
                publication_stage = next((s for s in completion.get('stages', []) if s['stage'] == 'publication'), None)
                receipt_id = (publication_stage or {}).get('evidence', {}).get('receipt', {}).get('receipt_id')
                rollback_identity = (
                    'No prior deployed revision exists; publication receipt ' + receipt_id +
                    ' is the only recorded state.' if receipt_id else
                    'No prior deployed revision or publication receipt exists for this release.')
                observations = {'artifact': observed, 'observed_revision': observed['sha256'],
                    'resource_identity': artifact['path'] + '@' + revision, 'rollback_identity': rollback_identity}
            elif stage == 'user_verification':
                raw, observed = read_artifact(tenant, project_id, campaign_id, release_id, artifact['name'])
                observations = {'artifact': observed, 'workflow': contract['workflow'],
                    'observations': ['Project access checked', 'Saved file retrieved through ' + url,
                                      'Actual file bytes parsed and digest checked']}
            else:
                raw, observed = read_artifact(tenant, project_id, campaign_id, release_id, artifact['name'])
                source_ref = contract.get('transform_recipe', {}).get('source_artifact', source)['path']
                observations = {'artifacts': [{'artifact_ref': source_ref, 'name': observed['name'],
                        'sha256': observed['sha256'], 'byte_count': len(raw), 'retrieved': True,
                        'valid': True, 'access_path': url, 'media_type': observed['media_type']}],
                    'replay_recipe': ('Open the authorized project, download ' + url +
                                      ', then open the downloaded ' + observed['format'] + ' file.'),
                    'known_limits': contract['deferred_items']}
                if contract.get('transform_recipe'):
                    original = contract['transform_recipe']['source_artifact']
                    observations['replay_recipe'] = (
                        'Open this project and download ' + url + '. To reproduce the transform, '
                        'use the frozen input ' + web_release.prefix(release) + 'source.json '
                        'with the published campaign-records-to-csv tool. Its exact publication and job '
                        'are recorded in implementation evidence. Original input: ' + original['path'] + '.')
        except web_release.producer.WebToolVerificationError as exc:
            status = 'failed'
            observations = {'reason': str(exc), 'recommended_action': 'Correct the converter and retry browser verification'}
        except (ValueError, UnicodeError, web_release.producer.WebToolUnavailable) as exc:
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
            current = store.get_release(org, project, campaign_id, release_id)
            if status == 'unavailable' and current['release']['status'] == 'active' and hasattr(store, 'set_progress'):
                store.set_progress(org, project, campaign_id, release_id, actor, state='waiting', next_action={
                    'wait_kind': 'authority', 'reason': observations['reason'],
                    'recommended_action': observations['recommended_action']})
            return store.get_release(org, project, campaign_id, release_id)
    authority(tenant, project_id)
    store.finish_release(org, project, campaign_id, release_id)
    return store.get_release(org, project, campaign_id, release_id)
