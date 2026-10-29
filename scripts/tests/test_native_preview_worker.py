"""Hermetic F22 worker checks. These fixtures are not native/live evidence."""
import ast
import base64
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
import zipfile
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_worker as worker
import native_preview_mac as mac

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
def test_http_product_user_agent(method, suffix, body):
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
                elif len(owner.calls) == 1: result = {'remote_clock': time.monotonic()}
                else: result = {'cleanup_complete': owner.cleanup}
                return subprocess.CompletedProcess([], 0, json.dumps(result), '')
        self.held = Held()
        try:
            yield self.held
        finally:
            self.held.release()


def test_mac_adapter_actual_facts_transfer_cleanup_release(source):
    allocator = Allocator()
    guard = worker.Authority(Transport(source), source[1])
    guard.beat()
    result, image = mac.MacAdapter(allocator)(source[0], source[1], guard)
    assert image == png() and result == facts()
    assert 'expiry' in allocator.calls[1]
    assert base64.b64decode(allocator.calls[2]['archive']) == source[0]
    assert guard.refresh is None


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
        mac.MacAdapter(Allocator(release_code, cleanup))(source[0], source[1], guard)


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
    adapter = mac.MacAdapter(BrokenAllocator())
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
