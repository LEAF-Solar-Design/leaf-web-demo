import base64,hashlib,json,uuid
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import deps,browser_project_flow as flow,browser_static_preview as preview
from routers import browser_project

def uid(n):return str(uuid.UUID(int=n))
@pytest.fixture
def fixture(monkeypatch):
 tenant=SimpleNamespace(subject='subject');project=uid(3);request=uid(4);job=uid(5);artifact=uid(6)
 current=(uid(1),project,uid(2));authority=dict(tenant_id=uid(1),organization_id=uid(1),project_id=project,repo_key=uid(7))
 row=dict(state='published',project_id=project,source_request_id=request,source_commit='a'*40,source_tree='b'*40,receipt_digest='c'*64,version=1)
 query={k:str(row[k]) for k in ('source_commit','source_tree','receipt_digest','version')};query.update(artifact_id=artifact,manifest_sha256='d'*64,path='/index.html')
 result=dict(schema='leaf.private-static-asset.v1',job_id=job,source_commit=row['source_commit'],source_tree=row['source_tree'],artifact_id=artifact,manifest_sha256='d'*64,path='/index.html',content_type='text/html',size_bytes=8,sha256=hashlib.sha256(b'<script>').hexdigest(),body_base64=base64.b64encode(b'<script>').decode())
 monkeypatch.setattr(flow,'scope',lambda *a,**k:current);monkeypatch.setattr(flow,'repository',lambda *a:authority);monkeypatch.setattr(flow,'read',lambda *a:dict(row))
 monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL','http://fixture.invalid');monkeypatch.setattr(preview.broker_client,'harness_headers',lambda:{'X-Harness-Secret':'synthetic-secret'})
 calls=[]
 class Response:
  status_code=200
  def iter_content(self,n):yield json.dumps(result).encode()
  def close(self):calls.append('closed')
 def post(*a,**k):calls.append(k);return Response()
 monkeypatch.setattr(preview.requests,'post',post)
 def read(q=None):return preview.asset(tenant,project,request,job,list((query if q is None else q).items()))
 return SimpleNamespace(**locals())
def test_valid_encoded_asset_and_no_bearer(fixture):
 f=fixture;assert f.read()==f.result;assert f.calls[-1]=='closed';assert 'Authorization' not in f.calls[0]['headers'];assert not f.calls[0]['allow_redirects']
@pytest.mark.parametrize('key,value',[('artifact_id','foreign'),('path','/../x'),('path','/x\\y'),('manifest_sha256','latest')])
def test_invalid_request_before_dispatch(fixture,key,value):
 f=fixture
 with pytest.raises(flow.FlowError):f.read(dict(f.query,**{key:value}))
 assert not f.calls
@pytest.mark.parametrize('key,value',[('source_tree','f'*40),('job_id',uid(99)),('body_base64','YQ'),('sha256','f'*64),('content_type','text/html\r\nX: bad')])
def test_foreign_or_corrupt_producer_refused(fixture,key,value):
 f=fixture;f.result[key]=value
 with pytest.raises(flow.FlowError):f.read()
 assert f.calls[-1]=='closed'
def test_revocation_after_read(fixture,monkeypatch):
 f=fixture
 class Response:
  status_code=200
  def iter_content(self,n):
   yield json.dumps(f.result).encode();monkeypatch.setattr(flow,'scope',lambda *a,**k:(_ for _ in ()).throw(flow.FlowError(403)))
  def close(self):pass
 monkeypatch.setattr(preview.requests,'post',lambda *a,**k:Response())
 with pytest.raises(flow.FlowError) as e:f.read()
 assert e.value.status==403
def test_route_is_inert_json_with_sandbox_headers(fixture):
 f=fixture;app=FastAPI();app.include_router(browser_project.router);app.dependency_overrides[deps.require_active_tenant]=lambda:f.tenant
 with TestClient(app) as c:r=c.get(f'/api/projects/{f.project}/browser/source/requests/{f.request}/preview/{f.job}/asset',params=f.query)
 assert r.status_code==200;assert r.headers['content-type']=='application/json';assert r.headers['x-content-type-options']=='nosniff';assert r.headers['cache-control']=='private, no-store';assert r.headers['content-security-policy'].startswith('sandbox;');assert r.json()['content_type']=='text/html';assert '<script>' not in r.text
