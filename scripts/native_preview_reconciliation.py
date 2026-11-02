"""Closed local proofs and read-only evidence for service-owned recovery.

Nothing here claims, settles, extends a lease, or selects a cycle by scanning.
The caller must hold the consumer OS lock through collection and commit.
"""
from __future__ import annotations

import ast
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

import native_preview_worker as worker

LEGACY_RUNTIME = '7dc50e1b7e0ce40a2849134668693ff437985f0f'
CHECKPOINT_SCHEMA = 'leaf.native-preview-consumer-checkpoint.v1'
JOURNAL_SCHEMA = 'leaf.native-preview-consumer-journal.v1'
CANDIDATE_SCHEMA = 'leaf.native-preview-reconciliation-candidate.v1'
RECEIPT_SCHEMA = 'leaf.native-preview-reconciliation-receipt.v1'
MAX_LOCAL = 16384

# Reviewed ea666682 producer ordering. Bind content, never a mutable ref or
# this whole module: a separate recovery must not change the Journal witness.
PRE_EXECUTION_WORKER_SHA256 = 'd0fa9e9778490278988382f573555538a2fe204160f8d57da44958858b2cd89d'
# Current connected Native producer; hash derived after the frozen source change.
REVIEWED_WORKER_SHA256 = frozenset({'acc9c1219ee4246f6b39da6a3c439add24f6573c0e54626eca8b3752b790ee37', PRE_EXECUTION_WORKER_SHA256,
    '6ad5eb2b343e3d9f749769805001d74282519d6ce41cdf7996999573fa306197'})
PRE_EXECUTION_JOURNAL_SOURCE_SHA256 = 'a8f668f0f84866ebf342229c7b707f6b985a66ad256d71f71e0431fde1a5a599'
# Same ordering, with writable-descriptor receipt replay for Windows fsync.
PRE_EXECUTION_CORRECTED_JOURNAL_SOURCE_SHA256 = '3375d56dcc1609b6b160e5af39365a8afc1b60d9d05c4360a88971931aea6ac0'


class Unavailable(Exception):
    """Only an already proven journal may wait for external evidence."""


def need(condition):
    worker.require(condition)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    need(type(value) is str and worker.SHA.fullmatch(value) is not None)
    return value


def source(value):
    if type(value) is dict and value.get('schema') == worker.NATIVE_SCHEMA:
        return worker.validate_native_identity(value)
    need(type(value) is dict and set(value) == {'revision', 'document_sha256', 'archive_sha256'})
    need(type(value['revision']) is int and 1 <= value['revision'] <= 2147483647)
    digest(value['document_sha256'])
    digest(value['archive_sha256'])
    return value


def identity(value):
    worker.canonical_uuid(value['job_id'])
    need(type(value['attempt']) is int and 1 <= value['attempt'] <= 2147483647)


def finite(value):
    need(type(value) in (int, float) and math.isfinite(value))


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read(path, limit=MAX_LOCAL):
    path = Path(path)
    need(not path.is_symlink())
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode))
        raw = stream.read(limit + 1)
    need(len(raw) <= limit)
    return raw


def immutable(path, raw):
    need(len(raw) <= MAX_LOCAL)
    try:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        need(read(path) == raw)
        # Windows requires a writable descriptor for fsync; r+b never truncates.
        with path.open('r+b') as stream:
            need(stream.read(MAX_LOCAL + 1) == raw)
            os.fsync(stream.fileno())
    sync_directory(path.parent)


def runtime_commit(revision):
    # Resolve an existing object only; no fetch or mutable abbreviated identity.
    result = subprocess.run(['git', 'rev-parse', '--verify', revision + '^{commit}'],
                            cwd=Path(__file__).resolve().parents[1],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    need(result.returncode == 0 and len(result.stdout) <= 41)
    value = result.stdout.decode('ascii').strip()
    need(re.fullmatch('[0-9a-f]{40}', value) is not None)
    if revision != 'HEAD':
        need(value == revision)
    return value


def cycle_path(directory, name):
    need(type(name) is str and re.fullmatch(r'cycle-[A-Za-z0-9_-]{1,100}', name) is not None)
    path = directory / name
    need(path.is_dir() and not path.is_symlink() and path.resolve().parent == directory.resolve())
    return path


def validate_events(events):
    need(type(events) is list and len(events) <= 5)
    seen = []
    for event in events:
        need(type(event) is dict and type(event.get('event')) is str)
        kind = event['event']
        need(kind not in seen)
        if kind == 'claim':
            need(not seen and set(event) == {'event', 'job_id', 'attempt', 'source', 'deadline'})
            identity(event)
            source(event['source'])
            finite(event['deadline'])
        elif kind == 'allocator':
            need(seen == ['claim'] and set(event) == {'event', 'ticket', 'marker_sha256'})
            need(type(event['ticket']) is str and worker.SAFE.fullmatch(event['ticket']) is not None)
            digest(event['marker_sha256'])
        elif kind == 'producer':
            need(seen == ['claim', 'allocator'] and
                 event == {'event': 'producer', 'producer_may_have_started': True}
                 and event['producer_may_have_started'] is True)
        elif kind == 'cleanup':
            need(seen in (['claim', 'allocator'], ['claim', 'allocator', 'producer']))
            need(set(event) == {'event', 'cleanup_complete'} and type(event['cleanup_complete']) is bool)
        elif kind == 'release':
            need(seen in (['claim', 'allocator'], ['claim', 'allocator', 'cleanup'],
                          ['claim', 'allocator', 'producer', 'cleanup']))
            need(set(event) == {'event', 'allocator_release_confirmed'} and
                 type(event['allocator_release_confirmed']) is bool)
        else:
            need(False)
        seen.append(kind)
    return seen


class Journal:
    def __init__(self, cycle, scope_digest, directory):
        self.path = cycle / 'journal.json'
        self.directory = directory
        self.failed = False
        self.value = {'schema': JOURNAL_SCHEMA, 'scope_sha256': scope_digest,
                      'cycle': cycle.name, 'runtime_commit': runtime_commit('HEAD'),
                      'claim_state': 'unknown', 'events': []}
        immutable(self.path, worker.encode(self.value))

    def __call__(self, event):
        need(not self.failed)
        try:
            need(read(self.path) == worker.encode(self.value))
            events = self.value['events'] + [event]
            validate_events(events)
            if event['event'] == 'claim':
                # Exact receipt lookup also prevents replay after a clean restart.
                receipt = receipt_path(self.directory, event['job_id'])
                need(not receipt.exists() and not receipt.is_symlink())
            value = {**self.value, 'events': events, 'claim_state': 'validated'}
            raw = worker.encode(value)
            need(len(raw) <= MAX_LOCAL)
            fd, name = tempfile.mkstemp(prefix='.journal-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
                sync_directory(self.path.parent)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.value = value
        except BaseException:
            self.failed = True
            raise worker.WorkerError('journal-failed') from None


def allocator_witness(value, job_id, attempt):
    need(type(value) is dict and set(value) == {'job_id', 'attempt', 'prefix', 'ticket',
         'event_times', 'active_match_counts', 'history_complete', 'witness_sha256'})
    identity(value)
    need(value['job_id'] == job_id and value['attempt'] == attempt)
    need(value['prefix'] == 'preview-' + job_id + '-' + str(attempt) + '-')
    need(type(value['ticket']) is str and worker.SAFE.fullmatch(value['ticket']) is not None)
    need(type(value['active_match_counts']) is list and
         len(value['active_match_counts']) == 2 and
         all(type(v) is int and v == 0 for v in value['active_match_counts']))
    need(value['history_complete'] is True)
    times = value['event_times']
    names = ('grant', 'release', 'confirm_dead', 'free')
    need(type(times) is dict and set(times) == set(names))
    for stamp in times.values():
        finite(stamp)
    need(all(times[a] <= times[b] for a, b in zip(names, names[1:])))
    digest(value['witness_sha256'])
    need(value['witness_sha256'] == sha(worker.encode(
        {k: v for k, v in value.items() if k != 'witness_sha256'})))
    return value


def disposition(raw, scope, claim):
    need(type(raw) is bytes and len(raw) <= MAX_LOCAL)
    value = worker.parse(raw)
    base = {'job_id', 'status'} | set(claim['source'])
    need(type(value) is dict and set(value) in (base, base | {'failure_disposition'}))
    need(value['job_id'] == claim['job_id'])
    need(source({k: value[k] for k in claim['source']}) == claim['source'])
    need(type(value['status']) is str and value['status'] in
         ('queued', 'running', 'failed', 'cancelled', 'complete'))
    if 'failure_disposition' not in value:
        need(value['status'] in ('queued', 'running', 'failed'))
        raise Unavailable()
    proof = value['failure_disposition']
    need(type(proof) is dict and set(proof) == {'schema', 'job_id', 'attempt', 'org_id',
         'project_id', 'status', 'reason', 'source', 'started_at', 'finished_at',
         'lease_cleared', 'disposition_sha256'})
    identity(proof)
    need(proof['schema'] == 'leaf.native-preview.failed-disposition.v1')
    need(all(proof[k] == claim[k] for k in ('job_id', 'attempt')))
    need(all(proof[k] == scope[k] for k in ('org_id', 'project_id')))
    need(source(proof['source']) == claim['source'])
    need(value['status'] == proof['status'] == 'failed' and
         proof['reason'] == 'execution_budget_expired' and proof['lease_cleared'] is True)
    finite(proof['started_at'])
    finite(proof['finished_at'])
    need(proof['started_at'] <= proof['finished_at'])
    digest(proof['disposition_sha256'])
    need(proof['disposition_sha256'] == sha(worker.encode(
        {k: v for k, v in proof.items() if k != 'disposition_sha256'})))
    return proof


def receipt_path(directory, job_id):
    worker.canonical_uuid(job_id)
    return directory / ('reconciliation-' + job_id + '.json')


def settled_failure_status(raw, claim):
    # Attempt authority is the source-proven local accepted callback, not a
    # synthetic server disposition. GET only corroborates current job/source.
    need(type(raw) is bytes and len(raw) <= MAX_LOCAL)
    value = worker.parse(raw)
    need(type(value) is dict and set(value) ==
         ({'job_id', 'status'} | set(claim['source'])))
    need(value['job_id'] == claim['job_id'])
    need(source({k: value[k] for k in claim['source']}) == claim['source'])
    need(type(value['status']) is str)
    if value['status'] in ('queued', 'running'):
        raise Unavailable()
    need(value['status'] == 'failed')
    return {'backend_status_sha256': sha(raw)}


class LocalProof:
    def __init__(self, directory, checkpoint, scope_digest):
        self.directory, self.checkpoint = directory, checkpoint
        self.files = {}
        self.settled_producer = False
        self.scope_digest = scope_digest
        old = self.capture(checkpoint)
        value = worker.parse(old)
        self.legacy = value == {'scope_sha256': scope_digest, 'phase': 'reconciliation-required'}
        if self.legacy:
            candidate = worker.parse(self.capture(directory / 'reconciliation-candidate.json'))
            need(type(candidate) is dict and set(candidate) == {'schema', 'scope_sha256', 'cycle',
                 'job_id', 'attempt', 'source', 'original_runtime_commit'})
            need(candidate['schema'] == CANDIDATE_SCHEMA and candidate['scope_sha256'] == scope_digest)
            need(candidate['original_runtime_commit'] == LEGACY_RUNTIME)
            self.runtime = runtime_commit(LEGACY_RUNTIME)
            identity(candidate)
            source(candidate['source'])
            self.claim = {k: candidate[k] for k in ('job_id', 'attempt', 'source')}
            self.cycle = cycle_path(directory, candidate['cycle'])
        else:
            need(type(value) is dict and set(value) == {'schema', 'scope_sha256', 'phase', 'cycle'})
            need(value['schema'] == CHECKPOINT_SCHEMA and value['scope_sha256'] == scope_digest
                 and value['phase'] == 'reconciliation-required')
            self.cycle = cycle_path(directory, value['cycle'])
            cycle_stat = (self.cycle.stat().st_dev, self.cycle.stat().st_ino)
            journal = worker.parse(self.capture(self.cycle / 'journal.json'))
            need(type(journal) is dict and set(journal) ==
                 {'schema', 'scope_sha256', 'cycle', 'runtime_commit', 'claim_state', 'events'})
            need(journal['schema'] == JOURNAL_SCHEMA and journal['scope_sha256'] == scope_digest
                 and journal['cycle'] == self.cycle.name and journal['claim_state'] == 'validated')
            need(type(journal['runtime_commit']) is str and
                 re.fullmatch('[0-9a-f]{40}', journal['runtime_commit']) is not None)
            self.runtime = runtime_commit(journal['runtime_commit'])
            events = validate_events(journal['events'])
            self.settled_producer = events == ['claim', 'allocator', 'producer', 'cleanup', 'release']
            need(self.settled_producer or events == ['claim', 'allocator', 'cleanup', 'release'])
            need(journal['events'][-2]['cleanup_complete'] is True and
                 journal['events'][-1]['allocator_release_confirmed'] is True)
            self.claim = journal['events'][0]
            self.ticket = journal['events'][1]['ticket']
        self.cycle_stat = (self.cycle.stat().st_dev, self.cycle.stat().st_ino)
        name = self.claim['job_id'] + '-' + str(self.claim['attempt']) + '-failure.json'
        self.failure_path = self.cycle / name
        self.names = sorted(p.name for p in self.cycle.iterdir())
        need(self.names == sorted([name] + ([] if self.legacy else ['journal.json'])))
        failure = worker.parse(self.capture(self.failure_path))
        fields = {'stage', 'result', 'job_id', 'archive_sha256', 'execution_stage',
                  'error_code', 'authority_failure', 'failure_settlement', 'cleanup_complete',
                  'allocator_release_confirmed'}
        self.build_artifact = None
        if self.claim['source'].get('schema') == worker.NATIVE_SCHEMA and 'build_artifact' in failure:
            fields.add('build_artifact')
            self.build_artifact = failure['build_artifact']
            need(type(self.build_artifact) is dict and self.build_artifact.get('source') == self.claim['source'])
        need(type(failure) is dict and set(failure) == fields)
        need(failure['stage'] == 'execution' and failure['result'] == 'failed'
             and failure['job_id'] == self.claim['job_id'] and
             failure['archive_sha256'] == self.claim['source']['archive_sha256'])
        if self.settled_producer:
            need(failure['execution_stage'] == 'mac-producer')
        else:
            need(failure['execution_stage'] == 'mac-permit-refresh' if self.legacy else
                 failure['execution_stage'] in ('mac-bootstrap', 'mac-permit-refresh'))
        need(type(failure['error_code']) is str and failure['error_code'] in
             worker.ERROR_CODES | {'unexpected'})
        if self.legacy:
            need(failure['error_code'] != 'journal-failed')
        need(type(failure['authority_failure']) is str and failure['authority_failure'] in worker.AUTHORITY_FAILURES)
        if self.settled_producer:
            need(failure['authority_failure'] == 'none' and failure['failure_settlement'] == 'accepted')
            need(failure['error_code'] not in ('completion-unknown', 'authority-lost', 'journal-failed'))
            need(not directory.is_symlink() and self.cycle_stat == cycle_stat)
            self.journal_source_sha256 = pre_execution_source(self.runtime)
            self.worker_sha256 = reviewed_worker_source(self.runtime)
            self.stats = {path: PreExecutionProof.file_stat(path) for path in self.files}
            need(all(read(path) == raw for path, raw in self.files.items()))
        else:
            need(failure['failure_settlement'] == 'not-attempted-authority-lost')
        need(failure['cleanup_complete'] is True and failure['allocator_release_confirmed'] is True)

    def capture(self, path):
        raw = read(path)
        self.files[path] = raw
        return raw

    def recheck(self, scope):
        need(sha(worker.encode(worker.binding(scope))) == self.scope_digest)
        need(cycle_path(self.directory, self.cycle.name) == self.cycle)
        need((self.cycle.stat().st_dev, self.cycle.stat().st_ino) == self.cycle_stat)
        need(sorted(p.name for p in self.cycle.iterdir()) == self.names)
        for path, raw in self.files.items():
            need(read(path) == raw)
        if self.settled_producer:
            need(not self.directory.is_symlink())
            for path, expected in self.stats.items():
                need(PreExecutionProof.file_stat(path) == expected)
            need(pre_execution_source(self.runtime) == self.journal_source_sha256)

    def collect(self, scope, session_factory):
        if self.build_artifact is not None:
            worker.validate_native_artifact(self.build_artifact, dict(self.claim['source'], project_id=scope['project_id']))
        try:
            return self._collect(scope, session_factory)
        except (worker.WorkerError, Unavailable):
            raise
        except Exception:
            raise Unavailable() from None

    def _collect(self, scope, session_factory):
        backend = allocator = None
        # Assignment is not sufficient: suppressed errors and failed __exit__
        # must never produce commit authority.
        closed = False
        with session_factory(scope.copy()) as (http, native):
            try:
                raw = http.status(self.claim['job_id'])
            except Exception:
                raise Unavailable() from None
            backend = (settled_failure_status(raw, self.claim) if self.settled_producer else
                       disposition(raw, scope, self.claim))
            try:
                observed = native.read_reconciliation_allocator(self.claim['job_id'], self.claim['attempt'])
            except worker.WorkerError as error:
                if worker.error_code(error) == 'mac-transport-failed':
                    raise Unavailable() from None
                raise
            except Exception:
                raise Unavailable() from None
            allocator = allocator_witness(observed, self.claim['job_id'], self.claim['attempt'])
            if not self.legacy:
                need(allocator['ticket'] == self.ticket)
            closed = True
        need(closed and backend is not None and allocator is not None)
        return backend, allocator

    def commit(self, scope, backend, allocator, clean_writer):
        self.recheck(scope)
        receipt = {'schema': RECEIPT_SCHEMA, 'scope_sha256': self.scope_digest,
                   'cycle': self.cycle.name,
                   **{k: self.claim[k] for k in ('job_id', 'attempt', 'source')},
                   'original_runtime_commit': self.runtime,
                   'checkpoint_sha256': sha(self.files[self.checkpoint]),
                   'original_checkpoint': self.files[self.checkpoint].decode('utf-8'),
                   'failure_sha256': sha(self.files[self.failure_path]),
                   'local_evidence_sha256': sha(worker.encode(
                       {p.name: sha(raw) for p, raw in self.files.items()})),
                   'allocator_witness_sha256': allocator['witness_sha256'],
                   'outcome': 'failed-pre-producer-reconciled'}
        if self.settled_producer:
            receipt.update(outcome='failed-producer-settled-reconciled',
                           backend_status_sha256=backend['backend_status_sha256'],
                           attempt_binding='local-accepted-failure-settlement',
                           worker_sha256=self.worker_sha256,
                           journal_source_sha256=self.journal_source_sha256)
        else:
            receipt['disposition_sha256'] = backend['disposition_sha256']
        immutable(receipt_path(self.directory, self.claim['job_id']), worker.encode(receipt))
        self.recheck(scope)
        need(read(self.checkpoint) == self.files[self.checkpoint])
        clean_writer(self.checkpoint, self.scope_digest, 'clean')


def journal_source_sha(raw):
    names = {'Journal', 'need', 'sha', 'digest', 'source', 'identity', 'finite',
             'sync_directory', 'read', 'immutable', 'runtime_commit',
             'validate_events', 'receipt_path', 'MAX_LOCAL', 'JOURNAL_SCHEMA'}
    text = raw.decode('utf-8').replace('\r\n', '\n')
    tree = ast.parse(text)
    nodes = [node for node in tree.body if getattr(node, 'name', None) in names or
             isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
             target.id in names for target in node.targets)]
    # AST selects reviewed definitions; interpreter-specific AST serialization
    # is not a source identity (its empty-field representation changes).
    segments = [ast.get_source_segment(text, node) for node in nodes]
    need(all(type(segment) is str for segment in segments))
    return sha(b'leaf.native-preview-journal-source.v1\0' +
               '\n\n'.join(segments).encode('utf-8'))


def runtime_source(revision, path):
    # revision is already a full, verified commit. No network or ref fallback.
    result = subprocess.run(['git', 'show', revision + ':' + path],
                            cwd=Path(__file__).resolve().parents[1],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    need(result.returncode == 0 and len(result.stdout) <= 262144)
    return result.stdout


def reviewed_worker_source(revision):
    observed = sha(runtime_source(revision, 'scripts/native_preview_worker.py'))
    need(observed in REVIEWED_WORKER_SHA256)
    return observed


def pre_execution_source(revision):
    paths = ('scripts/native_preview_worker.py', 'scripts/native_preview_reconciliation.py')
    root = Path(__file__).resolve().parents[1]
    original = [runtime_source(revision, path) for path in paths]
    current = [read(root / path, 262144) for path in paths]
    for producer, journal in (original, current):
        need(sha(producer) in REVIEWED_WORKER_SHA256)
        need(journal_source_sha(journal) in (PRE_EXECUTION_JOURNAL_SOURCE_SHA256,
                                        PRE_EXECUTION_CORRECTED_JOURNAL_SOURCE_SHA256))
    return journal_source_sha(original[1])


class PreExecutionProof:
    """No durable claim event means no heartbeat/source/native in reviewed code.

    A claim POST may still have committed. This proof carries no job identity
    and grants no backend settlement, allocator release or preview completion.
    """
    def __init__(self, directory, checkpoint, scope_digest):
        self.directory, self.checkpoint = directory, checkpoint
        self.scope_digest = scope_digest
        self.files, self.stats = {}, {}
        need(not directory.is_symlink())
        value = worker.parse(self.capture(checkpoint))
        need(type(value) is dict and set(value) == {'schema', 'scope_sha256', 'phase', 'cycle'})
        need(value['schema'] == CHECKPOINT_SCHEMA and value['scope_sha256'] == scope_digest
             and value['phase'] == 'reconciliation-required')
        self.cycle = cycle_path(directory, value['cycle'])
        self.cycle_stat = (self.cycle.stat().st_dev, self.cycle.stat().st_ino)
        self.names = ['journal.json']
        need(sorted(p.name for p in self.cycle.iterdir()) == self.names)
        self.journal_path = self.cycle / 'journal.json'
        journal = worker.parse(self.capture(self.journal_path))
        need(type(journal) is dict and set(journal) ==
             {'schema', 'scope_sha256', 'cycle', 'runtime_commit', 'claim_state', 'events'})
        need(journal['schema'] == JOURNAL_SCHEMA and journal['scope_sha256'] == scope_digest
             and journal['cycle'] == self.cycle.name and journal['claim_state'] == 'unknown'
             and type(journal['events']) is list and journal['events'] == [])
        need(type(journal['runtime_commit']) is str and
             re.fullmatch('[0-9a-f]{40}', journal['runtime_commit']) is not None)
        self.runtime = runtime_commit(journal['runtime_commit'])
        self.journal_source_sha256 = pre_execution_source(self.runtime)
        self.worker_sha256 = reviewed_worker_source(self.runtime)

    @staticmethod
    def file_stat(path):
        value = path.lstat()
        need(stat.S_ISREG(value.st_mode))
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def capture(self, path):
        before = self.file_stat(path)
        raw = read(path)
        need(self.file_stat(path) == before)
        self.files[path] = raw
        self.stats[path] = before
        return raw

    def recheck(self, scope):
        need(not self.directory.is_symlink())
        need(sha(worker.encode(worker.binding(scope))) == self.scope_digest)
        need(cycle_path(self.directory, self.cycle.name) == self.cycle)
        need((self.cycle.stat().st_dev, self.cycle.stat().st_ino) == self.cycle_stat)
        need(sorted(p.name for p in self.cycle.iterdir()) == self.names)
        for path, raw in self.files.items():
            need(read(path) == raw)
        for path, expected in self.stats.items():
            need(self.file_stat(path) == expected)
        need(pre_execution_source(self.runtime) == self.journal_source_sha256)

    def commit(self, scope, clean_writer):
        self.recheck(scope)
        receipt = {'schema': RECEIPT_SCHEMA, 'scope_sha256': self.scope_digest,
                   'cycle': self.cycle.name, 'original_runtime_commit': self.runtime,
                   'original_checkpoint': self.files[self.checkpoint].decode('utf-8'),
                   'checkpoint_sha256': sha(self.files[self.checkpoint]),
                   'journal_sha256': sha(self.files[self.journal_path]),
                   'local_evidence_sha256': sha(worker.encode(
                       {p.name: sha(raw) for p, raw in self.files.items()})),
                   'worker_sha256': self.worker_sha256,
                   'journal_source_sha256': self.journal_source_sha256,
                   'outcome': 'claim-unconfirmed-pre-execution',
                   'backend_settlement': 'unconfirmed'}
        path = self.directory / ('reconciliation-' + self.cycle.name + '.json')
        immutable(path, worker.encode(receipt))
        self.recheck(scope)
        clean_writer(self.checkpoint, self.scope_digest, 'clean')
