"""Hermetic lifecycle fixtures; no installed identity or native execution."""
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_service as service

SCOPE = dict(org_id='11111111-1111-4111-8111-111111111111',
             project_id='22222222-2222-4222-8222-222222222222',
             actor_id='33333333-3333-4333-8333-333333333333', worker_id='fixture')
EMPTY = {'stage': 'empty', 'result': 'empty'}
COMPLETE = dict(stage='settled', result='complete',
                job_id='44444444-4444-4444-8444-444444444444',
                archive_sha256='a' * 64, screenshot_sha256='b' * 64)
SECRET = 'synthetic-secret-never-persist'


class Stop:
    def __init__(self, after=1):
        self.stopped = False
        self.waits = []
        self.after = after

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) >= self.after:
            self.set()
        return self.stopped


def directory(root, scope=SCOPE):
    return root / hashlib.sha256(service.worker.encode(scope)).hexdigest()


def marker(root, scope=SCOPE):
    return directory(root, scope) / 'consumer.json'


def phase(root, scope=SCOPE):
    return json.loads(marker(root, scope).read_text())['phase']


def forbidden(*args, **kwargs):
    pytest.fail('unexpected session or claim')


@contextmanager
def session(scope):
    assert scope == SCOPE and scope is not SCOPE
    yield object(), forbidden


def stub(monkeypatch, replies, paths=None):
    replies = iter(replies)
    def run(scope, http, native, output):
        assert phase(output.parent.parent) == 'reconciliation-required'
        assert output.is_dir()
        if paths is not None:
            paths.append(output)
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply
    monkeypatch.setattr(service.worker, 'run_once', run)


def test_real_empty_worker_and_clean_restart(tmp_path):
    calls = []
    class Http:
        def request(self, method, suffix, body):
            calls.append((method, suffix, body))
            return b'{"job":null}'
    @contextmanager
    def factory(scope):
        yield Http(), forbidden
    for _ in range(2):
        stop = Stop()
        assert service.serve(SCOPE, factory, tmp_path, stop) == dict(state='stopped', completed=0)
        assert stop.waits == [5.0]
        assert phase(tmp_path) == 'clean'
    assert calls == [('POST', 'claim', {}), ('POST', 'claim', {})]


def test_complete_then_empty_unique_dirs_fresh_sessions(tmp_path, monkeypatch):
    paths, sessions, exits = [], [], []
    stub(monkeypatch, [COMPLETE, EMPTY], paths)
    @contextmanager
    def factory(scope):
        http = object()
        sessions.append(http)
        yield http, forbidden
        # Success is still unresolved until __exit__ finishes.
        assert phase(tmp_path) == 'reconciliation-required'
        exits.append(http)
    assert service.serve(SCOPE, factory, tmp_path, Stop()) == dict(state='stopped', completed=1)
    assert len(paths) == 2 and paths[0] != paths[1]
    assert len(sessions) == 2 and sessions[0] is not sessions[1] and exits == sessions
    assert phase(tmp_path) == 'clean'
    stub(monkeypatch, [EMPTY])
    assert service.serve(SCOPE, session, tmp_path, Stop())['state'] == 'stopped'


def test_stop_during_acquisition_closes_without_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(service.worker, 'run_once', forbidden)
    stop, exits = Stop(), []
    @contextmanager
    def factory(scope):
        stop.set()
        try:
            yield object(), forbidden
        finally:
            exits.append(True)
    assert service.serve(SCOPE, factory, tmp_path, stop) == dict(state='stopped', completed=0)
    assert exits == [True] and not marker(tmp_path).exists()


def test_auth_acquisition_retries_with_bounded_interruptible_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(service.worker, 'run_once', forbidden)
    calls = []
    @contextmanager
    def factory(scope):
        calls.append(True)
        raise RuntimeError(SECRET)
        yield
    stop = Stop(after=2)
    result = service.serve(SCOPE, factory, tmp_path, stop, error_seconds=7)
    assert result == dict(state='stopped', completed=0)
    assert calls == [True, True] and stop.waits == [7.0, 7.0]
    assert not marker(tmp_path).exists()
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize('reply', [
    {'stage': 'completion', 'result': 'unknown'},
    {'stage': 'execution', 'result': 'failed'},
    None, [], {}, {'stage': 'settled', 'result': 'complete'},
    {**COMPLETE, 'stage': 'completion'}, {**COMPLETE, 'job_id': 'bad'},
    {**COMPLETE, 'archive_sha256': 'BAD'}, {**COMPLETE, 'screenshot_sha256': 1},
    {**COMPLETE, 'extra': SECRET}, {**EMPTY, 'extra': SECRET},
])
def test_uncertain_failed_or_malformed_result_never_clears(tmp_path, monkeypatch, reply):
    stub(monkeypatch, [reply])
    assert service.serve(SCOPE, session, tmp_path, Stop()) == dict(
        state='reconciliation-required', completed=0)
    assert phase(tmp_path) == 'reconciliation-required'
    assert service.serve(SCOPE, forbidden, tmp_path, Stop())['state'] == 'reconciliation-required'


@pytest.mark.parametrize('error', [RuntimeError(SECRET), KeyboardInterrupt(SECRET)])
def test_thrown_claim_releases_lock_and_redacts(tmp_path, monkeypatch, error):
    stub(monkeypatch, [error])
    result = service.serve(SCOPE, session, tmp_path, Stop())
    assert result == dict(state='reconciliation-required', completed=0)
    # Acquiring the actual lock again proves release despite the exception.
    with (directory(tmp_path) / 'consumer.lock').open('a+b') as lock:
        assert service._lock(lock)
    assert service.serve(SCOPE, forbidden, tmp_path, Stop()) == result
    assert SECRET not in json.dumps(result)
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()


def test_context_cleanup_failure_preserves_marker(tmp_path, monkeypatch):
    stub(monkeypatch, [COMPLETE])
    @contextmanager
    def factory(scope):
        yield object(), forbidden
        raise RuntimeError(SECRET)
    assert service.serve(SCOPE, factory, tmp_path, Stop()) == dict(
        state='reconciliation-required', completed=0)
    assert phase(tmp_path) == 'reconciliation-required'


def test_suppressed_worker_exception_is_still_uncertain(tmp_path, monkeypatch):
    stub(monkeypatch, [RuntimeError(SECRET)])
    @contextmanager
    def factory(scope):
        try:
            yield object(), forbidden
        except RuntimeError:
            pass
    assert service.serve(SCOPE, factory, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert phase(tmp_path) == 'reconciliation-required'


@pytest.mark.parametrize('contents', [b'{broken', b'{}', b'{"phase":"reconciliation-required"}'])
def test_preexisting_checkpoint_refuses_before_factory(tmp_path, contents):
    directory(tmp_path).mkdir()
    marker(tmp_path).write_bytes(contents)
    assert service.serve(SCOPE, forbidden, tmp_path, Stop()) == dict(
        state='reconciliation-required', completed=0)
    assert marker(tmp_path).read_bytes() == contents


def test_stop_before_start_has_no_filesystem_effect(tmp_path):
    stop = Stop()
    stop.set()
    assert service.serve(SCOPE, forbidden, tmp_path / 'absent', stop)['state'] == 'stopped'
    assert not (tmp_path / 'absent').exists()


def test_stop_during_active_cycle_finishes_cleanup(tmp_path, monkeypatch):
    stop, closed = Stop(), []
    def run(*args):
        stop.set()
        return COMPLETE
    monkeypatch.setattr(service.worker, 'run_once', run)
    @contextmanager
    def factory(scope):
        yield object(), forbidden
        closed.append(True)
    assert service.serve(SCOPE, factory, tmp_path, stop) == dict(state='stopped', completed=1)
    assert closed == [True] and phase(tmp_path) == 'clean' and not stop.waits


def test_stop_after_checkpoint_before_invocation(tmp_path, monkeypatch):
    stop = Stop()
    original = service._checkpoint
    def checkpoint(path, digest, state):
        original(path, digest, state)
        if state == 'reconciliation-required':
            stop.set()
    monkeypatch.setattr(service, '_checkpoint', checkpoint)
    monkeypatch.setattr(service.worker, 'run_once', forbidden)
    assert service.serve(SCOPE, session, tmp_path, stop) == dict(state='stopped', completed=0)
    assert phase(tmp_path) == 'clean'


def _hold_service(root, ready, release):
    @contextmanager
    def factory(scope):
        ready.set()
        if not release.wait(15):
            raise RuntimeError('fixture-timeout')
        stop.set()
        yield object(), forbidden
    stop = threading.Event()
    service.serve(SCOPE, factory, root, stop)


def test_process_singleton_and_independent_scope(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_service, args=(tmp_path, ready, release))
    child.start()
    try:
        assert ready.wait(10)
        assert service.serve(SCOPE, forbidden, tmp_path, Stop()) == dict(state='busy', completed=0)
        other = {**SCOPE, 'worker_id': 'independent'}
        stop = Stop()
        @contextmanager
        def factory(scope):
            assert scope == other
            stop.set()
            yield object(), forbidden
        assert service.serve(other, factory, tmp_path, stop)['state'] == 'stopped'
        assert directory(tmp_path, other) != directory(tmp_path)
    finally:
        release.set()
        child.join(15)
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert child.exitcode == 0
    # Clean predecessor releases the lock and permits a fresh session.
    stop = Stop()
    @contextmanager
    def factory(scope):
        stop.set()
        yield object(), forbidden
    assert service.serve(SCOPE, factory, tmp_path, stop)['state'] == 'stopped'


def _interrupt_claim(root, ready, release):
    class Http:
        def request(self, method, suffix, body):
            ready.set()
            release.wait(20)
            return b'{"job":null}'
    @contextmanager
    def factory(scope):
        yield Http(), forbidden
    service.serve(SCOPE, factory, root, threading.Event())


def test_process_interruption_requires_reconciliation_on_restart(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_interrupt_claim, args=(tmp_path, ready, release))
    child.start()
    try:
        assert ready.wait(10)
        assert phase(tmp_path) == 'reconciliation-required'
    finally:
        if child.is_alive():
            child.terminate()
        child.join(10)
    assert not child.is_alive()
    with (directory(tmp_path) / 'consumer.lock').open('a+b') as lock:
        assert service._lock(lock)
    assert service.serve(SCOPE, forbidden, tmp_path, Stop()) == dict(
        state='reconciliation-required', completed=0)


@pytest.mark.parametrize('value', [0, -1, True, '5', float('inf'), float('nan'), 3601])
@pytest.mark.parametrize('setting', ['poll_seconds', 'error_seconds'])
def test_invalid_wait_has_no_effects(tmp_path, value, setting):
    with pytest.raises(ValueError, match='invalid-wait'):
        service.serve(SCOPE, forbidden, tmp_path / 'absent', Stop(), **{setting: value})
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('scope', [None, {}, {**SCOPE, 'token': SECRET},
                                  {**SCOPE, 'worker_id': '../escape'},
                                  {**SCOPE, 'org_id': 'bad'}])
def test_invalid_binding_has_no_effects(tmp_path, scope):
    with pytest.raises(service.worker.WorkerError):
        service.serve(scope, forbidden, tmp_path / 'absent', Stop())
    assert not (tmp_path / 'absent').exists()


def test_scope_copy_and_bounded_valid_waits(tmp_path, monkeypatch):
    original = SCOPE.copy()
    stub(monkeypatch, [EMPTY])
    @contextmanager
    def factory(scope):
        scope['worker_id'] = 'changed-by-provider'
        yield object(), forbidden
    stop = Stop()
    assert service.serve(SCOPE, factory, tmp_path, stop,
                         poll_seconds=3600, error_seconds=0.01)['state'] == 'stopped'
    assert stop.waits == [3600.0] and SCOPE == original
