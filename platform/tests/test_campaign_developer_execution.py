"""Campaign broker budget and restart proofs against real PostgreSQL."""
import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from leaf_platform import campaign_developer_execution as ledger, campaigns, db
from test_campaign_execution_store import _seed, _submit, _claim, _expire

REF = dict(bucket='evidence',key='immutable/object',version_id='version-1',sha256='b'*64)


def setup(make_org, *, funded=True, limit=100, max_active=1):
    scope, _, _ = _seed(make_org)
    if funded:
        ledger.record_allocation(*scope,allocation_id='approved-budget',limit_microusd=limit,
                                 max_active=max_active,evidence_ref=REF)
    _submit(scope)
    attempt = _claim(scope)
    return scope, attempt


def payload(scope, attempt, key='walk'):
    return dict(org_id=str(scope[0]),project_id=str(scope[1]),campaign_id=str(scope[2]),
        task_id=attempt['task_id'],parent_attempt_id=attempt['attempt_id'],parent_attempt_fence=attempt['fence'],
        repository_id='org/repository',commit_sha='a'*40,source=REF.copy(),job_name='browser-walk',
        environment='staging',idempotency_key=key)


def prepare(scope, attempt, *, amount=70, request=None):
    return ledger.prepare(*scope,task_id=attempt['task_id'],parent_attempt_id=attempt['attempt_id'],
        parent_attempt_fence=attempt['fence'],request=request or payload(scope,attempt),reservation_microusd=amount)


def balance(scope):
    with db.connection() as conn:
        return conn.execute('SELECT spent_microusd,reserved_microusd FROM campaign_developer_allocations '
            'WHERE org_id=%s AND project_id=%s AND campaign_id=%s',tuple(map(lambda v:uuid.UUID(str(v)),scope))).fetchone()


def settle(scope, row, cost=40, **changes):
    values=dict(operation_id=row['operation_id'],developer_attempt_id=row['developer_attempt_id'],
                cost_microusd=cost,receipt_ref=REF,resources_reconciled=True)
    values.update(changes)
    return ledger.settle(*scope,**values)


def race(functions):
    barrier=Barrier(len(functions))
    def run(fn):
        barrier.wait(timeout=10)
        try:
            return fn()
        except campaigns.CampaignError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(run,functions))


def test_duplicate_concurrent_prepare_and_restart(make_org):
    scope, attempt=setup(make_org)
    rows=race([lambda:prepare(scope,attempt)]*2)
    assert all(isinstance(row,dict) for row in rows)
    assert rows[0]['operation_id']==rows[1]['operation_id']
    assert sum(row['replayed'] for row in rows)==1
    assert balance(scope)==dict(spent_microusd=0,reserved_microusd=70)
    _expire(attempt)
    # A fresh caller requires no scratch file or live parent lease for recovery.
    recovered=ledger.read(*scope,attempt['task_id'])
    assert recovered['request']==payload(scope,attempt)
    assert ledger.pending(*scope)[0]['developer_attempt_id']==recovered['developer_attempt_id']
    assert prepare(scope,attempt)['replayed']
    assert ledger.record_admission(*scope,operation_id=recovered['operation_id'],
        developer_attempt_id=recovered['developer_attempt_id'])['admitted']


def test_concurrent_distinct_requests_obey_lifetime_cap(make_org):
    scope, first=setup(make_org,max_active=2)
    _submit(scope,'second')
    second=_claim(scope)
    results=race([lambda:prepare(scope,first),lambda:prepare(scope,second,request=payload(scope,second,'second'))])
    assert sum(isinstance(row,dict) for row in results)==1
    assert 'allocation_exhausted' in results
    assert balance(scope)['reserved_microusd']==70


def test_no_funding_and_exact_allocation(make_org):
    scope, attempt=setup(make_org,funded=False)
    with pytest.raises(campaigns.CampaignError,match='allocation required'):
        prepare(scope,attempt)
    kwargs=dict(allocation_id='grant',limit_microusd=100,evidence_ref=REF)
    ledger.record_allocation(*scope,**kwargs)
    assert ledger.record_allocation(*scope,**kwargs)['replayed']
    for change in ({'limit_microusd':101},{'allocation_id':'replacement'},{'max_active':2}):
        with pytest.raises(campaigns.CampaignConflict):
            ledger.record_allocation(*scope,**{**kwargs,**change})
    with pytest.raises(campaigns.CampaignError):
        ledger.record_allocation(*scope,**{**kwargs,'limit_microusd':True})


def test_wrong_scope_task_fence_and_changed_request(make_org):
    scope, attempt=setup(make_org)
    for key,value in [('project_id',str(uuid.uuid4())),('task_id',str(uuid.uuid4())),
                      ('parent_attempt_fence',attempt['fence']+1)]:
        request=payload(scope,attempt)
        request[key]=value
        with pytest.raises(campaigns.CampaignError):
            prepare(scope,attempt,request=request)
    altered=dict(attempt,fence=attempt['fence']+1)
    with pytest.raises(campaigns.CampaignConflict):
        prepare(scope,altered)
    row=prepare(scope,attempt)
    changed=payload(scope,attempt)
    changed['commit_sha']='c'*40
    with pytest.raises(campaigns.CampaignConflict):
        prepare(scope,attempt,request=changed)
    other=(str(uuid.uuid4()),scope[1],scope[2])
    assert ledger.read(*other,attempt['task_id']) is None
    assert ledger.pending(*other)==[]
    with pytest.raises(campaigns.CampaignError):
        ledger.record_admission(*other,operation_id=row['operation_id'],developer_attempt_id=row['developer_attempt_id'])


def test_unknown_cancellation_and_unverified_cleanup_retain_reservation(make_org):
    scope, attempt=setup(make_org)
    row=prepare(scope,attempt)
    with db.connection() as conn:
        conn.execute("UPDATE campaign_tasks SET status='cancelled' WHERE task_id=%s",(uuid.UUID(attempt['task_id']),))
    _expire(attempt)
    for flag in (False,1,None):
        with pytest.raises(campaigns.CampaignConflict):
            settle(scope,row,resources_reconciled=flag)
    assert balance(scope)==dict(spent_microusd=0,reserved_microusd=70)
    assert len(ledger.pending(*scope))==1
    with pytest.raises(campaigns.CampaignConflict):
        ledger.reserve_retry(*scope,operation_id=row['operation_id'],expected_attempt_id=row['developer_attempt_id'],reservation_microusd=70)


def test_settlement_exact_once_retry_accumulates_across_days(make_org):
    scope, attempt=setup(make_org)
    row=prepare(scope,attempt)
    results=race([lambda:settle(scope,row)]*2)
    assert sum(item['replayed'] for item in results)==1
    assert balance(scope)==dict(spent_microusd=40,reserved_microusd=0)
    with pytest.raises(campaigns.CampaignConflict):
        settle(scope,row,41)
    with pytest.raises(campaigns.CampaignConflict):
        settle(scope,row,receipt_ref={**REF,'version_id':'different'})
    # Aging timestamps past midnight cannot create new funding.
    with db.connection() as conn:
        conn.execute("UPDATE campaign_developer_allocations SET created_at=NOW()-interval '2 days' WHERE campaign_id=%s",(uuid.UUID(scope[2]),))
    with pytest.raises(campaigns.CampaignConflict):
        ledger.reserve_retry(*scope,operation_id=row['operation_id'],expected_attempt_id=row['developer_attempt_id'],reservation_microusd=70)
    retry=lambda:ledger.reserve_retry(*scope,operation_id=row['operation_id'],expected_attempt_id=row['developer_attempt_id'],reservation_microusd=60)
    retries=race([retry,retry])
    assert sum(item['replayed'] for item in retries)==1
    second=retries[0]
    assert second['developer_attempt_id'].endswith('-00000002')
    assert balance(scope)==dict(spent_microusd=40,reserved_microusd=60)
    assert settle(scope,row)['replayed']
    settle(scope,second,60)
    assert balance(scope)==dict(spent_microusd=100,reserved_microusd=0)
    with pytest.raises(campaigns.CampaignConflict):
        ledger.reserve_retry(*scope,operation_id=row['operation_id'],expected_attempt_id=second['developer_attempt_id'],reservation_microusd=1)
    assert ledger.pending(*scope)==[]


def test_active_limit_includes_unknown_and_expired(make_org):
    scope, attempt=setup(make_org,limit=1000)
    prepare(scope,attempt)
    _expire(attempt)
    _submit(scope,'second')
    # Claiming may re-claim the expired first task, so target second directly.
    with ledger._cursor() as cur:
        from leaf_platform import campaign_execution
        second=campaign_execution._claim_task_cursor(cur,ledger._scoped(*scope),worker_id='second',lease=30,task_key='second')
    with pytest.raises(campaigns.CampaignConflict,match='active limit'):
        prepare(scope,second,request=payload(scope,second,'second'))
