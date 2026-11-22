"""Hermetic lifecycle fixtures; no installed identity or native execution."""
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import threading
import time

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
        journal(dict(event='claim', job_id=COMPLETE['job_id'], attempt=1, deadline=1000,
                     source=dict(revision=3, document_sha256='d' * 64, archive_sha256='a' * 64)))
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


@pytest.mark.parametrize('prior_checkpoint', [False, True])
@pytest.mark.parametrize('failure', ['empty', 'partial-journal', 'session-close'])
def test_journal_initialization_failure_preserves_prior_checkpoint_and_evidence(
        tmp_path, monkeypatch, prior_checkpoint, failure):
    base = directory(tmp_path)
    base.mkdir()
    old = service.worker.encode(dict(scope_sha256=base.name, phase='clean')) + b'\n'
    if prior_checkpoint:
        marker(tmp_path).write_bytes(old)
    monkeypatch.setattr(service.worker, 'run_once', forbidden)
    partial = b'partial journal fixture, never a claim witness'
    def broken_journal(cycle, digest, directory):
        if failure == 'partial-journal':
            (cycle / 'journal.json').write_bytes(partial)
        raise RuntimeError(SECRET)
    monkeypatch.setattr(service.recovery, 'Journal', broken_journal)
    exits = []
    @contextmanager
    def factory(scope):
        try:
            yield object(), forbidden
        finally:
            exits.append(True)
            if failure == 'session-close':
                raise RuntimeError(SECRET)
    stop = Stop()
    result = service.serve(SCOPE, factory, tmp_path, stop)
    assert result == dict(state='reconciliation-required' if failure == 'session-close' else 'stopped', completed=0)
    assert exits == [True]
    assert stop.waits == ([] if failure == 'session-close' else [30.0])
    if prior_checkpoint:
        assert marker(tmp_path).read_bytes() == old
    else:
        assert not marker(tmp_path).exists()
    cycles = list(base.glob('cycle-*'))
    assert len(cycles) == 1
    evidence = cycles[0] / 'initialization-failure.json'
    assert json.loads(evidence.read_bytes()) == dict(
        schema='leaf.native-preview-initialization-failure.v1', scope_sha256=base.name,
        cycle=cycles[0].name, stage='journal-initialization', worker_invoked=False)
    if failure == 'partial-journal':
        assert (cycles[0] / 'journal.json').read_bytes() == partial
    for path in cycles[0].iterdir():
        assert SECRET.encode() not in path.read_bytes()


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


def test_process_interruption_recovers_without_backend_settlement_on_restart(tmp_path):
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
    stop = Stop()
    assert service.serve(SCOPE, forbidden, tmp_path, stop) == dict(state='stopped', completed=0)
    assert stop.waits == [30.0] and phase(tmp_path) == 'clean'
    receipts = list(directory(tmp_path).glob('reconciliation-cycle-*.json'))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_bytes())['backend_settlement'] == 'unconfirmed'


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


def settled_producer_fixture(root):
    claim, status, witness, failure = recovery_fixture(root, legacy=False)
    journal_path = failure.parent / 'journal.json'
    journal = json.loads(journal_path.read_bytes())
    journal['events'].insert(2, dict(event='producer', producer_may_have_started=True))
    journal_path.write_bytes(service.worker.encode(journal))
    value = json.loads(failure.read_bytes())
    value.update(execution_stage='mac-producer', error_code='mac-transport-failed',
                 authority_failure='none', failure_settlement='accepted')
    failure.write_bytes(service.worker.encode(value))
    del status['failure_disposition']
    return claim, status, witness, failure


def assert_settled_local_hold(root):
    calls = []
    original = marker(root).read_bytes()
    def unexpected(*args):
        calls.append('session')
        forbidden()
    assert service.serve(SCOPE, unexpected, root, Stop())['state'] == 'reconciliation-required'
    assert calls == [] and marker(root).read_bytes() == original
    assert not service.recovery.receipt_path(directory(root), COMPLETE['job_id']).exists()


def test_settled_producer_recovers_once_preserves_evidence_and_resumes_polling(tmp_path):
    r = service.recovery
    claim, status, witness, failure = settled_producer_fixture(tmp_path)
    journal = failure.parent / 'journal.json'
    originals = {path: path.read_bytes() for path in (marker(tmp_path), journal, failure)}
    events = []
    recovery = recovery_session(tmp_path, status, witness, events)
    receipt = r.receipt_path(directory(tmp_path), claim['job_id'])
    class NextHttp:
        def request(self, method, suffix, body):
            assert (method, suffix, body) == ('POST', 'claim', {})
            assert receipt.exists() and phase(tmp_path) == 'reconciliation-required'
            events.append('poll')
            return b'{"job":null}'
    @contextmanager
    def factory(scope):
        if not events:
            with recovery(scope) as pair:
                yield pair
        else:
            assert phase(tmp_path) == 'clean'
            yield NextHttp(), forbidden
    assert service.serve(SCOPE, factory, tmp_path, Stop()) == dict(state='stopped', completed=0)
    assert events == ['enter', 'status', 'allocator', 'close', 'poll']
    raw = receipt.read_bytes()
    value = json.loads(raw)
    assert value['outcome'] == 'failed-producer-settled-reconciled'
    assert value['attempt_binding'] == 'local-accepted-failure-settlement'
    assert value['backend_status_sha256'] == r.sha(service.worker.encode(status))
    assert value['allocator_witness_sha256'] == witness['witness_sha256']
    assert value['original_checkpoint'] == originals[marker(tmp_path)].decode()
    assert value['checkpoint_sha256'] == r.sha(originals[marker(tmp_path)])
    assert value['failure_sha256'] == r.sha(originals[failure])
    assert value['local_evidence_sha256'] == r.sha(service.worker.encode(
        {path.name: r.sha(data) for path, data in originals.items()}))
    assert value['worker_sha256'] == r.reviewed_worker_source(value['original_runtime_commit'])
    assert value['journal_source_sha256'] == r.PRE_EXECUTION_CONNECTED_JOURNAL_SOURCE_SHA256
    assert value['scope_sha256'] == directory(tmp_path).name
    assert value['cycle'] == failure.parent.name
    assert value['original_runtime_commit'] == json.loads(originals[journal])['runtime_commit']
    assert all(value[k] == claim[k] for k in ('job_id', 'attempt', 'source'))
    assert 'disposition_sha256' not in value
    assert service.serve(SCOPE, factory, tmp_path, Stop()) == dict(state='stopped', completed=0)
    assert events.count('status') == 1 and events.count('poll') == 2
    assert receipt.read_bytes() == raw
    assert journal.read_bytes() == originals[journal] and failure.read_bytes() == originals[failure]
    cycle = directory(tmp_path) / 'cycle-replay'
    cycle.mkdir()
    replay = r.Journal(cycle, directory(tmp_path).name, directory(tmp_path))
    with pytest.raises(service.worker.WorkerError, match='journal-failed'):
        replay(dict(event='claim', **claim, deadline=1000))


@pytest.mark.parametrize('event', ['claim', 'allocator', 'producer', 'cleanup', 'release'])
def test_settled_producer_requires_every_event(tmp_path, event):
    _, _, _, failure = settled_producer_fixture(tmp_path)
    path = failure.parent / 'journal.json'
    value = json.loads(path.read_bytes())
    value['events'] = [item for item in value['events'] if item['event'] != event]
    path.write_bytes(service.worker.encode(value))
    assert_settled_local_hold(tmp_path)


@pytest.mark.parametrize('field,value', [
    ('stage', 'completion'), ('result', 'unknown'), ('job_id', SCOPE['actor_id']),
    ('archive_sha256', '0' * 64), ('execution_stage', 'mac-permit-refresh'),
    ('error_code', 'completion-unknown'), ('error_code', 'authority-lost'),
    ('error_code', 'journal-failed'), ('authority_failure', 'permit-refresh'),
    ('failure_settlement', 'response-unconfirmed'), ('failure_settlement', 'request-failed'),
    ('failure_settlement', 'not-attempted-authority-lost'), ('failure_settlement', None),
    ('cleanup_complete', False), ('cleanup_complete', 1),
    ('allocator_release_confirmed', False), ('allocator_release_confirmed', 1),
])
def test_settled_producer_failure_mismatch_refuses_before_session(tmp_path, field, value):
    _, _, _, failure = settled_producer_fixture(tmp_path)
    report = json.loads(failure.read_bytes())
    if value is None:
        del report[field]
    else:
        report[field] = value
    failure.write_bytes(service.worker.encode(report))
    assert_settled_local_hold(tmp_path)


@pytest.mark.parametrize('mutation', ['schema', 'scope', 'cycle', 'runtime', 'claim-state',
                                      'job', 'source', 'attempt', 'filename', 'extra-file',
                                      'producer-false', 'producer-integer', 'cleanup-false',
                                      'release-false', 'checkpoint-scope', 'checkpoint-schema',
                                      'checkpoint-cycle', 'failure-symlink', 'journal-symlink',
                                      'checkpoint-symlink', 'cycle-symlink'])
def test_settled_producer_local_identity_and_flags_refuse(tmp_path, mutation):
    _, _, _, failure = settled_producer_fixture(tmp_path)
    path = failure.parent / 'journal.json'
    value = json.loads(path.read_bytes())
    if mutation in ('schema', 'scope', 'cycle', 'runtime', 'claim-state'):
        field, replacement = {'schema': ('schema', 'unknown'), 'scope': ('scope_sha256', '0' * 64),
                              'cycle': ('cycle', 'cycle-other'), 'runtime': ('runtime_commit', '0' * 40),
                              'claim-state': ('claim_state', 'unknown')}[mutation]
        value[field] = replacement
    elif mutation == 'job':
        value['events'][0]['job_id'] = SCOPE['actor_id']
    elif mutation == 'source':
        value['events'][0]['source']['archive_sha256'] = '0' * 64
    elif mutation == 'attempt':
        value['events'][0]['attempt'] = 8
    elif mutation.startswith('producer-'):
        value['events'][2]['producer_may_have_started'] = False if mutation.endswith('false') else 1
    elif mutation == 'cleanup-false':
        value['events'][3]['cleanup_complete'] = False
    elif mutation == 'release-false':
        value['events'][4]['allocator_release_confirmed'] = False
    path.write_bytes(service.worker.encode(value))
    if mutation == 'filename':
        failure.rename(failure.with_name(failure.name.replace('-7-', '-8-')))
    elif mutation == 'extra-file':
        (failure.parent / 'unexpected').write_bytes(b'')
    elif mutation in ('checkpoint-scope', 'checkpoint-schema', 'checkpoint-cycle'):
        checkpoint = json.loads(marker(tmp_path).read_bytes())
        field, replacement = {'checkpoint-scope': ('scope_sha256', '0' * 64),
                              'checkpoint-schema': ('schema', 'unknown'),
                              'checkpoint-cycle': ('cycle', '../cycle-explicit')}[mutation]
        checkpoint[field] = replacement
        marker(tmp_path).write_bytes(service.worker.encode(checkpoint))
    elif mutation.endswith('-symlink'):
        target = {'failure-symlink': failure, 'journal-symlink': path,
                  'checkpoint-symlink': marker(tmp_path), 'cycle-symlink': failure.parent}[mutation]
        moved = directory(tmp_path) / 'moved-evidence'
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=mutation == 'cycle-symlink')
    assert_settled_local_hold(tmp_path)


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_settled_producer_current_pending_status_waits_then_recovers(tmp_path, state):
    _, status, witness, failure = settled_producer_fixture(tmp_path)
    events = []
    recovery = recovery_session(tmp_path, status, witness, events)
    pending = recovery_session(tmp_path, {**status, 'status': state}, witness, events)
    class NextHttp:
        def request(self, method, suffix, body):
            assert (method, suffix, body) == ('POST', 'claim', {})
            return b'{"job":null}'
    @contextmanager
    def factory(scope):
        if events.count('status') < 2:
            with (pending if not events else recovery)(scope) as pair:
                yield pair
        else:
            yield NextHttp(), forbidden
    stop = Stop(after=2)
    assert service.serve(SCOPE, factory, tmp_path, stop, error_seconds=9)['state'] == 'stopped'
    assert stop.waits == [9.0, 5.0] and events.count('allocator') == 1
    assert phase(tmp_path) == 'clean' and failure.exists()


@pytest.mark.parametrize('mutation', ['cancelled', 'complete', 'unknown', 'non-string',
                                      'job', 'revision', 'document', 'archive', 'extra', 'missing'])
def test_settled_producer_current_status_refuses_before_allocator(tmp_path, mutation):
    _, status, witness, _ = settled_producer_fixture(tmp_path)
    if mutation in ('cancelled', 'complete', 'unknown', 'non-string'):
        status['status'] = 1 if mutation == 'non-string' else mutation
    elif mutation == 'job':
        status['job_id'] = SCOPE['actor_id']
    elif mutation == 'revision':
        status['revision'] += 1
    elif mutation in ('document', 'archive'):
        status[mutation + '_sha256'] = '0' * 64
    elif mutation == 'extra':
        status['failure_disposition'] = {}
    else:
        del status['status']
    events = []
    factory = recovery_session(tmp_path, status, witness, events)
    assert service.serve(SCOPE, factory, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert 'allocator' not in events


@pytest.mark.parametrize('raw', [b'{broken', b'null', b'[]',
                                b'{"job_id":"first","job_id":"second"}'])
def test_settled_producer_malformed_status_refuses(tmp_path, raw):
    claim, _, _, _ = settled_producer_fixture(tmp_path)
    with pytest.raises(service.worker.WorkerError):
        service.recovery.settled_failure_status(raw, claim)


@pytest.mark.parametrize('field,value', [
    ('ticket', 'foreign-full-ticket'), ('attempt', 8), ('job_id', SCOPE['actor_id']),
    ('prefix', 'preview-foreign-7-'), ('active_match_counts', [1, 0]),
    ('active_match_counts', [0, 1]), ('history_complete', False),
    ('event_times', dict(grant=10, release=20, confirm_dead=15, free=40)),
    ('event_times', dict(grant=10, release=20, confirm_dead=30)),
])
def test_settled_producer_allocator_extinction_and_full_ticket_bind(tmp_path, field, value):
    _, status, witness, _ = settled_producer_fixture(tmp_path)
    witness[field] = value
    witness['witness_sha256'] = service.recovery.sha(service.worker.encode(
        {k: v for k, v in witness.items() if k != 'witness_sha256'}))
    factory = recovery_session(tmp_path, status, witness, [])
    assert service.serve(SCOPE, factory, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert not service.recovery.receipt_path(directory(tmp_path), COMPLETE['job_id']).exists()


@pytest.mark.parametrize('which', ['checkpoint', 'journal', 'failure', 'extra-file',
                                  'cycle', 'same-bytes-new-inode', 'close', 'extinction-refused'])
def test_settled_producer_races_and_unclosed_evidence_stay_held(tmp_path, which):
    _, status, witness, failure = settled_producer_fixture(tmp_path)
    r = service.recovery
    def drift():
        paths = dict(checkpoint=marker(tmp_path), journal=failure.parent / 'journal.json', failure=failure)
        if which in paths:
            paths[which].write_bytes(paths[which].read_bytes() + b' ')
        elif which == 'extra-file':
            (failure.parent / 'unexpected').write_bytes(b'')
        elif which == 'same-bytes-new-inode':
            replacement = failure.parent / 'replacement'
            replacement.write_bytes(failure.read_bytes())
            replacement.replace(failure)
        elif which == 'cycle':
            moved = failure.parent.with_name('cycle-moved')
            failure.parent.rename(moved)
            failure.parent.mkdir()
            for path in moved.iterdir():
                (failure.parent / path.name).write_bytes(path.read_bytes())
    if which == 'extinction-refused':
        witness = service.worker.WorkerError('invalid-protocol')
    factory = recovery_session(tmp_path, status, witness, [], hook=drift, close_error=which == 'close')
    stop = Stop()
    result = service.serve(SCOPE, factory, tmp_path, stop)
    # A transport/session-close failure waits for evidence, then honors stop.
    # The persisted hold must remain, regardless of the loop's stop reason.
    assert result['state'] == ('stopped' if which == 'close' else 'reconciliation-required')
    assert phase(tmp_path) == 'reconciliation-required'
    if which == 'close':
        assert stop.waits == [30.0]
    assert not r.receipt_path(directory(tmp_path), COMPLETE['job_id']).exists()


def test_settled_producer_crash_reconstructs_identical_receipt_and_refuses_conflict(tmp_path):
    claim, status, witness, failure = settled_producer_fixture(tmp_path)
    r = service.recovery
    backend = r.settled_failure_status(service.worker.encode(status), claim)
    def proof():
        return r.LocalProof(directory(tmp_path), marker(tmp_path), directory(tmp_path).name)
    def crash(*args):
        raise OSError('synthetic-crash')
    with pytest.raises(OSError, match='synthetic-crash'):
        proof().commit(SCOPE, backend, witness, crash)
    receipt = r.receipt_path(directory(tmp_path), claim['job_id'])
    original, evidence = receipt.read_bytes(), failure.read_bytes()
    with pytest.raises(OSError, match='synthetic-crash'):
        proof().commit(SCOPE, backend, witness, crash)
    assert receipt.read_bytes() == original and phase(tmp_path) == 'reconciliation-required'
    receipt.write_bytes(b'{}')
    with pytest.raises(service.worker.WorkerError):
        proof().commit(SCOPE, backend, witness, service._checkpoint)
    assert phase(tmp_path) == 'reconciliation-required'
    receipt.write_bytes(original)
    proof().commit(SCOPE, backend, witness, service._checkpoint)
    assert phase(tmp_path) == 'clean' and receipt.read_bytes() == original
    assert failure.read_bytes() == evidence


@pytest.mark.parametrize('which', ['original-worker', 'original-journal', 'current-worker', 'current-journal'])
def test_settled_producer_altered_runtime_source_refuses(tmp_path, monkeypatch, which):
    settled_producer_fixture(tmp_path)
    r = service.recovery
    load, read = r.runtime_source, r.read
    def altered(raw, path):
        if which.endswith('worker') and path.endswith('native_preview_worker.py'):
            return raw.replace(b"failure_settlement = 'accepted'", b"failure_settlement = 'unconfirmed'")
        if which.endswith('journal') and path.endswith('native_preview_reconciliation.py'):
            return raw.replace(b'self.failed = True', b'self.failed = False')
        return raw
    def original(revision, path):
        return altered(load(revision, path), path)
    def current(path, limit=r.MAX_LOCAL):
        return altered(read(path, limit), str(path))
    if which.startswith('original'):
        monkeypatch.setattr(r, 'runtime_source', original)
    else:
        monkeypatch.setattr(r, 'read', current)
    assert_settled_local_hold(tmp_path)


@pytest.mark.parametrize('which', ['worker', 'journal'])
def test_settled_producer_source_changed_after_collection_prevents_receipt(tmp_path, monkeypatch, which):
    _, status, witness, _ = settled_producer_fixture(tmp_path)
    r = service.recovery
    proof = r.LocalProof(directory(tmp_path), marker(tmp_path), directory(tmp_path).name)
    backend, allocator = proof.collect(SCOPE, recovery_session(tmp_path, status, witness, []))
    read = r.read
    def changed(path, limit=r.MAX_LOCAL):
        raw = read(path, limit)
        if which == 'worker' and str(path).endswith('native_preview_worker.py'):
            return raw.replace(b"failure_settlement = 'accepted'", b"failure_settlement = 'unconfirmed'")
        if which == 'journal' and str(path).endswith('native_preview_reconciliation.py'):
            return raw.replace(b'self.failed = True', b'self.failed = False')
        return raw
    monkeypatch.setattr(r, 'read', changed)
    with pytest.raises(service.worker.WorkerError):
        proof.commit(SCOPE, backend, allocator, forbidden)
    assert not r.receipt_path(directory(tmp_path), COMPLETE['job_id']).exists()
    assert phase(tmp_path) == 'reconciliation-required'


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


def test_immutable_receipt_replay_fsyncs_writable_descriptor_without_changing_bytes(tmp_path, monkeypatch):
    r = service.recovery
    receipt = tmp_path / 'receipt.json'
    raw = b'{"immutable":true}'
    r.immutable(receipt, raw)
    original_stat = receipt.stat()
    opened = []
    synced = []
    open_path, fsync = Path.open, r.os.fsync

    def observe_open(path, mode='r', *args, **kwargs):
        stream = open_path(path, mode, *args, **kwargs)
        if path == receipt and mode == 'r+b':
            opened.append(stream)
        return stream

    def require_writable(fd):
        for stream in opened:
            if not stream.closed and stream.fileno() == fd:
                assert stream.writable()
                synced.append(fd)
        return fsync(fd)

    monkeypatch.setattr(Path, 'open', observe_open)
    monkeypatch.setattr(r.os, 'fsync', require_writable)
    r.immutable(receipt, raw)
    assert len(opened) == len(synced) == 1
    assert receipt.read_bytes() == raw
    assert receipt.stat().st_mtime_ns == original_stat.st_mtime_ns


def test_receipt_crash_idempotence_and_conflict(tmp_path):
    claim, status, witness, _ = recovery_fixture(tmp_path)
    r = service.recovery
    proof = r.LocalProof(directory(tmp_path), marker(tmp_path), directory(tmp_path).name)
    backend = r.disposition(service.worker.encode(status), SCOPE, claim)
    def crash(*args):
        raise OSError('synthetic-crash')
    with pytest.raises(OSError, match='synthetic-crash'):
        proof.commit(SCOPE, backend, witness, crash)
    receipt = r.receipt_path(directory(tmp_path), claim['job_id'])
    original = receipt.read_bytes()
    with pytest.raises(OSError, match='synthetic-crash'):
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


def pre_execution_fixture(root):
    r = service.recovery
    base = directory(root)
    base.mkdir()
    cycle = base / 'cycle-pre-execution'
    cycle.mkdir()
    journal = r.Journal(cycle, base.name, base)
    checkpoint = dict(schema=r.CHECKPOINT_SCHEMA, scope_sha256=base.name,
                      phase='reconciliation-required', cycle=cycle.name)
    marker(root).write_bytes(service.worker.encode(checkpoint))
    return journal.path, base / ('reconciliation-' + cycle.name + '.json')


def test_pre_execution_receipt_preserves_evidence_then_backoff_before_fresh_claim(tmp_path):
    journal, receipt = pre_execution_fixture(tmp_path)
    old_checkpoint, old_journal = marker(tmp_path).read_bytes(), journal.read_bytes()
    stop = Stop(after=2)
    calls = []
    class Http:
        def request(self, method, suffix, body):
            assert stop.waits == [11.0]
            assert (method, suffix, body) == ('POST', 'claim', {})
            calls.append(suffix)
            value = json.loads(receipt.read_bytes())
            assert value['outcome'] == 'claim-unconfirmed-pre-execution'
            assert value['backend_settlement'] == 'unconfirmed'
            assert value['original_checkpoint'] == old_checkpoint.decode()
            assert value['checkpoint_sha256'] == service.recovery.sha(old_checkpoint)
            assert value['journal_sha256'] == service.recovery.sha(old_journal)
            assert value['original_runtime_commit'] == json.loads(old_journal)['runtime_commit']
            assert not {'job_id', 'attempt', 'allocator_release_confirmed', 'source'} & value.keys()
            return b'{"job":null}'
    @contextmanager
    def factory(scope):
        assert stop.waits == [11.0]
        assert phase(tmp_path) == 'clean'
        yield Http(), forbidden
    assert service.serve(SCOPE, factory, tmp_path, stop, error_seconds=11) == dict(
        state='stopped', completed=0)
    assert calls == ['claim'] and stop.waits == [11.0, 5.0]
    assert journal.read_bytes() == old_journal


def test_lost_claim_responses_use_distinct_cycles_and_normal_backoff(tmp_path):
    calls = []
    class Http:
        def request(self, method, suffix, body):
            assert (method, suffix, body) == ('POST', 'claim', {})
            calls.append(True)
            raise OSError(SECRET)
    @contextmanager
    def factory(scope):
        yield Http(), forbidden
    stop = Stop(after=3)
    assert service.serve(SCOPE, factory, tmp_path, stop, error_seconds=13) == dict(
        state='stopped', completed=0)
    assert calls == [True] * 3 and stop.waits == [13.0] * 3
    receipts = list(directory(tmp_path).glob('reconciliation-cycle-*.json'))
    assert len(receipts) == 3 and phase(tmp_path) == 'clean'
    assert len({json.loads(path.read_bytes())['cycle'] for path in receipts}) == 3


@pytest.mark.parametrize('mutation', ['scope', 'cycle', 'runtime', 'schema', 'extra-key',
                                      'event', 'validated', 'extra-file', 'symlink',
                                      'checkpoint-schema', 'checkpoint-scope',
                                      'checkpoint-cycle', 'checkpoint-symlink', 'cycle-symlink'])
def test_pre_execution_mismatch_stays_held_without_session(tmp_path, mutation):
    journal, receipt = pre_execution_fixture(tmp_path)
    value = json.loads(journal.read_bytes())
    if mutation == 'scope':
        value['scope_sha256'] = '0' * 64
    elif mutation == 'cycle':
        value['cycle'] = 'cycle-other'
    elif mutation == 'runtime':
        value['runtime_commit'] = '0' * 40
    elif mutation == 'schema':
        value['schema'] += '-unknown'
    elif mutation == 'extra-key':
        value['extra'] = True
    elif mutation == 'event':
        value['events'] = [dict(event='cleanup', cleanup_complete=True)]
    elif mutation == 'validated':
        value['claim_state'] = 'validated'
    elif mutation == 'extra-file':
        (journal.parent / 'unexpected').write_bytes(b'')
    journal.write_bytes(service.worker.encode(value))
    if mutation == 'symlink':
        target = directory(tmp_path) / 'journal-target.json'
        journal.rename(target)
        journal.symlink_to(target)
    elif mutation.startswith('checkpoint-') and mutation != 'checkpoint-symlink':
        checkpoint = json.loads(marker(tmp_path).read_bytes())
        field, replacement = {'checkpoint-schema': ('schema', 'unknown'),
                              'checkpoint-scope': ('scope_sha256', '0' * 64),
                              'checkpoint-cycle': ('cycle', '../cycle-pre-execution')}[mutation]
        checkpoint[field] = replacement
        marker(tmp_path).write_bytes(service.worker.encode(checkpoint))
    elif mutation == 'checkpoint-symlink':
        target = directory(tmp_path) / 'checkpoint-target.json'
        marker(tmp_path).rename(target)
        marker(tmp_path).symlink_to(target)
    elif mutation == 'cycle-symlink':
        target = directory(tmp_path) / 'cycle-target'
        journal.parent.rename(target)
        journal.parent.symlink_to(target, target_is_directory=True)
    old = marker(tmp_path).read_bytes()
    assert service.serve(SCOPE, forbidden, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert marker(tmp_path).read_bytes() == old and not receipt.exists()


@pytest.mark.parametrize('which', ['checkpoint', 'journal', 'extra-file', 'cycle', 'same-bytes-new-inode'])
def test_pre_execution_rechecks_exact_evidence_before_receipt(tmp_path, which):
    journal, receipt = pre_execution_fixture(tmp_path)
    r = service.recovery
    proof = r.PreExecutionProof(directory(tmp_path), marker(tmp_path), directory(tmp_path).name)
    if which in ('checkpoint', 'journal'):
        path = marker(tmp_path) if which == 'checkpoint' else journal
        path.write_bytes(path.read_bytes() + b' ')
    elif which == 'extra-file':
        (journal.parent / 'unexpected').write_bytes(b'')
    elif which == 'same-bytes-new-inode':
        replacement = journal.parent / 'replacement.json'
        replacement.write_bytes(journal.read_bytes())
        replacement.replace(journal)
    else:
        journal.parent.rename(journal.parent.with_name('cycle-moved'))
        journal.parent.mkdir()
        journal.write_bytes(proof.files[journal])
    with pytest.raises(service.worker.WorkerError):
        proof.commit(SCOPE, service._checkpoint)
    assert not receipt.exists() and phase(tmp_path) == 'reconciliation-required'


def test_pre_execution_receipt_crash_idempotence_and_conflict(tmp_path):
    journal, receipt = pre_execution_fixture(tmp_path)
    old = journal.read_bytes()
    r = service.recovery
    def crash(*args):
        raise OSError('synthetic-crash')
    for _ in range(2):
        proof = r.PreExecutionProof(directory(tmp_path), marker(tmp_path), directory(tmp_path).name)
        with pytest.raises(OSError, match='synthetic-crash'):
            proof.commit(SCOPE, crash)
        if _ == 0:
            original = receipt.read_bytes()
        assert receipt.read_bytes() == original
    receipt.write_bytes(b'{}')
    assert service.serve(SCOPE, forbidden, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert phase(tmp_path) == 'reconciliation-required'
    receipt.write_bytes(original)
    stop = Stop()
    assert service.serve(SCOPE, forbidden, tmp_path, stop)['state'] == 'stopped'
    assert stop.waits == [30.0] and phase(tmp_path) == 'clean'
    assert journal.read_bytes() == old and receipt.read_bytes() == original


def test_pre_execution_source_binding_requires_reviewed_git_history():
    r = service.recovery
    revision = 'ea6666829719f014dd955f5d61db14f24836177d'
    try:
        r.runtime_commit(revision)
    except service.worker.WorkerError:
        # Shallow CI must prove refusal, not skip a required source boundary.
        with pytest.raises(service.worker.WorkerError):
            r.pre_execution_source(revision)
        return
    r.pre_execution_source(revision)
    producer = r.runtime_source(revision, 'scripts/native_preview_worker.py')
    journal = r.runtime_source(revision, 'scripts/native_preview_reconciliation.py')
    assert r.sha(producer) == r.PRE_EXECUTION_WORKER_SHA256
    assert r.journal_source_sha(journal) == r.PRE_EXECUTION_JOURNAL_SOURCE_SHA256
    current = r.read(Path(r.__file__), 262144)
    assert r.journal_source_sha(current) == r.PRE_EXECUTION_CONNECTED_JOURNAL_SOURCE_SHA256


def test_worker_source_allowlist_contains_only_captured_producers():
    r = service.recovery
    assert r.REVIEWED_WORKER_SHA256 == {
        'd0fa9e9778490278988382f573555538a2fe204160f8d57da44958858b2cd89d',
        '6ad5eb2b343e3d9f749769805001d74282519d6ce41cdf7996999573fa306197',
        'acc9c1219ee4246f6b39da6a3c439add24f6573c0e54626eca8b3752b790ee37',
        '1d2844f0937c498ab7b5b743e40e6b2421c3571f8c722dd6abe027e5ebb13087',
    }
    assert r.sha(Path(service.worker.__file__).read_bytes()) in r.REVIEWED_WORKER_SHA256
    # Bind the new Journal dependency closure to its exact reviewed producer,
    # including both original-history and current-source checks.
    # The relay bundles main/head ancestry, not squash-merged feature commits.
    # Bind the actual runtime bytes to the same exact reviewed source pins.
    connected = r.runtime_commit('HEAD')
    worker = r.runtime_source(connected, 'scripts/native_preview_worker.py')
    journal = r.runtime_source(connected, 'scripts/native_preview_reconciliation.py')
    assert r.sha(worker) == r.EDIT_CUSTODY_WORKER_SHA256
    assert r.journal_source_sha(journal) == r.PRE_EXECUTION_CONNECTED_JOURNAL_SOURCE_SHA256
    assert r.pre_execution_source(connected) == r.PRE_EXECUTION_CONNECTED_JOURNAL_SOURCE_SHA256
    load, read = r.runtime_source, r.read
    legacy_worker = load('e158ace5709231f9d9f37418d6b2c48d69228647', 'scripts/native_preview_worker.py')
    assert r.sha(legacy_worker) == '6ad5eb2b343e3d9f749769805001d74282519d6ce41cdf7996999573fa306197'
    with pytest.MonkeyPatch.context() as patch:
        for original in (False, True):
            for target in ('worker', 'journal', 'legacy-worker-with-connected-journal'):
                def changed(raw, path):
                    if path.endswith('native_preview_worker.py'):
                        if target == 'worker':
                            return raw.replace(b'guard.start()', b'guard.beat()')
                        if target == 'legacy-worker-with-connected-journal':
                            return legacy_worker
                    if target == 'journal' and path.endswith('native_preview_reconciliation.py'):
                        return raw.replace(b'self.failed = True', b'self.failed = False')
                    return raw
                patch.setattr(r, 'runtime_source', lambda revision, path: changed(load(revision, path), path) if original else load(revision, path))
                patch.setattr(r, 'read', lambda path, limit=r.MAX_LOCAL: read(path, limit) if original else changed(read(path, limit), str(path)))
                with pytest.raises(service.worker.WorkerError):
                    r.pre_execution_source(connected)


def test_pre_execution_receipt_records_the_actual_corrected_source(tmp_path, monkeypatch):
    _, receipt = pre_execution_fixture(tmp_path)
    r = service.recovery
    load = r.runtime_source
    corrected = Path(r.__file__).read_bytes()
    def original(revision, path):
        return corrected if path.endswith('native_preview_reconciliation.py') else load(revision, path)
    monkeypatch.setattr(r, 'runtime_source', original)
    proof = r.PreExecutionProof(directory(tmp_path), marker(tmp_path), directory(tmp_path).name)
    proof.commit(SCOPE, service._checkpoint)
    assert json.loads(receipt.read_bytes())['journal_source_sha256'] == r.PRE_EXECUTION_CONNECTED_JOURNAL_SOURCE_SHA256


@pytest.mark.parametrize('which', ['original-worker', 'original-journal', 'current-worker', 'current-journal'])
def test_pre_execution_altered_source_refuses(tmp_path, monkeypatch, which):
    _, receipt = pre_execution_fixture(tmp_path)
    r = service.recovery
    load, read = r.runtime_source, r.read
    def altered(raw, path):
        if which.endswith('worker') and path.endswith('native_preview_worker.py'):
            return raw.replace(b'guard.start()', b'guard.beat()')
        if which.endswith('journal') and path.endswith('native_preview_reconciliation.py'):
            return raw.replace(b'self.failed = True', b'self.failed = False')
        return raw
    def original(revision, path):
        return altered(load(revision, path), path)
    def current(path, limit=r.MAX_LOCAL):
        raw = read(path, limit)
        return altered(raw, str(path))
    if which.startswith('original'):
        monkeypatch.setattr(r, 'runtime_source', original)
    else:
        monkeypatch.setattr(r, 'read', current)
    assert service.serve(SCOPE, forbidden, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert not receipt.exists() and phase(tmp_path) == 'reconciliation-required'


def test_claim_journal_write_failure_cannot_reach_heartbeat_source_or_native(tmp_path, monkeypatch):
    r = service.recovery
    base = directory(tmp_path)
    base.mkdir()
    cycle = base / 'cycle-write-failure'
    cycle.mkdir()
    journal = r.Journal(cycle, base.name, base)
    old = journal.path.read_bytes()
    calls = []
    descriptor = dict(schema='leaf.project-preview-source.v1', target='ios-simulator',
                      org_id=SCOPE['org_id'], project_id=SCOPE['project_id'], revision=1,
                      document_sha256='d' * 64, archive_sha256='a' * 64, byte_size=1)
    job = dict(job_id=COMPLETE['job_id'], lease_owner=SCOPE['actor_id'], attempt=1,
               descriptor=descriptor, deadline=time.time() + 300)
    class Http:
        def request(self, method, suffix, body):
            calls.append(suffix)
            assert suffix == 'claim'
            return service.worker.encode(dict(job=job))
    def denied(*args):
        raise OSError(SECRET)
    monkeypatch.setattr(r.os, 'replace', denied)
    result = service.worker.run_once(SCOPE, Http(), forbidden, cycle, journal=journal)
    assert result['result'] == 'failed' and calls == ['claim']
    assert journal.failed and journal.path.read_bytes() == old


@pytest.mark.parametrize("journal_sha", [
    service.recovery.PRE_EXECUTION_JOURNAL_SOURCE_SHA256,
    service.recovery.PRE_EXECUTION_CORRECTED_JOURNAL_SOURCE_SHA256,
])
def test_edit_custody_worker_refuses_legacy_journal_pair(monkeypatch, journal_sha):
    r = service.recovery
    current_worker = Path(service.worker.__file__).read_bytes()
    current_journal = Path(r.__file__).read_bytes()
    assert r.sha(current_worker) == r.EDIT_CUSTODY_WORKER_SHA256
    monkeypatch.setattr(r, "runtime_source", lambda revision, path:
                        current_worker if path.endswith("native_preview_worker.py") else current_journal)
    assert r.pre_execution_source("a" * 40) == r.PRE_EXECUTION_CONNECTED_JOURNAL_SOURCE_SHA256
    monkeypatch.setattr(r, "journal_source_sha", lambda raw: journal_sha)
    with pytest.raises(service.worker.WorkerError):
        r.pre_execution_source("a" * 40)
