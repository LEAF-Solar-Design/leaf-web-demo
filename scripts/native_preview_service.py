"""Dormant host-local supervision of the existing native preview worker.

No standalone activation is available in this slice. A trusted owner must supply
a context-manager session factory and install its lifecycle separately. This
module neither provisions credentials nor installs a scheduler. The lock only
excludes local consumers; the server lease remains the job authority. Uncertain
cycles require exact local and authenticated reconciliation evidence.
"""
from __future__ import annotations

import errno
import hashlib
import math
import os
from pathlib import Path
import tempfile

import native_preview_worker as worker
import native_preview_reconciliation as recovery

MAX_WAIT_SECONDS = 3600.0


def _wait_setting(value):
    if (type(value) not in (int, float) or not 0 < value <= MAX_WAIT_SECONDS
            or not math.isfinite(value)):
        raise ValueError('invalid-wait')
    return float(value)


def _lock(stream):
    if os.name == 'nt':
        import msvcrt
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
    else:
        import fcntl
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
    return True


def _checkpoint(path, digest, phase):
    """Replace and sync before claiming; never store session or job payloads."""
    payload = worker.encode(phase if type(phase) is dict else
                            {'scope_sha256': digest, 'phase': phase})
    fd, name = tempfile.mkstemp(prefix='.checkpoint-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _accepted(result):
    if type(result) is not dict:
        return None
    if result == {'stage': 'empty', 'result': 'empty'}:
        return 'empty'
    if (set(result) != {'stage', 'result', 'job_id', 'archive_sha256', 'screenshot_sha256'}
            or result['stage'] != 'settled' or result['result'] != 'complete'):
        return None
    try:
        worker.canonical_uuid(result['job_id'])
    except worker.WorkerError:
        return None
    if not all(type(result[key]) is str and worker.SHA.fullmatch(result[key])
               for key in ('archive_sha256', 'screenshot_sha256')):
        return None
    return 'complete'


def _cycle(scope, session_factory, directory, checkpoint, digest, stop_event):
    entered = False
    armed = False
    initialization_failed = False
    accepted = None
    output = None
    try:
        with session_factory(scope.copy()) as (http, native):
            entered = True
            if stop_event.is_set():
                return 'stopped'
            output = Path(tempfile.mkdtemp(prefix='cycle-', dir=directory))
            try:
                journal = recovery.Journal(output, digest, directory)
            except BaseException as error:
                # This new invocation has not reached run_once or its claim POST.
                # Preserve partial journal bytes and the prior checkpoint; this
                # evidence grants no recovery authority for historical cycles.
                initialization_failed = True
                recovery.immutable(output / 'initialization-failure.json', worker.encode({
                    'schema': 'leaf.native-preview-initialization-failure.v1',
                    'scope_sha256': digest, 'cycle': output.name,
                    'stage': 'journal-initialization', 'worker_invoked': False}))
                return 'retry' if isinstance(error, Exception) else 'stopped'
            dirty = {'schema': recovery.CHECKPOINT_SCHEMA, 'scope_sha256': digest,
                     'phase': 'reconciliation-required', 'cycle': output.name}
            _checkpoint(checkpoint, digest, dirty)
            armed = True
            if stop_event.is_set():
                accepted = 'stopped'
            else:
                accepted = _accepted(worker.run_once(scope.copy(), http, native, output,
                                                     journal=journal))
                if journal.failed:
                    accepted = None
        # A context manager may suppress an exception. That is not evidence of
        # completion: accepted remains unset, and the marker remains durable.
        if armed:
            if accepted is None:
                return 'reconciliation-required'
            recovery.need(recovery.read(checkpoint) == worker.encode(dirty))
            recovery.need(recovery.cycle_path(directory, output.name) == output)
            recovery.need(recovery.read(journal.path) == worker.encode(journal.value))
            _checkpoint(checkpoint, digest, 'clean')
            return accepted
        return 'retry'
    except BaseException as error:
        if entered:
            if initialization_failed:
                # Evidence-write or session-close failure is still uncertain,
                # but cannot replace a valid predecessor with a missing journal.
                return 'reconciliation-required'
            # Includes context cleanup failures even before a claim could start.
            try:
                if not armed:
                    # Never manufacture a legacy checkpoint on a new journal
                    # failure: a stale explicit legacy candidate cannot apply.
                    _checkpoint(checkpoint, digest, {'schema': recovery.CHECKPOINT_SCHEMA,
                                'scope_sha256': digest, 'phase': 'reconciliation-required',
                                'cycle': output.name if output is not None else 'cycle-unavailable'})
            except BaseException:
                pass
            return 'reconciliation-required'
        return 'retry' if isinstance(error, Exception) else 'stopped'


def _recover(scope, session_factory, directory, checkpoint, digest, stop_event, error_seconds):
    try:
        proof = recovery.PreExecutionProof(directory, checkpoint, digest)
    except BaseException:
        pass
    else:
        try:
            if stop_event.is_set():
                return 'stopped'
            proof.commit(scope, _checkpoint)
            return 'pre-execution-recovered'
        except BaseException:
            return 'reconciliation-required'
    try:
        proof = recovery.LocalProof(directory, checkpoint, digest)
    except BaseException:
        return 'reconciliation-required'
    while not stop_event.is_set():
        try:
            proof.recheck(scope)
            backend, allocator = proof.collect(scope, session_factory)
            if stop_event.is_set():
                return 'stopped'
            proof.commit(scope, backend, allocator, _checkpoint)
            return 'recovered'
        except recovery.Unavailable:
            if proof.legacy:
                return 'reconciliation-required'
            stop_event.wait(error_seconds)
        except BaseException:
            return 'reconciliation-required'
    return 'stopped'


def serve(scope, session_factory, output_root, stop_event, *, poll_seconds=5.0,
          error_seconds=30.0):
    """Run serial fresh sessions until stopped or reconciliation is required.

    Waits are finite, positive, and at most one hour. An active run_once is
    allowed to finish its bounded authority and cleanup before stop is honored.
    Only fixed states and a completed integer count cross this boundary.
    """
    scope = worker.binding(scope)
    poll_seconds = _wait_setting(poll_seconds)
    error_seconds = _wait_setting(error_seconds)
    completed = 0

    def result(state):
        return {'state': state, 'completed': completed}

    if stop_event.is_set():
        return result('stopped')
    digest = hashlib.sha256(worker.encode(scope)).hexdigest()
    directory = Path(output_root) / digest
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Closing the descriptor releases the real OS lock on every exit. Never
        # unlink its inode: an old or young lock file says nothing about its owner.
        with (directory / 'consumer.lock').open('a+b') as lock:
            if not _lock(lock):
                return result('busy')
            checkpoint = directory / 'consumer.json'
            if checkpoint.exists():
                clean = {'scope_sha256': digest, 'phase': 'clean'}
                if worker.parse(recovery.read(checkpoint)) != clean:
                    outcome = _recover(scope, session_factory, directory, checkpoint,
                                       digest, stop_event, error_seconds)
                    if outcome == 'pre-execution-recovered':
                        stop_event.wait(error_seconds)
                    elif outcome != 'recovered':
                        return result(outcome)
            while not stop_event.is_set():
                outcome = _cycle(scope, session_factory, directory, checkpoint, digest, stop_event)
                if outcome == 'reconciliation-required':
                    outcome = _recover(scope, session_factory, directory, checkpoint,
                                       digest, stop_event, error_seconds)
                if outcome in ('reconciliation-required', 'stopped'):
                    return result(outcome)
                if outcome == 'recovered':
                    continue
                if outcome == 'pre-execution-recovered':
                    stop_event.wait(error_seconds)
                    continue
                if outcome == 'complete':
                    completed += 1
                elif outcome == 'empty':
                    stop_event.wait(poll_seconds)
                else:
                    stop_event.wait(error_seconds)
            return result('stopped')
    except BaseException:
        # Filesystem, lock and checkpoint errors cannot expose arbitrary text.
        return result('reconciliation-required')
