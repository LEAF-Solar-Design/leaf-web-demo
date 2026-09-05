"""HTTP proofs for the project campaign authority, using its store seam."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
from routers import campaigns as router

ORG, PROJECT, OTHER, PRINCIPAL = [str(uuid.uuid4()) for _ in range(4)]


class CampaignError(ValueError):
    def __init__(self, code, message='campaign error'):
        super().__init__(message)
        self.code = code


class CampaignConflict(CampaignError):
    pass


class CampaignUnavailable(CampaignError):
    pass


class FakeStore:
    CampaignError = CampaignError
    CampaignConflict = CampaignConflict
    CampaignUnavailable = CampaignUnavailable

    def __init__(self):
        self.campaigns, self.questions, self.answers = {}, {}, {}
        self.calls = []

    def submit_campaign(self, org, project, tenant, principal, **fields):
        self.calls.append(('submit', org, project, tenant, principal))
        for row in self.campaigns.values():
            if (row['org_id'], row['project_id'], row['idempotency_key']) == (org, project, fields['idempotency_key']):
                if (row['title'], row['prompt']) != (fields['title'], fields['prompt']):
                    raise CampaignConflict('idempotency_conflict')
                return {**row, 'replayed': True}
        row = dict(fields, campaign_id=str(uuid.uuid4()), org_id=org, project_id=project,
                   status='accepted', dispatch_ref=None, replayed=False)
        self.campaigns[row['campaign_id']] = row
        return dict(row)

    def get_campaign(self, org, project, campaign):
        row = self.campaigns.get(campaign)
        return row if row and (row['org_id'], row['project_id']) == (org, project) else None

    def _require(self, org, project, campaign):
        if self.get_campaign(org, project, campaign) is None:
            raise CampaignUnavailable('project_unavailable')

    def list_campaigns(self, org, project, limit):
        return [row for row in self.campaigns.values()
                if (row['org_id'], row['project_id']) == (org, project)][:limit]

    def ask_question(self, org, project, campaign, **fields):
        self._require(org, project, campaign)
        for row in self.questions.values():
            if row['campaign_id'] == campaign and row['question_key'] == fields['question_key']:
                if row['prompt'] != fields['prompt']:
                    raise CampaignConflict('question_conflict')
                return {**row, 'replayed': True}
        row = dict(fields, question_id=str(uuid.uuid4()), campaign_id=campaign, status='open', replayed=False)
        self.questions[row['question_id']] = row
        return dict(row)

    def list_questions(self, org, project, campaign):
        self._require(org, project, campaign)
        return [row for row in self.questions.values() if row['campaign_id'] == campaign]

    def answer_question(self, org, project, campaign, question, principal, *, answer):
        self.calls.append(('answer', org, project, principal))
        self._require(org, project, campaign)
        row = self.questions.get(question)
        if row is None or row['campaign_id'] != campaign:
            raise CampaignUnavailable('project_unavailable')
        if question in self.answers:
            existing = self.answers[question]
            if existing['answer'] != answer:
                raise CampaignConflict('answer_conflict')
            return {**existing, 'replayed': True}
        result = dict(answer_id=str(uuid.uuid4()), answer=answer, question_id=question, replayed=False)
        self.answers[question] = result
        row['status'] = 'answered'
        return dict(result)


class FakeExecution:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def read_execution(self, org, project, campaign, *, limit):
        self.calls.append((org, project, campaign, limit))
        self.store._require(org, project, campaign)
        hidden = dict(spec={'secret': True}, verify_command='private command', fence=9,
                      idempotency_key='private key', payload_fingerprint='private fingerprint',
                      active_attempt={'worker_id': 'worker', 'fence': 9,
                                      'budget_reservation_ref': 'budget', 'outward_operation_key': 'operation'},
                      result={'private': True}, artifact_ref='artifact', resource_identity='resource',
                      rollback_identity='rollback', payload={'private': True}, future_secret='secret',
                      dispatch={'action': 'mount-fleet-adapter'})
        return {
            'tasks': [dict(hidden, task_id='task', task_key='build', title='Build recipes', kind='build',
                           status='reconcile_required', stages=['build'], current_stage='build',
                           depends_on=['design'], blocked_by_questions=['question'], created_at='now', updated_at='now')],
            'pending_questions': [dict(hidden, question_id='question', question_key='format', prompt='Which format?',
                                       options=['PDF'], status='open', blocks_dispatch=True, task_ids=['task'], created_at='now')],
            'receipts': [dict(hidden, receipt_id='receipt', task_id='task', stage='build', outcome='unknown',
                              verified=False, created_at='now', reconciles_receipt_id=None)],
            'events': [dict(hidden, event_id='event', task_id='task', event_type='task_created', created_at='now')],
            'future_secret': 'secret',
        }


@pytest.fixture
def setup(monkeypatch):
    store = FakeStore()
    router.set_store(store)
    store.execution = FakeExecution(store)
    router.set_execution_store(store.execution)
    allowed = {PROJECT, OTHER}

    def access(tenant, project_id, *, write):
        assert tenant.org_id == ORG and write is True
        if project_id not in allowed:
            raise router.platform_link.ProjectSessionForbidden('wrong role')
        return tenant.org_id

    monkeypatch.setattr(router.platform_link, 'require_project_access', access)
    monkeypatch.setattr(router.platform_link, 'resolve_caller_binding',
                        lambda tenant: SimpleNamespace(binding_id=PRINCIPAL))
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: SimpleNamespace(
        tenant_id=ORG, org_id=ORG, subject='auth0|campaign-test')
    with TestClient(app) as client:
        yield client, store, allowed
    router.set_store(None)
    router.set_execution_store(None)


def _submit(client, **overrides):
    return client.post('/api/campaigns', headers={'Idempotency-Key': 'key'}, json={
        'project_id': PROJECT, 'title': 'ReciPDF', 'prompt': 'Organize recipes', **overrides})


def _question(client):
    campaign = _submit(client).json()['campaign']['campaign_id']
    response = client.post(f'/api/campaigns/{campaign}/questions', json={
        'project_id': PROJECT, 'question_key': 'organization', 'prompt': 'How to organize?',
        'options': ['tags', 'collections']})
    assert response.status_code == 201
    return campaign, response.json()['question']['question_id']


def test_execution_projection_and_limit_validation(setup):
    client, store, _ = setup
    campaign = _submit(client).json()['campaign']['campaign_id']
    url = f'/api/campaigns/{campaign}/execution'
    response = client.get(url, params={'project_id': PROJECT})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {'ok', 'execution'} and body['ok'] is True
    expected = {
        'tasks': {'task_id', 'task_key', 'title', 'kind', 'status', 'stages', 'current_stage',
                  'depends_on', 'blocked_by_questions', 'created_at', 'updated_at'},
        'questions': {'question_id', 'question_key', 'prompt', 'options', 'status', 'blocks_dispatch', 'task_ids', 'created_at'},
        'receipts': {'receipt_id', 'task_id', 'stage', 'outcome', 'verified', 'created_at', 'reconciles_receipt_id'},
        'events': {'event_id', 'task_id', 'event_type', 'created_at'},
    }
    assert set(body['execution']) == set(expected)
    for name, keys in expected.items():
        assert len(body['execution'][name]) == 1
        assert set(body['execution'][name][0]) == keys
    assert '"dispatch":' not in response.text
    assert store.execution.calls == [(ORG, PROJECT, campaign, 50)]
    for value, clamped in [('999', 200), ('0', 1), ('-20', 1)]:
        assert client.get(url, params={'project_id': PROJECT, 'limit': value}).status_code == 200
        assert store.execution.calls[-1] == (ORG, PROJECT, campaign, clamped)
    calls = len(store.execution.calls)
    assert client.get(url, params={'project_id': PROJECT, 'limit': 'bad'}).status_code == 400
    assert len(store.execution.calls) == calls


def test_execution_missing_scope_and_invalid_ids(setup):
    client, store, _ = setup
    campaign = _submit(client).json()['campaign']['campaign_id']
    foreign = client.get(f'/api/campaigns/{campaign}/execution', params={'project_id': OTHER})
    missing = client.get(f'/api/campaigns/{uuid.uuid4()}/execution', params={'project_id': OTHER})
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {'ok': False, 'error': {
        'error_code': 'project_unavailable', 'message': 'project is unavailable', 'retryable': False}}
    calls = len(store.execution.calls)
    for identifier, params in [('bad', {'project_id': PROJECT}), (campaign, {'project_id': 'bad'}), (campaign, {})]:
        assert client.get(f'/api/campaigns/{identifier}/execution', params=params).status_code == 400
    assert len(store.execution.calls) == calls


def test_execution_forbidden_never_calls_ledger(setup):
    client, store, allowed = setup
    campaign = _submit(client).json()['campaign']['campaign_id']
    allowed.remove(PROJECT)
    response = client.get(f'/api/campaigns/{campaign}/execution', params={'project_id': PROJECT})
    assert response.status_code == 403
    assert response.json()['error']['error_code'] == 'forbidden'
    assert store.execution.calls == []


def test_execution_store_failure_is_retryable_503(setup, monkeypatch):
    client, _, _ = setup
    campaign = _submit(client).json()['campaign']['campaign_id']

    def unavailable():
        raise RuntimeError('private database failure')

    monkeypatch.setattr(router, '_execution_store', unavailable)
    response = client.get(f'/api/campaigns/{campaign}/execution', params={'project_id': PROJECT})
    assert response.status_code == 503
    assert response.json() == {'ok': False, 'error': {'error_code': 'campaigns_unavailable',
        'message': 'campaign store is unavailable', 'retryable': True}}


def test_submission_replay_conflict_verified_identity_and_dispatch(setup):
    client, store, _ = setup
    first, replay = _submit(client), _submit(client)
    assert first.status_code == 201 and replay.status_code == 200
    row = first.json()['campaign']
    assert row['dispatch'] == {'available': False, 'action': 'mount-fleet-adapter'}
    assert replay.json()['campaign']['campaign_id'] == row['campaign_id']
    assert replay.json()['campaign']['replayed'] is True
    assert store.calls[0] == ('submit', ORG, PROJECT, ORG, PRINCIPAL)
    conflict = _submit(client, prompt='Different')
    assert conflict.status_code == 409
    assert conflict.json()['error']['error_code'] == 'idempotency_conflict'


def test_cross_project_answer_matches_missing_campaign_404(setup):
    client, store, allowed = setup
    campaign, question = _question(client)
    allowed.remove(PROJECT)  # Caller now holds owner on B only.
    url = f'/api/campaigns/{campaign}/questions/{question}/answer'
    foreign = client.post(url, json={'project_id': OTHER, 'answer': 'use tags'})
    missing = client.post(f'/api/campaigns/{uuid.uuid4()}/questions/{question}/answer',
                          json={'project_id': OTHER, 'answer': 'use tags'})
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {'ok': False, 'error': {
        'error_code': 'project_unavailable', 'message': 'project is unavailable', 'retryable': False}}
    assert store.answers == {}
    count = len(store.calls)
    forbidden = client.post(url, json={'project_id': PROJECT, 'answer': 'use tags'})
    assert forbidden.status_code == 403
    assert forbidden.json()['error']['error_code'] == 'forbidden'
    assert len(store.calls) == count  # Membership refusal never reaches the store.


def test_duplicate_answer_three_step_contract(setup):
    client, store, _ = setup
    campaign, question = _question(client)
    url = f'/api/campaigns/{campaign}/questions/{question}/answer'
    first = client.post(url, json={'project_id': PROJECT, 'answer': 'use tags'})
    replay = client.post(url, json={'project_id': PROJECT, 'answer': 'use tags'})
    conflict = client.post(url, json={'project_id': PROJECT, 'answer': 'use collections'})
    assert (first.status_code, replay.status_code, conflict.status_code) == (201, 200, 409)
    assert replay.json()['answer']['answer_id'] == first.json()['answer']['answer_id']
    assert replay.json()['answer']['replayed'] is True
    assert conflict.json()['error']['error_code'] == 'answer_conflict'
    assert len(store.answers) == 1 and store.questions[question]['status'] == 'answered'


def test_all_reads_and_question_replay_carry_dispatch(setup):
    client, store, _ = setup
    campaign, question = _question(client)
    for url, key, multiple in [('/api/campaigns', 'campaigns', True),
                              (f'/api/campaigns/{campaign}', 'campaign', False),
                              (f'/api/campaigns/{campaign}/questions', 'questions', True)]:
        response = client.get(url, params={'project_id': PROJECT})
        assert response.status_code == 200
        value = response.json()[key]
        for row in value if multiple else [value]:
            assert row['dispatch'] == {'available': False, 'action': 'mount-fleet-adapter'}
    body = {'project_id': PROJECT, 'question_key': 'organization', 'prompt': 'How to organize?'}
    replay = client.post(f'/api/campaigns/{campaign}/questions', json=body)
    assert replay.status_code == 200 and replay.json()['question']['replayed'] is True
    conflict = client.post(f'/api/campaigns/{campaign}/questions', json={**body, 'prompt': 'Different'})
    assert conflict.status_code == 409 and conflict.json()['error']['error_code'] == 'question_conflict'
    assert store.questions[question]['asked_by'] == 'operator'


def test_missing_and_oversized_fields_are_400(setup):
    client, store, _ = setup
    for body, headers in [({}, {}), ({'project_id': PROJECT, 'title': 'x', 'prompt': 'x'}, {}),
                          ({'project_id': PROJECT, 'title': 'x' * 201, 'prompt': 'x'}, {'Idempotency-Key': 'k'}),
                          ({'project_id': PROJECT, 'title': 'x', 'prompt': 'x' * 32769}, {'Idempotency-Key': 'k'}),
                          ({'project_id': PROJECT, 'title': 'x', 'prompt': 'x'}, {'Idempotency-Key': 'k' * 129})]:
        response = client.post('/api/campaigns', json=body, headers=headers)
        assert response.status_code == 400 and response.json()['error']['error_code'] == 'invalid_request'
    assert store.calls == []
    assert client.get('/api/campaigns').status_code == 400
    assert client.get('/api/campaigns', params={'project_id': PROJECT, 'limit': 'bad'}).status_code == 400


def test_question_and_answer_validation(setup):
    client, _, _ = setup
    campaign, question = _question(client)
    base = {'project_id': PROJECT, 'question_key': 'q', 'prompt': 'Question'}
    for fields in [{'options': [1]}, {'options': ['x'] * 17}, {'blocks_dispatch': 'yes'},
                   {'question_key': 'bad key'}, {'prompt': 'x' * 4097}]:
        response = client.post(f'/api/campaigns/{campaign}/questions', json={**base, **fields})
        assert response.status_code == 400
    for answer in ['', 'x' * 8193, None]:
        response = client.post(f'/api/campaigns/{campaign}/questions/{question}/answer',
                               json={'project_id': PROJECT, 'answer': answer})
        assert response.status_code == 400


def test_import_or_database_failure_is_retryable_503(setup, monkeypatch):
    client, store, _ = setup

    def unavailable(*args, **kwargs):
        raise RuntimeError('database is unavailable')

    monkeypatch.setattr(store, 'submit_campaign', unavailable)
    response = _submit(client)
    assert response.status_code == 503
    assert response.json()['error'] == {'error_code': 'campaigns_unavailable',
        'message': 'campaign store is unavailable', 'retryable': True}
    monkeypatch.setattr(router, '_store', unavailable)
    assert _submit(client).json() == response.json()


def test_lookup_failure_is_404_without_store_call(setup, monkeypatch):
    client, store, _ = setup

    def missing(*args, **kwargs):
        raise LookupError('missing project')

    monkeypatch.setattr(router.platform_link, 'require_project_access', missing)
    response = _submit(client)
    assert response.status_code == 404
    assert response.json()['error']['error_code'] == 'project_unavailable'
    assert store.calls == []
