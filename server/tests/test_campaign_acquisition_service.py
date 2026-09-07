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
        'local_only': True, 'capabilities': ['drawing.read'], 'version': '1.0.0',
        'params': {'type': 'object', 'properties': {'source_json': {'type': 'string',
                   'minLength': 1, 'maxLength': 1048576}}, 'required': ['source_json'],
                   'additionalProperties': False}}


class Store:
    def __init__(self, release):
        self.release = release
        self.decisions = []

    def get_release(self, org, project, campaign, release):
        assert tuple(map(str, (org, project, campaign, release))) == (ORG, PROJECT, CAMPAIGN, RELEASE)
        return {'release': deepcopy(self.release), 'decisions': deepcopy(self.decisions)}

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


def test_reuse_cumulative_publication_without_authoring(setup):
    result = advance(setup)
    assert result['state'] == 'complete'
    assert result['output_bytes'] == setup.expected
    assert result['metadata']['sha256'] == hashlib.sha256(setup.expected).hexdigest()
    assert setup.calls['stage'] == setup.calls['publish'] == 0
    assert setup.calls['submit'] == 1
    assert {r['decision_key'] for r in setup.store.decisions} == {
        'acquisition-v1-intent', 'acquisition-v1-publication', 'acquisition-v1-invocation'}


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
