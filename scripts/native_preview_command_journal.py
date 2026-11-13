"""Dormant per-session command decisions, not an effect or authority producer.

The caller owns an exclusive private directory and authenticated context/results.
SQLite commits pending intent before returning dispatch=True. A crash after that
commit is uncertain, never permission to dispatch twice. Native execution and
membership, evidence and inspector-handle verification remain caller-owned.
"""
from contextlib import contextmanager
import copy
import functools
import hashlib
import json
from pathlib import Path
import sqlite3

import native_preview_session_contract as contract


class JournalError(RuntimeError):
    def __init__(self):
        super().__init__('Native session journal unavailable or conflicting.')


def _need(condition):
    if not condition:
        raise JournalError()


def _safe(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pass
        raise JournalError() from None
    return call


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CommandJournal:
    @_safe
    def __init__(self, directory, context):
        self.context = copy.deepcopy(context)
        # This is the same frozen context contract used by the command parser.
        contract._context(self.context, 0)
        self.binding = _sha(_encode(self.context))
        self.directory = Path(directory)
        _need(self.directory.is_absolute() and not self.directory.is_symlink())
        created = False
        try:
            self.directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            _need(self.directory.is_dir())
        self.path = self.directory / 'commands.sqlite'
        with self._transaction(create=created) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS session '
                               '(id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL, '
                               'sequence INTEGER NOT NULL, phase TEXT NOT NULL, teardown TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS commands '
                               '(request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, '
                               'sequence INTEGER NOT NULL, operation TEXT NOT NULL, '
                               'status TEXT NOT NULL, evidence TEXT)')
            if connection.execute('SELECT id FROM session').fetchone() is None:
                _need(created)
                connection.execute('INSERT INTO session VALUES (1,?,?,?,NULL)',
                                   (self.binding, self.context['next_sequence'], self.context['state']))
            self._metadata(connection)

    @contextmanager
    def _transaction(self, *, create=False):
        _need(not self.directory.is_symlink() and not self.path.is_symlink())
        # Only an exclusively created directory may initialize state. Missing
        # state in an existing session is uncertainty, never a fresh lease.
        _need(create or self.path.is_file())
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            self._commit(connection)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _commit(self, connection):
        connection.commit()

    def _metadata(self, connection):
        _need(_sha(_encode(self.context)) == self.binding)
        rows = connection.execute('SELECT binding,sequence,phase,teardown FROM session').fetchall()
        _need(len(rows) == 1)
        binding, sequence, phase, teardown = rows[0]
        _need(binding == self.binding and type(sequence) is int and sequence > 0
              and phase in ('active', 'closing', 'closed'))
        if teardown is not None:
            contract._digest(teardown, 64)
        _need((phase == 'closed') == (teardown is not None))
        return sequence, phase, teardown

    def _record(self, row):
        request_id, digest, sequence, operation, status, evidence = row
        contract._uuid(request_id)
        contract._digest(digest, 64)
        _need(type(sequence) is int and sequence > 0 and operation in contract.OPERATIONS
              and status in ('pending', 'unknown', 'complete', 'failed'))
        if evidence is not None:
            contract._digest(evidence, 64)
        _need((status == 'pending') == (evidence is None))
        return dict(request_id=request_id, command_sha256=digest, sequence=sequence,
                    operation=operation, status=status, evidence_sha256=evidence)

    def _find(self, connection, request_id):
        row = connection.execute('SELECT * FROM commands WHERE request_id=?', (request_id,)).fetchone()
        return self._record(row) if row is not None else None

    @_safe
    def prepare(self, raw, *, now):
        _need(type(raw) is bytes and 0 < len(raw) <= contract.MAX_COMMAND)
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=contract._pairs,
                            parse_constant=contract._constant)
        _need(type(parsed) is dict and _encode(parsed) == raw)
        request_id = parsed.get('request_id')
        contract._uuid(request_id)
        digest = _sha(raw)
        with self._transaction() as connection:
            sequence, phase, _ = self._metadata(connection)
            previous = self._find(connection, request_id)
            if previous is not None:
                # A cached decision never authorizes another effect, even after
                # expiry/close or when the accepted sequence has advanced.
                _need(previous['command_sha256'] == digest)
                result = dict(previous, dispatch=False)
            else:
                _need(phase != 'closed')
                current = dict(self.context, next_sequence=sequence, state=phase)
                value = contract.validate_command(raw, current, now=now)
                _need(connection.execute("SELECT 1 FROM commands WHERE status IN ('pending','unknown')").fetchone() is None)
                connection.execute('INSERT INTO commands VALUES (?,?,?,?,?,NULL)',
                                   (request_id, digest, sequence, value['operation'], 'pending'))
                if value['operation'] == 'close':
                    connection.execute("UPDATE session SET phase='closing' WHERE id=1")
                result = dict(self._find(connection, request_id), dispatch=True)
        # No dispatch decision escapes a failed commit.
        return result

    @_safe
    def finish(self, request_id, command_sha256, outcome, evidence_sha256):
        contract._uuid(request_id)
        contract._digest(command_sha256, 64)
        contract._digest(evidence_sha256, 64)
        _need(type(outcome) is str and outcome in ('complete', 'failed', 'unknown'))
        with self._transaction() as connection:
            sequence, phase, _ = self._metadata(connection)
            previous = self._find(connection, request_id)
            _need(previous is not None and previous['command_sha256'] == command_sha256)
            if previous['status'] in ('complete', 'failed'):
                _need(previous['status'] == outcome and previous['evidence_sha256'] == evidence_sha256)
            else:
                _need(phase != 'closed' and previous['sequence'] == sequence)
                connection.execute('UPDATE commands SET status=?,evidence=? WHERE request_id=?',
                                   (outcome, evidence_sha256, request_id))
                if outcome != 'unknown':
                    connection.execute('UPDATE session SET sequence=sequence+1 WHERE id=1')
            result = self._find(connection, request_id)
        return result

    @_safe
    def start_closing(self):
        """Record owner-driven teardown, even with an unresolved app command.

        This performs no cleanup and clears no pending evidence. It lets the
        future owner stop app effects immediately after expiry or revocation.
        """
        with self._transaction() as connection:
            _, phase, _ = self._metadata(connection)
            if phase != 'closed':
                connection.execute("UPDATE session SET phase='closing' WHERE id=1")

    @_safe
    def close(self, proof):
        fields = {'session_id', 'context_sha256', 'allocator_ticket', 'process_extinct',
                  'cleanup_complete', 'allocator_release_confirmed'}
        _need(type(proof) is dict and set(proof) == fields
              and proof['session_id'] == self.context['session_id']
              and proof['context_sha256'] == self.binding
              and proof['allocator_ticket'] == self.context['allocator_ticket']
              and all(proof[key] is True for key in
                      ('process_extinct', 'cleanup_complete', 'allocator_release_confirmed')))
        digest = _sha(_encode(proof))
        with self._transaction() as connection:
            _, phase, previous = self._metadata(connection)
            _need(phase in ('closing', 'closed') and (previous is None or previous == digest))
            connection.execute("UPDATE session SET phase='closed',teardown=? WHERE id=1", (digest,))

    @_safe
    def snapshot(self):
        with self._transaction() as connection:
            sequence, phase, teardown = self._metadata(connection)
            rows = connection.execute('SELECT * FROM commands ORDER BY sequence,request_id').fetchall()
            result = dict(context_sha256=self.binding, next_sequence=sequence, phase=phase,
                          teardown_sha256=teardown, commands=[self._record(row) for row in rows])
        return result
