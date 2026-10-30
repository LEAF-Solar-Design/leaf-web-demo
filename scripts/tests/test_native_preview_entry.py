"""Synthetic config, CLI and full resolver/host composition boundaries."""
import base64
import ctypes
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_entry as entry

CONFIG_PATH = r'C:\synthetic\host.json'
SECRET = 'synthetic-secret-payload'
SCOPE = dict(org_id='11111111-1111-4111-8111-111111111111',
             project_id='22222222-2222-4222-8222-222222222222',
             actor_id='33333333-3333-4333-8333-333333333333', worker_id='synthetic')
STOPPED = {'state': 'stopped', 'completed': 0}


def config():
    return dict(schema='leaf.native-preview-host.v1', scope=SCOPE.copy(),
                credential_store_path=r'C:\synthetic\store.json',
                expected_client_id='synthetic-client', audience='https://api-staging.leafdesign.ai',
                output_root=r'C:\synthetic\output', poll_seconds=5, error_seconds=30)


def changed(**changes):
    return {**config(), **changes}


def forbidden(*args, **kwargs):
    pytest.fail('unexpected effect')


def fail(*args, **kwargs):
    raise RuntimeError(SECRET)


def safe(call, code):
    with pytest.raises(entry.EntryError) as caught:
        call()
    error = caught.value
    assert error.args == (code,)
    assert error.__cause__ is error.__context__ is None
    assert SECRET not in str(error) + repr(error)


@pytest.fixture
def source(monkeypatch):
    state = SimpleNamespace(raw=json.dumps(config()).encode(), opens=[], reads=[])
    class Stream(io.BytesIO):
        def read(self, limit):
            state.reads.append(limit)
            return super().read(limit)
    def open_file(path, mode):
        state.opens.append((path, mode))
        return Stream(state.raw)
    monkeypatch.setattr(entry, 'open', open_file, raising=False)
    return state


def test_import_is_inert(monkeypatch):
    monkeypatch.setattr(entry.store, 'make_credential_resolver', forbidden)
    monkeypatch.setattr(entry.host, 'run_host', forbidden)
    monkeypatch.setattr(entry.host.signal, 'signal', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden, raising=False)
    monkeypatch.setattr('builtins.open', forbidden)
    for platform in ('linux', 'win32'):
        monkeypatch.setattr(entry.store, 'sys', SimpleNamespace(platform=platform))
        spec = importlib.util.spec_from_file_location('_inert_entry', entry.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert callable(module.load_config) and callable(module.run_config) and callable(module.main)


def test_load_exact_and_byte_bounds(source):
    raw = source.raw
    source.raw = raw + b' ' * (entry.MAX_FILE_BYTES - len(raw))
    assert entry.load_config(CONFIG_PATH) == config()
    assert source.opens == [(CONFIG_PATH, 'rb')]
    assert source.reads == [16385]
    source.raw += b' '
    safe(lambda: entry.load_config(CONFIG_PATH), 'invalid-config')


def test_strict_json_and_nested_duplicates(source):
    actor = json.dumps(SCOPE['actor_id']).encode()
    nested_duplicate = source.raw.replace(b'"actor_id": ' + actor,
        b'"actor_id": ' + actor + b', "actor_id": ' + actor)
    for raw in (b'', b'\xff', b'\xef\xbb\xbf{}', b'null', b'[]', b'{', b'NaN',
                b'{"schema":"a","schema":"b"}',
                nested_duplicate,
                source.raw[:-1] + b',"schema":"leaf.native-preview-host.v1"}'):
        source.raw = raw
        safe(lambda: entry.load_config(CONFIG_PATH), 'invalid-config')


def test_nonfinite_json_and_waits(source):
    for token in ('NaN', 'Infinity', '-Infinity', '1e999'):
        source.raw = json.dumps(config()).replace('"poll_seconds": 5', '"poll_seconds": ' + token).encode()
        safe(lambda: entry.load_config(CONFIG_PATH), 'invalid-config')
    for key in ('poll_seconds', 'error_seconds'):
        for value in (True, False, None, '5', [], {}, 0, -1, 3600.01, float('inf'), float('nan')):
            safe(lambda: entry.run_config(changed(**{key: value})), 'invalid-config')


def test_unknown_missing_fields_and_types():
    safe(lambda: entry.run_config(changed(extra=SECRET)), 'invalid-config')
    for key in config():
        value = config()
        del value[key]
        safe(lambda: entry.run_config(value), 'invalid-config')
        for bad in (None, True, [], {}):
            safe(lambda: entry.run_config(changed(**{key: bad})), 'invalid-config')
    for value in (None, [], 'text', 1):
        safe(lambda: entry.run_config(value), 'invalid-config')
    safe(lambda: entry.run_config(changed(schema='other')), 'invalid-config')
    safe(lambda: entry.run_config(changed(expected_client_id='\U0001f600' * 4096)), 'invalid-config')


def test_scope_identity_and_metadata_validation():
    for key in SCOPE:
        for bad in (None, True, SECRET + '\n', 'not-a-uuid' if key != 'worker_id' else ''):
            safe(lambda: entry.run_config(changed(scope={**SCOPE, key: bad})), 'invalid-config')
    for scope in ({}, {**SCOPE, 'extra': SECRET}):
        safe(lambda: entry.run_config(changed(scope=scope)), 'invalid-config')
    for client in ('', 'x' * 4097, 'x\n', 'x\u200d', 'x\ud800', 1):
        safe(lambda: entry.run_config(changed(expected_client_id=client)), 'invalid-config')
    for audience in ('', 'https://other.invalid', config()['audience'] + '/', 1):
        safe(lambda: entry.run_config(changed(audience=audience)), 'invalid-config')


def test_paths_refused_before_any_effect(monkeypatch):
    monkeypatch.setattr(entry, 'open', forbidden, raising=False)
    monkeypatch.setattr(entry.store, 'make_credential_resolver', forbidden)
    monkeypatch.setattr(entry.host, 'run_host', forbidden)
    monkeypatch.setattr(entry.host.signal, 'signal', forbidden)
    monkeypatch.setattr(entry.host.session.urllib.request, 'build_opener', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    paths = ('', 'relative', r'C:relative', r'\rooted', r'\\server\share\file',
             r'\\?\C:\file', r'\\.\pipe\name', '//server/share/file',
             'https://example.invalid/file', 'C:/file:stream', 'C:/NUL',
             'C:/CON.json', 'C:/bad\x00file', 'C:/bad?file', 'C:/bad.', None, Path('/tmp/a'))
    for path in paths:
        safe(lambda: entry.load_config(path), 'invalid-config')
        for key in ('credential_store_path', 'output_root'):
            safe(lambda: entry.run_config(changed(**{key: path})), 'invalid-config')
    safe(lambda: entry.run_config(changed(poll_seconds=True)), 'invalid-config')
    safe(lambda: entry.run_config(changed(scope={})), 'invalid-config')


def test_exact_passthrough_and_no_resolver_preflight(monkeypatch):
    value = changed(output_root='D:/Stable Output', poll_seconds=3600, error_seconds=0.1)
    calls = []
    def factory(*args):
        calls.append(args)
        return forbidden
    def host(*args, **kwargs):
        assert args == (value['scope'], forbidden, value['audience'], value['output_root'])
        assert args[0] is value['scope']
        assert kwargs == dict(poll_seconds=3600, error_seconds=0.1)
        return STOPPED
    monkeypatch.setattr(entry.store, 'make_credential_resolver', factory)
    monkeypatch.setattr(entry.host, 'run_host', host)
    assert entry.run_config(value) == STOPPED
    assert calls == [(value['credential_store_path'], value['expected_client_id'], value['audience'])]
    assert value['scope']['actor_id'] == SCOPE['actor_id']


def test_real_factory_construction_is_inert(monkeypatch):
    monkeypatch.setattr(entry.store, 'open', forbidden, raising=False)
    monkeypatch.setattr(entry.store, '_unprotect', forbidden)
    def host(scope, resolver, audience, output_root, **kwargs):
        assert callable(resolver)
        return STOPPED
    monkeypatch.setattr(entry.host, 'run_host', host)
    for root in (config()['output_root'], 'C:/', 'D:/Stable Output/'):
        assert entry.run_config(changed(output_root=root)) == STOPPED


def test_terminal_mapping_once(source, monkeypatch, capsys):
    calls = []
    for state, status in (('stopped', 0), ('busy', 3), ('reconciliation-required', 4)):
        def host(*args, **kwargs):
            calls.append(state)
            return {'state': state, 'completed': 2}
        monkeypatch.setattr(entry.host, 'run_host', host)
        assert entry.main(['--config', CONFIG_PATH]) == status
        assert json.loads(capsys.readouterr().out) == {'state': state, 'completed': 2}
    assert calls == ['stopped', 'busy', 'reconciliation-required']


def test_invalid_host_results(source, monkeypatch, capsys):
    values = (None, [], {}, {'state': 'stopped'}, {**STOPPED, 'extra': SECRET},
              {**STOPPED, 'state': SECRET}, {**STOPPED, 'state': []},
              *({**STOPPED, 'completed': value} for value in (True, -1, 0.0, None, SECRET)))
    for value in values:
        monkeypatch.setattr(entry.host, 'run_host', lambda *a, **k: value)
        safe(lambda: entry.run_config(config()), 'invalid-result')
        assert entry.main(['--config', CONFIG_PATH]) == 1
        assert json.loads(capsys.readouterr().out) == {'state': 'invalid-result'}


def test_cli_exact_arguments_and_fixed_help(monkeypatch, capsys):
    monkeypatch.setattr(entry, 'load_config', forbidden)
    for args in ([], ['--config'], ['--config=' + SECRET], ['--other', SECRET],
                 ['--config', CONFIG_PATH, SECRET], ['--config', SECRET],
                 ['--help', SECRET], [SECRET], ['--config', CONFIG_PATH, '--config', CONFIG_PATH]):
        assert entry.main(args) == 2
        assert capsys.readouterr().out == '{"state":"invalid-config"}\n'
    for args in (['--help'], ['-h']):
        assert entry.main(args) == 0
        assert capsys.readouterr().out == entry._USAGE


def test_cli_default_argv(source, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['ignored', '--config', CONFIG_PATH])
    monkeypatch.setattr(entry.host, 'run_host', lambda *a, **k: STOPPED)
    assert entry.main() == 0
    assert json.loads(capsys.readouterr().out) == STOPPED


def test_parser_and_io_exception_chains(source, monkeypatch, capsys):
    with monkeypatch.context() as patch:
        patch.setattr(entry.json, 'loads', fail)
        safe(lambda: entry.load_config(CONFIG_PATH), 'invalid-config')
        assert entry.main(['--config', CONFIG_PATH]) == 2
        assert capsys.readouterr().out == '{"state":"invalid-config"}\n'
    for stage in ('open', 'read', 'close'):
        class Stream(io.BytesIO):
            def read(self, limit):
                return fail() if stage == 'read' else source.raw
            def __exit__(self, *args):
                if stage == 'close':
                    fail()
        with monkeypatch.context() as patch:
            patch.setattr(entry, 'open', fail if stage == 'open' else lambda *a: Stream())
            safe(lambda: entry.load_config(CONFIG_PATH), 'read-failed')
            assert entry.main(['--config', CONFIG_PATH]) == 2
            assert capsys.readouterr().out == '{"state":"read-failed"}\n'


def test_store_host_and_arbitrary_cli_failures(source, monkeypatch, capsys):
    for target, name, code in ((entry.store, 'make_credential_resolver', 'entry-failed'),
                               (entry.host, 'run_host', 'host-failed')):
        with monkeypatch.context() as patch:
            patch.setattr(target, name, fail)
            safe(lambda: entry.run_config(config()), code)
            assert entry.main(['--config', CONFIG_PATH]) == 1
            assert capsys.readouterr().out == json.dumps({'state': code}, separators=(',', ':')) + '\n'
    monkeypatch.setattr(entry, 'load_config', fail)
    assert entry.main(['--config', CONFIG_PATH]) == 1
    assert capsys.readouterr().out == '{"state":"entry-failed"}\n'


def test_keyboard_interrupt_is_not_restarted(monkeypatch):
    calls = []
    def interrupt(*args, **kwargs):
        calls.append(True)
        raise KeyboardInterrupt()
    monkeypatch.setattr(entry.host, 'run_host', interrupt)
    with pytest.raises(KeyboardInterrupt):
        entry.run_config(config())
    assert calls == [True]


def test_error_codes_are_closed():
    for value in (SECRET, None, [], 1):
        error = entry.EntryError(value)
        assert error.args == ('entry-failed',)
        assert error.__cause__ is error.__context__ is None


def test_real_store_host_session_service_worker_empty_queue(monkeypatch, tmp_path):
    store, host = entry.store, entry.host
    value = config()
    cipher = b'synthetic-ciphertext'
    raw = json.dumps(dict(schema='leaf.native-preview-client.v1',
        client_id=value['expected_client_id'], subject=value['expected_client_id'] + '@clients',
        audience=value['audience'], token_url=host.session.TOKEN_URL, dpapi_scope='CurrentUser',
        secret_dpapi_b64=base64.b64encode(cipher).decode(),
        secret_fingerprint=f'len={len(SECRET)} sha256[:8]={hashlib.sha256(SECRET.encode()).hexdigest()[:8]}')).encode()
    opened, freed, requests, closed, native = [], [], [], [], []
    old = {host.signal.SIGINT: object(), host.signal.SIGTERM: object()}
    handlers = old.copy()
    def signal(signum, handler):
        handlers[signum] = handler
    monkeypatch.setattr(host.signal, 'getsignal', handlers.__getitem__)
    monkeypatch.setattr(host.signal, 'signal', signal)
    def open_store(path, mode):
        opened.append((path, mode))
        return io.BytesIO(raw)
    monkeypatch.setattr(store, 'open', open_store, raising=False)
    monkeypatch.setattr(store, 'sys', SimpleNamespace(platform='win32'))
    buffer = (ctypes.c_ubyte * len(SECRET)).from_buffer_copy(SECRET.encode())
    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *args):
            return self.call(*args)
    def decrypt(incoming, description, entropy, reserved, prompt, flags, outgoing):
        incoming = ctypes.cast(incoming, ctypes.POINTER(store._DataBlob)).contents
        assert ctypes.string_at(incoming.pbData, incoming.cbData) == cipher
        assert flags == 1 and description is entropy is reserved is prompt is None
        outgoing = ctypes.cast(outgoing, ctypes.POINTER(store._DataBlob)).contents
        outgoing.cbData = len(buffer)
        outgoing.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return 1
    def free(pointer):
        freed.append(bytes(buffer))
        return None
    libraries = SimpleNamespace(CryptUnprotectData=Function(decrypt), LocalFree=Function(free))
    monkeypatch.setattr(store, '_load_library', lambda name: libraries)
    class Response:
        status = 200
        def __init__(self, url, raw):
            self.url, self.raw = url, raw
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def geturl(self):
            return self.url
        def read(self, limit):
            return self.raw[:limit]
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 10
            if request.full_url == host.session.TOKEN_URL:
                assert json.loads(request.data) == dict(client_id=value['expected_client_id'],
                    client_secret=SECRET, audience=value['audience'], grant_type='client_credentials')
                return Response(request.full_url, b'{"access_token":"fixture","token_type":"Bearer","expires_in":60}')
            assert request.full_url.endswith('/worker/claim') and request.method == 'POST'
            assert request.get_header('Authorization') == 'Bearer fixture'
            assert json.loads(request.data) == {}
            handlers[host.signal.SIGTERM](host.signal.SIGTERM, None)
            return Response(request.full_url, b'{"job":null}')
    monkeypatch.setattr(host.session.urllib.request, 'build_opener', lambda *a: Opener())
    def adapter():
        native.append(True)
        return forbidden
    monkeypatch.setattr(host.session, 'MacAdapter', adapter)
    close = host.session._Session.close
    def record_close(session):
        close(session)
        closed.append(session)
    monkeypatch.setattr(host.session._Session, 'close', record_close)
    # Translate only the filesystem boundary so Windows configuration remains exact.
    original_path = host.Path
    seen = []
    def output_path(path):
        seen.append(path)
        assert path == value['output_root']
        return original_path(tmp_path)
    monkeypatch.setattr(host, 'Path', output_path)
    assert entry.run_config(value) == STOPPED
    assert seen == [value['output_root']] and handlers == old
    assert opened == [(value['credential_store_path'], 'rb')]
    assert freed == [b'\0' * len(SECRET)] and len(requests) == 2 and native == [True]
    assert len(closed) == 1
    assert all(getattr(closed[0], name) is None for name in
               ('_token', '_scope', '_resolver', '_audience', '_opener', '_clock'))
    directory = tmp_path / hashlib.sha256(host.service.worker.encode(SCOPE)).hexdigest()
    assert json.loads((directory / 'consumer.json').read_bytes()) == {
        'scope_sha256': directory.name, 'phase': 'clean'}
    assert not list(directory.rglob('*.png'))
    assert all(SECRET.encode() not in path.read_bytes()
               for path in directory.rglob('*') if path.is_file())
