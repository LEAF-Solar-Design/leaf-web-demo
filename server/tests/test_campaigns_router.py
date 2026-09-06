"""HTTP proofs for the project campaign authority, using its store seam."""
import uuid
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
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
        self.enrollments = {}
        self.calls = []

    def allowed_machines(self):
        return ['VM-C', 'VM-D']

    def list_enrollments(self, org, project, campaign):
        self._require(org, project, campaign)
        return [row for row in self.enrollments.values() if row['campaign_id'] == campaign]

    def request_enrollment(self, org, project, campaign, principal, *, machine_id):
        self._require(org, project, campaign)
        self.calls.append(('enroll', org, project, campaign, principal, machine_id))
        if machine_id not in self.allowed_machines():
            raise CampaignError('invalid_machine')
        for row in self.enrollments.values():
            if row['campaign_id'] == campaign and row['machine_id'] == machine_id:
                return {**row, 'replayed': True}
        row = dict(enrollment_id=str(uuid.uuid4()), org_id=org, project_id=project,
                   campaign_id=campaign, machine_id=machine_id, state='pending',
                   capability_link={'state': 'pending_link'})
        self.enrollments[row['enrollment_id']] = row
        return dict(row)

    def _change_enrollment(self, org, project, campaign, eid, principal, state):
        self._require(org, project, campaign)
        row = self.enrollments.get(eid)
        if not row or (row['org_id'], row['project_id'], row['campaign_id']) != (org, project, campaign):
            raise CampaignUnavailable('project_unavailable')
        if row['state'] == 'revoked' and state == 'enabled':
            raise CampaignConflict('enrollment_revoked')
        replayed = row['state'] == state
        row['state'] = state
        return {**row, 'replayed': replayed}

    def enable_enrollment(self, *args):
        return self._change_enrollment(*args, 'enabled')

    def revoke_enrollment(self, *args):
        return self._change_enrollment(*args, 'revoked')

    def resolve_worker_enrollment(self, eid, subject):
        row = self.enrollments.get(eid)
        if not row or row['state'] != 'enabled' or subject != os.environ.get('LEAF_CAMPAIGN_WORKER_SUBJECT'):
            raise CampaignError('worker_forbidden')
        self.calls.append(('recover', row['org_id'], row['project_id'], row['campaign_id']))
        return []

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
    monkeypatch.delenv('LEAF_PROJECT_SOURCE_PRODUCER', raising=False)
    store = FakeStore()
    router.set_store(store)
    router.set_enrollment_store(store)
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
    router.set_enrollment_store(None)


def _submit(client, **overrides):
    return client.post('/api/campaigns', headers={'Idempotency-Key': 'key'}, json={
        'project_id': PROJECT, 'title': 'ReciPDF', 'prompt': 'Organize recipes', **overrides})


@pytest.mark.parametrize('op', ['next', 'export', 'bind', 'admit', 'settle', 'recover'])
@pytest.mark.parametrize('credential', ['browser', 'admin', 'missing'])
def test_bridge_every_operation_requires_worker(setup, monkeypatch, op, credential):
    import auth
    client, _, _ = setup
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'worker-service')

    def verify(header):
        if credential == 'missing':
            raise HTTPException(status_code=401, detail='Authentication required')
        return {'sub': credential, 'role': 'admin'}

    monkeypatch.setattr(auth, 'verify_platform_token', verify)
    response = client.post('/internal/campaigns/bridge/' + op, json={'enrollment_id': str(uuid.uuid4())})
    assert response.status_code == (401 if credential == 'missing' else 403)


@pytest.mark.parametrize('op', ['next', 'export', 'bind', 'admit', 'settle', 'recover'])
def test_bridge_routes_worker_and_redacts_errors(setup, monkeypatch, op):
    client, _, _ = setup
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'worker-service'
    body = {'enrollment_id': str(uuid.uuid4())}
    calls = []

    def handle(operation, request, subject):
        calls.append((operation, request, subject))
        return {'ok': True}

    monkeypatch.setattr(router.campaign_bridge, 'handle', handle)
    assert client.post('/internal/campaigns/bridge/' + op, json=body).json() == {'ok': True}
    assert calls == [(op, body, 'worker-service')]

    def fail(*args):
        raise RuntimeError('PRIVATE_BRIDGE_SENTINEL')

    monkeypatch.setattr(router.campaign_bridge, 'handle', fail)
    response = client.post('/internal/campaigns/bridge/' + op, json=body)
    assert response.status_code == 503 and 'PRIVATE_BRIDGE_SENTINEL' not in response.text


@pytest.mark.parametrize('op,body', [
    ('unknown', {}), ('next', {'enrollment_id': 'bad'}),
    ('next', {'enrollment_id': 3}), ('next', {'enrollment_id': str(uuid.uuid4()), 'org_id': ORG}),
    ('export', {'enrollment_id': str(uuid.uuid4()), 'attempt_id': str(uuid.uuid4()), 'fence': True}),
    ('settle', {'enrollment_id': str(uuid.uuid4()), 'verified': True}),
    ('recover', []),
])
def test_bridge_closed_http_requests(setup, op, body):
    client, _, _ = setup
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'worker-service'
    assert client.post('/internal/campaigns/bridge/' + op, json=body).status_code == 400


@pytest.mark.parametrize('missing', ['LEAF_CAMPAIGN_BRIDGE', 'LEAF_CAMPAIGN_WORKER_SUBJECT',
                                     'LEAF_CAMPAIGN_ALLOWED_MACHINES'])
def test_bridge_defaults_off_without_full_configuration(setup, monkeypatch, missing):
    client, _, _ = setup
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'worker-service'
    monkeypatch.setenv('LEAF_CAMPAIGN_BRIDGE', 'on')
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'worker-service')
    monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', 'VM-C')
    monkeypatch.delenv(missing)
    response = client.post('/internal/campaigns/bridge/next', json={'enrollment_id': str(uuid.uuid4())})
    assert response.status_code == 503


@pytest.mark.parametrize('phase,error_type,code,status', [
    ('validation', ValueError, 'invalid_request', 400),
    ('limit', ValueError, 'invalid_request', 400),
    ('store', CampaignError, 'invalid_machine', 400),
    ('store', CampaignConflict, 'idempotency_conflict', 409),
    ('store', CampaignUnavailable, 'project_unavailable', 404),
    ('store', CampaignUnavailable, 'source_unavailable', 503),
    ('store', CampaignUnavailable, 'worker_unavailable', 503),
    ('store', CampaignUnavailable, 'campaigns_unavailable', 503),
])
def test_campaign_errors_keep_contract_without_exception_text(setup, monkeypatch, phase, error_type, code, status):
    client, store, _ = setup
    sentinel = 'PRIVATE_CAMPAIGN_EXCEPTION_SENTINEL'

    def fail(*args, **kwargs):
        if error_type is ValueError:
            raise ValueError(sentinel)
        raise error_type(code, sentinel)

    if phase == 'limit':
        response = client.get('/api/campaigns', params={'project_id': PROJECT, 'limit': sentinel})
    else:
        if phase == 'validation':
            monkeypatch.setattr(router, '_id', fail)
        else:
            monkeypatch.setattr(store, 'submit_campaign', fail)
        response = _submit(client)
    assert response.status_code == status
    assert response.json()['error']['error_code'] == code
    assert response.json()['error']['retryable'] is (status >= 500)
    assert sentinel not in response.text


def test_enrollment_human_routes_and_server_owned_fields(setup):
    client, store, allowed = setup
    campaign = _submit(client).json()['campaign']['campaign_id']
    url = f'/api/campaigns/{campaign}/enrollments'
    payload = {'project_id': PROJECT, 'machine_id': 'VM-C', 'service_subject': 'forged',
               'publication_id': 'forged', 'first_invocation_receipt_id': 'forged'}
    listed = client.get(url, params={'project_id': PROJECT})
    assert listed.json()['enrollment']['allowed_machines'] == ['VM-C', 'VM-D']
    first = client.post(url, json=payload)
    assert first.status_code == 201
    row = first.json()['enrollment']
    assert row['capability_link']['state'] == 'pending_link'
    assert 'service_subject' not in row and 'publication_id' not in row
    assert store.calls[-1] == ('enroll', ORG, PROJECT, campaign, PRINCIPAL, 'VM-C')
    assert client.post(url, json=payload).status_code == 200
    assert len(store.enrollments) == 1
    assert client.post(url, json={**payload, 'machine_id': 'other'}).status_code == 400
    eid = row['enrollment_id']
    for action in ('enable', 'revoke'):
        assert client.post(f'{url}/{eid}/{action}', json={'project_id': OTHER}).status_code == 404
    allowed.remove(PROJECT)
    for action in ('enable', 'revoke'):
        assert client.post(f'{url}/{eid}/{action}', json={'project_id': PROJECT}).status_code == 403
    assert client.post(url, json=payload).status_code == 403


def test_worker_recovery_always_verifies_bearer_and_derives_scope(setup, monkeypatch):
    import auth
    client, store, _ = setup
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'service-worker')
    monkeypatch.setenv('LEAF_AUTH_ENABLED', '0')
    verified = []

    def verify(header):
        verified.append(header)
        if header not in ('Bearer signed', 'Bearer wrong'):
            raise HTTPException(401, 'Invalid bearer')
        return {'sub': 'service-worker' if header == 'Bearer signed' else 'wrong'}

    monkeypatch.setattr(auth, 'verify_platform_token', verify)
    campaign = _submit(client).json()['campaign']['campaign_id']
    url = f'/api/campaigns/{campaign}/enrollments'
    row = client.post(url, json={'project_id': PROJECT, 'machine_id': 'VM-C'}).json()['enrollment']
    eid = row['enrollment_id']
    recover = '/internal/campaign-worker/recover'
    body = {'enrollment_id': eid}
    assert client.post(recover, json=body).status_code == 401
    assert client.post(recover, json=body, headers={'Authorization': 'Bearer unsigned'}).status_code == 401
    assert client.post(recover, json=body, headers={'Authorization': 'Bearer wrong'}).status_code == 403
    headers = {'Authorization': 'Bearer signed'}
    assert client.post(recover, json=body, headers=headers).status_code == 403
    assert client.post(f'{url}/{eid}/enable', json={'project_id': PROJECT}).status_code == 200
    assert client.post(recover, json=body, headers=headers).json() == {'ok': True, 'pending_remote_bindings': []}
    assert store.calls[-1] == ('recover', ORG, PROJECT, campaign)
    assert client.post(recover, json={**body, 'project_id': OTHER}, headers=headers).status_code == 400
    assert client.post(recover, json={'enrollment_id': str(uuid.uuid4())}, headers=headers).status_code == 403
    assert client.post(f'{url}/{eid}/revoke', json={'project_id': PROJECT}).status_code == 200
    assert client.post(recover, json=body, headers=headers).status_code == 403
    assert client.post(url, json={'project_id': PROJECT, 'machine_id': 'VM-C'}).json()['enrollment']['state'] == 'revoked'
    assert None in verified and 'Bearer unsigned' in verified


def test_worker_uses_real_signature_verifier_with_tenant_auth_off(setup, monkeypatch):
    import time
    import auth
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    client, _, _ = setup
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth, '_signing_key', lambda token: key.public_key())
    monkeypatch.setenv('LEAF_AUTH_LIVE', '0')
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'service-worker')
    campaign = _submit(client).json()['campaign']['campaign_id']
    url = f'/api/campaigns/{campaign}/enrollments'
    row = client.post(url, json={'project_id': PROJECT, 'machine_id': 'VM-C'}).json()['enrollment']
    eid = row['enrollment_id']
    client.post(f'{url}/{eid}/enable', json={'project_id': PROJECT})
    claims = {'sub': 'service-worker', 'iat': int(time.time()), 'exp': int(time.time()) + 300,
              'iss': auth.issuer(), 'aud': auth.audience()}
    recover = '/internal/campaign-worker/recover'
    for token, status in (
        (jwt.encode(claims, key, algorithm='RS256'), 200),
        (jwt.encode(claims, '', algorithm='none'), 401),
        (jwt.encode({**claims, 'sub': 'wrong'}, key, algorithm='RS256'), 403),
    ):
        response = client.post(recover, json={'enrollment_id': eid}, headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == status
    assert client.post(recover, json={'enrollment_id': eid}).status_code == 401


def test_next_worker_default_off_closed_body_and_auth(setup, monkeypatch):
    import auth
    client, store, _ = setup
    monkeypatch.delenv('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', raising=False)
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'service-worker')

    def verify(header):
        if header not in ('Bearer signed', 'Bearer wrong'):
            raise HTTPException(401, 'Invalid bearer')
        return {'sub': 'service-worker' if header == 'Bearer signed' else 'wrong'}

    monkeypatch.setattr(auth, 'verify_platform_token', verify)
    calls = []
    eid = str(uuid.uuid4())
    result = dict(ok=True, kind='claimed', enrollment_id=eid,
                  scope=dict(org_id=ORG, project_id=PROJECT, campaign_id=str(uuid.uuid4()), machine_id='VM-C'),
                  attempt=dict(attempt_id=str(uuid.uuid4()), fence=1, stage='implementation',
                               deadline_at='future', attempt_token='one-use'),
                  plan_task=dict(task_key='campaign-plan'))

    def next_work(enrollment_id, subject):
        calls.append((enrollment_id, subject))
        return result

    monkeypatch.setattr(router.campaign_worker_service, 'next_work', next_work)
    url, body, headers = '/internal/campaign-worker/next', {'enrollment_id': eid}, {'Authorization': 'Bearer signed'}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={'Authorization': 'Bearer wrong'}).status_code == 403
    for value in (None, '', 'off', 'ON', ' on ', 'true'):
        if value is None:
            monkeypatch.delenv('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', raising=False)
        else:
            monkeypatch.setenv('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', value)
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 503 and response.json()['error']['error_code'] == 'producer_disabled'
    assert calls == [] and store.calls == []
    monkeypatch.setenv('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', 'on')
    for invalid in ({}, {'enrollment_id': ''}, {'enrollment_id': 'bad'},
                    dict(body, lease_seconds=30), dict(body, project_id=OTHER),
                    dict(body, attempt_token='forged'), dict(body, owned_paths=['src']),
                    dict(body, budget=1)):
        assert client.post(url, json=invalid, headers=headers).status_code == 400
    assert calls == []
    assert client.post(url, json=body, headers=headers).json() == result
    assert calls == [(eid, 'service-worker')]


@pytest.mark.parametrize('code,status', [('worker_forbidden', 403), ('project_unavailable', 403),
    ('plan_source_conflict', 409), ('task_conflict', 409), ('prompt_too_large', 409),
    ('source_unavailable', 503), ('invalid_request', 400), ('campaigns_unavailable', 503)])
def test_next_worker_sanitized_campaign_errors(setup, monkeypatch, code, status):
    client, _, _ = setup
    monkeypatch.setenv('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', 'on')
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'service-worker'

    def fail(*args):
        raise CampaignError(code, 'private database and provider details')

    monkeypatch.setattr(router.campaign_worker_service, 'next_work', fail)
    response = client.post('/internal/campaign-worker/next', json={'enrollment_id': str(uuid.uuid4())})
    assert response.status_code == status and 'private' not in response.text


@pytest.mark.parametrize('error,status,code', [
    (router.project_repository_source.SourceConflict, 409, 'source_conflict'),
    (router.project_repository_source.SourceUnavailable, 503, 'source_unavailable'),
    (RuntimeError, 503, 'campaigns_unavailable')])
def test_next_worker_sanitized_source_errors(setup, monkeypatch, error, status, code):
    client, _, _ = setup
    monkeypatch.setenv('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', 'on')
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'service-worker'

    def fail(*args):
        raise error('private source path')

    monkeypatch.setattr(router.campaign_worker_service, 'next_work', fail)
    response = client.post('/internal/campaign-worker/next', json={'enrollment_id': str(uuid.uuid4())})
    assert response.status_code == status and response.json()['error']['error_code'] == code
    assert 'private' not in response.text


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


def test_source_failure_preserves_admission_and_retry_uses_persisted_prompt(setup, monkeypatch):
    client, store, allowed = setup
    monkeypatch.setenv('LEAF_PROJECT_SOURCE_PRODUCER', 'on')
    calls = []
    def produce(tenant, org, project, prompt):
        assert (tenant, org, project) == (ORG, ORG, PROJECT)
        assert len(store.campaigns) == 1
        assert prompt == next(iter(store.campaigns.values()))['prompt']
        calls.append(prompt)
        if len(calls) == 1:
            raise router.project_repository_source.SourceUnavailable('private details')
        return dict(source_commit='a' * 40, source_tree='b' * 40, seed_digest='c' * 64, replayed=True)
    monkeypatch.setattr(router.project_repository_source, 'initialize_project_source', produce)
    failure = _submit(client)
    assert failure.status_code == 503 and failure.json()['error']['error_code'] == 'source_unavailable'
    assert 'private' not in failure.text and len(store.campaigns) == 1
    retry = _submit(client)
    assert retry.status_code == 200
    assert retry.json()['campaign']['source']['source_commit'] == 'a' * 40
    assert len(calls) == 2
    assert _submit(client, prompt='Changed').json()['error']['error_code'] == 'idempotency_conflict'
    assert len(calls) == 2
    assert client.get('/api/campaigns', params={'project_id': PROJECT}).status_code == 200
    assert len(calls) == 2
    allowed.remove(PROJECT)
    before = len(store.calls)
    assert _submit(client).status_code == 403
    assert len(store.calls) == before and len(calls) == 2


def test_source_feature_off_and_conflict(setup, monkeypatch):
    client, store, _ = setup
    calls = []
    def conflict(*args):
        calls.append(args)
        raise router.project_repository_source.SourceConflict('private path')
    monkeypatch.setattr(router.project_repository_source, 'initialize_project_source', conflict)
    assert 'source' not in _submit(client).json()['campaign']
    assert not calls
    monkeypatch.setenv('LEAF_PROJECT_SOURCE_PRODUCER', 'on')
    result = _submit(client)
    assert result.status_code == 409 and result.json()['error']['error_code'] == 'source_conflict'
    assert 'private' not in result.text and len(store.campaigns) == 1
