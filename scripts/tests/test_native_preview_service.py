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


@pytest.fixture(autouse=True)
def legacy_runtime_object(monkeypatch):
    """The managed test checkout is shallow; supply the legacy runtime witness.

    Keep current-runtime Git resolution real, and let all legacy refusal tests
    reach the predicate they intend to exercise rather than fail on Git supply.
    """
    resolve = service.recovery.runtime_commit

    def runtime_commit(revision):
        if revision == service.recovery.LEGACY_RUNTIME:
            return revision
        return resolve(revision)

    monkeypatch.setattr(service.recovery, 'runtime_commit', runtime_commit)


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
    def run(scope, http, native, output, *, journal=None):
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
    def run(*args, **kwargs):
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
        if state == 'reconciliation-required' or (type(state) is dict and
                                                 state['phase'] == 'reconciliation-required'):
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


def recovery_fixture(root, *, legacy=True):
    r = service.recovery
    base = directory(root)
    base.mkdir()
    cycle = base / 'cycle-explicit'
    cycle.mkdir()
    digest = base.name
    claim = dict(job_id=COMPLETE['job_id'], attempt=7,
                 source=dict(revision=3, document_sha256='d' * 64, archive_sha256='a' * 64))
    if legacy:
        checkpoint = dict(scope_sha256=digest, phase='reconciliation-required')
        candidate = dict(schema=r.CANDIDATE_SCHEMA, scope_sha256=digest, cycle=cycle.name,
                         **claim, original_runtime_commit=r.LEGACY_RUNTIME)
        (base / 'reconciliation-candidate.json').write_bytes(service.worker.encode(candidate))
    else:
        journal = r.Journal(cycle, digest, base)
        journal(dict(event='claim', **claim, deadline=1000))
        journal(dict(event='allocator', ticket='full-fixture-ticket', marker_sha256='e' * 64))
        journal(dict(event='cleanup', cleanup_complete=True))
        journal(dict(event='release', allocator_release_confirmed=True))
        checkpoint = dict(schema=r.CHECKPOINT_SCHEMA, scope_sha256=digest,
                          phase='reconciliation-required', cycle=cycle.name)
    marker(root).write_bytes(service.worker.encode(checkpoint))
    failure = dict(stage='execution', result='failed', job_id=claim['job_id'],
                   archive_sha256=claim['source']['archive_sha256'], execution_stage='mac-permit-refresh',
                   error_code='authority-lost', authority_failure='permit-refresh',
                   failure_settlement='not-attempted-authority-lost',
                   cleanup_complete=True, allocator_release_confirmed=True)
    failure_path = cycle / (claim['job_id'] + '-7-failure.json')
    failure_path.write_bytes(service.worker.encode(failure))
    proof = dict(schema='leaf.native-preview.failed-disposition.v1', **claim,
                 org_id=SCOPE['org_id'], project_id=SCOPE['project_id'], status='failed',
                 reason='execution_budget_expired', started_at=100.0, finished_at=200.0,
                 lease_cleared=True)
    proof['disposition_sha256'] = r.sha(service.worker.encode(proof))
    status = dict(job_id=claim['job_id'], status='failed', **claim['source'], failure_disposition=proof)
    witness = dict(job_id=claim['job_id'], attempt=7,
                   prefix='preview-' + claim['job_id'] + '-7-', ticket='full-fixture-ticket',
                   event_times=dict(grant=10, release=20, confirm_dead=30, free=40),
                   active_match_counts=[0, 0], history_complete=True)
    witness['witness_sha256'] = r.sha(service.worker.encode(witness))
    return claim, status, witness, failure_path


def recovery_session(root, status, witness, events, *, close_error=False, hook=None):
    class Http:
        def status(self, job_id):
            events.append('status')
            assert job_id == COMPLETE['job_id'] and phase(root) == 'reconciliation-required'
            if hook:
                hook()
            if isinstance(status, Exception):
                raise status
            return service.worker.encode(status)
        request = forbidden
    class Native:
        def read_reconciliation_allocator(self, job_id, attempt):
            events.append('allocator')
            assert job_id == COMPLETE['job_id'] and attempt == 7
            if isinstance(witness, Exception):
                raise witness
            return witness
        __call__ = forbidden
    @contextmanager
    def factory(scope):
        events.append('enter')
        yield Http(), Native()
        assert phase(root) == 'reconciliation-required'
        assert not service.recovery.receipt_path(directory(root), COMPLETE['job_id']).exists()
        events.append('close')
        if close_error:
            raise RuntimeError(SECRET)
    return factory


def test_exact_legacy_receipt_before_clean_and_next_claim(tmp_path, monkeypatch):
    claim, status, witness, failure = recovery_fixture(tmp_path)
    old_failure = failure.read_bytes()
    old_checkpoint = marker(tmp_path).read_bytes()
    events = []
    recovery_factory = recovery_session(tmp_path, status, witness, events)
    class NextHttp:
        def request(self, method, suffix, body):
            assert (method, suffix, body) == ('POST', 'claim', {})
            assert events == ['enter', 'status', 'allocator', 'close']
            receipt = service.recovery.receipt_path(directory(tmp_path), claim['job_id'])
            assert receipt.exists()
            assert json.loads(receipt.read_bytes())['original_checkpoint'] == old_checkpoint.decode()
            return b'{"job":null}'
    @contextmanager
    def factory(scope):
        if not events:
            with recovery_factory(scope) as pair:
                yield pair
        else:
            assert phase(tmp_path) == 'clean'
            yield NextHttp(), forbidden
    assert service.serve(SCOPE, factory, tmp_path, Stop()) == dict(state='stopped', completed=0)
    assert phase(tmp_path) == 'clean' and failure.read_bytes() == old_failure


@pytest.mark.parametrize('field,value', [
    ('attempt', True), ('attempt', 8), ('job_id', SCOPE['actor_id']),
    ('org_id', SCOPE['project_id']), ('project_id', SCOPE['org_id']),
    ('status', 'complete'), ('reason', 'other'), ('lease_cleared', 1),
    ('started_at', True), ('finished_at', 0), ('disposition_sha256', '0' * 64),
    ('source', dict(revision=4, document_sha256='d' * 64, archive_sha256='a' * 64)),
])
def test_recovery_foreign_or_tampered_disposition_refuses(tmp_path, field, value):
    _, status, witness, _ = recovery_fixture(tmp_path)
    status['failure_disposition'][field] = value
    old = marker(tmp_path).read_bytes()
    events = []
    factory = recovery_session(tmp_path, status, witness, events)
    assert service.serve(SCOPE, factory, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert 'allocator' not in events and marker(tmp_path).read_bytes() == old


@pytest.mark.parametrize('mutation', ['producer', 'unknown', 'missing-release', 'conflict', 'extra-result'])
def test_local_journal_ambiguity_refuses_without_session(tmp_path, mutation):
    _, _, _, failure = recovery_fixture(tmp_path, legacy=False)
    journal_path = failure.parent / 'journal.json'
    journal = json.loads(journal_path.read_bytes())
    if mutation == 'producer':
        journal['events'].insert(2, dict(event='producer', producer_may_have_started=True))
    elif mutation == 'unknown':
        journal['events'] = []
        journal['claim_state'] = 'unknown'
    elif mutation == 'missing-release':
        journal['events'].pop()
    elif mutation == 'conflict':
        journal['events'].append(journal['events'][0])
    else:
        (failure.parent / 'result.png').write_bytes(b'not-admissible')
    journal_path.write_bytes(service.worker.encode(journal))
    assert service.serve(SCOPE, forbidden, tmp_path, Stop())['state'] == 'reconciliation-required'


@pytest.mark.parametrize('first', ['unavailable-disposition', 'read-outage'])
def test_recoverable_hold_retries_without_claim_or_restart(tmp_path, first):
    _, status, witness, _ = recovery_fixture(tmp_path, legacy=False)
    old = marker(tmp_path).read_bytes()
    calls = []
    class Http:
        def status(self, job_id):
            calls.append('status')
            assert marker(tmp_path).read_bytes() == old
            if calls.count('status') == 1:
                if first == 'read-outage':
                    raise OSError(SECRET)
                return service.worker.encode({k: v for k, v in status.items()
                                              if k != 'failure_disposition'})
            return service.worker.encode(status)
        def request(self, method, suffix, body):
            calls.append('claim')
            assert phase(tmp_path) == 'reconciliation-required'
            return b'{"job":null}'
    class Native:
        def read_reconciliation_allocator(self, *args):
            return witness
    @contextmanager
    def factory(scope):
        yield Http(), Native()
    stop = Stop(after=2)
    assert service.serve(SCOPE, factory, tmp_path, stop, error_seconds=9)['state'] == 'stopped'
    assert calls == ['status', 'status', 'claim'] and stop.waits == [9.0, 5.0]
    assert phase(tmp_path) == 'clean'


def test_stop_during_recoverable_hold_keeps_checkpoint(tmp_path):
    _, _, witness, _ = recovery_fixture(tmp_path, legacy=False)
    old = marker(tmp_path).read_bytes()
    factory = recovery_session(tmp_path, OSError(SECRET), witness, [])
    stop = Stop()
    assert service.serve(SCOPE, factory, tmp_path, stop)['state'] == 'stopped'
    assert stop.waits == [30.0] and marker(tmp_path).read_bytes() == old


@pytest.mark.parametrize('which', ['checkpoint', 'candidate', 'failure', 'cycle', 'close'])
def test_evidence_drift_and_session_close_prevent_commit(tmp_path, which):
    _, status, witness, failure = recovery_fixture(tmp_path)
    original = marker(tmp_path).read_bytes()
    def drift():
        paths = dict(checkpoint=marker(tmp_path), candidate=directory(tmp_path) / 'reconciliation-candidate.json',
                     failure=failure)
        if which in paths:
            paths[which].write_bytes(paths[which].read_bytes() + b' ')
        elif which == 'cycle':
            (failure.parent / 'unexpected.json').write_bytes(b'{}')
    factory = recovery_session(tmp_path, status, witness, [], close_error=which == 'close', hook=drift)
    assert service.serve(SCOPE, factory, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert not service.recovery.receipt_path(directory(tmp_path), COMPLETE['job_id']).exists()
    if which != 'checkpoint':
        assert marker(tmp_path).read_bytes() == original


def test_receipt_crash_idempotence_and_conflict(tmp_path):
    claim, status, witness, _ = recovery_fixture(tmp_path)
    r = service.recovery
    proof = r.LocalProof(directory(tmp_path), marker(tmp_path), directory(tmp_path).name)
    backend = r.disposition(service.worker.encode(status), SCOPE, claim)
    def crash(*args):
        raise OSError('synthetic-crash')
    with pytest.raises(OSError):
        proof.commit(SCOPE, backend, witness, crash)
    receipt = r.receipt_path(directory(tmp_path), claim['job_id'])
    original = receipt.read_bytes()
    with pytest.raises(OSError):
        proof.commit(SCOPE, backend, witness, crash)
    assert receipt.read_bytes() == original and phase(tmp_path) == 'reconciliation-required'
    receipt.write_bytes(b'{}')
    with pytest.raises(service.worker.WorkerError):
        proof.commit(SCOPE, backend, witness, service._checkpoint)
    assert phase(tmp_path) == 'reconciliation-required'
    receipt.write_bytes(original)
    proof.commit(SCOPE, backend, witness, service._checkpoint)
    assert phase(tmp_path) == 'clean'


@pytest.mark.parametrize('field,value', [
    ('ticket', ''), ('attempt', True), ('active_match_counts', [1, 0]),
    ('active_match_counts', [0, 1]), ('active_match_counts', [False, 0]),
    ('history_complete', False), ('witness_sha256', '0' * 64),
    ('event_times', dict(grant=10, release=20, free=40)),
    ('event_times', dict(grant=10, release=20, confirm_dead=15, free=40)),
])
def test_allocator_incomplete_or_tampered_refuses(tmp_path, field, value):
    _, status, witness, _ = recovery_fixture(tmp_path)
    witness[field] = value
    factory = recovery_session(tmp_path, status, witness, [])
    assert service.serve(SCOPE, factory, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert phase(tmp_path) == 'reconciliation-required'


@pytest.mark.parametrize('field,value', [
    ('scope_sha256', '0' * 64), ('cycle', '../cycle-explicit'),
    ('job_id', 'BAD'), ('attempt', True), ('original_runtime_commit', '0' * 40),
    ('source', dict(revision=True, document_sha256='d' * 64, archive_sha256='a' * 64)),
    ('extra', SECRET),
])
def test_candidate_refuses_before_external_reads(tmp_path, field, value):
    recovery_fixture(tmp_path)
    path = directory(tmp_path) / 'reconciliation-candidate.json'
    candidate = json.loads(path.read_bytes())
    candidate[field] = value
    path.write_bytes(service.worker.encode(candidate))
    old = marker(tmp_path).read_bytes()
    assert service.serve(SCOPE, forbidden, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert marker(tmp_path).read_bytes() == old


@pytest.mark.parametrize('which', ['status', 'allocator'])
def test_legacy_read_outage_leaves_old_bytes_unchanged(tmp_path, which):
    _, status, witness, failure = recovery_fixture(tmp_path)
    old_checkpoint, old_failure = marker(tmp_path).read_bytes(), failure.read_bytes()
    if which == 'status':
        status = OSError(SECRET)
    else:
        witness = OSError(SECRET)
    factory = recovery_session(tmp_path, status, witness, [])
    assert service.serve(SCOPE, factory, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert marker(tmp_path).read_bytes() == old_checkpoint and failure.read_bytes() == old_failure


def test_journal_write_failure_keeps_last_durable_proof_and_stays_failed(tmp_path, monkeypatch):
    r = service.recovery
    base = directory(tmp_path)
    base.mkdir()
    cycle = base / 'cycle-write-failure'
    cycle.mkdir()
    journal = r.Journal(cycle, base.name, base)
    claim = dict(event='claim', job_id=COMPLETE['job_id'], attempt=1, deadline=1000,
                 source=dict(revision=3, document_sha256='d' * 64, archive_sha256='a' * 64))
    journal(claim)
    journal(dict(event='allocator', ticket='full-fixture-ticket', marker_sha256='e' * 64))
    before = journal.path.read_bytes()
    def denied(*args):
        raise OSError(SECRET)
    monkeypatch.setattr(r.os, 'replace', denied)
    with pytest.raises(service.worker.WorkerError, match='journal-failed'):
        journal(dict(event='producer', producer_may_have_started=True))
    assert journal.failed and journal.path.read_bytes() == before
    with pytest.raises(service.worker.WorkerError):
        journal(dict(event='cleanup', cleanup_complete=True))
    assert journal.path.read_bytes() == before and SECRET.encode() not in before


def test_producer_journal_event_cannot_be_reset(tmp_path):
    r = service.recovery
    base = directory(tmp_path)
    base.mkdir()
    cycle = base / 'cycle-sticky-producer'
    cycle.mkdir()
    journal = r.Journal(cycle, base.name, base)
    journal(dict(event='claim', job_id=COMPLETE['job_id'], attempt=1, deadline=1000,
                 source=dict(revision=3, document_sha256='d' * 64, archive_sha256='a' * 64)))
    journal(dict(event='allocator', ticket='full-fixture-ticket', marker_sha256='e' * 64))
    journal(dict(event='producer', producer_may_have_started=True))
    before = journal.path.read_bytes()
    with pytest.raises(service.worker.WorkerError, match='journal-failed'):
        journal(dict(event='producer', producer_may_have_started=False))
    assert journal.path.read_bytes() == before
