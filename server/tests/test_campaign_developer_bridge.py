"""Offline broker transaction boundaries; no claim of installed AWS execution."""
import copy
import hashlib
import io
import json

import pytest

import campaign_bridge
import campaign_developer_bridge as bridge
from developer_jobs_transport import TransportError

ORG, PROJECT, CAMPAIGN, TASK, PARENT, ENROLLMENT = [str(__import__('uuid').UUID(int=i)) for i in range(1, 7)]


class S3:
    def __init__(self, events):
        self.events, self.objects = events, {}
        self.lost = False

    def put_object(self, **kwargs):
        self.events.append('put')
        key = (kwargs['Bucket'], kwargs['Key'])
        assert kwargs['IfNoneMatch'] == '*'
        if key in self.objects:
            raise RuntimeError('conflict')
        self.objects[key] = (kwargs['Body'], 'v1')
        if self.lost:
            self.lost = False
            raise TimeoutError('response lost')
        return {'VersionId': 'v1'}

    def head_object(self, **kwargs):
        self.events.append('head')
        return {'VersionId': self.objects[(kwargs['Bucket'], kwargs['Key'])][1]}

    def get_object(self, **kwargs):
        self.events.append('get')
        data, version = self.objects[(kwargs['Bucket'], kwargs['Key'])]
        assert kwargs['VersionId'] == version
        return {'VersionId': version, 'ContentLength': len(data), 'Body': io.BytesIO(data)}

    def receipt(self, value):
        raw = bridge._raw(value)
        self.objects[('acceptance', 'walks/receipt.json')] = (raw, 'v2')
        return dict(bucket='acceptance', key='walks/receipt.json', version_id='v2',
                    sha256=hashlib.sha256(raw).hexdigest())


class Ledger:
    def __init__(self, events):
        self.events, self.row = events, None

    def read(self, *args):
        return copy.deepcopy(self.row)

    def prepare(self, *args, **kwargs):
        self.events.append('reserve')
        request = copy.deepcopy(kwargs['request'])
        op = bridge._operation(request)
        self.row = dict(request=request, reservation_microusd=kwargs['reservation_microusd'],
                        developer_attempt_id=op[:48] + '-00000001', current_sequence=1,
                        expected_attempt_id=None, settled_at=None)
        return copy.deepcopy(self.row)

    def record_admission(self, *args, **kwargs):
        self.events.append('admission')
        assert kwargs['developer_attempt_id'] == self.row['developer_attempt_id']

    def settle(self, *args, **kwargs):
        self.events.append('settle')
        self.row.update(settled_at='now', cost_microusd=kwargs['cost_microusd'],
                        receipt_ref=kwargs['receipt_ref'])

    def reserve_retry(self, *args, **kwargs):
        self.events.append('reserve_retry')
        assert self.row['settled_at'] is not None
        self.row.update(developer_attempt_id=kwargs['operation_id'][:48] + '-00000002',
                        current_sequence=2, expected_attempt_id=kwargs['expected_attempt_id'], settled_at=None)
        return copy.deepcopy(self.row)


class Transport:
    def __init__(self, events, ledger):
        self.events, self.ledger = events, ledger
        self.response, self.error = None, None

    def request(self, body):
        self.events.append(body['action'])
        if self.error:
            raise self.error
        if self.response is not None:
            return copy.deepcopy(self.response)
        return result(self.ledger.row)


def result(row, state='RUNNING'):
    request = row['request']
    op = bridge._operation(request)
    binding = {k: v for k, v in request.items() if k != 'idempotency_key'}
    binding.update(operation_id=op, attempt_id=row['developer_attempt_id'], registry_digest='c' * 64,
                   reservation={'amount_microusd': row['reservation_microusd']}, resource_locks=['owned-worker'])
    attempt = dict(operation_id=op, attempt_id=row['developer_attempt_id'], binding=binding,
                   state=state, resources_reconciled=state in bridge.TERMINAL, evidence=None)
    return dict(operation_id=op, request=copy.deepcopy(request), registry_digest='c' * 64,
                current_attempt=row['current_sequence'], state=state, attempts=[attempt], next_cursor=None)


@pytest.fixture
def fixture():
    events = []
    scope = dict(org=ORG, project=PROJECT, campaign=CAMPAIGN, enrollment_id=ENROLLMENT)
    control = {'stale': False, 'forbidden': False}
    def resolve(enrollment, campaign, task, parent, fence, active, subject):
        events.append('scope')
        if control['forbidden'] or enrollment != ENROLLMENT or campaign != CAMPAIGN or subject != 'service':
            raise bridge.BridgeError(403)
        if task:
            assert task == TASK and parent == PARENT and fence == 1
            if active and control['stale']:
                raise bridge.BridgeError(409)
        return scope, {'limit_microusd': 1000, 'spent_microusd': 0, 'reserved_microusd': 0}
    profile = dict(version=1, profile_id='synthetic', profile_revision=1,
                   max_runtime_seconds=90, max_cost_microusd=100, viewport={'width':1280,'height':720})
    manifest = dict(version=1, profile_id='synthetic', profile_revision=1, profile_digest=bridge._sha(profile),
        campaign_id=CAMPAIGN, task_id=TASK, parent_attempt_id=PARENT, parent_attempt_fence=1,
        max_cost_microusd=100, max_runtime_seconds=90)
    job = dict(org_id=ORG, project_id=PROJECT, repository_id='repo', job_name='browser-v1', environment='staging',
        commit_sha='a' * 40, source_bucket='source', source_prefix='walks/', acceptance_bucket='acceptance',
        acceptance_prefix='walks/', acceptance_producer='producer-v1', runtime_seconds=90,
        reservation_microusd=100, browser_startup_verified=True, profile_id='synthetic',
        media_bucket='media', media_prefix='walks/media/')
    job['runtime_identity'] = dict(chromium_sha256='1' * 64, chromium_version='123.0.0.1',
        lock_sha256='2' * 64, executor_sha256='3' * 64, playwright_version='1.2.3',
        executor_package_digest='4' * 64)
    config = dict(version=1, client_version=1, enabled=True,
                  endpoint='https://abcdefghij.execute-api.us-east-1.amazonaws.com/v1/developer/jobs', region='us-east-1',
                  role_arn=None, registry_digest='c' * 64, profiles={'synthetic': profile}, jobs=[job])
    ledger, s3 = Ledger(events), S3(events)
    transport = Transport(events, ledger)
    service = bridge.WalkBridge(config, ledger, resolve=resolve, clients=lambda config: (s3, transport))
    return service, ledger, s3, transport, manifest, control, events


def prepare(f):
    return f[0].handle('walk_prepare', {'enrollment_id': ENROLLMENT, 'manifest': f[4]}, 'service')['result']


def body(action=None, **extra):
    value = dict(enrollment_id=ENROLLMENT, campaign_id=CAMPAIGN, task_id=TASK)
    if action:
        value['action'] = action
    return {**value, **extra}


def terminal(f, state='SUCCEEDED', **updates):
    service, ledger, s3, transport, *_ = f
    response = result(ledger.row, state)
    attempt = response['attempts'][0]
    receipt = dict(binding=attempt['binding'], producer='producer-v1', status=state,
                   resources_reconciled=True, workers_terminal=True,
                   resource_outcomes=[{'key': 'owned-worker', 'state': 'reconciled'}],
                   cost_basis='actual', actual_cost_microusd=40, billed_cost_verified=True)
    receipt.update(updates)
    attempt['evidence'] = s3.receipt(receipt)
    transport.response = response
    return receipt


@pytest.mark.parametrize('control', ['stale', 'forbidden'])
def test_scope_and_parent_reject_before_source_network(fixture, control):
    fixture[5][control] = True
    with pytest.raises(bridge.BridgeError):
        prepare(fixture)
    assert 'put' not in fixture[6] and 'reserve' not in fixture[6]


def test_duplicate_prepare_after_deadline_preserves_one_binding(fixture):
    first = prepare(fixture)
    fixture[5]['stale'] = True
    assert prepare(fixture) == first
    assert fixture[6].count('put') == 1 and fixture[6].count('reserve') == 1


def test_source_lost_response_uses_exact_version_readback(fixture):
    fixture[2].lost = True
    request = prepare(fixture)
    assert request['source']['version_id'] == 'v1'
    assert fixture[6].index('head') < fixture[6].index('reserve')


def test_reservation_precedes_admission_and_source_not_caller_selected(fixture):
    request = prepare(fixture)
    operation = bridge._operation(request)
    fixture[0].handle('walk_request', body('submit', operation_id=operation), 'service')
    assert fixture[6].index('reserve') < fixture[6].index('submit') < fixture[6].index('admission')
    with pytest.raises(bridge.BridgeError):
        fixture[0].handle('walk_request', body('submit', operation_id=operation, source={}), 'service')


def test_source_readback_hash_mismatch_never_reserves(fixture):
    original = fixture[2].get_object
    def corrupt(**kwargs):
        obj = original(**kwargs)
        obj.update(Body=io.BytesIO(b'corrupt'), ContentLength=7)
        return obj
    fixture[2].get_object = corrupt
    with pytest.raises(bridge.BridgeError):
        prepare(fixture)
    assert 'reserve' not in fixture[6]


def test_registry_mismatch_never_records_admission(fixture):
    request = prepare(fixture)
    fixture[3].response = result(fixture[1].row)
    fixture[3].response['registry_digest'] = 'd' * 64
    with pytest.raises(bridge.BridgeError):
        fixture[0].handle('walk_request', body('submit', operation_id=bridge._operation(request)), 'service')
    assert 'admission' not in fixture[6]


@pytest.mark.parametrize('changes', [dict(producer='wrong'), dict(binding={}),
    dict(resources_reconciled=False), dict(workers_terminal=False), dict(resource_outcomes=[])])
def test_untrusted_terminal_receipt_retains_reservation(fixture, changes):
    prepare(fixture)
    terminal(fixture, **changes)
    with pytest.raises(bridge.BridgeError):
        fixture[0].handle('walk_receipt', body(), 'service')
    assert 'settle' not in fixture[6]


@pytest.mark.parametrize('pending,amount', [(False, 40), (True, 100)])
def test_actual_and_pending_billing_have_distinct_accounting(fixture, pending, amount):
    prepare(fixture)
    updates = dict(cost_basis='reserved_maximum_pending_billing', billed_cost_verified=False,
                   actual_cost_microusd=None, estimated_cost_microusd=20) if pending else {}
    terminal(fixture, **updates)
    output = fixture[0].handle('walk_receipt', body(), 'service')['result']
    assert fixture[1].row['cost_microusd'] == amount
    assert output['accounting'] == ('reserved_maximum_pending_billing' if pending else 'actual')
    assert output['acceptance'] == 'pending' and output['cleanup'] == 'verified'
    fixture[0].handle('walk_receipt', body(), 'service')
    assert fixture[6].count('settle') == 1


def test_retry_settles_then_reserves_before_network(fixture):
    request = prepare(fixture)
    terminal(fixture, state='FAILED')
    transport = fixture[3]
    original = transport.request
    def retry_result(payload):
        if payload['action'] == 'retry':
            transport.response = None
        return original(payload)
    transport.request = retry_result
    expected = fixture[1].row['developer_attempt_id']
    fixture[0].handle('walk_request', body('retry', operation_id=bridge._operation(request),
                                          expected_attempt_id=expected), 'service')
    events = fixture[6]
    assert events.index('settle') < events.index('reserve_retry') < events.index('retry')


def test_only_real_inspect_404_maps_missing(fixture):
    request = prepare(fixture)
    fixture[3].error = TransportError('developer_http_error', 404)
    output = fixture[0].handle('walk_request', body('inspect', operation_id=bridge._operation(request)), 'service')
    assert output == {'ok': True, 'result': {'error': 'operation_not_found'}}
    fixture[3].error = TransportError('developer_http_error', 503)
    with pytest.raises(TransportError):
        fixture[0].handle('walk_request', body('inspect', operation_id=bridge._operation(request)), 'service')


def test_old_bridge_validation_unchanged():
    assert campaign_bridge._validate('next', {'enrollment_id': ENROLLMENT}) == {'enrollment_id': ENROLLMENT}
    with pytest.raises(campaign_bridge.BridgeError):
        campaign_bridge._validate('next', {'enrollment_id': ENROLLMENT, 'roles': ['admin']})


def test_pending_receipt_does_not_settle(fixture):
    prepare(fixture)
    output = fixture[0].handle('walk_receipt', body(), 'service')['result']
    assert output['accounting'] == 'reserved' and output['cleanup'] == 'unverified'
    assert output['acceptance'] == 'pending' and 'settle' not in fixture[6]


def test_reserved_retry_without_remote_post_can_be_inspected_and_recovered(fixture):
    request = prepare(fixture)
    terminal(fixture, state='FAILED')
    operation = bridge._operation(request)
    old_attempt = fixture[1].row['developer_attempt_id']
    fixture[1].row.update(developer_attempt_id=operation[:48] + '-00000002', current_sequence=2,
                          expected_attempt_id=old_attempt)
    response = fixture[0].handle('walk_request', body('inspect', operation_id=operation), 'service')
    assert response['result']['current_attempt'] == 1
    assert 'admission' not in fixture[6]
    receipt = fixture[0].handle('walk_receipt', body(), 'service')['result']
    assert receipt['accounting'] == 'reserved' and receipt['cleanup'] == 'unverified'
    assert 'settle' not in fixture[6]


@pytest.mark.parametrize('action', ['inspect', 'recover', 'cancel'])
def test_disabled_execution_preserves_admitted_recovery(fixture, action):
    request = prepare(fixture)
    fixture[0].config['enabled'] = False
    loaded = fixture[0].handle('walk_read', body(), 'service')['result']
    assert loaded == request
    output = fixture[0].handle('walk_request', body(action, operation_id=bridge._operation(request)), 'service')
    assert output['result']['request'] == request and action in fixture[6]


@pytest.mark.parametrize('action', ['prepare', 'submit', 'retry'])
def test_disabled_execution_blocks_new_work_before_network(fixture, action):
    request = prepare(fixture)
    fixture[0].config['enabled'] = False
    fixture[6].clear()
    with pytest.raises(bridge.BridgeError) as error:
        if action == 'prepare':
            prepare(fixture)
        else:
            extra = {'expected_attempt_id': fixture[1].row['developer_attempt_id']} if action == 'retry' else {}
            fixture[0].handle('walk_request', body(action, operation_id=bridge._operation(request), **extra), 'service')
    assert error.value.status == 503
    assert not any(event in fixture[6] for event in ('put', 'submit', 'inspect', 'retry', 'reserve_retry'))


def test_disabled_execution_and_unrelated_registry_update_preserve_settlement(fixture):
    request = prepare(fixture)
    terminal(fixture)
    fixture[0].config.update(enabled=False, registry_digest='d' * 64)
    inspection = fixture[0].handle('walk_request', body('inspect', operation_id=bridge._operation(request)), 'service')
    assert inspection['result']['registry_digest'] == 'c' * 64
    output = fixture[0].handle('walk_receipt', body(), 'service')['result']
    assert output['accounting'] == 'actual' and output['cleanup'] == 'verified'
    assert fixture[1].row['cost_microusd'] == 40
    assert output['upload'] == 'pending' and output['acceptance'] == 'pending'


@pytest.mark.parametrize('drift', ['malformed_registry', 'binding_registry', 'job_generation', 'source'])
def test_unrelated_registry_update_does_not_allow_frozen_binding_drift(fixture, drift):
    request = prepare(fixture)
    terminal(fixture)
    fixture[0].config['registry_digest'] = 'd' * 64
    response = fixture[3].response
    binding = response['attempts'][0]['binding']
    if drift == 'malformed_registry':
        response['registry_digest'] = 'not-a-sha'
    elif drift == 'binding_registry':
        binding['registry_digest'] = 'e' * 64
    elif drift == 'job_generation':
        binding['job_name'] = 'another-generation'
    else:
        binding['source'] = {**request['source'], 'sha256': 'f' * 64}
    with pytest.raises(bridge.BridgeError):
        fixture[0].handle('walk_receipt', body(), 'service')
    assert 'settle' not in fixture[6]


def test_new_submit_keeps_current_registry_gate_even_with_consistent_remote_binding(fixture):
    request = prepare(fixture)
    fixture[3].response = result(fixture[1].row)
    fixture[3].response['registry_digest'] = 'd' * 64
    fixture[3].response['attempts'][0]['binding']['registry_digest'] = 'd' * 64
    with pytest.raises(bridge.BridgeError):
        fixture[0].handle('walk_request', body('submit', operation_id=bridge._operation(request)), 'service')
    assert 'admission' not in fixture[6]


def test_terminal_media_reference_is_not_verified_upload(fixture):
    prepare(fixture)
    media = dict(bucket='media', key='bundle.json', version_id='v1', sha256='e' * 64)
    terminal(fixture, media_ref=media)
    output = fixture[0].handle('walk_receipt', body(), 'service')['result']
    assert output['evidence']['media_ref'] == media
    assert output['cleanup'] == 'verified' and output['upload'] == 'pending'
    assert output['acceptance'] == 'pending'


def test_doctor_returns_scoped_nonsecret_runtime_installation(fixture, monkeypatch):
    import boto3
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail('discovery attempted provider or HTTP client creation')
    monkeypatch.setattr(boto3, 'Session', forbidden)
    monkeypatch.setattr(requests, 'Session', forbidden)
    service = fixture[0]
    job = service.config['jobs'][0]
    foreign_org = copy.deepcopy(job)
    foreign_org.update(org_id=str(__import__('uuid').UUID(int=90)), job_name='foreign-org-job')
    foreign_project = copy.deepcopy(job)
    foreign_project.update(project_id=str(__import__('uuid').UUID(int=91)), job_name='foreign-project-job')
    service.config['jobs'].extend([foreign_org, foreign_project])
    service.config['role_arn'] = 'arn:aws:iam::123456789012:role/private-runtime-role'
    service.config['profiles']['synthetic']['synthetic_messages'] = ['private synthetic material']
    output = service.handle('walk_doctor', {'enrollment_id': ENROLLMENT, 'campaign_id': CAMPAIGN}, 'service')['result']
    assert output['state'] == 'ready'
    install = output['installation']
    assert set(install) == {'version', 'client_version', 'endpoint', 'registry_digest', 'capabilities', 'jobs'}
    assert install['version'] == install['client_version'] == 1
    assert install['capabilities'] == ['browser.walk']
    assert len(install['jobs']) == 1
    assert set(install['jobs'][0]) == set(bridge.DISCOVERY_JOB_FIELDS)
    assert install['jobs'][0]['runtime_identity'] == job['runtime_identity']
    encoded = json.dumps(output)
    for private in ('role_arn', 'source_bucket', 'acceptance_bucket', 'media_bucket', 'reservation_microusd',
                    'source_prefix', 'acceptance_prefix', 'media_prefix', 'profiles', 'synthetic_messages',
                    'private-runtime-role', 'private synthetic material', 'foreign-org-job', 'foreign-project-job'):
        assert private not in encoded
    assert fixture[6] == ['scope']


@pytest.mark.parametrize('fault', ['missing', 'extra', 'bad_sha', 'bool_sha', 'bad_version', 'missing_field'])
def test_installation_rejects_missing_or_wrong_runtime_identity(fixture, tmp_path, monkeypatch, fault):
    config = copy.deepcopy(fixture[0].config)
    job = config['jobs'][0]
    if fault == 'missing':
        del job['runtime_identity']
    elif fault == 'extra':
        job['runtime_identity']['credential_path'] = 'private'
    elif fault == 'bad_sha':
        job['runtime_identity']['executor_package_digest'] = 'not-a-sha'
    elif fault == 'bool_sha':
        job['runtime_identity']['chromium_sha256'] = True
    elif fault == 'bad_version':
        job['runtime_identity']['playwright_version'] = 'latest'
    else:
        del job['runtime_identity']['lock_sha256']
    path = tmp_path / 'installation.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv('LEAF_WALK_INSTALLATION_FILE', str(path))
    with pytest.raises(bridge.BridgeError) as error:
        bridge.installation()
    assert error.value.status == 503


def test_installation_validates_and_doctor_rejects_bad_endpoint(fixture, tmp_path, monkeypatch):
    config = copy.deepcopy(fixture[0].config)
    path = tmp_path / 'installation.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv('LEAF_WALK_INSTALLATION_FILE', str(path))
    assert bridge.installation() == config
    fixture[0].config['endpoint'] = 'https://user:private@example.org/jobs'
    with pytest.raises(bridge.BridgeError):
        fixture[0].doctor({'enrollment_id': ENROLLMENT, 'campaign_id': CAMPAIGN}, 'service')


def test_discovery_job_count_bound(fixture):
    fixture[0].config['jobs'] *= 33
    with pytest.raises(bridge.BridgeError):
        fixture[0].doctor({'enrollment_id': ENROLLMENT, 'campaign_id': CAMPAIGN}, 'service')


def test_disabled_doctor_still_discovers_runtime_pins(fixture):
    fixture[0].config['enabled'] = False
    output = fixture[0].doctor({'enrollment_id': ENROLLMENT, 'campaign_id': CAMPAIGN}, 'service')
    assert output['state'] == 'execution_disabled'
    assert output['installation']['jobs'][0]['runtime_identity']['executor_package_digest'] == '4' * 64


def test_media_failure_preserves_verified_terminal_accounting(fixture, monkeypatch):
    prepare(fixture)
    media_ref = dict(bucket='media',key='unavailable/index.json',version_id='v1',sha256='e'*64)
    terminal(fixture,media_ref=media_ref)
    def fail(*args):
        raise RuntimeError('private provider error')
    monkeypatch.setattr(bridge,'media_download',fail)
    output=fixture[0].handle('walk_receipt',body(),'service')['result']
    assert output['accounting']=='actual' and output['cleanup']=='verified'
    assert fixture[1].row['cost_microusd']==40 and fixture[6].count('settle')==1
    assert output['evidence']=={'terminal_ref':fixture[1].row['receipt_ref'],'media_ref':media_ref,
        'media_verification':{'status':'unavailable','code':'media_download_unavailable'}}


def test_media_urls_are_transient_not_settlement_evidence(fixture,monkeypatch):
    prepare(fixture)
    media_ref=dict(bucket='media',key='media/index.json',version_id='v1',sha256='e'*64)
    terminal(fixture,media_ref=media_ref)
    transient={'version':1,'bundle':{'url':'https://example.invalid/transient'}}
    monkeypatch.setattr(bridge,'media_download',lambda *args:transient)
    output=fixture[0].handle('walk_receipt',body(),'service')['result']
    assert output['evidence']['download']==transient
    assert 'download' not in fixture[1].row and 'example.invalid' not in str(fixture[1].row)
    assert output['upload']=='pending' and output['acceptance']=='pending' and output['cleanup']=='verified'


def test_malformed_optional_media_does_not_block_accounting_or_escape(fixture,monkeypatch):
    prepare(fixture)
    terminal(fixture,media_ref={'url':'private-provider-data'})
    def forbidden(*args):
        pytest.fail('malformed media must never reach presigning')
    monkeypatch.setattr(bridge,'media_download',forbidden)
    output=fixture[0].handle('walk_receipt',body(),'service')['result']
    assert output['accounting']=='actual' and output['cleanup']=='verified'
    assert fixture[1].row['cost_microusd']==40 and fixture[6].count('settle')==1
    assert output['evidence']=={'terminal_ref':fixture[1].row['receipt_ref'],
        'media_verification':{'status':'unavailable','code':'media_download_unavailable'}}
    assert 'private-provider-data' not in str(output)
