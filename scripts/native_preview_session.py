"""Dormant machine sessions; credentials come only from a trusted resolver.

The server remains the JWT verification authority. Closing drops references and
refuses future calls; it does not promise forensic memory zeroization. Native
allocation and cleanup remain the worker's responsibility.
"""
from contextlib import contextmanager
import json
import math
import threading
import time
import unicodedata
import urllib.request

from native_preview_worker import Http, NoRedirect, USER_AGENT, binding
from native_preview_mac import MacAdapter

ISSUER = 'https://leafautomation.us.auth0.com'
TOKEN_URL = ISSUER + '/oauth/token'
AUDIENCES = frozenset({'https://api.leafdesign.ai', 'https://api-staging.leafdesign.ai'})
MAX_RESPONSE = 32768
ERROR_CODES = frozenset({'invalid-audience', 'invalid-scope', 'invalid-credentials',
                         'token-unavailable', 'invalid-token-response',
                         'session-closed', 'native-unavailable', 'platform-unavailable'})


class SessionError(RuntimeError):
    def __init__(self, code):
        super().__init__(code if type(code) is str and code in ERROR_CODES else 'token-unavailable')


def _check(condition, code):
    if not condition:
        raise SessionError(code)


def _credentials(resolver):
    code = 'invalid-credentials'
    try:
        value = resolver()
        _check(type(value) is dict and set(value) == {'client_id', 'client_secret'}, code)
        for item in value.values():
            _check(type(item) is str and 1 <= len(item) <= 4096
                   and all(unicodedata.category(c) not in ('Cc', 'Cf', 'Cs') for c in item), code)
        return value.copy()
    except BaseException:
        pass
    # Raise outside the handler: even __context__ must not retain secret text.
    raise SessionError(code)


def _pairs(items):
    result = {}
    for key, value in items:
        _check(key not in result, 'invalid-token-response')
        result[key] = value
    return result


def _token(opener, credentials, audience):
    code = 'token-unavailable'
    try:
        data = json.dumps({**credentials, 'audience': audience,
                           'grant_type': 'client_credentials'}, allow_nan=False).encode('utf-8')
        request = urllib.request.Request(TOKEN_URL, data=data, method='POST', headers={
            'Content-Type': 'application/json', 'User-Agent': USER_AGENT})
        with opener.open(request, timeout=10) as response:
            code = 'invalid-token-response'
            _check(response.status == 200 and response.geturl() == TOKEN_URL, code)
            raw = response.read(MAX_RESPONSE + 1)
            _check(type(raw) is bytes and len(raw) <= MAX_RESPONSE, code)
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _check(False, code))
        _check(type(value) is dict and value.get('token_type') == 'Bearer', code)
        token, expiry = value.get('access_token'), value.get('expires_in')
        _check(type(token) is str and 1 <= len(token) <= 4096
               and all(33 <= ord(c) <= 126 for c in token), code)
        _check(type(expiry) in (int, float) and 60 <= expiry <= 86400
               and math.isfinite(expiry), code)
        return token, expiry
    except BaseException:
        pass
    raise SessionError(code)


class _Session:
    def __init__(self, scope, resolver, audience, opener, clock):
        self._scope, self._resolver, self._audience = scope, resolver, audience
        self._opener, self._clock = opener, clock
        self._lock = threading.Lock()
        self._closed = False
        self._token = None
        self._expiry = float('-inf')

    def _http(self):
        with self._lock:
            _check(not self._closed, 'session-closed')
            if self._token is None or self._clock() >= self._expiry - 30:
                credentials = _credentials(self._resolver)
                sent = self._clock()
                token, lifetime = _token(self._opener, credentials, self._audience)
                self._token, self._expiry = token, sent + lifetime
            return Http(self._scope, self._token, opener=self._opener)

    def request(self, method, suffix, body=None, limit=16384):
        http = self._http()
        try:
            # No refresh lock across platform I/O, and no retry of any request.
            return http.request(method, suffix, body, limit)
        except BaseException:
            pass
        finally:
            http._token = None
        raise SessionError('platform-unavailable')

    def close(self):
        with self._lock:
            self._closed = True
            self._token = self._scope = self._resolver = self._audience = None
            self._opener = self._clock = None


def make_session_factory(credential_resolver, audience, *, opener_factory=None,
                         native_factory=None, clock=time.monotonic):
    """Return factory(scope); creation itself performs no I/O or allocation."""
    _check(type(audience) is str and audience in AUDIENCES, 'invalid-audience')
    if opener_factory is None:
        opener_factory = lambda: urllib.request.build_opener(NoRedirect())
    if native_factory is None:
        native_factory = MacAdapter

    @contextmanager
    def session(scope):
        checked = None
        try:
            checked = binding(scope)
        except BaseException:
            pass
        _check(checked is not None, 'invalid-scope')
        opener = None
        try:
            opener = opener_factory()
        except BaseException:
            pass
        _check(opener is not None, 'token-unavailable')
        http = _Session(checked, credential_resolver, audience, opener, clock)
        native = None
        try:
            initial = http._http()
            initial._token = None
            initial = None
            created = False
            try:
                native = native_factory()
                created = True
            except BaseException:
                pass
            _check(created, 'native-unavailable')
            yield http, native
        finally:
            http.close()
            native = opener = checked = None

    return session
