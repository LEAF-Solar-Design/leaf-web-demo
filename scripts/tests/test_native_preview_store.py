"""Synthetic store and WinAPI boundaries only; no real store or network."""
import base64
import ctypes
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_session as session
import native_preview_store as store

PATH = r'C:\synthetic\credentials.json'
CLIENT = 'synthetic-client'
AUDIENCE = 'https://api-staging.leafdesign.ai'
SECRET = 'synthetic-secret-\u00e9'
CIPHER = b'synthetic-encrypted-data'


def fingerprint(secret):
    return f'len={len(secret)} sha256[:8]={hashlib.sha256(secret.encode()).hexdigest()[:8]}'


def document(secret=SECRET, **changes):
    return {'schema': 'leaf.native-preview-client.v1', 'client_id': CLIENT,
            'subject': CLIENT + '@clients', 'audience': AUDIENCE,
            'token_url': session.TOKEN_URL, 'dpapi_scope': 'CurrentUser',
            'secret_dpapi_b64': base64.b64encode(CIPHER).decode(),
            'secret_fingerprint': fingerprint(secret), **changes}


def encode(value):
    return json.dumps(value, ensure_ascii=True).encode()


def safe(error, code):
    assert type(error) is store.StoreError
    assert error.args == (code,)
    assert error.__cause__ is None
    assert error.__context__ is None
    for data in (PATH, SECRET, CIPHER.decode(), fingerprint(SECRET)):
        assert data not in str(error) + repr(error)


def refused(call, code):
    with pytest.raises(store.StoreError) as caught:
        call()
    safe(caught.value, code)


@pytest.fixture
def source(monkeypatch):
    state = SimpleNamespace(raw=encode(document()), opens=[], reads=[], decrypts=[])
    monkeypatch.setattr(store, 'sys', SimpleNamespace(platform='win32'))

    class Stream(io.BytesIO):
        def read(self, size):
            state.reads.append(size)
            return super().read(size)

    def open_file(path, mode):
        state.opens.append((path, mode))
        return Stream(state.raw)

    def decrypt(ciphertext):
        state.decrypts.append(ciphertext)
        return SECRET

    monkeypatch.setattr(store, 'open', open_file, raising=False)
    monkeypatch.setattr(store, '_unprotect', decrypt)
    return state


def resolver():
    return store.make_credential_resolver(PATH, CLIENT, AUDIENCE)


def test_import_and_factory_are_inert(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unexpected effect')
    with monkeypatch.context() as patch:
        patch.setattr(ctypes, 'WinDLL', forbidden, raising=False)
        patch.setattr('builtins.open', forbidden)
        importlib.reload(store)
        assert callable(resolver())


@pytest.mark.parametrize('audience', sorted(session.AUDIENCES))
def test_exact_schema_f67_and_fresh_reads(source, audience):
    source.raw = encode(document(audience=audience))
    resolve = store.make_credential_resolver(PATH, CLIENT, audience)
    assert source.opens == source.decrypts == []
    first = resolve()
    second = session._credentials(resolve)
    assert first == second == {'client_id': CLIENT, 'client_secret': SECRET}
    assert first is not second
    first['client_secret'] = 'caller-modified'
    source.raw = encode(document(audience=audience, secret_fingerprint=fingerprint('replacement')))
    # A new read, including changed fingerprint, cannot reuse earlier plaintext.
    refused(resolve, 'invalid-secret')
    assert source.opens == [(PATH, 'rb')] * 3
    assert source.reads == [store.MAX_FILE_BYTES + 1] * 3
    assert source.decrypts == [CIPHER] * 3
    assert resolve.__dict__ == {}
    assert sorted(cell.cell_contents for cell in resolve.__closure__) == sorted([PATH, CLIENT, audience])
    assert not any(value == SECRET for value in vars(store).values() if type(value) is str)


@pytest.mark.parametrize('path', ['', 'relative.json', r'C:relative.json', r'\rooted.json',
    r'\\server\share\file', r'\\?\C:\file', r'\\.\pipe\name',
    'https://example.invalid/file', 'C:/file:stream', 'C:/', 'C:/bad\x00file',
    'C:/NUL', 'C:/CON.json', 'C:/COM1', None, Path('/tmp/file')])
def test_invalid_paths_before_effects(source, path):
    refused(lambda: store.make_credential_resolver(path, CLIENT, AUDIENCE), 'invalid-input')
    assert source.opens == source.decrypts == []


@pytest.mark.parametrize('client', ['', 'x' * 4097, 'x\n', 'x\u200d', 'x\ud800', None, [], 1])
def test_invalid_expected_identity(source, client):
    refused(lambda: store.make_credential_resolver(PATH, client, AUDIENCE), 'invalid-input')
    assert source.opens == []


@pytest.mark.parametrize('audience', ['', 'https://other.invalid', None, [], 1])
def test_invalid_expected_audience(source, audience):
    refused(lambda: store.make_credential_resolver(PATH, CLIENT, audience), 'invalid-input')
    assert source.opens == []


def test_string_subclasses_are_not_immutable_inputs(source):
    class Text(str):
        pass
    for args in [(Text(PATH), CLIENT, AUDIENCE), (PATH, Text(CLIENT), AUDIENCE),
                 (PATH, CLIENT, Text(AUDIENCE))]:
        refused(lambda: store.make_credential_resolver(*args), 'invalid-input')
    assert source.opens == []


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'cygwin'])
def test_platform_before_file_or_decrypt(source, monkeypatch, platform):
    resolve = resolver()
    monkeypatch.setattr(store, 'sys', SimpleNamespace(platform=platform))
    refused(resolve, 'unsupported-platform')
    assert source.opens == source.decrypts == []


@pytest.mark.parametrize('field', sorted(document()))
@pytest.mark.parametrize('value', [None, 1, True, [], {}])
def test_every_field_requires_string(source, field, value):
    source.raw = encode(document(**{field: value}))
    refused(resolver(), 'invalid-store')
    assert source.decrypts == []


@pytest.mark.parametrize('field', sorted(document()))
def test_missing_fields(source, field):
    value = document()
    del value[field]
    source.raw = encode(value)
    refused(resolver(), 'invalid-store')
    assert source.decrypts == []


@pytest.mark.parametrize('changes', [
    {'schema': 'leaf.native-preview-client.v2'}, {'client_id': 'other'},
    {'subject': 'other@clients'}, {'audience': 'https://api.leafdesign.ai'},
    {'token_url': session.TOKEN_URL + '/'}, {'dpapi_scope': 'LocalMachine'},
    {'extra': 'value'}, {'secret_fingerprint': 'len=0 sha256[:8]=00000000'},
    {'secret_fingerprint': 'len=16 sha256[:8]=ABCDEF12'},
    {'secret_fingerprint': 'len=016 sha256[:8]=abcdef12'},
    {'secret_fingerprint': 'len=4097 sha256[:8]=abcdef12'},
    {'secret_fingerprint': 'len=16 sha256[:8]=abcdef12\n'},
    {'secret_dpapi_b64': ''}, {'secret_dpapi_b64': '%%%='},
    {'secret_dpapi_b64': 'YQ'}, {'secret_dpapi_b64': 'YQ==\n'},
    {'secret_dpapi_b64': 'YR=='}, {'secret_dpapi_b64': '\u00e9'},
    {'secret_dpapi_b64': base64.b64encode(b'x' * (store.MAX_CIPHERTEXT_BYTES + 1)).decode()},
])
def test_schema_identity_and_ciphertext_drift(source, changes):
    source.raw = encode(document(**changes))
    refused(resolver(), 'invalid-store')
    assert source.decrypts == []


@pytest.mark.parametrize('raw', [b'', b'\xff', b'null', b'[]', b'NaN', b'{',
    b'{' + b'"schema":"a","schema":"b"}',
    b'{"schema":{"nested":1,"nested":2}}', b' ' * (store.MAX_FILE_BYTES + 1),
    b'"' + b'x' * 5000 + b'"'])
def test_parse_and_byte_bounds(source, raw):
    source.raw = raw
    refused(resolver(), 'invalid-store')
    assert source.decrypts == []


def test_exact_file_and_ciphertext_bounds(source, monkeypatch):
    cipher = b'x' * store.MAX_CIPHERTEXT_BYTES
    raw = encode(document(secret_dpapi_b64=base64.b64encode(cipher).decode()))
    source.raw = raw + b' ' * (store.MAX_FILE_BYTES - len(raw))
    assert resolver()()['client_secret'] == SECRET
    assert source.decrypts == [cipher]


@pytest.mark.parametrize('secret', ['', 'x' * 4097, 'x\n', 'x\x00', 'x\u200d',
                                    'x\ud800', None, b'bytes', 123])
def test_secret_restrictions(source, monkeypatch, secret):
    monkeypatch.setattr(store, '_unprotect', lambda _: secret)
    refused(resolver(), 'invalid-secret')


@pytest.mark.parametrize('secret', ['a', 'x' * 4096, '\U0001f600' * 4096])
def test_secret_character_bounds_and_utf8_fingerprint(source, monkeypatch, secret):
    source.raw = encode(document(secret=secret))
    monkeypatch.setattr(store, '_unprotect', lambda _: secret)
    assert session._credentials(resolver())['client_secret'] == secret


@pytest.mark.parametrize('value', ['len=1 sha256[:8]=00000000',
                                  f'len={len(SECRET)} sha256[:8]=00000000'])
def test_fingerprint_mismatch(source, value):
    source.raw = encode(document(secret_fingerprint=value))
    refused(resolver(), 'invalid-secret')


@pytest.mark.parametrize('stage', ['open', 'read', 'close', 'parse', 'decrypt'])
@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt])
def test_exception_payloads_are_discarded(source, monkeypatch, stage, error_type):
    def fail(*args, **kwargs):
        raise error_type(SECRET + PATH + fingerprint(SECRET))
    if stage == 'open':
        monkeypatch.setattr(store, 'open', fail)
    elif stage in ('read', 'close'):
        class Stream(io.BytesIO):
            def read(self, size):
                return fail() if stage == 'read' else source.raw

            def __exit__(self, *args):
                if stage == 'close':
                    fail()
        monkeypatch.setattr(store, 'open', lambda *args: Stream())
    elif stage == 'parse':
        monkeypatch.setattr(store.json, 'loads', fail)
    else:
        monkeypatch.setattr(store, '_unprotect', fail)
    refused(resolver(), 'read-failed' if stage in ('open', 'read', 'close') else
            'invalid-store' if stage == 'parse' else 'decrypt-failed')


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def api(monkeypatch, payload=SECRET.encode(), outcome='success', size=None):
    state = SimpleNamespace(freed=[], calls=[], loaded=[])
    buffer = (ctypes.c_ubyte * max(1, len(payload))).from_buffer_copy(payload or b'\0')
    address = ctypes.addressof(buffer)

    def decrypt(incoming, description, entropy, reserved, prompt, flags, outgoing):
        state.calls.append(flags)
        assert description is entropy is reserved is prompt is None
        incoming = ctypes.cast(incoming, ctypes.POINTER(store._DataBlob)).contents
        assert ctypes.string_at(incoming.pbData, incoming.cbData) == CIPHER
        result = ctypes.cast(outgoing, ctypes.POINTER(store._DataBlob)).contents
        if outcome != 'null':
            result.cbData = len(payload) if size is None else size
            result.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        if outcome == 'raise':
            raise RuntimeError(SECRET)
        return outcome not in ('failure', 'null')

    def free(pointer):
        assert type(pointer) is ctypes.c_void_p
        assert pointer.value == address
        state.freed.append(bytes(buffer))
        return None

    state.decrypt = Function(decrypt)
    state.free = Function(free)

    def load(name):
        state.loaded.append(name)
        return SimpleNamespace(CryptUnprotectData=state.decrypt, LocalFree=state.free)

    monkeypatch.setattr(store, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(store, '_load_library', load)
    return state


def test_winapi_success_signatures_and_zero_free(monkeypatch):
    state = api(monkeypatch)
    assert store._unprotect(CIPHER) == SECRET
    assert state.calls == [1]
    assert state.loaded == ['crypt32.dll', 'kernel32.dll']
    assert state.freed == [b'\0' * len(SECRET.encode())]
    assert state.decrypt.restype is ctypes.c_int
    assert state.decrypt.argtypes == [ctypes.POINTER(store._DataBlob),
        ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(store._DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(store._DataBlob)]
    assert state.free.argtypes == [ctypes.c_void_p]
    assert state.free.restype is ctypes.c_void_p
    assert ctypes.sizeof(store._DataBlob) >= 4 + ctypes.sizeof(ctypes.c_void_p)


@pytest.mark.parametrize('outcome,payload', [('failure', b'allocated'),
    ('raise', b'allocated'), ('null', b''), ('success', b'\xff'), ('success', b'')])
def test_winapi_failure_and_decode_zero_free(monkeypatch, outcome, payload):
    state = api(monkeypatch, payload, outcome)
    refused(lambda: store._unprotect(CIPHER), 'decrypt-failed')
    assert state.freed == ([] if outcome == 'null' else [b'\0' * max(1, len(payload))])


def test_winapi_oversized_plaintext_bounded_before_copy(monkeypatch):
    payload = b'x' * (store.MAX_SECRET_BYTES + 1)
    state = api(monkeypatch, payload)
    copied = []
    # Input inspection also uses string_at, so leave that one bounded call intact.
    original = ctypes.string_at
    def copy(pointer, size):
        if size != len(CIPHER):
            copied.append(size)
        return original(pointer, size)
    monkeypatch.setattr(store.ctypes, 'string_at', copy)
    refused(lambda: store._unprotect(CIPHER), 'decrypt-failed')
    assert copied == []
    assert state.freed == [b'\0' * len(payload)]


def test_winapi_copy_failure_still_zero_free(monkeypatch):
    state = api(monkeypatch)
    original = ctypes.string_at
    def copy(pointer, size):
        if size == len(CIPHER):
            return original(pointer, size)
        raise RuntimeError(SECRET)
    monkeypatch.setattr(store.ctypes, 'string_at', copy)
    refused(lambda: store._unprotect(CIPHER), 'decrypt-failed')
    assert state.freed == [b'\0' * len(SECRET.encode())]


def test_local_free_even_if_overwrite_fails(monkeypatch):
    state = api(monkeypatch)
    def fail(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(store.ctypes, 'memset', fail)
    refused(lambda: store._unprotect(CIPHER), 'decrypt-failed')
    assert state.freed == [SECRET.encode()]


def test_loader_failure_and_non_windows_are_sanitized(monkeypatch):
    calls = []
    def fail(name):
        calls.append(name)
        raise RuntimeError(SECRET)
    monkeypatch.setattr(store, '_load_library', fail)
    monkeypatch.setattr(store, 'sys', SimpleNamespace(platform='linux'))
    refused(lambda: store._unprotect(CIPHER), 'decrypt-failed')
    assert calls == []
    monkeypatch.setattr(store, 'sys', SimpleNamespace(platform='win32'))
    refused(lambda: store._unprotect(CIPHER), 'decrypt-failed')
    assert calls == ['crypt32.dll']


def test_error_code_set_is_closed():
    for value in (SECRET, None, [], 1):
        safe(store.StoreError(value), 'invalid-store')


def test_exact_duplicate_field_is_refused_before_decrypt(source):
    raw = encode(document())
    source.raw = raw[:-1] + b',"client_id":"synthetic-client"}'
    refused(resolver(), 'invalid-store')
    assert source.decrypts == []


@pytest.mark.parametrize('raw', ['text', None, bytearray(b'{}'), b'x' * (store.MAX_FILE_BYTES + 1)])
def test_read_result_is_bounded_bytes(source, monkeypatch, raw):
    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == store.MAX_FILE_BYTES + 1
            return raw
    monkeypatch.setattr(store, 'open', lambda *args: Stream())
    refused(resolver(), 'invalid-store')
    assert source.decrypts == []


def test_winapi_full_resolver_to_f67_and_validation_cleanup(monkeypatch):
    state = api(monkeypatch)
    raw = encode(document())
    monkeypatch.setattr(store, 'open', lambda *args: io.BytesIO(raw), raising=False)
    assert session._credentials(resolver()) == {'client_id': CLIENT, 'client_secret': SECRET}
    assert state.freed == [b'\0' * len(SECRET.encode())]
    raw = encode(document(secret_fingerprint='len=1 sha256[:8]=00000000'))
    refused(resolver(), 'invalid-secret')
    assert state.freed == [b'\0' * len(SECRET.encode())] * 2


def test_winapi_exact_plaintext_byte_bound(monkeypatch):
    secret = '\U0001f600' * 4096
    state = api(monkeypatch, secret.encode())
    assert store._unprotect(CIPHER) == secret
    assert state.freed == [b'\0' * store.MAX_SECRET_BYTES]


def test_local_free_failure_is_closed(monkeypatch):
    state = api(monkeypatch)
    def fail(pointer):
        raise RuntimeError(SECRET)
    state.free.call = fail
    refused(lambda: store._unprotect(CIPHER), 'decrypt-failed')
