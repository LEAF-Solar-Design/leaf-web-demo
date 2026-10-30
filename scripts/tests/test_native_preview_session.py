"""Synthetic OAuth and lifecycle boundaries; no credentials, network or Mac."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_session as session
import native_preview_service as service
import native_preview_worker as worker

SCOPE = dict(org_id='11111111-1111-4111-8111-111111111111',
             project_id='22222222-2222-4222-8222-222222222222',
             actor_id='33333333-3333-4333-8333-333333333333', worker_id='synthetic')
AUDIENCE = 'https://api-staging.leafdesign.ai'
SECRET = 'synthetic-secret-payload'
CREDS = {'client_id': 'synthetic-client', 'client_secret': SECRET}


def payload(**changes):
    return json.dumps({'access_token': 'synthetic-token', 'token_type': 'Bearer',
                       'expires_in': 60, **changes}).encode()


class Response:
    def __init__(self, raw=None, status=200, url=session.TOKEN_URL):
        self.raw = payload() if raw is None else raw
        self.status, self.url = status, url
        self.reads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, limit):
        self.reads.append(limit)
        return self.raw[:limit]


class Opener:
    def __init__(self, tokens=None):
        self.tokens = list(tokens if tokens is not None else [Response()])
        self.auth, self.platform = [], []
        self.platform_error = None
        self.platform_hook = None

    def open(self, request, timeout):
        assert timeout == 10
        if request.full_url == session.TOKEN_URL:
            self.auth.append(request)
            response = self.tokens.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        self.platform.append(request)
        if self.platform_hook:
            self.platform_hook(request)
        if self.platform_error:
            raise self.platform_error
        return Response(b'{"job":null}', url=request.full_url)


def factory(opener, *, resolver=lambda: CREDS.copy(), native=lambda: object(), clock=lambda: 0):
    return session.make_session_factory(resolver, AUDIENCE, opener_factory=lambda: opener,
                                        native_factory=native, clock=clock)


def safe_error(error, code):
    assert type(error) is session.SessionError
    assert error.args == (code,)
    assert SECRET not in str(error) + repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize('audience', sorted(session.AUDIENCES))
def test_exact_token_contract_and_fixed_platform(audience):
    response = Response()
    opener = Opener([response])
    make = session.make_session_factory(lambda: CREDS.copy(), audience,
        opener_factory=lambda: opener, native_factory=lambda: object(), clock=lambda: 0)
    assert not opener.auth
    with make(SCOPE) as (http, native):
        assert http.request('POST', 'claim', {}) == b'{"job":null}'
    request = opener.auth[0]
    assert request.full_url == 'https://leafautomation.us.auth0.com/oauth/token'
    assert request.method == 'POST'
    assert json.loads(request.data) == dict(CREDS, audience=audience, grant_type='client_credentials')
    assert request.get_header('User-agent') == worker.USER_AGENT
    assert request.get_header('Content-type') == 'application/json'
    assert request.get_header('Authorization') is None
    assert response.reads == [32769]
    assert opener.platform[0].full_url == (worker.ORIGIN + '/api/projects/' +
        SCOPE['project_id'] + '/native-previews/worker/claim')
    assert opener.platform[0].get_header('Authorization') == 'Bearer synthetic-token'
    assert SECRET not in repr(http) + repr(native)


@pytest.mark.parametrize('audience', [None, '', 'https://api.leafdesign.ai/',
                                   'https://evil.invalid', 1])
def test_audience_has_no_fallback(audience):
    with pytest.raises(session.SessionError) as caught:
        session.make_session_factory(lambda: CREDS, audience)
    safe_error(caught.value, 'invalid-audience')


@pytest.mark.parametrize('credentials', [None, {}, dict(CREDS, extra='x'),
    {'client_id': 'x'}, dict(CREDS, client_id=''), dict(CREDS, client_secret=7),
    dict(CREDS, client_secret='x' * 4097), dict(CREDS, client_id='x\n'),
    dict(CREDS, client_secret='x\x7f'), dict(CREDS, client_secret='x\x85')])
def test_malformed_credentials(credentials):
    opener = Opener()
    with pytest.raises(session.SessionError) as caught:
        with factory(opener, resolver=lambda: credentials)(SCOPE):
            pytest.fail('entered')
    safe_error(caught.value, 'invalid-credentials')
    assert not opener.auth and not opener.platform


BAD_TOKENS = [
    b'{}', b'[]', b'null', b'not-json', b'\xff',
    b'{"access_token":"a","access_token":"b","token_type":"Bearer","expires_in":60}',
    b'{"access_token":"a","token_type":"Bearer","expires_in":NaN}',
] + [json.dumps(dict(access_token=token, token_type='Bearer', expires_in=60)).encode()
     for token in ['', 'x' * 4097, 'x y', 'x\n', 'x\x00', '\u00e9', None]] + [
    json.dumps(dict(access_token='a', token_type='Bearer', expires_in=expiry)).encode()
    for expiry in [True, '60', 59, 86401, float('inf')]
] + [b'{"access_token":"a","token_type":"bearer","expires_in":60}', b'x' * 32769]


@pytest.mark.parametrize('raw', BAD_TOKENS)
def test_malformed_token_response(raw):
    opener = Opener([Response(raw)])
    with pytest.raises(session.SessionError) as caught:
        with factory(opener)(SCOPE):
            pytest.fail('entered')
    safe_error(caught.value, 'invalid-token-response')
    assert not opener.platform


@pytest.mark.parametrize('response', [Response(status=302), Response(status=401),
                                     Response(url='https://evil.invalid/token')])
def test_status_and_final_url_refused(response):
    with pytest.raises(session.SessionError) as caught:
        with factory(Opener([response]))(SCOPE):
            pytest.fail('entered')
    safe_error(caught.value, 'invalid-token-response')


def test_secret_response_body_is_sanitized(capsys):
    opener = Opener([Response(json.dumps({'error_description': SECRET}).encode())])
    with pytest.raises(session.SessionError) as caught:
        with factory(opener)(SCOPE):
            pytest.fail('entered')
    safe_error(caught.value, 'invalid-token-response')
    assert SECRET not in str(capsys.readouterr())


@pytest.mark.parametrize('expiry', [60, 60.5, 86400])
def test_valid_expiry_boundaries(expiry):
    with factory(Opener([Response(payload(expires_in=expiry))]))(SCOPE):
        pass


def test_invalid_scope_is_sanitized_before_resolving():
    opener = Opener()
    with pytest.raises(session.SessionError) as caught:
        with factory(opener)({'project_id': SECRET}):
            pytest.fail('entered')
    safe_error(caught.value, 'invalid-scope')
    assert not opener.auth


def test_default_opener_disables_redirects_and_keeps_tls(monkeypatch):
    seen = []
    opener = Opener()
    def build(*handlers):
        seen.extend(handlers)
        return opener
    monkeypatch.setattr(session.urllib.request, 'build_opener', build)
    make = session.make_session_factory(lambda: CREDS, AUDIENCE, native_factory=object)
    with make(SCOPE):
        pass
    assert len(seen) == 1 and isinstance(seen[0], worker.NoRedirect)
    assert seen[0].redirect_request(None, None, 302, '', {}, 'https://evil.invalid') is None


@pytest.mark.parametrize('stage', ['resolver', 'token', 'native', 'opener'])
def test_secret_exceptions_have_no_retained_context(stage, capsys):
    def fail():
        raise RuntimeError(SECRET)
    opener = Opener([RuntimeError(SECRET)]) if stage == 'token' else Opener()
    make = session.make_session_factory(fail if stage == 'resolver' else lambda: CREDS,
        AUDIENCE, opener_factory=fail if stage == 'opener' else lambda: opener,
        native_factory=fail if stage == 'native' else object)
    with pytest.raises(session.SessionError) as caught:
        with make(SCOPE):
            pytest.fail('entered')
    safe_error(caught.value, {'resolver': 'invalid-credentials', 'token': 'token-unavailable',
                            'native': 'native-unavailable', 'opener': 'token-unavailable'}[stage])
    assert SECRET not in str(capsys.readouterr())


def test_initial_token_precedes_native_and_construction_is_dormant():
    events = []
    opener = Opener()
    def resolve():
        events.append('token')
        return CREDS
    def native():
        assert len(opener.auth) == 1
        events.append('native')
        return object()
    make = factory(opener, resolver=resolve, native=native)
    context = make(SCOPE)
    assert events == []
    with context:
        assert events == ['token', 'native']
    events.clear()
    with pytest.raises(session.SessionError):
        with factory(Opener([Response(b'{}')]), native=native)(SCOPE):
            pass
    assert events == []


def test_refresh_at_skew_and_fresh_credentials():
    now, resolutions = [0], []
    opener = Opener([Response(), Response(b'{"access_token":"new","token_type":"Bearer","expires_in":60}')])
    def resolve():
        resolutions.append(True)
        return CREDS.copy()
    with factory(opener, resolver=resolve, clock=lambda: now[0])(SCOPE) as (http, _):
        now[0] = 29
        http.request('GET', 'one')
        assert len(opener.auth) == 1
        now[0] = 30
        http.request('POST', 'claim', {})
        http.request('POST', 'complete', {})
    assert len(resolutions) == 2
    assert [r.get_header('Authorization') for r in opener.platform] == [
        'Bearer synthetic-token', 'Bearer new', 'Bearer new']


def test_concurrent_refresh_only_once():
    now = [0]
    opener = Opener([Response(), Response()])
    start = threading.Barrier(8)
    with factory(opener, clock=lambda: now[0])(SCOPE) as (http, _):
        now[0] = 30
        def call(_):
            start.wait(timeout=5)
            return http.request('POST', 'heartbeat', {})
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(call, range(8))) == [b'{"job":null}'] * 8
    assert len(opener.auth) == 2 and len(opener.platform) == 8


def test_platform_call_does_not_hold_refresh_lock():
    entered, release = threading.Event(), threading.Event()
    opener = Opener()
    def hook(request):
        if request.full_url.endswith('/complete'):
            entered.set()
            assert release.wait(5)
    opener.platform_hook = hook
    with factory(opener)(SCOPE) as (http, _):
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(http.request, 'POST', 'complete', {})
            try:
                assert entered.wait(5)
                beat = pool.submit(http.request, 'POST', 'heartbeat', {})
                assert beat.result(timeout=5) == b'{"job":null}'
            finally:
                release.set()
            slow.result(timeout=5)


@pytest.mark.parametrize('suffix', ['claim', 'job/complete'])
def test_failed_post_never_replayed(suffix):
    opener = Opener()
    opener.platform_error = RuntimeError(SECRET)
    with factory(opener)(SCOPE) as (http, _):
        with pytest.raises(session.SessionError) as caught:
            http.request('POST', suffix, {})
        safe_error(caught.value, 'platform-unavailable')
    assert len(opener.platform) == 1 and len(opener.auth) == 1


def test_failed_refresh_prevents_platform_call():
    now = [0]
    opener = Opener([Response(), RuntimeError(SECRET)])
    with factory(opener, clock=lambda: now[0])(SCOPE) as (http, _):
        now[0] = 30
        with pytest.raises(session.SessionError) as caught:
            http.request('POST', 'claim', {})
        safe_error(caught.value, 'token-unavailable')
    assert not opener.platform


def test_closed_session_refuses_and_releases_references():
    opener = Opener()
    with factory(opener)(SCOPE) as (http, _):
        pass
    http.close()
    with pytest.raises(session.SessionError) as caught:
        http.request('POST', 'claim', {})
    safe_error(caught.value, 'session-closed')
    assert all(getattr(http, name) is None for name in
               ('_token', '_resolver', '_opener', '_scope', '_clock', '_audience'))
    assert not opener.platform


def test_independent_sessions_renew_independently():
    now, resolutions = [0], []
    opener = Opener([Response() for _ in range(4)])
    def resolve():
        resolutions.append(True)
        return CREDS
    make = factory(opener, resolver=resolve, clock=lambda: now[0])
    with make(SCOPE) as (first, _), make(SCOPE) as (second, _):
        assert first is not second
        now[0] = 30
        first.request('POST', 'claim', {})
        second.request('POST', 'claim', {})
    assert len(resolutions) == 4


def test_real_serve_and_worker_empty_queue(tmp_path):
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            self.stopped = True
    def no_native(*args):
        pytest.fail('native execution')
    opener = Opener()
    result = service.serve(SCOPE, factory(opener, native=lambda: no_native), tmp_path, Stop())
    assert result == {'state': 'stopped', 'completed': 0}
    assert len(opener.auth) == len(opener.platform) == 1
    assert opener.platform[0].method == 'POST'
    directory = tmp_path / hashlib.sha256(worker.encode(SCOPE)).hexdigest()
    assert json.loads((directory / 'consumer.json').read_bytes())['phase'] == 'clean'
    assert not list(directory.rglob('*.png'))
    assert all(SECRET.encode() not in path.read_bytes() for path in directory.rglob('*') if path.is_file())
