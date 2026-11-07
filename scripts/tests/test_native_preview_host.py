"""Synthetic host composition and signal cleanup; no external actions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_host as host

SCOPE = dict(org_id='11111111-1111-4111-8111-111111111111',
             project_id='22222222-2222-4222-8222-222222222222',
             actor_id='33333333-3333-4333-8333-333333333333', worker_id='synthetic')
AUDIENCE = 'https://api-staging.leafdesign.ai'
SECRET = 'synthetic-secret-payload'
CREDS = {'client_id': 'synthetic-client', 'client_secret': SECRET}
STOPPED = {'state': 'stopped', 'completed': 0}


def forbidden(*args, **kwargs):
    pytest.fail('unexpected effect')


def fail(*args, **kwargs):
    raise RuntimeError(SECRET)


def safe_error(error, code):
    assert type(error) is host.HostError
    assert error.args == (code,)
    assert error.__cause__ is None and error.__context__ is None
    assert SECRET not in str(error) + repr(error)


@pytest.fixture
def signals(monkeypatch):
    old = {host.signal.SIGINT: object(), host.signal.SIGTERM: object()}
    current = old.copy()
    calls = []
    def install(signum, handler):
        calls.append((signum, handler))
        previous = current[signum]
        current[signum] = handler
        return previous
    monkeypatch.setattr(host.signal, 'getsignal', current.__getitem__)
    monkeypatch.setattr(host.signal, 'signal', install)
    return old, current, calls


def test_import_is_inert(monkeypatch):
    monkeypatch.setattr(host.session, 'make_session_factory', forbidden)
    monkeypatch.setattr(host.service, 'serve', forbidden)
    monkeypatch.setattr(host.threading, 'Event', forbidden)
    monkeypatch.setattr(host.signal, 'getsignal', forbidden)
    monkeypatch.setattr(host.signal, 'signal', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    spec = importlib.util.spec_from_file_location('_inert_native_host', host.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.run_host)


def test_main_thread_required_before_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(host.threading, 'current_thread', lambda: object())
    monkeypatch.setattr(host.session, 'make_session_factory', forbidden)
    monkeypatch.setattr(host.service, 'serve', forbidden)
    monkeypatch.setattr(host.threading, 'Event', forbidden)
    monkeypatch.setattr(host.signal, 'getsignal', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    with pytest.raises(host.HostError) as caught:
        host.run_host(SCOPE, forbidden, AUDIENCE, tmp_path / 'absent')
    safe_error(caught.value, 'main-thread-required')
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('changes', [
    {'scope': {}}, {'scope': dict(SCOPE, worker_id=SECRET + '\n')},
    {'credential_resolver': None}, {'audience': None},
    {'audience': AUDIENCE + '/'}, {'output_root': None},
    {'poll_seconds': True}, {'poll_seconds': 0}, {'poll_seconds': float('nan')},
    {'error_seconds': -1}, {'error_seconds': float('inf')}, {'error_seconds': 3601},
])
def test_invalid_input_precedes_setup(monkeypatch, tmp_path, changes):
    monkeypatch.setattr(host.session, 'make_session_factory', forbidden)
    monkeypatch.setattr(host.signal, 'signal', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    args = dict(scope=SCOPE, credential_resolver=forbidden, audience=AUDIENCE,
                output_root=tmp_path)
    args.update(changes)
    with pytest.raises(host.HostError) as caught:
        host.run_host(**args)
    safe_error(caught.value, 'invalid-input')


def test_signals_share_one_event_and_restore_in_reverse(monkeypatch, tmp_path, signals):
    old, current, calls = signals
    event = threading.Event()
    creations = []
    def create():
        creations.append(True)
        return event
    monkeypatch.setattr(host.threading, 'Event', create)
    def serve(scope, factory, root, stop, **settings):
        assert scope == SCOPE and scope is not SCOPE
        assert root == tmp_path and stop is event
        assert settings == {'poll_seconds': 2.0, 'error_seconds': 3.0}
        assert current[host.signal.SIGINT] is current[host.signal.SIGTERM]
        for signum in old:
            assert not stop.is_set()
            current[signum](signum, None)
            assert stop.is_set()
            stop.clear()
        return STOPPED
    monkeypatch.setattr(host.service, 'serve', serve)
    result = host.run_host(SCOPE, forbidden, AUDIENCE, tmp_path,
                           poll_seconds=2, error_seconds=3)
    assert result == STOPPED and result is not STOPPED
    assert creations == [True]
    assert current == old
    assert calls[2:] == list(reversed(list(old.items())))


@pytest.mark.parametrize('stage', ['factory', 'event', 'get-first', 'get-second',
                                   'install-first', 'install-second', 'serve'])
def test_failures_are_sanitized_and_restore_partial_setup(
        monkeypatch, tmp_path, signals, stage, capsys):
    old, current, calls = signals
    install = host.signal.signal
    def get(signum):
        if stage == ('get-first' if signum == host.signal.SIGINT else 'get-second'):
            fail()
        return current[signum]
    def set_handler(signum, handler):
        # Fail only installation, after simulating a partially applied change.
        previous = install(signum, handler)
        if handler is not old[signum] and stage == (
                'install-first' if signum == host.signal.SIGINT else 'install-second'):
            fail()
        return previous
    monkeypatch.setattr(host.signal, 'getsignal', get)
    monkeypatch.setattr(host.signal, 'signal', set_handler)
    if stage == 'factory':
        monkeypatch.setattr(host.session, 'make_session_factory', fail)
    if stage == 'event':
        monkeypatch.setattr(host.threading, 'Event', fail)
    monkeypatch.setattr(host.service, 'serve', fail if stage == 'serve' else forbidden)
    with pytest.raises(host.HostError) as caught:
        host.run_host(SCOPE, forbidden, AUDIENCE, tmp_path)
    safe_error(caught.value, 'serve-failed' if stage == 'serve' else 'setup-failed')
    assert current == old
    expected = (list(reversed(list(old.items()))) if stage in ('install-second', 'serve')
                else [(host.signal.SIGINT, old[host.signal.SIGINT])]
                if stage in ('get-second', 'install-first') else [])
    restores = [(s, h) for s, h in calls if h is old[s]]
    assert restores == expected
    assert SECRET not in str(capsys.readouterr())


@pytest.mark.parametrize('serve_fails', [False, True])
@pytest.mark.parametrize('failed_signals', ['first', 'both'])
def test_every_restore_attempted_even_on_failure(
        monkeypatch, tmp_path, signals, serve_fails, failed_signals):
    old, _, calls = signals
    install = host.signal.signal
    def set_handler(signum, handler):
        previous = install(signum, handler)
        if handler is old[signum] and (failed_signals == 'both' or signum == host.signal.SIGTERM):
            fail()
        return previous
    monkeypatch.setattr(host.signal, 'signal', set_handler)
    monkeypatch.setattr(host.service, 'serve', fail if serve_fails else lambda *a, **k: STOPPED)
    with pytest.raises(host.HostError) as caught:
        host.run_host(SCOPE, forbidden, AUDIENCE, tmp_path)
    safe_error(caught.value, 'signal-restore-failed')
    assert calls[2:] == list(reversed(list(old.items())))


@pytest.mark.parametrize('state', ['stopped', 'busy', 'reconciliation-required'])
def test_closed_states_pass_once_without_restart(monkeypatch, tmp_path, signals, state):
    calls = []
    value = {'state': state, 'completed': 2}
    def serve(*args, **kwargs):
        calls.append(True)
        return value
    monkeypatch.setattr(host.service, 'serve', serve)
    result = host.run_host(SCOPE, forbidden, AUDIENCE, tmp_path)
    assert result == value and result is not value
    assert calls == [True]
    assert signals[1] == signals[0]


@pytest.mark.parametrize('value', [
    None, [], {}, {'state': 'stopped'}, dict(STOPPED, extra=SECRET),
    dict(STOPPED, state=SECRET), dict(STOPPED, state=None), dict(STOPPED, state=[]),
    dict(STOPPED, completed=True), dict(STOPPED, completed=-1),
    dict(STOPPED, completed=0.0), dict(STOPPED, completed=SECRET),
])
def test_malformed_results_rejected(monkeypatch, tmp_path, signals, value, capsys):
    monkeypatch.setattr(host.service, 'serve', lambda *a, **k: value)
    with pytest.raises(host.HostError) as caught:
        host.run_host(SCOPE, forbidden, AUDIENCE, tmp_path)
    safe_error(caught.value, 'invalid-result')
    assert signals[1] == signals[0]
    assert SECRET not in str(capsys.readouterr())


def test_error_constructor_has_closed_shape():
    safe_error(host.HostError(SECRET), 'setup-failed')


def test_real_session_service_worker_empty_queue(monkeypatch, tmp_path, signals):
    old, handlers, _ = signals
    calls, closed, resolutions, native_creations = [], [], [], []
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
            assert timeout == 10
            calls.append(request)
            if request.full_url == host.session.TOKEN_URL:
                assert json.loads(request.data) == dict(
                    CREDS, audience=AUDIENCE, grant_type='client_credentials')
                return Response(request.full_url, json.dumps(dict(
                    access_token='fixture', token_type='Bearer', expires_in=60)).encode())
            assert request.method == 'POST' and request.full_url.endswith('/worker/claim')
            assert request.get_header('Authorization') == 'Bearer fixture'
            assert json.loads(request.data) == {}
            handlers[host.signal.SIGTERM](host.signal.SIGTERM, None)
            return Response(request.full_url, b'{"job":null}')
    def resolve():
        resolutions.append(True)
        return CREDS.copy()
    def native():
        native_creations.append(True)
        return forbidden
    close = host.session._Session.close
    def record_close(session):
        close(session)
        closed.append(session)
    monkeypatch.setattr(host.session.urllib.request, 'build_opener', lambda *a: Opener())
    monkeypatch.setattr(host.session, 'MacAdapter', native)
    monkeypatch.setattr(host.session._Session, 'close', record_close)
    assert host.run_host(SCOPE, resolve, AUDIENCE, tmp_path) == STOPPED
    assert len(calls) == 2 and resolutions == native_creations == [True]
    assert len(closed) == 1
    assert all(getattr(closed[0], name) is None for name in
               ('_token', '_scope', '_resolver', '_audience', '_opener', '_clock'))
    directory = tmp_path / hashlib.sha256(host.service.worker.encode(SCOPE)).hexdigest()
    assert json.loads((directory / 'consumer.json').read_bytes()) == {
        'scope_sha256': directory.name, 'phase': 'clean'}
    assert not list(directory.rglob('*.png'))
    assert all(SECRET.encode() not in path.read_bytes()
               for path in directory.rglob('*') if path.is_file())
    assert handlers == old
