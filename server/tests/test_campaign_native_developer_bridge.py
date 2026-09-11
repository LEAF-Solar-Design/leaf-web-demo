"""Offline acceptance for the registered native-change campaign adapter and ingress."""
import copy
import io
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import campaign_native_developer_bridge as b

ORG, PROJECT, CAMPAIGN, TASK, PARENT, ENROLL = [str(uuid.uuid4()) for _ in range(6)]
SHA = 'a' * 40
SCOPE = dict(org=ORG, project=PROJECT, campaign=CAMPAIGN)


class Objects:
    def __init__(self):
        self.data = {}
        self.streams = []

    def add(self, value, prefix='source/leaf-automation-aws-terraform/'):
        raw = b._raw(value)
        ref = dict(bucket='artifacts', key=prefix + b._sha(value), version_id='v1', sha256=b._sha(value))
        self.data[ref['key']] = raw
        return ref

    def get_object(self, Bucket, Key, VersionId):
        stream = io.BytesIO(self.data[Key])
        self.streams.append(stream)
        return dict(Body=stream, ContentLength=len(self.data[Key]), VersionId=VersionId)


class Ledger:
    def __init__(self):
        self.row = None
        self.reserves = 0
        self.settles = 0

    def prepare(self, *scope, request, reservation_microusd, **kw):
        if self.row:
            b._require(self.row['request'] == request)
            return copy.deepcopy(self.row)
        op = b._operation(request)
        self.reserves += 1
        self.row = dict(request=copy.deepcopy(request), reservation_microusd=reservation_microusd,
                        developer_attempt_id=op[:48] + '-00000001', settled_at=None)
        return copy.deepcopy(self.row)

    def read(self, *args):
        return copy.deepcopy(self.row)

    def record_admission(self, *args, **kwargs):
        assert kwargs['developer_attempt_id'] == self.row['developer_attempt_id']

    def settle(self, *args, **kw):
        if self.row['settled_at'] is None:
            self.settles += 1
        self.row.update(settled_at='now', cost_microusd=kw['cost_microusd'], receipt_ref=kw['receipt_ref'])

    def reserve_retry(self, *args, expected_attempt_id, **kw):
        if self.row.get('expected_attempt_id') != expected_attempt_id:
            assert self.row['settled_at'] is not None
            self.reserves += 1
            self.row.update(expected_attempt_id=expected_attempt_id,
                developer_attempt_id=expected_attempt_id[:-8] + '00000002', settled_at=None)
        return copy.deepcopy(self.row)


@pytest.fixture
def lane():
    objects, ledger = Objects(), Ledger()
    nested = dict(bucket='artifacts', key='source/leaf-automation-aws-terraform/archive',
                  version_id='v1', sha256='b'*64)
    source = objects.add(dict(schema_version=1, commit=SHA, archive=nested, bundle=nested))
    job = dict(org_id=ORG, project_id=PROJECT, repository_id='LEAF-Solar-Design/leaf-automation-aws-terraform',
        job_name='arlo-worker', environment='staging', commit_sha=SHA, source_bucket='artifacts',
        source_prefix='source/leaf-automation-aws-terraform/', acceptance_bucket='artifacts',
        acceptance_prefix='evidence/admission/', acceptance_producer='leaf-forge-native-receipt-v1',
        runtime_seconds=3000, reservation_microusd=1250000, executor_digest='c'*64)
    config = dict(version=1, enabled=True, endpoint='https://abcdefghij.execute-api.us-east-1.amazonaws.com/v1/developer/jobs',
                  region='us-east-1', role_arn=None, registry_digest='d'*64, jobs=[job])
    task = dict(task_id=TASK, kind='task', capability='terraform.native', source_sha=SHA,
                status='claimed', current_stage='implementation', fence=1)
    state = dict(active=True, revoked=False, calls=[], result=None, lost=False)

    def resolve(enrollment, campaign, task_id, parent, fence, active, subject):
        b._require(subject == 'worker' and enrollment == ENROLL and not state['revoked']
                   and campaign == CAMPAIGN and task_id == TASK
                   and task['capability'] == 'terraform.native', 403)
        if parent is not None:
            b._require(parent == PARENT and fence == 1)
        if active:
            b._require(state['active'])
        return SCOPE, task

    def send(body):
        state['calls'].append(copy.deepcopy(body))
        if state['lost']:
            state['lost'] = False
            raise TimeoutError()
        return copy.deepcopy(state['result'])

    adapter = b.NativeBridge(config, ledger, resolve=resolve,
                             clients=lambda _: (objects, SimpleNamespace(request=send)))
    body = dict(enrollment_id=ENROLL, campaign_id=CAMPAIGN, task_id=TASK,
                parent_attempt_id=PARENT, parent_attempt_fence=1, source=source)
    return SimpleNamespace(**locals())


def prepared(x):
    return x.adapter.handle('native_prepare', x.body, 'worker')['result']


def result(x, request, terminal=False, sequence=1):
    op = b._operation(request)
    aid = op[:48] + '-%08d' % sequence
    binding = {k:v for k,v in request.items() if k != 'idempotency_key'}
    binding.update(operation_id=op, attempt_id=aid, registry_digest=x.config['registry_digest'],
        executor_digest=x.job['executor_digest'], runtime_seconds=3000, resource_locks=['terraform:staging/us-east-1'],
        reservation=dict(amount_microusd=1250000))
    attempt = dict(operation_id=op, attempt_id=aid, binding=binding, state='FAILED' if terminal else 'ADMITTED',
                   resources_reconciled=terminal, stages=[{'stage':'plan','status':'SUCCEEDED'}])
    if terminal:
        evidence = dict(binding=binding, producer=x.job['acceptance_producer'], status='FAILED',
            resources_reconciled=True, workers_terminal=True,
            resource_outcomes=[dict(key='terraform:staging/us-east-1',state='reconciled')],
            cost_basis='actual', actual_cost_microusd=10, billed_cost_verified=True)
        attempt['evidence'] = x.objects.add(evidence, 'evidence/admission/')
    x.state['result'] = dict(operation_id=op, request=request, registry_digest=x.config['registry_digest'],
                             current_attempt=sequence, attempts=[attempt])
    return attempt


def request_body(x, action):
    return dict(enrollment_id=ENROLL,campaign_id=CAMPAIGN,task_id=TASK,action=action,
                operation_id=b._operation(x.ledger.row['request']))


def test_prepare_replay_lost_response(lane):
    x=lane
    req=prepared(x)
    assert prepared(x)==req and x.ledger.reserves==1 and not x.state['calls']
    result(x,req)
    x.state['lost']=True
    with pytest.raises(TimeoutError):
        x.adapter.handle('native_request',request_body(x,'submit'),'worker')
    assert x.ledger.row['request']==req
    for action in ('inspect','recover','cancel','submit'):
        x.adapter.handle('native_request',request_body(x,action),'worker')
    assert all(s.closed for s in x.objects.streams)


@pytest.mark.parametrize('change', ['subject','revoked','campaign','browser','expired','fence','source','privileged'])
def test_denials_never_submit(lane,change):
    x=lane;body=copy.deepcopy(x.body);subject='worker'
    if change=='subject': subject='other'
    if change=='revoked': x.state['revoked']=True
    if change=='campaign': body['campaign_id']=str(uuid.uuid4())
    if change=='browser': x.task['capability']='browser.walk'
    if change=='expired': x.state['active']=False
    if change=='fence': body['parent_attempt_fence']=2
    if change=='source': body['source']['key']='outside/x'
    if change=='privileged': body['mode']='apply'
    with pytest.raises(b.BridgeError): x.adapter.handle('native_prepare',body,subject)
    assert not x.state['calls'] and x.ledger.reserves==0


def test_changed_frozen_request_conflicts(lane):
    x=lane;prepared(x)
    x.body['source']=x.objects.add(dict(schema_version=1,commit=SHA,
        archive=dict(bucket='artifacts',key=x.job['source_prefix']+'new',version_id='v2',sha256='e'*64),
        bundle=dict(bucket='artifacts',key=x.job['source_prefix']+'new',version_id='v2',sha256='e'*64)))
    with pytest.raises(b.BridgeError): prepared(x)
    assert x.ledger.reserves==1 and not x.state['calls']


def test_receipt_replay_retry_lost_response(lane):
    x=lane;req=prepared(x);attempt=result(x,req,True)
    body=dict(enrollment_id=ENROLL,campaign_id=CAMPAIGN,task_id=TASK)
    x.adapter.handle('native_receipt',body,'worker')
    x.adapter.handle('native_receipt',body,'worker')
    assert x.ledger.settles==1
    retry=request_body(x,'retry');retry['expected_attempt_id']=attempt['attempt_id']
    original=x.adapter._request
    def fail_retry(row,action,**extra):
        if action=='retry': raise TimeoutError()
        return original(row,action,**extra)
    x.adapter._request=fail_retry
    with pytest.raises(TimeoutError): x.adapter.handle('native_request',retry,'worker')
    assert x.ledger.reserves==2
    result(x,req,sequence=2)
    x.adapter._request=original
    x.adapter.handle('native_request',retry,'worker')
    assert x.ledger.reserves==2 and x.ledger.settles==1


@pytest.mark.parametrize('target',['source','receipt'])
def test_altered_bytes_close_stream(lane,target):
    x=lane
    if target=='source':
        ref=x.body['source'];call=lambda:prepared(x)
    else:
        attempt=result(x,prepared(x),True);ref=attempt['evidence']
        call=lambda:x.adapter.handle('native_receipt',dict(enrollment_id=ENROLL,campaign_id=CAMPAIGN,task_id=TASK),'worker')
    x.objects.data[ref['key']]+=b' '
    with pytest.raises(b.BridgeError):call()
    assert x.objects.streams[-1].closed and x.ledger.settles==0


def test_object_size_bound(lane):
    x=lane;x.objects.data[x.body['source']['key']]=b'x'*(b.LIMIT+1)
    with pytest.raises(b.BridgeError):prepared(x)
    assert x.objects.streams[-1].closed


@pytest.mark.parametrize('mode', ['plan', 'apply'])
def test_installation_cannot_claim_execution_mode(lane, mode):
    lane.config['jobs'][0]['mode']=mode
    with pytest.raises(b.BridgeError):b._validate_installation(lane.config)


def test_mounted_native_dispatch(lane,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    import campaign_bridge
    from routers import campaigns
    monkeypatch.setattr(campaigns,'_store',lambda:None)
    monkeypatch.setattr(campaigns,'_bridge_retry_after',lambda _:0)
    monkeypatch.setattr(campaign_bridge,'_configured',lambda:None)
    monkeypatch.setattr(b,'handle',lane.adapter.handle)
    app=FastAPI();app.include_router(campaigns.router)
    app.dependency_overrides[deps.require_campaign_worker]=lambda:'worker'
    client=TestClient(app)
    response=client.post('/api/internal/campaigns/bridge/native_prepare',json=lane.body)
    assert response.status_code==200 and response.json()['ok']
    assert b.OPS <= campaigns._BRIDGE_OPS
    assert lane.ledger.reserves==1

@pytest.mark.parametrize('defect',[None,'scope','capability','worker','stage','expired','fence'])
def test_real_resolver_checks_persisted_rows(lane,monkeypatch,defect):
    import campaign_bridge
    import campaign_worker_service
    task=copy.deepcopy(lane.task)
    attempt=dict(task_id=TASK,stage='implementation',fence=1,status='active',overdue=False)
    scope=dict(SCOPE)
    if defect=='scope':scope['campaign']=str(uuid.uuid4())
    if defect=='capability':task['capability']='browser.walk'
    if defect=='stage':attempt['stage']='deployment'
    if defect=='expired':attempt['overdue']=True
    if defect=='fence':task['fence']=2
    class Cursor:
        def execute(self,sql,args):
            self.sql=sql
            if 'campaign_task_attempts' in sql:
                assert args['worker']=='enrollment-'+ENROLL
        def fetchone(self):
            return (None if defect=='worker' else attempt) if 'campaign_task_attempts' in self.sql else task
    @contextmanager
    def cursor():yield Cursor()
    execution=SimpleNamespace(_cursor=cursor,SCOPE='org_id=%(org)s')
    monkeypatch.setattr(campaign_worker_service,'_platform',lambda:(object(),execution,None))
    monkeypatch.setattr(campaign_bridge,'_scope',lambda *args:scope)
    if defect:
        with pytest.raises(b.BridgeError):b._resolve(ENROLL,CAMPAIGN,TASK,PARENT,1,True,'worker')
    else:
        assert b._resolve(ENROLL,CAMPAIGN,TASK,PARENT,1,True,'worker')==(scope,task)
    assert not lane.state['calls']


def test_named_claim_never_uses_generic_claim(lane,monkeypatch):
    import campaign_bridge
    import campaign_worker_service
    task={**lane.task,'task_key':'native-plan'}
    calls=[]
    class Cursor:
        def execute(self,sql,args):
            assert 'task_key=' in sql and 'FOR UPDATE' in sql and args['key']=='native-plan'
        def fetchone(self):return task
    @contextmanager
    def cursor():yield Cursor()
    def claim(cur,scope,**kw):
        calls.append(kw)
        return dict(task_id=TASK,attempt_id=PARENT,fence=1,stage='implementation',deadline_at='later',attempt_token='never-return')
    execution=SimpleNamespace(_cursor=cursor,SCOPE='org_id=%(org)s',_claim_task_cursor=claim)
    monkeypatch.setattr(campaign_worker_service,'_platform',lambda:(object(),execution,None))
    monkeypatch.setattr(campaign_bridge,'_scope',lambda *args:SCOPE)
    response=lane.adapter.handle('native_claim',dict(enrollment_id=ENROLL,campaign_id=CAMPAIGN,task_key='native-plan'),'worker')
    assert calls==[dict(worker_id='enrollment-'+ENROLL,lease=900,task_key='native-plan')]
    assert 'attempt_token' not in response['result'] and not lane.state['calls']


@pytest.mark.parametrize('field', ['mode', 'authority', 'plan', 'apply_refs', 'role_arn', 'commands', 'buildspec'])
@pytest.mark.parametrize('op', ['native_prepare', 'native_request'])
def test_client_cannot_inject_native_execution_authority(lane, field, op):
    x = lane
    if op == 'native_prepare':
        body = copy.deepcopy(x.body)
    else:
        prepared(x)
        body = request_body(x, 'submit')
    body[field] = 'untrusted'
    with pytest.raises(b.BridgeError):
        x.adapter.handle(op, body, 'worker')
    assert not x.state['calls']


def test_native_producer_pending_billing_receipt_settles_once(lane):
    # Shape from admission_receipt.py:204-207 in admission-receipt-package.zip.
    # The actual producer emits SUCCEEDED only after plan/apply native evidence
    # and CodeBuild terminal checks. This tests its adapter/accounting boundary,
    # not the upstream producer's semantic verifier.
    x = lane
    req = prepared(x)
    attempt = result(x, req)
    binding = attempt['binding']
    records = [dict(name=name, status='SUCCEEDED',
                    started_at='2026-09-11T10:00:00+00:00',
                    finished_at='2026-09-11T10:00:00+00:00',
                    evidence=dict(bucket='artifacts',
                        key='evidence/native/' + binding['operation_id'] + '/' + name + '/receipt.json',
                        version_id='native-v1', sha256='f' * 64))
               for name in ('plan', 'apply')]
    evidence = dict(binding=binding, producer='leaf-forge-native-receipt-v1',
        status='SUCCEEDED', resources_reconciled=True, workers_terminal=True,
        resource_outcomes=[dict(key=key, state='reconciled') for key in binding['resource_locks']],
        stages=records, cost_basis='reserved_maximum_pending_billing',
        billed_cost_verified=False, actual_cost_microusd=None)
    ref = x.objects.add(evidence, 'evidence/admission/')
    attempt.update(state='SUCCEEDED', resources_reconciled=True, stages=records, evidence=ref)
    body = dict(enrollment_id=ENROLL, campaign_id=CAMPAIGN, task_id=TASK)
    first = x.adapter.handle('native_receipt', body, 'worker')['result']
    second = x.adapter.handle('native_receipt', body, 'worker')['result']
    assert first == second
    assert first['accounting'] == 'reserved_maximum_pending_billing'
    assert first['cleanup'] == 'verified'
    assert x.ledger.row['cost_microusd'] == x.job['reservation_microusd']
    assert x.ledger.settles == 1 and x.ledger.reserves == 1
    assert x.state['result']['attempts'][0]['stages'] == records
    assert 'mode' not in req and 'authority' not in req and 'plan' not in req
    assert all(stream.closed for stream in x.objects.streams)
