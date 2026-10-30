"""Inert, explicit-path reader for the CurrentUser native preview store.

No provisioning, discovery, activation or plaintext caching. DPAPI's allocated
buffer is overwritten and freed; Python strings do not offer forensic erasure.
"""
import base64
import ctypes
import hashlib
import hmac
import json
import ntpath
import re
import sys
import unicodedata

from native_preview_session import AUDIENCES, TOKEN_URL

MAX_FILE_BYTES = 65536
MAX_CIPHERTEXT_BYTES = 32768
MAX_SECRET_BYTES = 16384
ERROR_CODES = frozenset({'invalid-input', 'unsupported-platform', 'read-failed',
                         'invalid-store', 'decrypt-failed', 'invalid-secret'})
_FIELDS = frozenset({'schema', 'client_id', 'subject', 'audience', 'token_url',
                     'dpapi_scope', 'secret_dpapi_b64', 'secret_fingerprint'})


class StoreError(RuntimeError):
    def __init__(self, code):
        super().__init__(code if type(code) is str and code in ERROR_CODES else 'invalid-store')


def _check(condition):
    if not condition:
        raise StoreError('invalid-store')


def _credential(value):
    return (type(value) is str and 1 <= len(value) <= 4096
            and all(unicodedata.category(c) not in ('Cc', 'Cf', 'Cs') for c in value))


def _local_path(path):
    if type(path) is not str or not path or any(ord(c) < 32 for c in path):
        return False
    drive, tail = ntpath.splitdrive(path)
    return (re.fullmatch(r'[A-Za-z]:', drive) is not None
            and tail.startswith(('\\', '/')) and ':' not in tail
            and not tail.endswith(('\\', '/'))
            and not any(re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])',
                                     part.split('.')[0].rstrip(' ')) is not None
                        for part in re.split(r'[\\/]', tail)))


def _pairs(items):
    value = {}
    for key, item in items:
        _check(key not in value)
        value[key] = item
    return value


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32),
                ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _load_library(name):
    # Loading is deferred until resolution, and never attempted on Linux.
    return ctypes.WinDLL(name, use_last_error=True)


def _unprotect(ciphertext):
    """Return UTF8 text, freeing DPAPI output even when copying/decoding fails."""
    success = False
    try:
        _check(sys.platform == 'win32')
        _check(type(ciphertext) is bytes and 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES)
        crypt = _load_library('crypt32.dll')
        kernel = _load_library('kernel32.dll')
        decrypt = crypt.CryptUnprotectData
        decrypt.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_wchar_p),
                            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_uint32, ctypes.POINTER(_DataBlob)]
        decrypt.restype = ctypes.c_int
        free = kernel.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        source = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
        incoming = _DataBlob(len(ciphertext), source)
        outgoing = _DataBlob()
        try:
            _check(decrypt(ctypes.byref(incoming), None, None, None, None,
                           0x1, ctypes.byref(outgoing)))  # CRYPTPROTECT_UI_FORBIDDEN
            _check(bool(outgoing.pbData) and 1 <= outgoing.cbData <= MAX_SECRET_BYTES)
            plaintext = ctypes.string_at(outgoing.pbData, outgoing.cbData).decode('utf-8')
            success = True
        finally:
            if outgoing.pbData:
                try:
                    ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
                finally:
                    _check(not free(ctypes.cast(outgoing.pbData, ctypes.c_void_p)))
        if success:
            return plaintext
    except BaseException:
        pass
    raise StoreError('decrypt-failed')


def _resolve(path, expected_client_id, audience):
    code = 'unsupported-platform'
    try:
        _check(sys.platform == 'win32')
        code = 'read-failed'
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        code = 'invalid-store'
        _check(type(raw) is bytes and 1 <= len(raw) <= MAX_FILE_BYTES)
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _check(False))
        _check(type(value) is dict and set(value) == _FIELDS)
        _check(all(type(item) is str for item in value.values()))
        _check(value['schema'] == 'leaf.native-preview-client.v1'
               and value['client_id'] == expected_client_id
               and value['subject'] == expected_client_id + '@clients'
               and value['audience'] == audience and value['token_url'] == TOKEN_URL
               and value['dpapi_scope'] == 'CurrentUser')
        fingerprint = value['secret_fingerprint']
        _check(re.fullmatch(r'len=[1-9][0-9]{0,3} sha256\[:8\]=[0-9a-f]{8}', fingerprint)
               is not None)
        _check(int(fingerprint.split(' ', 1)[0][4:]) <= 4096)
        encoded = value['secret_dpapi_b64']
        _check(1 <= len(encoded) <= 4 * ((MAX_CIPHERTEXT_BYTES + 2) // 3))
        ciphertext = base64.b64decode(encoded, validate=True)
        _check(1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES
               and base64.b64encode(ciphertext).decode('ascii') == encoded)
        code = 'decrypt-failed'
        secret = _unprotect(ciphertext)
        code = 'invalid-secret'
        _check(_credential(secret))
        digest = hashlib.sha256(secret.encode('utf-8')).hexdigest()[:8]
        _check(hmac.compare_digest(fingerprint, f'len={len(secret)} sha256[:8]={digest}'))
        return {'client_id': expected_client_id, 'client_secret': secret}
    except BaseException:
        pass
    # Do not chain arbitrary read, parser, codec or WinAPI exception payloads.
    raise StoreError(code)


def make_credential_resolver(path, expected_client_id, audience):
    """Validate immutable configuration without I/O; reopen on every call."""
    valid = False
    try:
        valid = (_local_path(path) and _credential(expected_client_id)
                 and type(audience) is str and audience in AUDIENCES)
    except BaseException:
        pass
    if not valid:
        raise StoreError('invalid-input')

    def resolve():
        return _resolve(path, expected_client_id, audience)

    return resolve
