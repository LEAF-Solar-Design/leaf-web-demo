"""One real acquisition service over substituted external stores and producers."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import sys
from types import SimpleNamespace
import uuid

import pytest

import agent_policy
import campaign_acquisition_service as service
import customization_service as customization
from customization_models import ChangeState
import deps
import entitlements
import jobs
from routers import author

ORG, PROJECT, CAMPAIGN, RELEASE, BINDING, CHANGE, JOB = [str(uuid.uuid4()) for _ in range(7)]
SOURCE = b'[{"name":"Example","value":1}]'
TOOL = {'name': service.TOOL_NAME, 'kind': 'script', 'entry': 'tools/records.py',
        'capabilities': ['drawing.read'], 'version': '1.0.0',
        'params': {'type': 'object', 'properties': {'source_json': {'type': 'string',
                   'minLength': 1, 'maxLength': 1048576}}, 'required': ['source_json'],
                   'additionalProperties': False}}


class Store:
    def __init__(self, release):
        self.release = release
        self.decisions = []
        self.stages = []

    def get_release(self, org, project, campaign, release):
        assert tuple(map(str, (org, project, campaign, release))) == (ORG, PROJECT, CAMPAIGN, RELEASE)
        return {'release': deepcopy(self.release), 'decisions': deepcopy(self.decisions),
                'stages': deepcopy(self.stages)}

    def record_decision(self, org, project, campaign, release, *, decision_key, kind, payload, decided_by):
        assert tuple(map(str, (org, project, campaign, release))) == (ORG, PROJECT, CAMPAIGN, RELEASE)
        assert decided_by == BINDING
        prior = next((row for row in self.decisions if row['decision_key'] == decision_key), None)
        if prior:
            assert prior['payload'] == payload
            return prior
        row = {'decision_key': decision_key, 'kind': kind, 'payload': deepcopy(payload)}
        self.decisions.append(row)
        return row


@pytest.fixture
def setup(monkeypatch):
    expected = service.recipe.expected_output(SOURCE)
    release = {'release_id': RELEASE, 'contract_version': 1, 'status': 'active',
        'contract': {'transform_recipe': {'recipe_id': 'json-records-to-csv', 'recipe_version': 1,
            'source_artifact': {'sha256': hashlib.sha256(SOURCE).hexdigest()}},
            'selected_artifact': service.delivery.validate_bytes('records.csv', expected)}}
    store = Store(release)
    calls = {'authority': 0, 'submit': 0, 'stage': 0, 'publish': 0}
    tenant = deps.TenantContext(ORG, org_id=ORG, subject='auth0|test', tier='hosted_pro')

    def authority(caller, project):
        assert caller is tenant and str(project) == PROJECT
        calls['authority'] += 1
        return uuid.UUID(ORG), uuid.UUID(PROJECT), uuid.UUID(BINDING)

    runtime = SimpleNamespace(authority=authority, _store=lambda: store)
    state = {'available': True, 'stage_status': 'queued', 'publish_status': 'published',
             'capacity': True, 'job': None, 'row': None, 'lost_response': False, 'wrong_csv': False}
    registry = json.dumps({'tools': [TOOL, {'name': 'unrelated-later-tool'}]}).encode()
    pin = SimpleNamespace(tenant_id=ORG, change_set_id=CHANGE, catalog_commit='a' * 40,
                          catalog_digest=hashlib.sha256(registry).hexdigest())
    change = SimpleNamespace(**vars(pin), state=ChangeState.PUBLISHED, staged_commit=pin.catalog_commit)

    def enqueue(**kwargs):
        calls['stage'] += 1
        assert kwargs['tenant'] is tenant
        assert kwargs['authority_session_id'] == 'active-session'
        assert kwargs['authority_turn_id'] == 'active-turn'
        assert kwargs['description'] == service.AUTHOR_DESCRIPTION
        assert SOURCE.decode() not in kwargs['description']
        return {'contract': 'leaf.customization-stage-job.v1', 'change_set_id': CHANGE,
                'status': state['stage_status']}

    def publish(**kwargs):
        calls['publish'] += 1
        assert kwargs == {'tenant': tenant, 'change_set_id': CHANGE}
        if state['publish_status'] == 'published':
            state['available'] = True
        return {'change_set_id': CHANGE, 'status': state['publish_status']}

    custom = SimpleNamespace(store=SimpleNamespace(get_effective_catalog=lambda **kw: pin,
                get_change_set=lambda **kw: change), enqueue_stage=enqueue,
                stage=lambda **kw: (_ for _ in ()).throw(AssertionError('sync fallback')),
                stage_status=lambda **kw: {'change_set_id': CHANGE, 'status': state['stage_status']},
                request_publication=publish)
    monkeypatch.setattr(customization.CustomizationService, 'configured', lambda: custom)
    monkeypatch.setattr(customization, '_bare_repo', lambda tid: 'fake-external-store')
    monkeypatch.setattr(customization, '_git_blob', lambda *args: registry)
    monkeypatch.setattr(customization, 'effective_catalog_pin', lambda tid: {
        'catalog_commit': pin.catalog_commit, 'effective_catalog_digest': pin.catalog_digest})
    monkeypatch.setattr(deps, 'effective_tools_with_provenance', lambda tid:
        [(deepcopy(TOOL), deps.TOOL_SOURCE_TENANT_REPO)] if state['available'] else [])
    monkeypatch.setattr(service.tool_loader, 'published_tool_source_sha256', lambda *args: 'c' * 64)
    monkeypatch.setattr(author, '_customization_gate', lambda *args: None)
    monkeypatch.setattr(deps, 'stage_author_identity', lambda caller, session, turn:
        caller if caller is tenant and (session, turn) == ('active-session', 'active-turn') else None)
    monkeypatch.setattr(entitlements, 'resolve_tier', lambda caller: 'hosted_pro')
    monkeypatch.setattr(entitlements, 'resolve_roles', lambda caller: ((), False))
    monkeypatch.setattr(agent_policy, 'load_tenant_state', lambda tid: {'agent_disabled': False, 'overlay': {}})
    monkeypatch.setattr(agent_policy, 'load_policy', lambda: None)
    monkeypatch.setattr(agent_policy, 'effective_action', lambda *a, **kw:
        SimpleNamespace(enabled=True, policy='auto'))

    @contextmanager
    def lock(*args):
        yield

    @contextmanager
    def capacity(*args):
        yield state['capacity']

    monkeypatch.setattr(service.admission, '_admission_lock', lock)
    monkeypatch.setattr(service.admission, '_lookup', lambda *args: state['row'])
    monkeypatch.setattr(service, '_capacity', capacity)
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, 'get_job', lambda jid: deepcopy(state['job']))
    # Sibling module is separately owned; only its closed validation seam is substituted here.
    def validate_context(context):
        assert context['schema'] == 'leaf.campaign-transform.v1'
        assert context['input_sha256'] == hashlib.sha256(SOURCE).hexdigest()
        return dict(context)
    monkeypatch.setitem(sys.modules, 'campaign_transform_job', SimpleNamespace(validate_context=validate_context))

    def submit(**kwargs):
        calls['submit'] += 1
        assert kwargs['org_id'] == ORG and kwargs['project_id'] == PROJECT
        assert kwargs['dwg'] == '' and kwargs['aps_live'] is False
        assert 'capability_provenance' not in kwargs
        context = kwargs['completion_provenance']
        state['row'] = {'job_id': JOB, 'tenant_id': ORG, 'org_id': ORG, 'project_id': PROJECT,
                        'tool': service.TOOL_NAME, 'execution_json': {'completion_provenance': deepcopy(context)}}
        actual = 'wrong' if state['wrong_csv'] else expected.decode()
        state['job'] = {'job_id': JOB, 'tenant_id': ORG, 'org_id': ORG, 'project_id': PROJECT,
                        'tool': service.TOOL_NAME, 'completion_provenance': deepcopy(context),
                        'params': deepcopy(kwargs['params']), 'idempotency_key': kwargs['idempotency_key'],
                        'status': 'complete', 'result': {'ok': True, 'tool': service.TOOL_NAME, 'result': {'csv': actual}}}
        if state['lost_response']:
            raise TimeoutError('external response lost')
        return JOB
    monkeypatch.setattr(jobs, 'submit_job', submit)
    return SimpleNamespace(runtime=runtime, tenant=tenant, release=release, store=store,
                           state=state, calls=calls, pin=pin, change=change, expected=expected)


def advance(setup, **kwargs):
    return service.advance(setup.runtime, setup.tenant, PROJECT, CAMPAIGN, setup.release, SOURCE, **kwargs)


def replace_published_tool(setup, monkeypatch, tool):
    registry = json.dumps({'tools': [tool, {'name': 'unrelated-later-tool'}]}).encode()
    setup.pin.catalog_digest = hashlib.sha256(registry).hexdigest()
    setup.change.catalog_digest = setup.pin.catalog_digest
    monkeypatch.setattr(customization, '_git_blob', lambda *args: registry)
    monkeypatch.setattr(deps, 'effective_tools_with_provenance', lambda *args:
        [(deepcopy(tool), deps.TOOL_SOURCE_TENANT_REPO)])


def test_omitted_local_only_preserves_published_manifest_and_pins(setup):
    assert 'local_only' not in TOOL
    original = deepcopy(TOOL)
    catalog_digest = setup.pin.catalog_digest
    manifest_digest = deps.catalog_tool_digest(TOOL)
    result = advance(setup)
    assert result['state'] == 'complete'
    assert result['publication']['effective_catalog_digest'] == catalog_digest
    assert result['publication']['tool_manifest_sha256'] == manifest_digest
    assert TOOL == original
    assert setup.calls['stage'] == setup.calls['publish'] == 0
    assert setup.calls['submit'] == 1


def test_explicit_local_only_true_remains_supported(setup, monkeypatch):
    tool = dict(deepcopy(TOOL), local_only=True)
    replace_published_tool(setup, monkeypatch, tool)
    result = advance(setup)
    assert result['state'] == 'complete'
    assert result['output_bytes'] == setup.expected
    assert result['publication']['tool_manifest_sha256'] == deps.catalog_tool_digest(tool)
    assert setup.calls['submit'] == 1


@pytest.mark.parametrize('value', [False, None, 0, 1, 'true', 'false', '', [], {}])
def test_explicit_local_only_contradiction_refuses_before_job(setup, monkeypatch, value):
    tool = dict(deepcopy(TOOL), local_only=value)
    # Keep publication hashes valid so only the explicit contradiction refuses.
    replace_published_tool(setup, monkeypatch, tool)
    result = advance(setup)
    assert result['state'] == 'failed'
    assert result['reason'] == 'Published transform does not match its verified contract'
    assert setup.calls['stage'] == setup.calls['publish'] == setup.calls['submit'] == 0


def test_reuse_cumulative_publication_without_authoring(setup):
    result = advance(setup)
    assert result['state'] == 'complete'
    assert result['output_bytes'] == setup.expected
    assert result['metadata']['sha256'] == hashlib.sha256(setup.expected).hexdigest()
    assert setup.calls['stage'] == setup.calls['publish'] == 0
    assert setup.calls['submit'] == 1
    assert {r['decision_key'] for r in setup.store.decisions} == {
        'acquisition-v1-intent', 'acquisition-v1-publication', 'acquisition-v1-invocation'}


def test_revised_contract_uses_new_job_key_and_existing_publication(setup):
    assert advance(setup)['state'] == 'complete'
    old_key = setup.state['job']['idempotency_key']
    setup.release['contract_version'] = 2
    setup.release['contract']['workflow'] = 'Reuse published tool with a new approach'
    setup.state.update(row=None, job=None)
    result = advance(setup)
    assert result['state'] == 'complete'
    assert setup.state['job']['idempotency_key'] != old_key
    assert setup.calls['submit'] == 2
    assert setup.calls['stage'] == setup.calls['publish'] == 0
    assert result['publication']['effective_catalog_digest'] == setup.pin.catalog_digest


def test_missing_tool_calls_real_author_route_and_resumes_its_reference(setup):
    setup.state['available'] = False
    first = advance(setup, authority_session_id='active-session', authority_turn_id='active-turn')
    assert first['state'] == 'working' and first['change_set_id'] == CHANGE
    assert setup.calls['stage'] == 1 and setup.calls['publish'] == setup.calls['submit'] == 0
    assert advance(setup)['state'] == 'working'
    assert setup.calls['stage'] == 1
    setup.state['stage_status'] = 'staged'
    assert advance(setup)['state'] == 'complete'
    assert setup.calls['publish'] == setup.calls['submit'] == 1


@pytest.mark.parametrize('authority', [{}, {'authority_session_id': 'foreign', 'authority_turn_id': 'active-turn'}])
def test_missing_or_foreign_turn_never_authors(setup, authority):
    setup.state['available'] = False
    result = advance(setup, **authority)
    assert result['state'] == 'awaiting_user'
    assert setup.calls['stage'] == setup.calls['submit'] == 0
    assert setup.store.decisions[0]['decision_key'] == 'acquisition-v1-intent'


@pytest.mark.parametrize('status', ['awaiting_approval', 'denied'])
def test_publication_action_retains_changeset_and_never_invokes(setup, status):
    setup.state.update(available=False, stage_status='staged', publish_status=status)
    result = advance(setup, authority_session_id='active-session', authority_turn_id='active-turn')
    assert result['state'] == 'awaiting_user' and result['change_set_id'] == CHANGE
    assert setup.calls['submit'] == 0


@pytest.mark.parametrize('mutation', ['tenant', 'change', 'source', 'catalog', 'winner', 'schema'])
def test_publication_mismatch_refuses_before_job(setup, monkeypatch, mutation):
    if mutation == 'tenant':
        setup.pin.tenant_id = str(uuid.uuid4())
    elif mutation == 'change':
        setup.change.state = ChangeState.STAGED
    elif mutation == 'source':
        monkeypatch.setattr(service.tool_loader, 'published_tool_source_sha256', lambda *a: None)
    elif mutation == 'catalog':
        monkeypatch.setattr(customization, 'effective_catalog_pin', lambda *a: None)
    else:
        tool = deepcopy(TOOL)
        if mutation == 'schema':
            tool['params']['additionalProperties'] = True
        monkeypatch.setattr(deps, 'effective_tools_with_provenance', lambda *a:
            [(tool, 'authored' if mutation == 'winner' else deps.TOOL_SOURCE_TENANT_REPO)])
    assert advance(setup)['state'] == 'failed'
    assert setup.calls['submit'] == 0


def test_run_entitlement_denied(setup, monkeypatch):
    monkeypatch.setattr(entitlements, 'entitlements_for', lambda *a: {'run_read': False})
    assert advance(setup)['state'] == 'awaiting_user'
    assert setup.calls['submit'] == 0


def test_existing_policy_confirmation_is_not_bypassed(setup, monkeypatch):
    monkeypatch.setattr(agent_policy, 'effective_action', lambda *a, **kw:
        SimpleNamespace(enabled=True, policy='always-confirm'))
    assert advance(setup)['state'] == 'awaiting_user'
    assert setup.calls['submit'] == 0


def test_lost_submission_recovers_same_job_without_duplicate(setup):
    setup.state['lost_response'] = True
    assert advance(setup)['state'] == 'complete'
    assert advance(setup)['job_id'] == JOB
    assert setup.calls['submit'] == 1


@pytest.mark.parametrize('location', ['row', 'job', 'params'])
def test_mismatched_prior_job_is_not_trusted(setup, location):
    assert advance(setup)['state'] == 'complete'
    if location == 'params':
        setup.state['job']['params'] = {'source_json': '[]'}
    else:
        setup.state[location]['tenant_id'] = str(uuid.uuid4())
    assert advance(setup)['state'] == 'failed'
    assert setup.calls['submit'] == 1


def test_wrong_actual_csv_is_never_replaced_with_expected(setup):
    setup.state['wrong_csv'] = True
    result = advance(setup)
    assert result['state'] == 'failed'
    assert 'output_bytes' not in result


def test_exhausted_capacity_creates_no_job(setup):
    setup.state['capacity'] = False
    assert advance(setup)['state'] == 'working'
    assert setup.calls['submit'] == 0


def test_failed_authoring_is_not_reenqueued(setup):
    setup.state.update(available=False, stage_status='failed')
    assert advance(setup, authority_session_id='active-session', authority_turn_id='active-turn')['state'] == 'failed'
    assert advance(setup)['state'] == 'failed'
    assert setup.calls['stage'] == 1


def test_live_authority_revocation_reaches_runtime_boundary(setup):
    def denied(*args):
        raise PermissionError('revoked')
    setup.runtime.authority = denied
    with pytest.raises(PermissionError):
        advance(setup)
    assert setup.calls['submit'] == 0


@pytest.fixture
def invocation_history(setup, monkeypatch):
    rows, records = {}, {}
    submit = jobs.submit_job

    def enqueue(**kwargs):
        lost = setup.state['lost_response']
        setup.state['lost_response'] = False
        try:
            submit(**kwargs)
        finally:
            setup.state['lost_response'] = lost
        jid = str(uuid.uuid4())
        setup.state['row']['job_id'] = setup.state['job']['job_id'] = jid
        rows[kwargs['idempotency_key']] = deepcopy(setup.state['row'])
        records[jid] = deepcopy(setup.state['job'])
        if lost:
            raise TimeoutError('durable submit response lost')
        return jid

    monkeypatch.setattr(jobs, 'submit_job', enqueue)
    monkeypatch.setattr(jobs, 'get_job', lambda jid: deepcopy(records.get(jid)))
    monkeypatch.setattr(service.admission, '_lookup', lambda tenant, project, key: deepcopy(rows.get(key)))
    first = advance(setup)
    records[first['job_id']]['status'] = 'failed'
    return SimpleNamespace(rows=rows, records=records, original=first['job_id'])


def authorize_invocation_retry(setup, version=1):
    stage_id = str(uuid.uuid4())
    stage = {'stage_id': stage_id, 'stage': 'implementation', 'status': 'failed',
             'contract_version': version, 'operation_key': 'implementation:' + stage_id}
    setup.store.stages.append(stage)
    decision = {'decision_key': 'retry-stage:' + stage_id, 'kind': 'revision',
                'payload': {'retry_stage': 'implementation', 'predecessor_stage_id': stage_id,
                            'predecessor_operation_key': stage['operation_key'], 'contract_version': version}}
    setup.store.decisions.append(decision)
    return decision


def test_failed_base_requires_explicit_retry(setup, invocation_history):
    assert advance(setup)['state'] == 'failed'
    assert advance(setup)['state'] == 'failed'
    assert setup.calls['submit'] == 1


@pytest.mark.parametrize('lost_response', [False, True])
def test_retry_keeps_contract_publication_input_and_original_invocation(setup, invocation_history, lost_response):
    history = invocation_history
    original = deepcopy(history.records[history.original])
    decisions = deepcopy(setup.store.decisions)
    contract = deepcopy(setup.release)
    retry = authorize_invocation_retry(setup)
    setup.state['lost_response'] = lost_response
    result = advance(setup)
    assert result['state'] == 'complete'
    assert result['job_id'] != history.original
    assert advance(setup)['job_id'] == result['job_id']
    assert setup.calls['submit'] == 2
    assert setup.calls['stage'] == setup.calls['publish'] == 0
    assert setup.release == contract
    assert history.records[history.original] == original
    assert all(d in setup.store.decisions for d in decisions)
    new = history.records[result['job_id']]
    assert new['params'] == original['params']
    assert new['completion_provenance'] == dict(original['completion_provenance'], broker_job_id=history.original)
    assert new['idempotency_key'] != original['idempotency_key']
    assert any(d['decision_key'] == 'acquisition-v1-invocation-' + retry['decision_key']
               and d['payload']['job_id'] == result['job_id'] for d in setup.store.decisions)


@pytest.mark.parametrize('status', ['submitted', 'running', 'complete'])
def test_retry_never_replaces_nonfailed_base(setup, invocation_history, status):
    invocation_history.records[invocation_history.original]['status'] = status
    authorize_invocation_retry(setup)
    result = advance(setup)
    assert result['job_id'] == invocation_history.original
    assert result['state'] == ('complete' if status == 'complete' else 'working')
    assert setup.calls['submit'] == 1


@pytest.mark.parametrize('mutation', ['version', 'stage_id', 'operation', 'stage', 'kind'])
def test_stale_or_unrelated_retry_is_ignored(setup, invocation_history, mutation):
    decision = authorize_invocation_retry(setup, version=2 if mutation == 'version' else 1)
    if mutation == 'stage_id': decision['payload']['predecessor_stage_id'] = str(uuid.uuid4())
    if mutation == 'operation': decision['payload']['predecessor_operation_key'] = 'stale'
    if mutation == 'stage': decision['payload']['retry_stage'] = 'deployment'
    if mutation == 'kind': decision['kind'] = 'capability_selection'
    assert advance(setup)['state'] == 'failed'
    assert setup.calls['submit'] == 1


@pytest.mark.parametrize('field', ['tenant_id', 'project_id', 'release_id', 'contract_version', 'input_sha256'])
def test_retry_requires_verified_original_scope(setup, invocation_history, field):
    authorize_invocation_retry(setup)
    invocation_history.records[invocation_history.original]['completion_provenance'][field] = 'foreign'
    assert advance(setup)['state'] == 'failed'
    assert setup.calls['submit'] == 1


def test_retry_submission_uncertainty_reuses_admission_key(setup, invocation_history, monkeypatch):
    authorize_invocation_retry(setup)
    submit = jobs.submit_job
    keys = []

    def uncertain(**kwargs):
        keys.append(kwargs['idempotency_key'])
        if len(keys) == 1:
            raise TimeoutError('before durable submission')
        return submit(**kwargs)

    monkeypatch.setattr(jobs, 'submit_job', uncertain)
    assert advance(setup)['state'] == 'working'
    result = advance(setup)
    assert result['state'] == 'complete'
    assert keys[0] == keys[1]
    assert advance(setup)['job_id'] == result['job_id']
    assert setup.calls['submit'] == 2


def test_old_stage_authorization_cannot_create_job_for_new_failure(setup, invocation_history):
    authorize_invocation_retry(setup)
    setup.store.stages.append(dict(setup.store.stages[-1], stage_id=str(uuid.uuid4()), operation_key='new-failure'))
    assert advance(setup)['state'] == 'failed'
    assert setup.calls['submit'] == 1


@pytest.mark.parametrize('lost_response', [False, True])
def test_current_retry_skips_unadmitted_older_authorization(setup, invocation_history, lost_response):
    history = invocation_history
    original = deepcopy(history.records[history.original])
    contract = deepcopy(setup.release)
    decisions = deepcopy(setup.store.decisions)
    stale = authorize_invocation_retry(setup)
    current = authorize_invocation_retry(setup)
    setup.state['lost_response'] = lost_response

    result = advance(setup)

    assert result['state'] == 'complete'
    assert result['job_id'] != history.original
    assert advance(setup)['job_id'] == result['job_id']
    assert setup.calls['submit'] == 2
    assert setup.calls['stage'] == setup.calls['publish'] == 0
    assert len(history.rows) == len(history.records) == 2
    assert setup.release == contract
    assert history.records[history.original] == original
    assert all(d in setup.store.decisions for d in decisions)
    new = history.records[result['job_id']]
    assert new['params'] == original['params']
    assert new['completion_provenance'] == dict(original['completion_provenance'], broker_job_id=history.original)
    identity = [original['completion_provenance']['release_id'],
                original['completion_provenance']['contract_version'],
                original['completion_provenance']['input_sha256']]
    stale_key = 'completion-transform:' + hashlib.sha256(json.dumps(
        identity + [stale['decision_key']], separators=(',', ':')).encode()).hexdigest()
    current_key = 'completion-transform:' + hashlib.sha256(json.dumps(
        identity + [current['decision_key']], separators=(',', ':')).encode()).hexdigest()
    assert stale_key not in history.rows
    assert new['idempotency_key'] == current_key
    assert not any(d['decision_key'] == 'acquisition-v1-invocation-' + stale['decision_key']
                   for d in setup.store.decisions)
    assert any(d['decision_key'] == 'acquisition-v1-invocation-' + current['decision_key']
               and d['payload']['job_id'] == result['job_id'] for d in setup.store.decisions)


@pytest.mark.parametrize('status', ['submitted', 'running', 'complete'])
def test_later_retry_decision_does_not_replace_live_retry(setup, invocation_history, status):
    authorize_invocation_retry(setup)
    first = advance(setup)
    invocation_history.records[first['job_id']]['status'] = status
    authorize_invocation_retry(setup)
    assert advance(setup)['job_id'] == first['job_id']
    assert setup.calls['submit'] == 2


def test_second_retry_keeps_original_broker_identity_and_release_guard(setup, invocation_history):
    authorize_invocation_retry(setup)
    first = advance(setup)
    invocation_history.records[first['job_id']]['status'] = 'failed'
    authorize_invocation_retry(setup)
    setup.release['status'] = 'needs_approach'
    assert advance(setup)['state'] == 'working'
    assert setup.calls['submit'] == 2
    setup.release['status'] = 'active'
    second = advance(setup)
    assert second['state'] == 'complete' and second['job_id'] != first['job_id']
    assert invocation_history.records[second['job_id']]['completion_provenance']['broker_job_id'] == invocation_history.original
    assert setup.calls['submit'] == 3
