"""Trusted campaign broker ledger, persisted before developer network submission.

Allocation and settlement callers must independently verify producer authority and
immutable evidence. A submitted request never grants funding or cleanup authority.
Every mutator locks allocation before task/parent and operation/reservation rows.
"""
from __future__ import annotations

import hashlib
import json
import re
from psycopg.types.json import Jsonb
from .campaigns import _scope, _uuid, _cursor, _campaign, _row, _text, _missing
from .campaign_execution import SCOPE, _remote_attempt, _invalid, _conflict

MAX_MONEY = 10**15
REQUEST_FIELDS = frozenset(('org_id', 'project_id', 'campaign_id', 'task_id',
    'parent_attempt_id', 'parent_attempt_fence', 'repository_id', 'commit_sha',
    'source', 'job_name', 'environment', 'idempotency_key'))


def _int(value, low=1, high=MAX_MONEY):
    if type(value) is not int or not low <= value <= high:
        _invalid('integer outside permitted bounds')
    return value


def _ref(value):
    if not isinstance(value, dict) or set(value) != {'bucket', 'key', 'version_id', 'sha256'}:
        _invalid('immutable object reference required')
    for key, item in value.items():
        _text(item, key, 1024)
        if not item.strip():
            _invalid()
    if value['version_id'].strip().lower() in ('null', 'latest') or not re.fullmatch('[0-9a-f]{64}', value['sha256']):
        _invalid('immutable object reference required')
    return dict(value)


def _scoped(org, project, campaign):
    return {**_scope(org, project), 'campaign': _uuid(campaign)}


def _live(cur, scope):
    campaign = _campaign(cur, scope)
    if campaign is None:
        _missing()
    cur.execute("SELECT status,deleted_at FROM projects WHERE org_id=%(org)s AND project_id=%(project)s AND deleted_at IS NULL AND status='active' FOR SHARE", scope)
    project = cur.fetchone()
    cur.execute('SELECT status FROM campaigns WHERE ' + SCOPE + ' FOR SHARE', scope)
    campaign = cur.fetchone()
    if project is None or project['deleted_at'] is not None or project['status'] != 'active' or campaign['status'] not in ('accepted', 'running'):
        _conflict('campaign_inactive')


def _allocation(cur, scope):
    cur.execute('SELECT * FROM campaign_developer_allocations WHERE ' + SCOPE + ' FOR UPDATE', scope)
    row = cur.fetchone()
    if row is None:
        _conflict('allocation_required')
    return row


def record_allocation(org_id, project_id, campaign_id, *, allocation_id,
                      limit_microusd, max_active=1, evidence_ref):
    scope = _scoped(org_id, project_id, campaign_id)
    _text(allocation_id, 'allocation_id', 128)
    _int(limit_microusd)
    _int(max_active, 1, 16)
    evidence = _ref(evidence_ref)
    with _cursor() as cur:
        # Insert's unique campaign key serializes concurrent first producers.
        _live(cur, scope)
        cur.execute('INSERT INTO campaign_developer_allocations '
            '(org_id,project_id,campaign_id,allocation_id,limit_microusd,max_active,evidence_ref) '
            'VALUES (%(org)s,%(project)s,%(campaign)s,%(id)s,%(limit)s,%(max)s,%(ref)s) '
            'ON CONFLICT DO NOTHING RETURNING *',
            {**scope, 'id': allocation_id, 'limit': limit_microusd, 'max': max_active, 'ref': Jsonb(evidence)})
        new = cur.fetchone()
        existing = new or _allocation(cur, scope)
        if any(existing[k] != v for k, v in dict(allocation_id=allocation_id,
                limit_microusd=limit_microusd,max_active=max_active,evidence_ref=evidence).items()):
            _conflict('allocation_conflict')
        return _row(existing, replayed=new is None)


def _request(request, scope, task, parent, fence):
    if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
        _invalid('request must match developer submit contract')
    for key in REQUEST_FIELDS - {'source', 'parent_attempt_fence'}:
        _text(request[key], key, 1024)
        if not request[key].strip():
            _invalid()
    _int(request['parent_attempt_fence'])
    expected = dict(org_id=str(scope['org']), project_id=str(scope['project']),
        campaign_id=str(scope['campaign']),task_id=str(task),parent_attempt_id=str(parent),parent_attempt_fence=fence)
    if any(request[k] != v for k,v in expected.items()):
        _conflict('parent_binding_conflict')
    if not re.fullmatch('[0-9a-f]{40}', request['commit_sha']) or not re.fullmatch('[A-Za-z0-9._:-]{1,128}', request['idempotency_key']):
        _invalid()
    result = {**request, 'source': _ref(request['source'])}
    encoded = json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > 12000:
        _invalid('request too large')
    return json.loads(encoded)


def _operation(cur, scope, operation_id):
    if not isinstance(operation_id, str) or not re.fullmatch('[0-9a-f]{64}', operation_id):
        _invalid('invalid operation ID')
    cur.execute('SELECT * FROM campaign_developer_operations WHERE '+SCOPE+
                ' AND operation_id=%(op)s FOR UPDATE', {**scope,'op':operation_id})
    op = cur.fetchone()
    if op is None:
        _missing()
    return op


def _current(cur, scope, op, sequence=None):
    cur.execute('SELECT * FROM campaign_developer_reservations WHERE '+SCOPE+
        ' AND operation_id=%(op)s AND sequence=%(seq)s FOR UPDATE',
        {**scope,'op':op['operation_id'],'seq':sequence or op['current_sequence']})
    row = cur.fetchone()
    if row is None:
        _missing()
    return row


def _result(op, reservation, replayed=False):
    return _row({**op, **reservation}, replayed=replayed)


def _reserve(cur, scope, allocation, op, amount, previous=None):
    cur.execute('SELECT count(*) AS n FROM campaign_developer_reservations WHERE '+SCOPE+' AND settled_at IS NULL', scope)
    if cur.fetchone()['n'] >= allocation['max_active']:
        _conflict('active_limit')
    if allocation['spent_microusd'] + allocation['reserved_microusd'] + amount > allocation['limit_microusd']:
        _conflict('allocation_exhausted')
    seq = op['current_sequence']
    cur.execute('INSERT INTO campaign_developer_reservations '
        '(org_id,project_id,campaign_id,operation_id,sequence,developer_attempt_id,expected_attempt_id,reservation_microusd) '
        'VALUES (%(org)s,%(project)s,%(campaign)s,%(op)s,%(seq)s,%(attempt)s,%(previous)s,%(amount)s) RETURNING *',
        {**scope,'op':op['operation_id'],'seq':seq,'attempt':f"{op['operation_id'][:48]}-{seq:08d}",
         'previous':previous,'amount':amount})
    row = cur.fetchone()
    cur.execute('UPDATE campaign_developer_allocations SET reserved_microusd=reserved_microusd+%(amount)s WHERE '+SCOPE,
                {**scope,'amount':amount})
    return row


def prepare(org_id, project_id, campaign_id, *, task_id, parent_attempt_id,
            parent_attempt_fence, request, reservation_microusd):
    scope = _scoped(org_id, project_id, campaign_id)
    task, parent = _uuid(task_id), _uuid(parent_attempt_id)
    _int(parent_attempt_fence)
    amount = _int(reservation_microusd)
    request = _request(request, scope, task, parent, parent_attempt_fence)
    identity = [[request[k] for k in ('org_id','project_id','repository_id')],request['idempotency_key']]
    operation_id = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()
    with _cursor() as cur:
        allocation = _allocation(cur, scope)
        cur.execute('SELECT * FROM campaign_developer_operations WHERE '+SCOPE+
            ' AND (task_id=%(task)s OR idempotency_key=%(key)s)',
            {**scope,'task':task,'key':request['idempotency_key']})
        existing = cur.fetchall()
        if existing:
            if len(existing)!=1 or existing[0]['request'] != request:
                _conflict('binding_conflict')
            original = _current(cur,scope,existing[0],1)
            if original['reservation_microusd'] != amount:
                _conflict('binding_conflict')
            return _result(existing[0],_current(cur,scope,existing[0]),True)
        _live(cur,scope)
        task_row, attempt = _remote_attempt(cur,scope,parent)
        if task_row['task_id'] != task or task_row['status'] != 'claimed' or attempt['status'] != 'active' or attempt['overdue'] or attempt['fence'] != parent_attempt_fence or task_row['fence'] != parent_attempt_fence:
            _conflict('parent_binding_conflict')
        cur.execute('INSERT INTO campaign_developer_operations '
            '(operation_id,org_id,project_id,campaign_id,task_id,parent_attempt_id,parent_attempt_fence,idempotency_key,request) '
            'VALUES (%(op)s,%(org)s,%(project)s,%(campaign)s,%(task)s,%(parent)s,%(fence)s,%(key)s,%(request)s) RETURNING *',
            {**scope,'op':operation_id,'task':task,'parent':parent,'fence':parent_attempt_fence,'key':request['idempotency_key'],'request':Jsonb(request)})
        op = cur.fetchone()
        return _result(op,_reserve(cur,scope,allocation,op,amount))


def pending(org_id, project_id, campaign_id):
    scope = _scoped(org_id,project_id,campaign_id)
    with _cursor() as cur:
        cur.execute('SELECT o.*,r.* FROM campaign_developer_operations o JOIN campaign_developer_reservations r '
            'USING(org_id,project_id,campaign_id,operation_id) WHERE o.org_id=%(org)s AND o.project_id=%(project)s '
            'AND o.campaign_id=%(campaign)s AND r.settled_at IS NULL ORDER BY o.created_at',scope)
        return [_row(row) for row in cur.fetchall()]


def read(org_id, project_id, campaign_id, task_id):
    scope = _scoped(org_id,project_id,campaign_id)
    with _cursor() as cur:
        cur.execute('SELECT o.*,r.* FROM campaign_developer_operations o JOIN campaign_developer_reservations r '
            'USING(org_id,project_id,campaign_id,operation_id) WHERE o.org_id=%(org)s AND o.project_id=%(project)s '
            'AND o.campaign_id=%(campaign)s AND o.task_id=%(task)s AND r.sequence=o.current_sequence',
            {**scope,'task':_uuid(task_id)})
        return _row(cur.fetchone())


def record_admission(org_id, project_id, campaign_id, *, operation_id, developer_attempt_id):
    scope = _scoped(org_id,project_id,campaign_id)
    with _cursor() as cur:
        _allocation(cur,scope)
        op = _operation(cur,scope,operation_id)
        row = _current(cur,scope,op)
        if row['developer_attempt_id'] != developer_attempt_id:
            _conflict('attempt_conflict')
        replay = row['admitted']
        cur.execute('UPDATE campaign_developer_reservations SET admitted=TRUE WHERE operation_id=%s AND sequence=%s RETURNING *',
                    (operation_id,row['sequence']))
        return _result(op,cur.fetchone(),replay)


def settle(org_id, project_id, campaign_id, *, operation_id, developer_attempt_id,
           cost_microusd, receipt_ref, resources_reconciled):
    amount = _int(cost_microusd,0)
    ref = _ref(receipt_ref)
    if resources_reconciled is not True:
        _conflict('cleanup_unverified')
    scope = _scoped(org_id,project_id,campaign_id)
    with _cursor() as cur:
        _allocation(cur,scope)
        op = _operation(cur,scope,operation_id)
        if not isinstance(developer_attempt_id,str) or not re.fullmatch(re.escape(operation_id[:48])+r'-[0-9]{8}',developer_attempt_id):
            _conflict('attempt_conflict')
        seq = _int(int(developer_attempt_id[-8:]),1,99999999)
        row = _current(cur,scope,op,seq)
        if row['settled_at'] is not None:
            if row['cost_microusd'] != amount or row['receipt_ref'] != ref:
                _conflict('settlement_conflict')
            return _result(op,row,True)
        if amount > row['reservation_microusd']:
            _conflict('reservation_exceeded')
        cur.execute('UPDATE campaign_developer_reservations SET cost_microusd=%(cost)s,receipt_ref=%(ref)s,'
            'resources_reconciled=TRUE,settled_at=NOW() WHERE operation_id=%(op)s AND sequence=%(seq)s RETURNING *',
            {'cost':amount,'ref':Jsonb(ref),'op':operation_id,'seq':seq})
        settled = cur.fetchone()
        cur.execute('UPDATE campaign_developer_allocations SET spent_microusd=spent_microusd+%(cost)s,'
            'reserved_microusd=reserved_microusd-%(reserved)s WHERE '+SCOPE,
            {**scope,'cost':amount,'reserved':row['reservation_microusd']})
        return _result(op,settled)


def reserve_retry(org_id, project_id, campaign_id, *, operation_id, expected_attempt_id, reservation_microusd):
    amount = _int(reservation_microusd)
    if not isinstance(operation_id, str) or not re.fullmatch("[0-9a-f]{64}", operation_id):
        _invalid("invalid operation ID")
    if not isinstance(expected_attempt_id, str) or not re.fullmatch(re.escape(operation_id[:48])+r"-[0-9]{8}", expected_attempt_id):
        _conflict("attempt_conflict")
    scope = _scoped(org_id,project_id,campaign_id)
    with _cursor() as cur:
        allocation = _allocation(cur,scope)
        op = _operation(cur,scope,operation_id)
        row = _current(cur,scope,op)
        if row['expected_attempt_id'] == expected_attempt_id:
            if row['reservation_microusd'] != amount:
                _conflict('retry_conflict')
            return _result(op,row,True)
        if row['developer_attempt_id'] != expected_attempt_id or row['settled_at'] is None or not row['resources_reconciled']:
            _conflict('retry_conflict')
        _live(cur,scope)
        _int(op['current_sequence']+1,1,99999999)
        cur.execute('UPDATE campaign_developer_operations SET current_sequence=current_sequence+1 WHERE operation_id=%s RETURNING *',(operation_id,))
        op = cur.fetchone()
        return _result(op,_reserve(cur,scope,allocation,op,amount,expected_attempt_id))
