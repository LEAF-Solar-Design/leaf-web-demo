"""Hermetic F22 worker checks. These fixtures are not native/live evidence."""
import ast
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import plistlib
import queue
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import threading
import types
import zipfile
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_worker as worker
import native_preview_mac as mac
import native_preview_reconciliation as reconciliation

ORG = '11111111-1111-4111-8111-111111111111'
PROJECT = '22222222-2222-4222-8222-222222222222'
ACTOR = '33333333-3333-4333-8333-333333333333'
JOB = '44444444-4444-4444-8444-444444444444'
OWNER = '55555555-5555-4555-8555-555555555555'
UDID = '66666666-6666-4666-8666-666666666666'
MID = '77777777-7777-4777-8777-777777777777'
SCOPE = dict(org_id=ORG, project_id=PROJECT, actor_id=ACTOR, worker_id='fixture-worker')


def png():
    body = b'IHDR' + struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    header = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + body + struct.pack('>I', zlib.crc32(body))
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))
    return header + chunk(b'IDAT', zlib.compress(b'\0\0\0\0')) + chunk(b'IEND', b'')


@pytest.fixture
def source():
    asset = b'fixture-jpeg-data'
    digest = hashlib.sha256(asset).hexdigest()
    doc = dict(schema='leaf.portfolio.v1', name='Fixture', headline='Preview', introduction='',
               about='', contact=dict(email='', location=''), resume=[], theme='light',
               categories=[dict(slug='work', title='Work', description='', hero=0,
                                images=[dict(material_id=MID, sha256=digest, preview_sha256=digest,
                                             title='', alt='Fixture photo')])])
    template = worker.producer()
    raw = template.source_archive(doc, 1, {MID: asset})
    descriptor = dict(schema='leaf.project-preview-source.v1', target='ios-simulator',
                      org_id=ORG, project_id=PROJECT, revision=1,
                      document_sha256=template.document_digest(doc),
                      archive_sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    job = dict(job_id=JOB, lease_owner=OWNER, attempt=1, descriptor=descriptor, deadline=time.time() + 300)
    return raw, job


def facts():
    return dict(simulator_udid=UDID, simulator_runtime='com.apple.CoreSimulator.SimRuntime.iOS-26-0',
                bundle_id=mac.BUNDLE, allocator_ticket='fixture-ticket', build_exit_code=0,
                install_exit_code=0, launch_exit_code=0, cleanup_complete=True,
                allocator_release_confirmed=True, screenshot_sha256=hashlib.sha256(png()).hexdigest())


@pytest.fixture(params=['create', 'edit'])
def native_package(request):
    """Authored-source-shaped fixture only, not model or SDK execution evidence."""
    data = b'{"complete":"input values"}\n'
    files = {'project.yml':('100644',worker.NATIVE_PROJECT_SPEC.replace('${bundleId}','private.leaf.project.p'+PROJECT.replace('-','')).encode()),
        'Sources/LeafProjectApp.swift':('100644',worker.NATIVE_APP_SOURCE.encode()),
        'Sources/ContentView.swift':('100644',b'import SwiftUI\nstruct ContentView: View { var body: some View { Text("Input") } }\n'),
        'Resources/Input/'+MID+'.json':('100644',data)}
    if request.param == 'edit':
        files['Sources/ContentView.swift'] = ('100644', files['Sources/ContentView.swift'][1].replace(b'Input', b'Revised input'))
    d = dict(schema=worker.NATIVE_SCHEMA,target='ios-simulator',source_profile=worker.NATIVE_PROFILE,
        org_id=ORG,project_id=PROJECT,actor_binding_id=ACTOR,repo_key=OWNER,source_request_id=JOB,
        source_operation=request.param,source_version=4,receipt_digest='a'*64,source_commit='b'*40,
        source_tree=worker.native_tree(files),material_refs=[dict(material_id=MID,
            sha256=hashlib.sha256(data).hexdigest(),byte_size=len(data),path='Resources/Input/'+MID+'.json')],
        bundle_sha256='c'*64,archive_sha256='0'*64,byte_size=1)
    raw=worker.native_archive(files,d)
    d.update(archive_sha256=hashlib.sha256(raw).hexdigest(),byte_size=len(raw))
    job=dict(job_id=JOB,lease_owner=OWNER,attempt=1,descriptor=d,deadline=time.time()+300)
    return raw,job


def test_native_package_is_complete_tree_and_matches_captured_profile(native_package):
    raw,job=native_package
    assert worker.validate_claim({'job':job},SCOPE,time.time()) == job
    assert worker.verify_source(raw,job['descriptor']) == raw
    files=worker.verify_native_archive(raw,job['descriptor'])
    assert worker.native_tree(files)==job['descriptor']['source_tree']
    profile=(Path(__file__).resolve().parents[2]/'harness/src/agent/nativeProjectSourceProfile.ts').read_text()
    import re
    assert worker.NATIVE_APP_SOURCE==re.search(r'const APP_SOURCE = `(.*?)`;',profile,re.S).group(1)
    assert worker.NATIVE_PROJECT_SPEC==re.search(r'"project.yml": `(.*?)`,',profile,re.S).group(1)


def test_native_edit_cannot_omit_retained_input_provenance(native_package):
    import copy
    import uuid
    raw, job = copy.deepcopy(native_package)
    descriptor = job['descriptor']
    files = worker.verify_native_archive(raw, descriptor)
    mid, data = str(uuid.uuid4()), b'Another retained original\n'
    path = 'Resources/Input/'+mid+'.txt'
    files[path] = ('100644', data)
    descriptor.update(source_operation='edit', source_tree=worker.native_tree(files))
    descriptor['material_refs'].append(dict(material_id=mid, sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data), path=path))
    raw = worker.native_archive(files, descriptor)
    descriptor.update(archive_sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    assert worker.verify_source(raw, descriptor) == raw
    descriptor['material_refs'].pop()
    with pytest.raises(worker.WorkerError, match='source-integrity'):
        worker.verify_source(raw, descriptor)


@pytest.mark.parametrize('kind',['profile','foreign','hash','tree','empty-material','oversize-material','operation','corrupt'])
def test_native_refusal_precedes_allocator(native_package,monkeypatch,kind):
    import copy
    raw,job=copy.deepcopy(native_package)
    d=job['descriptor']
    if kind=='profile':d['source_profile']='web-next-static-v1'
    elif kind=='foreign':d['project_id']=ACTOR
    elif kind=='hash':d['archive_sha256']='f'*64
    elif kind=='tree':d['source_tree']='f'*40
    elif kind=='empty-material':d['material_refs']=[]
    elif kind=='oversize-material':d['material_refs'][0]['byte_size']=1048577
    elif kind=='operation':d['source_operation']='rollback'
    else:raw=b'corrupt ZIP'
    monkeypatch.setattr(mac,'canonical_allocator',lambda:pytest.fail('allocator reached'))
    if kind=='foreign':
        with pytest.raises(worker.WorkerError):worker.validate_claim({'job':job},SCOPE,time.time())
    else:
        with pytest.raises(worker.WorkerError):mac.MacAdapter()(raw,job,types.SimpleNamespace())


def test_native_worker_completion_carries_sdk_identity(native_package,tmp_path,monkeypatch):
    raw,job=native_package
    monkeypatch.setattr(worker, 'producer', lambda: pytest.fail('native source must not re-import Portfolio'))
    http=Transport(native_package)
    bundle='private.leaf.project.p'+PROJECT.replace('-','')
    result_facts=dict(facts(),bundle_id=bundle,build_artifact=dict(source=worker.native_identity(job['descriptor']),
        bundle_id=bundle,sdk='iphonesimulator',app_tree_sha256='d'*64,file_count=5,xcodegen_exit_code=0,
        xcode_version='Xcode fixture',xcodegen_version='fixture version',sdk_version='fixture sdk'))
    result=worker.run_once(SCOPE,http,lambda *a:(result_facts,png()),tmp_path)
    assert result['result']=='complete'
    receipt=http.calls[-1][2]['receipt']
    assert receipt['build_artifact']['source']['source_tree']==job['descriptor']['source_tree']
    assert receipt['bundle_id']==bundle


def test_native_recovery_projection_preserves_source_and_rejects_drift(native_package):
    _,job=native_package
    identity=worker.native_identity(job['descriptor'])
    assert reconciliation.source(identity)==identity
    assert 'revision' not in identity and 'document_sha256' not in identity
    claim=dict(job_id=JOB,source=identity)
    status=worker.encode(dict(job_id=JOB,status='failed',**identity))
    assert reconciliation.settled_failure_status(status,claim)['backend_status_sha256']==hashlib.sha256(status).hexdigest()
    with pytest.raises(worker.WorkerError):
        reconciliation.settled_failure_status(worker.encode(dict(job_id=JOB,status='failed',**dict(identity,source_commit='f'*40))),claim)


@pytest.mark.parametrize('fail_install',[False,True,'xcodegen-unavailable'])
def test_native_driver_generates_fixed_project_builds_and_hashes_copied_app(native_package,tmp_path,capsys,fail_install):
    raw,job=native_package
    expected_files = worker.verify_native_archive(raw, job['descriptor'])
    owned,derived,internal=tmp_path/'owned',tmp_path/'derived',tmp_path/'internal'
    owned.mkdir()
    commands=[]
    bundle='private.leaf.project.p'+PROJECT.replace('-','')
    runtime='com.apple.CoreSimulator.SimRuntime.iOS-26-0'
    def checked(argv,timeout=30,cwd=None):
        commands.append(argv)
        if argv==['/usr/bin/xcodebuild','-version']:return b'Xcode fixture\nBuild fixture'
        if argv==['/Users/evanhaug/xcodegen/xcodegen/bin/xcodegen','--version']:return b'fixture version'
        if argv==['/usr/bin/xcrun','--sdk','iphonesimulator','--show-sdk-version']:return b'26.0'
        if argv[0]=='/Users/evanhaug/xcodegen/xcodegen/bin/xcodegen':
            assert argv==['/Users/evanhaug/xcodegen/xcodegen/bin/xcodegen','generate','--spec','project.yml']
            assert cwd==owned/'source'
            assert (cwd/'Sources/ContentView.swift').read_bytes() == expected_files['Sources/ContentView.swift'][1]
            assert (cwd/('Resources/Input/'+MID+'.json')).read_bytes() == expected_files['Resources/Input/'+MID+'.json'][1]
            if fail_install=='xcodegen-unavailable':raise FileNotFoundError('fixed managed XcodeGen unavailable')
        elif argv[0]=='/usr/bin/xcodebuild':
            assert argv[argv.index('-project')+1]=='LeafProject.xcodeproj'
            assert argv[argv.index('-scheme')+1]=='LeafProject'
            assert 'CODE_SIGNING_ALLOWED=NO' in argv and UDID not in str(argv)
            app=derived/'Build/Products/Debug-iphonesimulator/LeafProject.app'
            app.mkdir(parents=True)
            (app/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':bundle}))
            (app/'LeafProject').write_bytes(b'not a real SDK binary; fixture only')
        elif argv[0]=='/usr/bin/ditto':shutil.copytree(argv[1],argv[2])
        else:
            assert argv[:2]==['/usr/bin/xcrun','simctl']
            if argv[2]=='list':return json.dumps({'devices':{runtime:[dict(udid=UDID,isAvailable=True,deviceTypeIdentifier='com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro-Max')]}}).encode()
            if argv[2]=='listapps':return plistlib.dumps({})
            if argv[2]=='install':
                assert argv[-1].endswith('LeafProject.app')
                if fail_install: raise RuntimeError('fixture installation failed after SDK build')
            if argv[2]=='launch':assert argv[-1]==bundle
            if argv[2]=='io':Path(argv[-1]).write_bytes(png())
        return b''
    payload=dict(archive=base64.b64encode(raw).decode(),byte_size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
        budget=500,udid=UDID,ticket='fixture-ticket',name='fixture-owned',descriptor=job['descriptor'])
    namespace=dict(base64=base64,hashlib=hashlib,io=io,json=json,plistlib=plistlib,zipfile=zipfile,
        time=types.SimpleNamespace(sleep=lambda _:None),sys=types.SimpleNamespace(stdin=io.StringIO(json.dumps(payload))),
        roots=lambda _:(owned,derived,internal),permitted=lambda _:None,checked=checked,application_inventory=plistlib.loads)
    if fail_install=='xcodegen-unavailable':
        with pytest.raises(FileNotFoundError,match='fixed managed XcodeGen unavailable'):
            exec(mac.native_remote_policy()+mac.DRIVER,namespace)
        assert commands==[['/Users/evanhaug/xcodegen/xcodegen/bin/xcodegen','generate','--spec','project.yml']]
        assert not (owned/'sdk-build.json').exists() and not (owned/'install-owned').exists()
        return
    if fail_install:
        with pytest.raises(RuntimeError,match='after SDK build'):
            exec(mac.native_remote_policy()+mac.DRIVER,namespace)
        result={'build_artifact':json.loads((owned/'sdk-build.json').read_text())}
    else:
        exec(mac.native_remote_policy()+mac.DRIVER,namespace)
        result=json.loads(capsys.readouterr().out)
    artifact=result['build_artifact']
    assert artifact['source']==worker.native_identity(job['descriptor']) and artifact['bundle_id']==bundle
    copied=internal/'LeafProject.app'
    manifest={str(p.relative_to(copied)):hashlib.sha256(p.read_bytes()).hexdigest() for p in copied.rglob('*') if p.is_file()}
    assert artifact['app_tree_sha256']==hashlib.sha256(worker._native_json(manifest)).hexdigest()
    worker.validate_native_artifact(artifact,job['descriptor'])
    assert commands.index(['/Users/evanhaug/xcodegen/xcodegen/bin/xcodegen','generate','--spec','project.yml']) < next(i for i,c in enumerate(commands) if '-project' in c)
    assert ['/Users/evanhaug/xcodegen/xcodegen/bin/xcodegen','--version'] in commands
    capture=next(node for node in ast.parse(mac.REMOTE_COMMON).body if isinstance(node,ast.FunctionDef) and node.name=='checked').body[0]
    assert eval(compile(ast.Expression(capture.value),'<capture>','eval'),{'argv':['/Users/evanhaug/xcodegen/xcodegen/bin/xcodegen','--version']}) is True
    assert (owned/'install-owned').read_text()==bundle


@pytest.mark.skipif(sys.platform=='win32',reason='Remote cleanup owns POSIX Mac directories and locks')
def test_native_cleanup_removes_only_project_bound_install(tmp_path):
    owned,derived,internal=tmp_path/'owned',tmp_path/'derived',tmp_path/'internal'
    name='fixture-owned'
    for folder in (owned,derived,internal):
        folder.mkdir();(folder/'owner').write_text(name)
    bundle='private.leaf.project.p'+PROJECT.replace('-','')
    (owned/'install-owned').write_text(bundle)
    state=dict(booted=True,installed=True)
    calls=[]
    def checked(argv):
        if argv[2]=='list':return json.dumps({'devices':{'runtime':[dict(udid=UDID,state='Booted' if state['booted'] else 'Shutdown')]}}).encode()
        assert argv[2]=='listapps'
        return plistlib.dumps({bundle:{}} if state['installed'] else {})
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[2]=='uninstall':assert argv[-1]==bundle;state['installed']=False
        elif argv[2]=='shutdown':state['booted']=False
        elif argv[2]=='terminate':assert argv[-1]==bundle
        return types.SimpleNamespace(returncode=0)
    namespace=dict(json=json,re=__import__('re'),os=__import__('os'),time=time,shutil=shutil,
        subprocess=types.SimpleNamespace(run=run),fcntl=__import__('fcntl'),
        sys=types.SimpleNamespace(stdin=io.StringIO(json.dumps(dict(ticket='fixture-ticket',udid=UDID,name=name,native_project_id=PROJECT)))),
        roots=lambda _:(owned,derived,internal),checked=checked,application_inventory=plistlib.loads)
    exec(mac.CLEANUP,namespace)
    assert not state['installed'] and not state['booted']
    assert not any(folder.exists() for folder in (owned,derived,internal))
    assert all(mac.BUNDLE not in argv for argv in calls)


class Transport:
    def __init__(self, source):
        self.raw, self.job = source
        self.calls = []
        self.deny = False
        self.uncertain = False

    def request(self, method, path, body=None, limit=16384):
        self.calls.append((method, path, body, limit))
        if path == 'claim':
            return worker.encode({'job': self.job})
        if path.endswith('/heartbeat'):
            if self.deny:
                raise worker.WorkerError('http-denied')
            return worker.encode({k: self.job[k] for k in ('job_id', 'lease_owner', 'attempt', 'deadline')})
        if '/source?' in path:
            return self.raw
        assert path.endswith('/complete')
        if self.uncertain:
            raise TimeoutError('synthetic secret must not escape')
        return worker.encode(dict(job_id=JOB, status=body['outcome']))


def test_exact_happy_sequence_and_closed_receipt(source, tmp_path):
    http = Transport(source)
    def native(raw, job, guard):
        assert raw == source[0]
        guard.check()
        return facts(), png()
    result = worker.run_once(SCOPE, http, native, tmp_path)
    assert result['result'] == 'complete'
    assert [path.split('/')[-1].split('?')[0] for _, path, _, _ in http.calls] == [
        'claim', 'heartbeat', 'source', 'heartbeat', 'complete']
    receipt = http.calls[-1][2]['receipt']
    assert set(receipt) == set(facts()) | {'schema', 'job_id', 'lease_owner', 'attempt',
                                         'archive_sha256', 'worker_binding', 'target'}
    assert receipt['worker_binding'] == SCOPE and receipt['attempt'] == 1
    assert receipt['schema'] == 'leaf.native-preview-worker-receipt.v1'
    assert base64.b64decode(http.calls[-1][2]['screenshot_base64']) == png()
    assert (tmp_path / (JOB + '-1.png')).read_bytes() == png()


def test_empty_has_no_native_effects(source, tmp_path):
    http = Transport(source)
    http.job = None
    assert worker.run_once(SCOPE, http, lambda *a: pytest.fail('native called'), tmp_path)['result'] == 'empty'
    assert len(http.calls) == 1


def test_claim_journal_failure_prevents_source_and_native(source, tmp_path):
    http = Transport(source)
    def broken(event):
        assert event['event'] == 'claim' and 'lease_owner' not in event
        raise OSError('synthetic-secret')
    result = worker.run_once(SCOPE, http, lambda *args: pytest.fail('native'), tmp_path, journal=broken)
    assert result['result'] == 'failed'
    assert [path for _, path, _, _ in http.calls] == ['claim']


def test_journal_failure_remains_a_producer_barrier(source):
    def broken(event):
        raise OSError('synthetic-secret')
    guard = worker.Authority(Transport(source), source[1], journal=broken)
    guard.beat()
    with pytest.raises(worker.WorkerError, match='journal-failed'):
        guard.record(dict(event='producer', producer_may_have_started=True))
    # Even an adapter that catches the callback error cannot clear this barrier
    # by restoring cancellation state or obtaining another acknowledgment.
    guard.cancelled.clear()
    with pytest.raises(worker.WorkerError, match='journal-failed'):
        guard.check()


@pytest.mark.parametrize('same_job', [True, False])
def test_reconciliation_receipt_blocks_only_old_job(source, tmp_path, same_job):
    cycle = tmp_path / 'cycle-next'
    cycle.mkdir()
    old = JOB if same_job else ACTOR
    reconciliation.receipt_path(tmp_path, old).write_bytes(b'{}')
    journal = reconciliation.Journal(cycle, 'a' * 64, tmp_path)
    http = Transport(source)
    calls = []
    def native(*args):
        calls.append(True)
        return facts(), png()
    result = worker.run_once(SCOPE, http, native, cycle, journal=journal)
    assert bool(calls) is (not same_job)
    assert result['result'] == ('failed' if same_job else 'complete')
    if same_job:
        assert [path for _, path, _, _ in http.calls] == ['claim']


@pytest.mark.parametrize('key,value', [('lease_owner', 'BAD'), ('attempt', True), ('attempt', 0),
                                      ('deadline', float('inf')), ('deadline', 0), ('deadline', True)])
def test_claim_identity_deadline_rejected(source, key, value):
    source[1][key] = value
    with pytest.raises(worker.WorkerError):
        worker.validate_claim({'job': source[1]}, SCOPE, time.time())


@pytest.mark.parametrize('key,value', [('org_id', PROJECT), ('project_id', ORG), ('revision', True),
                                      ('archive_sha256', 'a'), ('byte_size', 0), ('extra', 'bad')])
def test_descriptor_closed_and_scoped(source, key, value):
    source[1]['descriptor'][key] = value
    with pytest.raises(worker.WorkerError):
        worker.validate_claim({'job': source[1]}, SCOPE, time.time())


def rewrite(source, change):
    raw, job = source
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(output, 'w') as new:
        for info in old.infolist():
            new.writestr(info, change(info.filename, old.read(info)))
    raw = output.getvalue()
    job['descriptor'].update(byte_size=len(raw), archive_sha256=hashlib.sha256(raw).hexdigest())
    return raw


@pytest.mark.parametrize('path', ['Portfolio/PortfolioApp.swift', 'Portfolio.xcodeproj/project.pbxproj',
                                 'Portfolio.xcodeproj/xcshareddata/xcschemes/Portfolio.xcscheme'])
def test_recomputed_digest_cannot_substitute_executable_source(source, path):
    raw = rewrite(source, lambda name, value: value + b'\n// hostile\n' if name == path else value)
    with pytest.raises(worker.WorkerError, match='producer-conflict'):
        worker.verify_source(raw, source[1]['descriptor'])


@pytest.mark.parametrize('name,mode', [('../escape', 0o100644), ('/escape', 0o100644),
                                     ('link', 0o120777), ('pipe', 0o010644)])
def test_zip_path_and_type_fences(source, name, mode):
    output = io.BytesIO(source[0])
    with zipfile.ZipFile(output, 'a') as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        archive.writestr(info, b'bad')
    raw = output.getvalue()
    descriptor = {**source[1]['descriptor'], 'byte_size': len(raw), 'archive_sha256': hashlib.sha256(raw).hexdigest()}
    with pytest.raises(worker.WorkerError, match='unsafe-archive'):
        worker.verify_source(raw, descriptor)


def test_exact_source_and_document_digest(source):
    assert worker.verify_source(*[source[0], source[1]['descriptor']]) == source[0]
    source[1]['descriptor']['document_sha256'] = '0' * 64
    with pytest.raises(worker.WorkerError, match='document-integrity'):
        worker.verify_source(source[0], source[1]['descriptor'])


@pytest.mark.parametrize('token', ['', 'a\rb', 'a\nb', 'a b', 'x' * 4097, '\x00'])
def test_stdin_auth_bounds(token):
    with pytest.raises(worker.WorkerError):
        worker.read_bearer(io.StringIO(token))


def test_auth_stdin_and_tty():
    assert worker.read_bearer(io.StringIO('synthetic\n')) == 'synthetic'
    stream = io.StringIO('synthetic')
    stream.isatty = lambda: True
    with pytest.raises(worker.WorkerError):
        worker.read_bearer(stream)


@pytest.mark.parametrize('origin', ['http://platform-staging.leafdesign.ai', worker.ORIGIN + '/',
                                   worker.ORIGIN + '?token=bad', 'https://user@platform-staging.leafdesign.ai'])
def test_origin_pinned(origin):
    with pytest.raises(worker.WorkerError):
        worker.Http(SCOPE, 'synthetic', origin)


@pytest.mark.parametrize('method,suffix,body', [
    ('POST', 'claim', {}), ('GET', 'job/source?attempt=1', None),
    ('POST', 'job/heartbeat', {}), ('POST', 'job/complete', {}),
])
def test_http_product_user_agent(method, suffix, body, monkeypatch):
    monkeypatch.setattr(worker, 'producer', lambda: pytest.fail('bounded native requests must not import Portfolio'))
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return http.prefix + suffix
        def read(self, size): return b'{}'
    class Opener:
        def open(self, request, timeout):
            assert request.get_header('User-agent') == 'leaf-native-preview-worker/1'
            assert request.get_header('Authorization') == 'Bearer synthetic'
            assert request.get_method() == method
            return Response()
    http = worker.Http(SCOPE, 'synthetic', opener=Opener())
    assert http.request(method, suffix, body) == b'{}'


def test_http_limits_redirect_and_redaction():
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return worker.ORIGIN + '/wrong'
        def read(self, size): return b'x' * size
    class Opener:
        def open(self, request, timeout):
            assert timeout == 10
            assert request.get_header('Authorization') == 'Bearer synthetic'
            return Response()
    http = worker.Http(SCOPE, 'synthetic', opener=Opener())
    with pytest.raises(worker.WorkerError, match='http-denied'):
        http.request('POST', 'claim', {})
    Response.geturl = lambda self: http.prefix + 'claim'
    with pytest.raises(worker.WorkerError, match='response-bounds'):
        http.request('POST', 'claim', {}, limit=4)
    def broken(*a, **kw): raise OSError('Bearer synthetic private url')
    http.opener.open = broken
    with pytest.raises(worker.WorkerError) as caught:
        http.request('POST', 'claim', {})
    assert str(caught.value) == 'http-unavailable'
    assert worker.NoRedirect().redirect_request(None, None, None, None, None, None) is None


@pytest.mark.parametrize('bad', ['redirect', 'oversize', 'unavailable'])
def test_status_bounds_and_redirect_refusal(bad):
    calls = []
    url = worker.ORIGIN + '/api/projects/' + PROJECT + '/native-previews/' + JOB
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return url if bad != 'redirect' else worker.ORIGIN + '/wrong'
        def read(self, size):
            assert size == 5
            return b'x' * size
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == url and request.method == 'GET' and timeout == 10
            if bad == 'unavailable':
                raise OSError('synthetic-secret')
            return Response()
    with pytest.raises(worker.WorkerError) as caught:
        worker.Http(SCOPE, 'synthetic', opener=Opener()).status(JOB, 4)
    assert len(calls) == 1 and 'synthetic-secret' not in str(caught.value)


def test_lost_heartbeat_sticky_and_no_success(source, tmp_path):
    http = Transport(source)
    def native(raw, job, guard):
        http.deny = True
        with pytest.raises(worker.WorkerError): guard.beat()
        http.deny = False
        guard.check()
        pytest.fail('subsequent effect')
    result = worker.run_once(SCOPE, http, native, tmp_path)
    assert result['result'] == 'failed'
    assert not any(path.endswith('/complete') for _, path, _, _ in http.calls)


def test_queue_sleep_interrupts(source):
    http = Transport(source)
    guard = worker.Authority(http, source[1])
    guard.beat()
    guard.cancelled.set()
    with pytest.raises(worker.WorkerError): guard.sleep(100)


@pytest.mark.parametrize('field,value', [('attempt', 2), ('deadline', 9999999999), ('extra', True)])
def test_heartbeat_drift_is_cancellation(source, field, value):
    http = Transport(source)
    original = http.request
    def request(method, path, body=None, **kw):
        reply = worker.parse(original(method, path, body, **kw))
        reply[field] = value
        return worker.encode(reply)
    http.request = request
    guard = worker.Authority(http, source[1])
    with pytest.raises(worker.WorkerError): guard.beat()
    assert guard.cancelled.is_set()


def test_source_transfer_failure_settles_failed(source, tmp_path):
    http = Transport(source)
    original = http.request
    def request(method, path, body=None, **kw):
        if '/source?' in path: raise TimeoutError('synthetic')
        return original(method, path, body, **kw)
    http.request = request
    assert worker.run_once(SCOPE, http, lambda *a: pytest.fail('native'), tmp_path)['result'] == 'failed'
    assert http.calls[-1][2]['outcome'] == 'failed'
    local = failure_receipt(tmp_path)
    assert local['execution_stage'] == 'source'
    assert local['failure_settlement'] == 'accepted'


@pytest.mark.parametrize('field', ['cleanup_complete', 'allocator_release_confirmed'])
def test_unconfirmed_cleanup_cannot_complete(source, tmp_path, field):
    http = Transport(source)
    bad = facts()
    bad[field] = False
    assert worker.run_once(SCOPE, http, lambda *a: (bad, png()), tmp_path)['result'] == 'failed'
    assert http.calls[-1][2]['outcome'] == 'failed'
    assert failure_receipt(tmp_path)['execution_stage'] == 'evidence'


def test_uncertain_completion_is_never_rebuilt_or_resettled(source, tmp_path):
    http = Transport(source)
    http.uncertain = True
    effects = []
    def native(*args):
        effects.append(1)
        return facts(), png()
    assert worker.run_once(SCOPE, http, native, tmp_path)['result'] == 'unknown'
    assert effects == [1]
    assert len([p for _, p, _, _ in http.calls if p.endswith('/complete')]) == 1


def test_screenshot_integrity_and_png_structure(source):
    bad = facts()
    bad['screenshot_sha256'] = '0' * 64
    with pytest.raises(worker.WorkerError, match='screenshot-integrity'):
        worker.make_receipt(source[1], SCOPE, bad, png())
    with pytest.raises(worker.WorkerError): worker.validate_png(png()[:29] + b'bad!')


def test_remote_permit_expiry_kills_only_owned_child(tmp_path):
    if not hasattr(mac.os, 'killpg'):
        # Inject the same process/clock boundary on Windows; no POSIX dependency.
        state = {'time': 0, 'alive': True, 'signals': []}
        class Child:
            pid = 12345
            stdin = io.BytesIO()
            returncode = -9
            def poll(self): return None if state['alive'] else -9
            def wait(self, **kw): state['alive'] = False; return -9
        def killpg(pid, sig):
            assert pid == 12345
            state['signals'].append(sig)
            if sig: state['alive'] = False
            elif not state['alive']: raise ProcessLookupError()
        permit = tmp_path / 'permit'
        permit.write_text('0.3')
        with pytest.raises(RuntimeError):
            mac.supervise(['fixed'], permit, clock=lambda: state['time'],
                          sleep=lambda n: state.update(time=state['time'] + n),
                          popen=lambda *a, **kw: Child(), killpg=killpg)
        assert signal.SIGTERM in state['signals']
        return
    permit = tmp_path / 'permit'
    permit.write_text(str(time.monotonic() + .5))
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    owned = []
    def start(*args, **kw):
        child = subprocess.Popen(*args, **kw)
        owned.append(child)
        return child
    try:
        with pytest.raises(RuntimeError, match='native-supervised-failure'):
            mac.supervise([sys.executable, '-c', 'import time; time.sleep(30)'], permit, popen=start)
        assert owned[0].poll() is not None
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_remote_supervisor_requires_initial_permit(tmp_path):
    with pytest.raises(RuntimeError, match='permit-expired'):
        mac.supervise(['never-start'], tmp_path / 'missing', popen=lambda *a, **k: pytest.fail('spawned'))


def test_generic_compile_keeps_granted_simulator_custody(source, tmp_path, capsys):
    owned = tmp_path / 'owned'
    owned.mkdir()
    derived, internal = tmp_path / 'derived', tmp_path / 'internal'
    commands = []
    runtime = 'com.apple.CoreSimulator.SimRuntime.iOS-26-0'
    def checked(argv, timeout=30, cwd=None):
        commands.append(argv)
        if argv[0] == '/usr/bin/xcodebuild':
            # A dynamically granted device need not be an Xcode build destination.
            assert argv[argv.index('-destination') + 1] == 'generic/platform=iOS Simulator'
            assert UDID not in str(argv)
            assert argv[argv.index('-sdk') + 1] == 'iphonesimulator'
            assert cwd == owned / 'source'
            app = derived / 'Build/Products/Debug-iphonesimulator/Portfolio.app'
            app.mkdir(parents=True)
            (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': mac.BUNDLE}))
        elif argv[0] == '/usr/bin/ditto':
            shutil.copytree(argv[1], argv[2])
        else:
            assert argv[:2] == ['/usr/bin/xcrun', 'simctl']
            if argv[2] == 'list':
                return json.dumps({'devices': {runtime: [dict(udid=UDID, isAvailable=True,
                    deviceTypeIdentifier='com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro-Max')]}}).encode()
            assert argv[3] == UDID
            if argv[2] == 'listapps':
                return plistlib.dumps({})
            if argv[2] == 'io':
                Path(argv[-1]).write_bytes(png())
        return b''
    payload = dict(archive=base64.b64encode(source[0]).decode(), byte_size=len(source[0]),
                   sha256=hashlib.sha256(source[0]).hexdigest(), budget=500, udid=UDID,
                   ticket='fixture-ticket', name='fixture-owned')
    namespace = dict(base64=base64, hashlib=hashlib, io=io, json=json, plistlib=plistlib,
                     zipfile=zipfile, time=types.SimpleNamespace(sleep=lambda _: None),
                     sys=types.SimpleNamespace(stdin=io.StringIO(json.dumps(payload))),
                     roots=lambda _: (owned, derived, internal),
                     permitted=lambda path: None, checked=checked,
                     application_inventory=plistlib.loads)
    exec(mac.DRIVER, namespace)
    result = json.loads(capsys.readouterr().out)
    assert result['simulator_udid'] == UDID and result['simulator_runtime'] == runtime
    assert result['screenshot_sha256'] == hashlib.sha256(png()).hexdigest()
    assert [c[2] for c in commands if c[0] == '/usr/bin/xcrun'] == [
        'list', 'bootstatus', 'listapps', 'install', 'launch', 'io']
    assert (owned / 'install-owned').read_text() == mac.BUNDLE


@pytest.mark.parametrize('exit_code,converted,accept', [
    (0, plistlib.dumps({'com.example.existing': {'CFBundleIdentifier': 'com.example.existing'}}), True),
    (1, b'', False), (0, b'not-a-plist', False), (0, plistlib.dumps([]), False),
    (0, b'x' * (2 * 1024 * 1024 + 1), False),
])
def test_app_inventory_converts_openstep_and_refuses_unusable_readback(exit_code, converted, accept):
    raw = b'{ "com.example.existing" = { CFBundleIdentifier = "com.example.existing"; }; }'
    calls = []
    def run(argv, **kwargs):
        assert argv == ['/usr/bin/plutil', '-convert', 'xml1', '-o', '-', '--', '-']
        assert kwargs['input'] == raw and kwargs['timeout'] == 30
        assert kwargs['stderr'] is subprocess.DEVNULL
        kwargs['stdout'].write(converted)
        calls.append(argv)
        return types.SimpleNamespace(returncode=exit_code)
    node = next(n for n in ast.parse(mac.REMOTE_COMMON).body
                if isinstance(n, ast.FunctionDef) and n.name == 'application_inventory')
    namespace = dict(tempfile=tempfile, plistlib=plistlib,
                     subprocess=types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<inventory>', 'exec'), namespace)
    if accept:
        assert 'com.example.existing' in namespace['application_inventory'](raw)
    else:
        with pytest.raises((AssertionError, plistlib.InvalidFileException)):
            namespace['application_inventory'](raw)
    assert len(calls) == 1


def remote_clock_process(tmp_path, program, payload, *, kernel, local_origin):
    """Model macOS Python 3.9 processes sharing only CLOCK_MONOTONIC."""
    script = mac.REMOTE_COMMON + '\n' + f'''
import types
time=types.SimpleNamespace(CLOCK_MONOTONIC=1,
    clock_gettime=lambda clock: {kernel!r}, monotonic=lambda: {local_origin!r})
source=Path({str(tmp_path)!r})/'source'
roots=lambda p: (source, source/'derived', source/'internal')
sys.stdin=io.StringIO({json.dumps(payload)!r})
def supervise(argv, permit, **kw):
    assert kw['clock'] is permit_clock
    assert kw['clock']()=={kernel!r}
    assert float(permit.read_text())>kw['clock']()
    return b'{{}}'
''' + program
    return subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)


def test_remote_permit_uses_shared_clock_across_processes(tmp_path):
    bootstrap = remote_clock_process(tmp_path, mac.BOOTSTRAP, {'name': 'owned'},
                                     kernel=10000.0, local_origin=.22)
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert json.loads(bootstrap.stdout)['remote_clock'] == 10000.0
    refresh = remote_clock_process(tmp_path, mac.REFRESH, {'expiry': 10008.0},
                                   kernel=10001.0, local_origin=.01)
    assert refresh.returncode == 0, refresh.stderr
    driver = remote_clock_process(tmp_path, 'permitted(source)', {},
                                  kernel=10002.0, local_origin=.03)
    assert driver.returncode == 0, driver.stderr
    supervisor = remote_clock_process(tmp_path, mac.SUPERVISOR_END, {'driver': 'fixed'},
                                      kernel=10003.0, local_origin=.02)
    assert supervisor.returncode == 0, supervisor.stderr
    expired_driver = remote_clock_process(tmp_path, 'permitted(source)', {},
                                          kernel=10009.0, local_origin=.01)
    assert expired_driver.returncode != 0


@pytest.mark.parametrize('expiry', [10000.0, 10011.0, float('inf'), float('nan')])
def test_remote_shared_clock_refuses_invalid_refresh(tmp_path, expiry):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'permit').write_text('previous')
    refresh = remote_clock_process(tmp_path, mac.REFRESH, {'expiry': expiry},
                                   kernel=10000.0, local_origin=.01)
    assert refresh.returncode != 0
    assert (source / 'permit').read_text() == 'previous'


def test_duplicate_zip_entries_rejected(source):
    output = io.BytesIO(source[0])
    with zipfile.ZipFile(output, 'a') as archive, pytest.warns(UserWarning):
        archive.writestr('README.md', b'substitute')
    raw = output.getvalue()
    descriptor = {**source[1]['descriptor'], 'byte_size': len(raw), 'archive_sha256': hashlib.sha256(raw).hexdigest()}
    with pytest.raises(worker.WorkerError, match='unsafe-archive'):
        worker.verify_source(raw, descriptor)


@pytest.mark.parametrize('count,expanded', [(251, 1), (1, 2048)])
def test_expanded_and_entry_count_bounds(source, monkeypatch, count, expanded):
    template = worker.producer()
    template.MAX_ARCHIVE_BYTES = 1024 if count == 1 else 100000
    monkeypatch.setattr(worker, 'producer', lambda: template)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for i in range(count): archive.writestr(str(i), b'x' * expanded)
    raw = output.getvalue()
    descriptor = {**source[1]['descriptor'], 'byte_size': len(raw), 'archive_sha256': hashlib.sha256(raw).hexdigest()}
    with pytest.raises(worker.WorkerError, match='unsafe-archive'):
        worker.verify_source(raw, descriptor)


def test_failed_permit_refresh_cancels_authority(source):
    guard = worker.Authority(Transport(source), source[1])
    guard.beat()
    def refresh(expiry): raise worker.WorkerError('mac-transport-failed')
    guard.refresh = refresh
    with pytest.raises(worker.WorkerError): guard.beat()
    with pytest.raises(worker.WorkerError): guard.check()


def test_absolute_deadline_and_allocator_loss(source):
    now = [100.0]
    source[1]['deadline'] = 110.0
    guard = worker.Authority(Transport(source), source[1], clock=lambda: now[0], monotonic=lambda: now[0])
    guard.beat()
    now[0] = 110.0
    with pytest.raises(worker.WorkerError): guard.check()
    now[0] = 100.0
    with pytest.raises(worker.WorkerError): guard.beat()
    guard = worker.Authority(Transport(source), source[1], clock=lambda: now[0], monotonic=lambda: now[0])
    guard.beat()
    guard.held = type('Held', (), {'lost': True})()
    with pytest.raises(worker.WorkerError): guard.check()


class Allocator:
    def __init__(self, release_code=0, cleanup=True):
        self.release_code, self.cleanup = release_code, cleanup
        self.calls = []
        self.held = None
    def Client(self, **kwargs):
        assert kwargs['host'] == 'mac-mini'
        assert callable(kwargs['sleep'])
        return object()
    @contextmanager
    def lease(self, lane, name, **kwargs):
        assert kwargs['slots'] == 'sim,build' and kwargs['kind'] == 'ios-preview'
        assert kwargs['sim_types'] == ('iPhone 17 Pro Max',)
        owner = self
        class Held:
            lost = False
            ticket = 'fixture-ticket'
            env = {'MINI_ALLOC_UDIDS': UDID}
            def release(self, *args, **kwargs):
                return subprocess.CompletedProcess([], owner.release_code)
            def ssh(self, command, payload=None, timeout=10):
                assert command.startswith('/usr/bin/python3 -c ')
                p = json.loads(payload)
                assert 'synthetic' not in payload
                owner.calls.append(p)
                if 'expiry' in p: result = {}
                elif 'driver' in p:
                    result = facts()
                    result.pop('cleanup_complete')
                    result.pop('allocator_release_confirmed')
                    result['screenshot_base64'] = base64.b64encode(png()).decode()
                elif 'remote_clock' in command: result = {'remote_clock': time.monotonic()}
                else: result = {'cleanup_complete': owner.cleanup}
                return subprocess.CompletedProcess([], 0, json.dumps(result), '')
        self.held = Held()
        try:
            yield self.held
        finally:
            self.held.release()


class FixturePermitStream:
    """Explicit hermetic substitute; production defaults to the leased stream."""
    def __init__(self, allocator, held, context, guard, offset):
        self.held, self.context, self.offset = held, context, offset
    def grant(self, expiry):
        result = self.held.ssh('/usr/bin/python3 -c ' + repr(mac.REFRESH),
                              payload=json.dumps({**self.context, 'expiry': expiry+self.offset}))
        if result.returncode: raise worker.WorkerError('mac-transport-failed')
    def close(self): return True


class PermitChild:
    def __init__(self, *, ready=b'{"ready":true}\n', ack=b'{}\n', blocked_close=False):
        self.lines = queue.Queue()
        self.sent = []
        self.ready, self.ack = ready, ack
        self.returncode = None
        self.killed = False
        self.blocked_close = blocked_close
        owner = self
        class Input:
            def write(self, raw):
                raw = bytes(raw)
                owner.sent.append(raw)
                reply = owner.ready if len(owner.sent)==1 else owner.ack
                if reply is not None: owner.lines.put(reply)
                return len(raw)
            def flush(self): pass
            def close(self):
                if not owner.blocked_close:
                    owner.returncode = 0
                    owner.lines.put(b'')
        class Output:
            def readline(self, limit):
                line = owner.lines.get()
                return line[:limit]
            def close(self): owner.lines.put(b'')
        self.stdin, self.stdout = Input(), Output()
    def poll(self): return self.returncode
    def kill(self):
        self.killed = True
        self.returncode = -9
        self.lines.put(b'')
    def wait(self, timeout):
        if self.returncode is None: raise subprocess.TimeoutExpired('owned-stream', timeout)
        return self.returncode


def permit_client(child, *, seconds=30):
    calls=[]
    def popen(argv, **kw):
        calls.append((argv, kw))
        return child
    client=types.SimpleNamespace(host='mac-mini',popen=popen)
    held=types.SimpleNamespace(ticket='fixture-ticket',client=client,lost=False)
    allocator=types.SimpleNamespace(ssh_argv=lambda args,host: [host,*args])
    guard=types.SimpleNamespace(monotonic=time.monotonic,end=time.monotonic()+seconds,check=lambda:None)
    return allocator,held,guard,calls


def test_persistent_permit_stream_uses_same_lease_and_closed_grants():
    child=PermitChild()
    allocator,held,guard,calls=permit_client(child)
    context={'ticket':held.ticket,'udid':UDID,'name':'owned'}
    stream=mac.PermitStream(allocator,held,context,guard,100)
    stream.grant(20)
    stream.grant(21)
    assert [json.loads(raw) for raw in child.sent]==[context,{'expiry':120},{'expiry':121}]
    argv,options=calls[0]
    assert argv[:4]==['mac-mini','run','--ticket',held.ticket]
    assert argv[-4:-1]==['/usr/bin/python3','-u','-c']
    assert options['stderr']==subprocess.DEVNULL and options['bufsize']==0
    assert options['stdin']==options['stdout']==subprocess.PIPE
    assert stream.replies.maxsize==2
    assert stream.close() and child.poll()==0


@pytest.mark.parametrize('ready', [b'', b'{"ready":false}\n', b'{"ready":1}\n', b'{"ready":true,"extra":1}\n', b'x'*1025])
def test_persistent_permit_stream_refuses_bad_handshake_and_closes(ready):
    child=PermitChild(ready=ready)
    allocator,held,guard,_=permit_client(child)
    with pytest.raises(worker.WorkerError,match='mac-transport-failed'):
        mac.PermitStream(allocator,held,{},guard,0)
    assert child.poll() is not None


@pytest.mark.parametrize('ack', [b'',b'{"extra":true}\n',b'x'*1025,None])
def test_persistent_permit_stream_refuses_eof_oversize_or_timeout(ack):
    child=PermitChild(ack=ack)
    allocator,held,guard,_=permit_client(child,seconds=.1)
    stream=mac.PermitStream(allocator,held,{},guard,0)
    with pytest.raises(worker.WorkerError,match='mac-transport-failed'): stream.grant(20)
    assert stream.failed.is_set()
    assert stream.close()


def test_persistent_permit_stream_bounded_close_kills_only_owned_child():
    child=PermitChild(blocked_close=True)
    allocator,held,guard,_=permit_client(child)
    stream=mac.PermitStream(allocator,held,{},guard,0)
    assert stream.close() and child.killed
    with pytest.raises(worker.WorkerError): stream.grant(20)


def test_persistent_permit_stream_oversize_context_closes_child():
    child=PermitChild()
    allocator,held,guard,_=permit_client(child)
    with pytest.raises(worker.WorkerError,match='mac-transport-failed'):
        mac.PermitStream(allocator,held,{'name':'x'*16384},guard,0)
    assert child.poll() is not None and not child.sent


def test_persistent_permit_stream_refuses_lost_lease_or_nonfinite_grant():
    child=PermitChild()
    allocator,held,guard,_=permit_client(child)
    stream=mac.PermitStream(allocator,held,{},guard,0)
    held.lost=True
    with pytest.raises(worker.WorkerError): stream.grant(20)
    assert len(child.sent)==1 and stream.close()
    child=PermitChild()
    allocator,held,guard,_=permit_client(child)
    stream=mac.PermitStream(allocator,held,{},guard,0)
    with pytest.raises(worker.WorkerError): stream.grant(float('inf'))
    assert len(child.sent)==1 and stream.close()


def test_mac_adapter_stream_close_failure_never_returns_success(source):
    class Unclosed(FixturePermitStream):
        def close(self): return False
    allocator=Allocator()
    guard=worker.Authority(Transport(source),source[1])
    guard.beat()
    adapter=mac.MacAdapter(allocator,permit_factory=Unclosed)
    with pytest.raises(worker.WorkerError,match='cleanup-release-unconfirmed'):
        adapter(source[0],source[1],guard)
    assert guard.refresh is None
    assert adapter.report=={'cleanup_complete':False,'allocator_release_confirmed':True}


@pytest.mark.skipif(sys.platform=='win32',reason='the shipped remote writer is POSIX-only')
@pytest.mark.parametrize('tail', [b'{"expiry":10005}\n',b'{"expiry":9999}\n',
                                b'{"expiry":10011}\n',b'{"expiry":true}\n',
                                b'{"expiry":NaN}\n',b'{"expiry":10005,"extra":1}\n',
                                b'x'*1025])
def test_remote_permit_stream_bounds_expiry_and_revokes_eof(tmp_path, tail):
    source=tmp_path/'source'
    source.mkdir()
    (source/'owner').write_text('owned')
    context=json.dumps({'ticket':'ticket','udid':UDID,'name':'owned'}).encode()+b'\n'
    script=mac.REMOTE_COMMON+f'''
source=Path({str(source)!r})
roots=lambda p:(source,source/'derived',source/'internal')
permit_clock=lambda:10000
''' + mac.PERMIT_STREAM
    result=subprocess.run([sys.executable,'-c',script],input=context+tail,capture_output=True)
    assert result.stdout.startswith(b'{"ready":true}\n')
    assert (source/'permit').read_text()=='0'
    assert result.returncode==(0 if tail==b'{"expiry":10005}\n' else 1)
    if result.returncode==0: assert result.stdout.endswith(b'{}\n')


def test_mac_adapter_default_is_real_permit_stream():
    assert mac.MacAdapter().permit_factory is mac.PermitStream


@pytest.mark.skipif(sys.platform=='win32',reason='the shipped remote writer is POSIX-only')
@pytest.mark.parametrize('change', ['ticket','udid','owner','extra','lock'])
def test_remote_permit_stream_refuses_foreign_scope_owner_or_lock(tmp_path, change):
    name='preview-'+JOB+'-1-'+'a'*32
    source=tmp_path/name
    source.mkdir()
    (source/'owner').write_text('foreign' if change=='owner' else name)
    (source/'permit').write_text('previous')
    context={'ticket':'ticket','udid':UDID,'name':name}
    if change=='ticket': context['ticket']='foreign'
    if change=='udid': context['udid']=JOB
    if change=='extra': context['extra']=True
    script=mac.REMOTE_COMMON+f'''
os.environ['MINI_ALLOC_TICKET']='ticket'
os.environ['MINI_ALLOC_UDIDS']={UDID!r}
checked=lambda *args,**kwargs:{str(tmp_path)!r}.encode()
permit_clock=lambda:10000
''' + mac.PERMIT_STREAM
    import fcntl
    with (source/'stream.lock').open('a+') as lock:
        if change=='lock': fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=subprocess.run([sys.executable,'-c',script],
                              input=json.dumps(context).encode()+b'\n',capture_output=True)
    assert result.returncode != 0 and result.stdout==b''
    assert (source/'permit').read_text()=='previous'


def test_mac_adapter_actual_facts_transfer_cleanup_release(source):
    allocator = Allocator()
    guard = worker.Authority(Transport(source), source[1])
    guard.beat()
    result, image = mac.MacAdapter(allocator, permit_factory=FixturePermitStream)(source[0], source[1], guard)
    assert image == png() and result == facts()
    assert 'expiry' in allocator.calls[1]
    assert base64.b64decode(allocator.calls[2]['archive']) == source[0]
    assert guard.refresh is None


@pytest.mark.parametrize('failed_event', ['allocator', 'producer', 'cleanup', 'release'])
def test_mac_journal_error_still_cleans_and_releases(source, failed_event):
    allocator = Allocator()
    events = []
    def record(event):
        events.append(event)
        if event['event'] == failed_event:
            raise OSError('synthetic-secret')
    guard = worker.Authority(Transport(source), source[1], journal=record)
    guard.beat()
    adapter = mac.MacAdapter(allocator, permit_factory=FixturePermitStream)
    with pytest.raises(worker.WorkerError, match='journal-failed'):
        adapter(source[0], source[1], guard)
    assert adapter.report == dict(cleanup_complete=True, allocator_release_confirmed=True)
    assert guard.refresh is None and guard.journal_failed
    if failed_event in ('allocator', 'producer'):
        assert not any('driver' in payload for payload in allocator.calls)
    assert all(set(event) in ({'event', 'ticket', 'marker_sha256'},
                             {'event', 'producer_may_have_started'},
                             {'event', 'cleanup_complete'},
                             {'event', 'allocator_release_confirmed'}) for event in events)


def allocator_history_fixture(tmp_path, monkeypatch):
    from types import SimpleNamespace
    namespace = {}
    # Define the actual shipped program without invoking its CLI exception rail.
    tree = ast.parse(mac.RECONCILIATION_READ)
    tree.body.pop()
    exec(compile(tree, '<allocator-read>', 'exec'), namespace)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    root = tmp_path / '.mini-alloc'
    root.mkdir()
    (root / 'state.json').write_text('{"jobs":{}}')
    name = 'preview-' + JOB + '-1-<redacted>'
    rows = [dict(event='submit', ticket='full-ticket', job=name, ts=1, redactions=1),
            dict(event='grant', ticket='full-ticket', job=name, ts=2, redactions=1),
            dict(event='legacy_seen', marker=name, ts=2.5, redactions=1),
            dict(event='release', ticket='full-ticket', ts=3, redactions=0),
            dict(event='confirm_dead', ticket='full-ticket', ts=4, redactions=0),
            dict(event='free', ticket='full-ticket', ts=5, at=5, redactions=0)]
    unrelated = dict(event='submit', ticket='unrelated-ticket', job='unrelated-secret', ts=0, redactions=0)
    rows.insert(0, unrelated)
    (root / 'history.jsonl').write_bytes(b''.join(worker.encode(row) + b'\n' for row in rows))
    output = io.BytesIO()
    namespace['sys'] = SimpleNamespace(stdin=SimpleNamespace(buffer=io.BytesIO(
        worker.encode(dict(job_id=JOB, attempt=1)))), stdout=SimpleNamespace(buffer=output))
    return namespace, root, rows, output


@pytest.mark.parametrize('retained_free', [False, True])
@pytest.mark.parametrize('redacted', [False, True])
def test_canonical_allocator_history_projection(tmp_path, monkeypatch, retained_free, redacted):
    namespace, root, rows, output = allocator_history_fixture(tmp_path, monkeypatch)
    if not redacted:
        rows = [{k: v.replace('<redacted>', 'e' * 32) if isinstance(v, str) else v
                 for k, v in row.items()} for row in rows]
        (root / 'history.jsonl').write_bytes(b''.join(worker.encode(row) + b'\n' for row in rows))
    if retained_free:
        (root / 'state.json').write_bytes(worker.encode(dict(jobs={
            'full-ticket': dict(ticket='full-ticket', job=rows[1]['job'],
                                state='free', confirmed_dead=True)})))
    namespace['main']()
    raw = output.getvalue()
    witness = reconciliation.allocator_witness(worker.parse(raw), JOB, 1)
    assert witness['ticket'] == 'full-ticket'
    assert b'unrelated' not in raw and b'e' * 32 not in raw
    assert witness['active_match_counts'] == [0, 0]


@pytest.mark.parametrize('bad', ['ambiguous', 'missing', 'active', 'active-after', 'order',
                                 'no-death', 'no-free', 'malformed', 'truncated', 'oversize',
                                 'ticket-reused', 'unassociated-ticket', 'rotation-gap',
                                 'free-without-death', 'foreign-free-ticket',
                                 'malformed-suffix', 'legacy-extra-key', 'free-clock-conflict'])
def test_canonical_allocator_history_fail_closed(tmp_path, monkeypatch, bad):
    namespace, root, rows, output = allocator_history_fixture(tmp_path, monkeypatch)
    history = root / 'history.jsonl'
    if bad == 'ambiguous':
        rows.append(dict(event='submit', ticket='second-ticket', job=rows[1]['job'], ts=1))
    elif bad == 'ticket-reused':
        rows.append(dict(event='submit', ticket='full-ticket',
                         job='preview-' + ACTOR + '-1-<redacted>', ts=6))
    elif bad == 'unassociated-ticket':
        rows.append(dict(event='release', ticket='second-ticket', job=rows[1]['job'], ts=6))
    elif bad == 'malformed-suffix':
        rows[1]['job'] = rows[1]['job'].replace('<redacted>', 'unknown')
    elif bad == 'legacy-extra-key':
        next(row for row in rows if row['event'] == 'legacy_seen')['ticket'] = 'second-ticket'
    elif bad == 'free-clock-conflict':
        rows[-1]['at'] = 9
    elif bad == 'rotation-gap':
        (root / 'history.jsonl.2').write_bytes(b'')
    elif bad == 'missing':
        rows = rows[:1]
    elif bad in ('free-without-death', 'foreign-free-ticket'):
        key = 'wrong-ticket' if bad == 'foreign-free-ticket' else 'full-ticket'
        (root / 'state.json').write_bytes(worker.encode(dict(jobs={key: dict(
            ticket=key, job=rows[1]['job'], state='free',
            confirmed_dead=bad != 'free-without-death')})))
    elif bad in ('active', 'active-after'):
        state = worker.encode(dict(jobs={'full-ticket': dict(job=rows[1]['job'])}))
        if bad == 'active':
            (root / 'state.json').write_bytes(state)
        else:
            original = namespace['read']
            def read(path, limit):
                raw = original(path, limit)
                if path.name == 'history.jsonl':
                    (root / 'state.json').write_bytes(state)
                return raw
            namespace['read'] = read
    elif bad == 'order':
        rows[-2]['ts'] = 0
    elif bad == 'no-death':
        rows.pop(-2)
    elif bad == 'no-free':
        rows.pop()
    elif bad == 'oversize':
        (root / 'history.jsonl.1').write_bytes(b' ' * (11 * 1024 * 1024 + 1))
    history.write_bytes(b''.join(worker.encode(row) + b'\n' for row in rows))
    if bad == 'malformed':
        history.write_bytes(history.read_bytes() + b'{bad}\n')
    if bad == 'truncated':
        history.write_bytes(history.read_bytes().rstrip(b'\n'))
    with pytest.raises((AssertionError, ValueError)):
        namespace['main']()
    assert not output.getvalue()


def test_reconciliation_transport_preserves_installed_ssh_prefix(tmp_path, monkeypatch):
    namespace, _, _, output = allocator_history_fixture(tmp_path, monkeypatch)
    namespace['main']()
    prefix = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', 'mac-mini']
    class Installed:
        @staticmethod
        def ssh_argv(args, host):
            assert args == [] and host == 'mac-mini'
            return prefix + ['fixed-allocator-command']
        Client = staticmethod(lambda *args, **kwargs: pytest.fail('no client or lease'))
    def transport(argv, *, input, timeout):
        assert argv[:-1] == prefix and timeout == 30
        assert argv[-1].startswith('/usr/bin/python3 -c ')
        assert worker.parse(input) == dict(job_id=JOB, attempt=1)
        return subprocess.CompletedProcess(argv, 0, output.getvalue())
    adapter = mac.MacAdapter(Installed, reconciliation_transport=transport)
    assert adapter.read_reconciliation_allocator(JOB, 1)['ticket'] == 'full-ticket'
    with pytest.raises(worker.WorkerError):
        adapter.read_reconciliation_allocator('../' + JOB, 1)
    with pytest.raises(worker.WorkerError):
        adapter.read_reconciliation_allocator(JOB, True)


@pytest.mark.parametrize('raw,code', [(b'not-json', 0), (b'x' * 16385, 0), (b'{}', 1), (b'{}', 255)])
def test_reconciliation_transport_malformed_or_oversize_output_refuses(raw, code):
    class Installed:
        @staticmethod
        def ssh_argv(args, host):
            return ['ssh', '-o', 'BatchMode=yes', host, 'allocator-command']
    def transport(argv, **kwargs):
        return subprocess.CompletedProcess(argv, code, raw)
    with pytest.raises(worker.WorkerError):
        mac.MacAdapter(Installed, reconciliation_transport=transport).read_reconciliation_allocator(JOB, 1)


@pytest.mark.parametrize('granted,allowed', [
    ('9721816E-24EB-4E8B-8062-8452841451BE', True),
    ('9721816E-24EB-4E8B-8062-8452841451BF', False),
])
def test_remote_roots_bind_actual_uppercase_allocator_uuid(tmp_path, monkeypatch, granted, allowed):
    # Execute the shipped roots function with the allocator's actual UUID casing.
    # Importing all REMOTE_COMMON would require POSIX-only fcntl on Windows.
    roots_node = next(node for node in ast.parse(mac.REMOTE_COMMON).body
                      if isinstance(node, ast.FunctionDef) and node.name == 'roots')
    namespace = {'Path': Path, 'os': mac.os, 're': mac.re,
                 'checked': lambda argv: str(tmp_path / argv[-1]).encode()}
    exec(compile(ast.Module(body=[roots_node], type_ignores=[]), '<remote-roots>', 'exec'), namespace)
    monkeypatch.setenv('MINI_ALLOC_TICKET', 'fixture-ticket')
    monkeypatch.setenv('MINI_ALLOC_UDIDS', granted)
    payload = {'ticket': 'fixture-ticket', 'udid': '9721816e-24eb-4e8b-8062-8452841451be',
               'name': 'preview-' + JOB + '-1-' + 'a' * 32}
    if allowed:
        assert namespace['roots'](payload)[0] == (tmp_path / 'work' / payload['name']).resolve()
    else:
        with pytest.raises(AssertionError):
            namespace['roots'](payload)


@pytest.mark.parametrize('release_code,cleanup', [(1, True), (0, False)])
def test_mac_adapter_release_and_cleanup_fail_closed(source, release_code, cleanup):
    guard = worker.Authority(Transport(source), source[1])
    guard.beat()
    with pytest.raises(worker.WorkerError, match='cleanup-release-unconfirmed'):
        mac.MacAdapter(Allocator(release_code, cleanup), permit_factory=FixturePermitStream)(source[0], source[1], guard)


@pytest.mark.parametrize('cause', ['heartbeat-request', 'heartbeat-contract', 'permit-refresh',
                                  'deadline', 'ack-expired', 'allocator-lost', 'unexpected'])
def test_first_authority_failure_is_sticky(source, cause):
    now = [100.0]
    source[1]['deadline'] = 200.0
    http = Transport(source)
    guard = worker.Authority(http, source[1], clock=lambda: now[0], monotonic=lambda: now[0])
    assert guard.authority_failure == 'none'
    guard.beat()
    if cause == 'heartbeat-request':
        http.deny = True
    elif cause == 'heartbeat-contract':
        http.request = lambda *a, **kw: b'{"secret":"Bearer synthetic"}'
    elif cause == 'permit-refresh':
        def refresh(expiry): raise RuntimeError('Bearer synthetic remote output')
        guard.refresh = refresh
    elif cause == 'deadline':
        now[0] = 200.0
    elif cause == 'ack-expired':
        now[0] = 112.0
    elif cause == 'allocator-lost':
        guard.held = type('Held', (), {'lost': True})()
    else:
        guard.cancelled.set()
    with pytest.raises((worker.WorkerError, RuntimeError)):
        if cause in ('heartbeat-request', 'heartbeat-contract', 'permit-refresh'):
            guard.beat()
        else:
            guard.check()
    assert guard.authority_failure == cause
    # Neither a later deadline nor a failed settlement heartbeat replaces the cause.
    now[0] = 201.0
    with pytest.raises(worker.WorkerError): guard.check()
    with pytest.raises(worker.WorkerError): guard.beat()
    assert guard.authority_failure == cause and guard.cancelled.is_set()


def test_delayed_heartbeat_keeps_send_time_expiry(source):
    now = [100.0]
    source[1]['deadline'] = 200.0
    http = Transport(source)
    original = http.request
    def delayed(*args, **kwargs):
        now[0] += 12.0
        return original(*args, **kwargs)
    http.request = delayed
    guard = worker.Authority(http, source[1], clock=lambda: now[0], monotonic=lambda: now[0])
    with pytest.raises(worker.WorkerError, match='authority-lost'): guard.beat()
    assert guard.ack == 100.0
    assert guard.authority_failure == 'ack-expired'


@pytest.mark.parametrize('http_seconds', [.3, 3.0])
def test_heartbeat_cadence_preserves_remote_permit_under_bounded_io(source, monkeypatch, http_seconds):
    now = [100.0]
    source[1]['deadline'] = 500.0
    http = Transport(source)
    request = http.request
    def delayed(*args, **kwargs):
        now[0] += http_seconds
        return request(*args, **kwargs)
    http.request = delayed
    guard = worker.Authority(http, source[1], clock=lambda: now[0], monotonic=lambda: now[0])
    permits, margins, waits = [], [], []
    def refresh(expiry):
        # Model a short outbound leg and a conservative bootstrap return offset.
        arrival = now[0] + .4
        if permits:
            margins.append(permits[-1] - arrival)
        assert expiry <= guard.ack + 10
        permits.append(expiry - 1.4)
        now[0] += 1.4
    guard.refresh = refresh
    class Stop:
        def wait(self, delay):
            if len(waits) == 4:
                return True
            waits.append(delay)
            now[0] += delay
            return False
        def set(self): pass
    class Thread:
        def __init__(self, *, target, daemon): self.target = target
        def start(self): self.target()
        def join(self, **kwargs): pass
    guard.stop = Stop()
    monkeypatch.setattr(worker.threading, 'Thread', Thread)
    guard.start()
    assert len(margins) == 4 and min(margins) > 0
    assert guard.authority_failure == 'none' and not guard.cancelled.is_set()
    if http_seconds == 3:
        assert waits == [0, 0, 0, 0]
        # The former extra two-second delay would create a real permit lapse.
        assert min(margins) - 2 < 0
    else:
        assert all(.29 < wait < .31 for wait in waits)


@pytest.mark.parametrize('remote_exit,error_code', [
    (71, 'mac-permit-expired'), (72, 'mac-producer-failed'), (73, 'mac-extinction-unconfirmed'),
])
def test_remote_producer_exit_is_closed_and_cleanup_still_runs(source, remote_exit, error_code):
    class FailingAllocator(Allocator):
        @contextmanager
        def lease(self, *args, **kwargs):
            with super().lease(*args, **kwargs) as held:
                original = held.ssh
                def ssh(command, payload=None, **options):
                    if 'driver' in json.loads(payload):
                        return subprocess.CompletedProcess([], remote_exit, 'private body', 'private stderr')
                    return original(command, payload, **options)
                held.ssh = ssh
                yield held
    guard = worker.Authority(Transport(source), source[1])
    guard.beat()
    adapter = mac.MacAdapter(FailingAllocator(), permit_factory=FixturePermitStream)
    with pytest.raises(worker.WorkerError) as caught:
        adapter(source[0], source[1], guard)
    assert worker.error_code(caught.value) == error_code
    assert 'private' not in str(caught.value)
    assert adapter.report == {'cleanup_complete': True, 'allocator_release_confirmed': True}


def failure_receipt(tmp_path):
    return json.loads((tmp_path / (JOB + '-1-failure.json')).read_text())


@pytest.mark.parametrize('reply,expected', [
    ({'job_id': JOB, 'status': 'failed'}, 'accepted'),
    ({'job_id': JOB, 'status': 'complete'}, 'response-unconfirmed'),
    ({'job_id': OWNER, 'status': 'failed'}, 'response-unconfirmed'),
    ({'job_id': JOB, 'status': 'failed', 'secret': 'Bearer synthetic'}, 'response-unconfirmed'),
    ([], 'response-unconfirmed'),
    (b'{invalid secret', 'response-unconfirmed'),
    (TimeoutError('Bearer synthetic'), 'request-failed'),
])
def test_failure_settlement_requires_exact_response(source, tmp_path, monkeypatch, reply, expected):
    monkeypatch.setattr(worker.Authority, 'start', lambda self: self.beat())
    http = Transport(source)
    original = http.request
    def request(method, path, body=None, **kw):
        if path.endswith('/complete'):
            original(method, path, body, **kw)
            if isinstance(reply, BaseException): raise reply
            return reply if type(reply) is bytes else worker.encode(reply)
        return original(method, path, body, **kw)
    http.request = request
    def native(*args): raise worker.WorkerError('native-failed')
    result = worker.run_once(SCOPE, http, native, tmp_path)
    local = failure_receipt(tmp_path)
    assert result['result'] == 'failed'
    assert local['failure_settlement'] == expected
    assert local['error_code'] == 'native-failed'
    assert local['authority_failure'] == 'none'
    assert len([p for _, p, _, _ in http.calls if p.endswith('/complete')]) == 1
    assert 'synthetic' not in json.dumps(local)


@pytest.mark.parametrize('error,expected', [
    (worker.WorkerError('source-integrity'), 'source-integrity'),
    (worker.WorkerError('Bearer synthetic https://private.invalid body stderr'), 'unexpected'),
    (worker.WorkerError('native-failed', {'Authorization': 'Bearer synthetic'}), 'unexpected'),
    (RuntimeError('native-failed'), 'unexpected'),
    (RuntimeError({'secret': 'Bearer synthetic'}), 'unexpected'),
])
def test_local_failure_diagnostics_are_closed_and_redacted(source, tmp_path, monkeypatch, error, expected):
    monkeypatch.setattr(worker.Authority, 'start', lambda self: self.beat())
    http = Transport(source)
    def native(raw, job, guard):
        guard.stage('mac-producer')
        raise error
    native.report = {'cleanup_complete': True, 'allocator_release_confirmed': True,
                     'secret': 'Bearer synthetic', 'stderr': 'remote output'}
    result = worker.run_once(SCOPE, http, native, tmp_path)
    local = failure_receipt(tmp_path)
    assert set(local) == {'stage', 'result', 'job_id', 'archive_sha256', 'execution_stage',
                          'error_code', 'authority_failure', 'failure_settlement',
                          'cleanup_complete', 'allocator_release_confirmed'}
    assert local['execution_stage'] == 'mac-producer'
    assert local['error_code'] == expected
    assert local['cleanup_complete'] is True and local['allocator_release_confirmed'] is True
    serialized = json.dumps([local, result])
    for secret in ('synthetic', 'private.invalid', 'Authorization', 'stderr', 'remote output'):
        assert secret not in serialized


@pytest.mark.parametrize('stage', ['mac-bootstrap', 'mac-permit-refresh', 'mac-producer', 'evidence'])
def test_mac_foreground_stage_survives_cleanup(source, stage):
    guard = worker.Authority(Transport(source), source[1])
    guard.beat()
    cleanup_stages = []
    class BrokenAllocator(Allocator):
        @contextmanager
        def lease(self, *args, **kwargs):
            with super().lease(*args, **kwargs) as held:
                original = held.ssh
                def ssh(command, payload=None, **kw):
                    p = json.loads(payload)
                    if len(self.calls) == 0:
                        current = 'mac-bootstrap'
                    elif 'expiry' in p:
                        current = 'mac-permit-refresh'
                    elif 'driver' in p:
                        current = 'mac-producer'
                    else:
                        cleanup_stages.append(guard.execution_stage)
                        return original(command, payload, **kw)
                    if current == stage:
                        # Count the attempted bootstrap so the following call is cleanup.
                        self.calls.append(p)
                        raise worker.WorkerError('mac-transport-failed')
                    result = original(command, payload, **kw)
                    if current == 'mac-producer' and stage == 'evidence':
                        data = json.loads(result.stdout)
                        data['screenshot_base64'] = 'invalid secret'
                        result.stdout = json.dumps(data)
                    return result
                held.ssh = ssh
                yield held
    adapter = mac.MacAdapter(BrokenAllocator(), permit_factory=FixturePermitStream)
    with pytest.raises(Exception): adapter(source[0], source[1], guard)
    assert guard.execution_stage == stage
    assert cleanup_stages == [stage]
    assert adapter.report == {'cleanup_complete': True, 'allocator_release_confirmed': True}
    assert guard.authority_failure == ('permit-refresh' if stage == 'mac-permit-refresh' else 'none')


def test_authority_lost_receipt_never_attempts_completion(source, tmp_path, monkeypatch):
    monkeypatch.setattr(worker.Authority, 'start', lambda self: self.beat())
    http = Transport(source)
    def native(raw, job, guard):
        guard.stage('mac-producer')
        http.deny = True
        guard.beat()
    worker.run_once(SCOPE, http, native, tmp_path)
    local = failure_receipt(tmp_path)
    assert local['authority_failure'] == 'heartbeat-request'
    assert local['execution_stage'] == 'mac-producer'
    assert local['error_code'] == 'http-denied'
    assert local['failure_settlement'] == 'not-attempted-authority-lost'
    assert not any(p.endswith('/complete') for _, p, _, _ in http.calls)


@pytest.mark.parametrize('reply,expected', [(TimeoutError('secret'), 'request-failed'),
                                          (b'not-json secret', 'response-unconfirmed')])
def test_uncertain_success_settlement_diagnostics(source, tmp_path, monkeypatch, reply, expected):
    monkeypatch.setattr(worker.Authority, 'start', lambda self: self.beat())
    http = Transport(source)
    original = http.request
    def request(method, path, body=None, **kw):
        result = original(method, path, body, **kw)
        if path.endswith('/complete'):
            if isinstance(reply, BaseException): raise reply
            return reply
        return result
    http.request = request
    assert worker.run_once(SCOPE, http, lambda *a: (facts(), png()), tmp_path)['result'] == 'unknown'
    local = failure_receipt(tmp_path)
    assert local['execution_stage'] == 'completion'
    assert local['failure_settlement'] == expected
    assert len([p for _, p, _, _ in http.calls if p.endswith('/complete')]) == 1


def test_native_sdk_predecessor_survives_later_preview_failure(native_package,tmp_path):
    raw,job=native_package
    bundle='private.leaf.project.p'+PROJECT.replace('-','')
    artifact=dict(source=worker.native_identity(job['descriptor']),bundle_id=bundle,
        sdk='iphonesimulator',app_tree_sha256='a'*64,file_count=3,xcodegen_exit_code=0,
        xcode_version='fixture',xcodegen_version='fixture',sdk_version='fixture')
    class LaterFailure:
        report=dict(build_artifact=artifact,cleanup_complete=True,allocator_release_confirmed=True)
        def __call__(self,raw,job,guard):
            guard.stage('mac-producer')
            raise worker.WorkerError('mac-producer-failed')
    result=worker.run_once(SCOPE,Transport(native_package),LaterFailure(),tmp_path)
    assert result['result']=='failed'
    local=json.loads((tmp_path/(JOB+'-1-failure.json')).read_text())
    assert local['build_artifact']==artifact and local['cleanup_complete'] is True
    assert local['failure_settlement']=='accepted'
