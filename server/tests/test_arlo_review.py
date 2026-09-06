"""Canonical proposal review boundaries through the actual API and SQL service.

Transaction-fake tests are boundary evidence, not a PostgreSQL durability claim.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from test_arlo_lab_inputs import MemoryTransactions, MemoryCursor, ORG, PROJECT, BINDING
from leaf_platform import api, arlo_lab, arlo_review
from leaf_platform.models import canonical_hash


class ReviewStore(MemoryTransactions):
    def __init__(self):
        super().__init__()
        self.history, self.edges, self.outbox = {}, [], []

    @contextmanager
    def cursor(self):
        yield ReviewCursor(self)

    def transaction(self, operation, **kwargs):
        with self.lock:
            saved = deepcopy((self.history,self.edges,self.outbox))
            try: return super().transaction(operation,**kwargs)
            except Exception:
                self.history,self.edges,self.outbox = saved
                raise


class ReviewCursor(MemoryCursor):
    def execute(self, sql, params):
        self.rows = []
        if sql.startswith('SELECT version_id FROM drawing_versions'):
            row=self.store.versions.get(params['version'])
            self.row={'version_id':params['version']} if row and not row.get('deleted') and params['org']==ORG and params['project']==PROJECT else None
        elif sql.startswith('SELECT * FROM history_operations'):
            rows=[r for r in self.store.history.values() if r['org_id']==params['org'] and r['project_id']==params['project']]
            if 'parent' in params: rows=[r for r in rows if r['operation_id']==params['parent'] and r['operation_type']=='solve.completed']
            if 'key' in params: rows=[r for r in rows if r['idempotency_key']==params['key']]
            if 'job' in params: rows=[r for r in rows if r['operation_type']==params['type'] and r['payload']['jobId']==params['job']]
            self.rows=deepcopy(rows);self.row=self.rows[0] if self.rows else None
        elif sql.startswith('INSERT INTO history_operations'):
            duplicate=any(r['org_id']==params['org'] and r['project_id']==params['project'] and r['idempotency_key']==params['key'] for r in self.store.history.values())
            self.row=None
            if not duplicate:
                self.row={'operation_id':params['id'],'org_id':params['org'],'project_id':params['project'],
                          'operation_type':params['type'],'payload':params['payload'].obj,'idempotency_key':params['key'],
                          'hash_value':params['value'],'created_at':datetime.now(timezone.utc)}
                self.store.history[params['id']]=deepcopy(self.row)
        elif sql.startswith('INSERT INTO history_edges'):
            self.store.edges.append(deepcopy(params));self.row=None
        elif sql.startswith('INSERT INTO outbox_entries'):
            self.store.outbox.append(deepcopy(params));self.row=None
        else: return super().execute(sql,params)
        self.store.sql.append(sql)

    def fetchall(self): return deepcopy(self.rows)


@pytest.fixture
def store(monkeypatch):
    store=ReviewStore()
    monkeypatch.setattr(arlo_review,'run_transaction',store.transaction)
    return store


@pytest.fixture
def job(store):
    job,version,parent=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
    params={'scenario':'test'}
    body={'organization_id':str(ORG),'project_id':str(PROJECT),'input_version_id':str(version)}
    proposal={'proposal_id':'proposal-test','source':{**body,'request_hash':'a'*64},'input_version_id':str(version),'production_valid':False,'violations':[]}
    result={'contract':'arlo_design_result_v1','status':'complete','request_hash':'a'*64,'production_valid':False,'proposals':[proposal]}
    envelope={'solver':'arlo-design','solver_input':body,'solver_result':result,'result_sha256':arlo_lab.digest(result),
              'input_sha256':arlo_lab.digest(body),'request_sha256':arlo_lab.digest(params),'history_operation_id':str(parent)}
    store.jobs[job]={'job_id':job,'org_id':ORG,'project_id':PROJECT,'input_version_id':version,'tool_name':'arlo-design',
                     'status':'succeeded','params':params,'result':envelope}
    store.versions[version]={'input_version_id':version}
    payload={'jobId':str(job),'resultHash':envelope['result_sha256']}
    store.history[parent]={'operation_id':parent,'org_id':ORG,'project_id':PROJECT,'operation_type':'solve.completed','payload':payload,
                           'hash_value':canonical_hash('history-operation',{'operationType':'solve.completed','payload':payload}).value,'idempotency_key':'solve-parent'}
    return job


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(api.platform_deps,'auth_live',lambda:True)
    def identity(authorization):
        if authorization!='Bearer verified-session': raise HTTPException(401,'authenticated platform session required')
        return SimpleNamespace(platform_tenant_id=ORG,binding_id=BINDING)
    monkeypatch.setattr(api.platform_deps,'_verified_identity',identity)
    app=FastAPI();app.include_router(api.router)
    return TestClient(app)


def url(job,project=PROJECT): return f'/api/projects/{project}/arlo-jobs/{job}/reviews'
def headers(key='review-1'): return {'Authorization':'Bearer verified-session','Idempotency-Key':key,'X-Org-Id':str(uuid.uuid4())}
def body(store,job,decision='accept'): return {'proposal_id':'proposal-test','result_sha256':store.jobs[job]['result']['result_sha256'],'decision':decision,'note':'Reviewed route and takeoff.'}


def test_review_append_reload_and_retry_preserve_terminal_job(client,store,job):
    original=deepcopy(store.jobs[job])
    first=client.post(url(job),json=body(store,job),headers=headers())
    assert first.status_code==201, first.text
    assert client.post(url(job),json=body(store,job),headers=headers()).json()==first.json()
    rejected=client.post(url(job),json=body(store,job,'reject'),headers=headers('review-2'))
    assert rejected.status_code==201
    read=client.get(url(job),headers=headers())
    assert [r['payload']['decision'] for r in read.json()['reviews']]==['accept','reject']
    assert len(store.edges)==len(store.outbox)==2 and store.jobs[job]==original
    assert first.json()['review']['payload']['nativeApplied'] is False
    assert first.json()['review']['payload']['engineeringAccepted'] is False
    assert first.json()['review']['payload']['actorBindingId']==str(BINDING)


def test_idempotency_key_cannot_change_decision(client,store,job):
    assert client.post(url(job),json=body(store,job),headers=headers()).status_code==201
    assert client.post(url(job),json=body(store,job,'reject'),headers=headers()).status_code==409
    assert len(store.edges)==1


@pytest.mark.parametrize('case',['wrong_project','wrong_job','no_membership','read_only','deleted_project','deleted_input','legacy_authority','running','bad_hash','changed_bytes','wrong_identity','unknown_proposal','invalid_proposal','missing_parent','other_parent'])
def test_refuses_unowned_stale_or_invalid_review(client,store,job,case):
    path=url(job);request=body(store,job)
    expected=409
    if case=='wrong_project': path=url(job,uuid.uuid4());expected=404
    elif case=='wrong_job': path=url(uuid.uuid4());expected=404
    elif case=='no_membership': store.role=None;expected=403
    elif case=='read_only': store.role='read_only';expected=403
    elif case=='deleted_project': store.live=False;expected=404
    elif case=='deleted_input': store.versions[store.jobs[job]['input_version_id']]['deleted']=True
    elif case=='legacy_authority': store.authority='legacy_sqlite'
    elif case=='running': store.jobs[job]['status']='running'
    elif case=='bad_hash': request['result_sha256']='b'*64
    elif case=='changed_bytes': store.jobs[job]['result']['solver_result']['proposals'][0]['new_field']='changed'
    elif case=='wrong_identity': store.jobs[job]['result']['solver_input']['organization_id']=str(uuid.uuid4())
    elif case=='unknown_proposal': request['proposal_id']='different-proposal'
    elif case=='invalid_proposal':
        result=store.jobs[job]['result']['solver_result'];result['proposals'][0]['violations']=['collision']
        store.jobs[job]['result']['result_sha256']=arlo_lab.digest(result);request['result_sha256']=arlo_lab.digest(result)
    elif case=='missing_parent': store.jobs[job]['result']['history_operation_id']=None
    else: store.history[uuid.UUID(store.jobs[job]['result']['history_operation_id'])]['payload']['jobId']=str(uuid.uuid4())
    response=client.post(path,json=request,headers=headers())
    assert response.status_code==expected,response.text
    assert not store.edges and not store.outbox


def test_read_requires_current_membership_and_detects_changed_review(client,store,job):
    saved=client.post(url(job),json=body(store,job),headers=headers()).json()['review']
    store.role='read_only'
    assert client.get(url(job),headers=headers()).status_code==200
    store.role=None
    assert client.get(url(job),headers=headers()).status_code==403
    store.role='owner'
    store.history[uuid.UUID(saved['operation_id'])]['payload']['decision']='reject'
    assert client.get(url(job),headers=headers()).status_code==409


def test_request_cannot_claim_identity_or_engineering_acceptance(client,store,job):
    assert client.post(url(job),json={**body(store,job),'engineeringAccepted':True},headers=headers()).status_code==422
    assert client.post(url(job),json=body(store,job),headers={'Idempotency-Key':'not-signed-in'}).status_code==401


def test_concurrent_duplicate_review_creates_one_event(store,job):
    decision=arlo_review.ReviewDecision.model_validate(body(store,job))
    def save(_): return arlo_review.review(ORG,PROJECT,BINDING,job,decision,'same-review')['review']['operation_id']
    with ThreadPoolExecutor(max_workers=2) as pool: ids=list(pool.map(save,range(2)))
    assert ids[0]==ids[1] and len(store.edges)==len(store.outbox)==1
