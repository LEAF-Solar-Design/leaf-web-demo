"""Dormant host-local supervision of the existing native preview worker.

No standalone activation is available in this slice. A trusted owner must supply
a context-manager session factory and install its lifecycle separately. This
module neither provisions credentials nor installs a scheduler. The lock only
excludes local consumers; the server lease remains the job authority. Uncertain
cycles require owner-supported reconciliation, which is deliberately absent here.
"""
from __future__ import annotations

import errno
import hashlib
import math
import os
from pathlib import Path
import tempfile

import native_preview_worker as worker

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
    payload = worker.encode({'scope_sha256': digest, 'phase': phase})
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
    accepted = None
    try:
        with session_factory(scope.copy()) as (http, native):
            entered = True
            if stop_event.is_set():
                return 'stopped'
            _checkpoint(checkpoint, digest, 'reconciliation-required')
            armed = True
            output = Path(tempfile.mkdtemp(prefix='cycle-', dir=directory))
            if stop_event.is_set():
                accepted = 'stopped'
            else:
                accepted = _accepted(worker.run_once(scope.copy(), http, native, output))
        # A context manager may suppress an exception. That is not evidence of
        # completion: accepted remains unset, and the marker remains durable.
        if armed:
            if accepted is None:
                return 'reconciliation-required'
            _checkpoint(checkpoint, digest, 'clean')
            return accepted
        return 'retry'
    except BaseException as error:
        if entered:
            # Includes context cleanup failures even before a claim could start.
            try:
                _checkpoint(checkpoint, digest, 'reconciliation-required')
            except BaseException:
                pass
            return 'reconciliation-required'
        return 'retry' if isinstance(error, Exception) else 'stopped'


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
                if worker.parse(checkpoint.read_bytes()) != clean:
                    return result('reconciliation-required')
            while not stop_event.is_set():
                outcome = _cycle(scope, session_factory, directory, checkpoint, digest, stop_event)
                if outcome in ('reconciliation-required', 'stopped'):
                    return result(outcome)
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
