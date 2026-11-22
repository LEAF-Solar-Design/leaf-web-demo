import hashlib
import json
import uuid
from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import deps
import browser_project_flow as flow
import browser_static_build as build
from routers import browser_project


def uid(n):
    return str(uuid.UUID(int=n))


@pytest.fixture
def fixture(monkeypatch):
    tenant = SimpleNamespace(subject='fixture-subject')
    project, request, job = uid(3), uid(4), uid(5)
    scope = (uid(1), project, uid(2))
    authority = dict(tenant_id=uid(1), organization_id=uid(1), project_id=project, repo_key=uid(7))
    row = dict(state='published', project_id=project, source_request_id=request,
               version=1, receipt_digest='c'*64, source_commit='a'*40, source_tree='b'*40)
    witnesses = {k: row[k] for k in ('version', 'receipt_digest', 'source_commit', 'source_tree')}
    result = dict(schema='leaf.private-static-build.v1', jobId=job, status='building')
    calls, scopes = [], []
    def current(*a, **kw):
        scopes.append(kw.get('write', False))
        return scope
    monkeypatch.setattr(flow, 'scope', current)
    monkeypatch.setattr(flow, 'repository', lambda *a: deepcopy(authority))
    monkeypatch.setattr(flow, 'read', lambda *a: deepcopy(row))
    monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL', 'https://fixture.invalid')
    monkeypatch.setattr(build.broker_client, 'harness_headers', lambda: {'X-Harness-Secret': 'fixture-only'})
    class Response:
        status_code = 200
        def iter_content(self, n):
            yield json.dumps(result).encode()
        def close(self):
            calls.append('closed')
    def post(url, **kw):
        calls.append((url, kw))
        return Response()
    monkeypatch.setattr(build.requests, 'post', post)
    def exchange(submit=False):
        return build.exchange(tenant, project, request, job, witnesses, submit=submit)
    app = FastAPI()
    app.include_router(browser_project.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    path = f'/api/projects/{project}/browser/source/requests/{request}/preview/{job}'
    return SimpleNamespace(**locals())


def ready(f):
    manifest = dict(requestDigest=build._digest([f.authority[k] for k in ('tenant_id','organization_id','project_id','repo_key')]
        + [f.job, f.row['source_commit'], f.row['source_tree']]), bundleSha256='d'*64,
        bindingId=uid(8), toolchainSha256='e'*64,
        files=[dict(path='/index.html', sha256=hashlib.sha256(b'fixture').hexdigest(), sizeBytes=7, contentType='text/html')])
    files = [[v[k] for k in ('path','sha256','sizeBytes','contentType')] for v in manifest['files']]
    preview = dict(jobId=f.job, sourceCommit=f.row['source_commit'], sourceTree=f.row['source_tree'],
        bundleSha256=manifest['bundleSha256'], bindingId=manifest['bindingId'], toolchainSha256=manifest['toolchainSha256'],
        artifactBindingId=uid(9), artifactId=uid(10), manifest=manifest,
        manifestSha256=build._digest([manifest[k] for k in ('requestDigest','bundleSha256','bindingId','toolchainSha256')] + [files]))
    candidate = {build._CAMEL[k]: v for k,v in dict(f.witnesses,source_request_id=f.request).items()}
    f.result.update(status='ready', result=dict(schema='leaf.browser-candidate-preview.v1', candidate=candidate, preview=preview))
    return preview


def test_build_and_status_are_distinct_and_no_bearer_forwarded(fixture):
    f=fixture
    assert f.exchange()['status']=='building'
    assert f.calls[0][0].endswith('/status') and not any(f.scopes)
    f.calls.clear();f.scopes.clear()
    assert f.exchange(True)['status']=='building'
    url,kwargs=f.calls[0]
    assert url.endswith('/build') and all(f.scopes)
    assert 'Authorization' not in kwargs['headers'] and not kwargs['allow_redirects']
    assert json.loads(kwargs['data'])==dict(f.authority,source_request_id=f.request,job_id=f.job,**f.witnesses)
    assert f.calls[-1]=='closed'


def test_ready_manifest_is_verified_before_return(fixture):
    f=fixture;ready(f)
    assert f.exchange()==f.result


@pytest.mark.parametrize('mutation', ['job','candidate','artifact','digest','path','size','scope','unready_result','boolean_version'])
def test_foreign_or_corrupt_ready_result_refused(fixture,mutation):
    f=fixture;p=ready(f)
    if mutation=='job':p['jobId']=uid(99)
    elif mutation=='candidate':f.result['result']['candidate']['receiptDigest']='f'*64
    elif mutation=='artifact':p['artifactId']='not-a-uuid'
    elif mutation=='digest':p['manifestSha256']='f'*64
    elif mutation=='path':p['manifest']['files'][0]['path']='/../index.html'
    elif mutation=='size':p['manifest']['files'][0]['sizeBytes']=True
    elif mutation=='scope':p['manifest']['requestDigest']='f'*64
    elif mutation=='boolean_version':f.result['result']['candidate']['version']=True
    else:f.result['status']='building'
    with pytest.raises(flow.FlowError) as error:f.exchange()
    assert error.value.status==503 and f.calls[-1]=='closed'


def test_stale_candidate_is_refused_before_dispatch(fixture):
    f=fixture;f.row['source_tree']='f'*40
    with pytest.raises(flow.FlowError):f.exchange(True)
    assert not f.calls


def test_revocation_while_reading_never_returns_ready(fixture,monkeypatch):
    f=fixture;ready(f)
    class Response:
        status_code=200
        def iter_content(self,n):
            yield json.dumps(f.result).encode()
            monkeypatch.setattr(flow,'scope',lambda *a,**kw:(_ for _ in ()).throw(flow.FlowError(403)))
        def close(self):f.calls.append('closed')
    monkeypatch.setattr(build.requests,'post',lambda *a,**kw:Response())
    with pytest.raises(flow.FlowError) as error:f.exchange()
    assert error.value.status==403 and f.calls[-1]=='closed'


@pytest.mark.parametrize('raw',[b'{}',b'{"version":1,"version":2}',b'[]',b'"x"',b'\xff'])
def test_invalid_build_body_refused(raw):
    with pytest.raises(flow.FlowError):build.parse_body(raw)


def test_customer_routes_use_existing_auth_and_read_only_status(fixture):
    f=fixture
    with TestClient(f.app) as client:
        response=client.post(f.path,json=f.witnesses)
        assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
        assert f.calls[0][0].endswith('/build')
        f.calls.clear();f.scopes.clear()
        response=client.get(f.path,params=f.witnesses)
        assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
        assert f.calls[0][0].endswith('/status') and not any(f.scopes)


def test_denied_write_never_consumes_body_or_dispatches(fixture,monkeypatch):
    f=fixture
    def scope(*a,**kw):
        if kw.get('write'):raise flow.FlowError(403)
        return f.scope
    monkeypatch.setattr(flow,'scope',scope)
    with TestClient(f.app) as client:
        response=client.post(f.path,content=b'not JSON',headers={'Content-Type':'application/json'})
    assert response.status_code==403 and not f.calls


def test_upstream_size_cap_and_close(fixture,monkeypatch):
    f=fixture
    class Response:
        status_code=200
        def iter_content(self,n):yield b'x'*(build.MAX_RESPONSE+1)
        def close(self):f.calls.append('closed')
    monkeypatch.setattr(build.requests,'post',lambda *a,**kw:Response())
    with pytest.raises(flow.FlowError) as error:f.exchange()
    assert error.value.status==503 and f.calls==['closed']


@pytest.fixture
def chart_fixture(fixture,monkeypatch):
    import browser_chart_state as chart
    f=fixture;p=ready(f);real=chart._store()
    fields=dict(f.witnesses,artifact_id=p['artifactId'],manifest_sha256=p['manifestSha256'])
    d=chart.descriptor(f.request,f.job,[(k,str(v)) for k,v in fields.items()])
    state=dict(version=1,range='all',scale='Log',reference='2023-01-01',averaging=56,timeAxis='expanded2026',series=['a'],groups=[])
    graph_calls=[];saved={'value':None}
    def read(*args):graph_calls.append(('read',args));return deepcopy(saved['value'])
    def save(*args,**kw):
        graph_calls.append(('save',args,kw));saved['value']=dict(revision=1,digest='e'*64,descriptor=deepcopy(args[3]),state=deepcopy(args[4]));return deepcopy(saved['value'])
    backend=SimpleNamespace(read=read,save=save,lifecycle=real.lifecycle,state_copy=real.state_copy)
    monkeypatch.setattr(chart,'_store',lambda:backend)
    body=dict(descriptor=fields,state=state,expected_revision=0,save_id=uid(100))
    return SimpleNamespace(**locals())


def test_chart_state_save_reload_uses_scoped_graph_and_read_only_preview(chart_fixture):
    q=chart_fixture;f=q.f
    with TestClient(f.app) as client:
        path=f.path+'/chart-state'
        assert client.get(path,params=q.fields).json()=={'chart':None}
        response=client.put(path,json=q.body)
        assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
        assert response.json()['chart']['state']==q.state
        assert client.get(path,params=q.fields).json()==response.json()
    args=next(v[1] for v in q.graph_calls if v[0]=='save')
    assert args[:3]==tuple(uuid.UUID(v) for v in f.scope)
    assert args[3]==q.d and all(v[0].endswith('/status') for v in f.calls if isinstance(v,tuple))


def test_chart_state_foreign_artifact_refused_before_graph(chart_fixture):
    q=chart_fixture
    with TestClient(q.f.app) as client:r=client.get(q.f.path+'/chart-state',params=dict(q.fields,artifact_id=uid(99)))
    assert r.status_code==409 and not q.graph_calls


def test_chart_state_revocation_during_graph_read_hides_state(chart_fixture,monkeypatch):
    q=chart_fixture
    def read(*args):
        monkeypatch.setattr(flow,'scope',lambda *a,**kw:(_ for _ in ()).throw(flow.FlowError(403)))
        return dict(revision=1,digest='e'*64,descriptor=q.d,state=q.state)
    q.backend.read=read
    with TestClient(q.f.app) as client:r=client.get(q.f.path+'/chart-state',params=q.fields)
    assert r.status_code==403 and 'series' not in r.text


def test_chart_state_conflict_preserves_request_and_never_builds(chart_fixture):
    q=chart_fixture
    def conflict(*a,**kw):raise q.backend.lifecycle.LifecycleConflict('private detail')
    q.backend.save=conflict
    with TestClient(q.f.app) as client:r=client.put(q.f.path+'/chart-state',json=q.body)
    assert r.status_code==409 and 'private detail' not in r.text
    assert all(v[0].endswith('/status') for v in q.f.calls if isinstance(v,tuple))


@pytest.mark.parametrize('mutation',['boolean_version','extra_descriptor','invalid_state','boolean_revision'])
def test_chart_state_invalid_body_never_reaches_graph(chart_fixture,mutation):
    q=chart_fixture;body=deepcopy(q.body)
    if mutation=='boolean_version':body['descriptor']['version']=True
    elif mutation=='extra_descriptor':body['descriptor']['org_id']=uid(99)
    elif mutation=='invalid_state':body['state']['series']=['a','a']
    else:body['expected_revision']=True
    with TestClient(q.f.app) as client:r=client.put(q.f.path+'/chart-state',json=body)
    assert r.status_code==422 and not q.graph_calls and not q.f.calls


def test_chart_state_unauthorized_requests_never_dispatch(monkeypatch):
    import browser_chart_state as chart
    monkeypatch.setenv('LEAF_AUTH_LIVE','1')
    monkeypatch.setattr(chart,'exchange',lambda *a,**kw:pytest.fail('unauthenticated chart dispatch'))
    app=FastAPI();app.include_router(browser_project.router)
    path=f'/api/projects/{uid(3)}/browser/source/requests/{uid(4)}/preview/{uid(5)}/chart-state'
    with TestClient(app) as client:
        for method in ('GET','PUT'):
            for headers in ({},{'Authorization':'Bearer invalid'}):
                assert client.request(method,path,headers=headers,json={} if method=='PUT' else None).status_code in (401,403)
    from route_flatten import iter_leaf_routes
    routes=[r for path,r in iter_leaf_routes(app.routes) if path.endswith('/chart-state')]
    assert len(routes)==2 and all(deps.require_active_tenant in [d.call for d in r.dependant.dependencies] for r in routes)



def test_chart_state_denied_write_precedes_body_read(chart_fixture,monkeypatch):
    q=chart_fixture
    def scope(*a,**kw):
        if kw.get('write'):raise flow.FlowError(403)
        return q.f.scope
    monkeypatch.setattr(flow,'scope',scope)
    with TestClient(q.f.app) as client:r=client.put(q.f.path+'/chart-state',content=b'not JSON',headers={'Content-Type':'application/json'})
    assert r.status_code==403 and not q.graph_calls and not q.f.calls
