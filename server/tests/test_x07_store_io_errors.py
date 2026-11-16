"""Injected SQLITE_IOERR at explicit seams, including after durable commit.

These are connection-level failure injections, not a claim of real disk faults.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES, INTAKE
from x07_workflow_adapter import WorkflowAdapter


def io_error():
    error = sqlite3.OperationalError('injected SQLite I/O failure')
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR
    error.sqlite_errorname = 'SQLITE_IOERR'
    return error


class WriteFault:
    def __init__(self, db, table, phase):
        self.db, self.table, self.phase = db, table, phase
    @property
    def row_factory(self): return self.db.row_factory
    @property
    def text_factory(self): return self.db.text_factory
    def execute(self, statement, *args):
        target = statement.startswith('INSERT INTO ' + self.table)
        if target and self.phase == 'before': raise io_error()
        result = self.db.execute(statement, *args)
        if target and self.phase == 'after': raise io_error()
        return result
    def __enter__(self):
        self.db.__enter__(); return self
    def __exit__(self, kind, error, trace):
        result = self.db.__exit__(kind, error, trace)
        if kind is None and self.phase == 'committed': raise io_error()
        return result
    def close(self): self.db.close()


class StoreIOErrors(unittest.TestCase):
    def test_profile_faults_distinguish_rollback_from_lost_commit_reply(self):
        for phase in ('before', 'after', 'committed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                current = adapter(Path(directory)); store = current.workflow.store
                old = current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
                connection = store._connection
                @contextmanager
                def failed():
                    with connection() as db:
                        yield WriteFault(db, 'profile_versions', phase)
                    if phase == 'committed': raise io_error()
                with patch.object(store, '_connection', failed):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        store.save_version('a', old['resolved_roles'], expected_version=1)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_IOERR)
                reopened = type(store)(store.path)
                self.assertEqual(reopened.latest('a')['version'], 2 if phase == 'committed' else 1)
                if phase == 'committed':
                    with self.assertRaisesRegex(ValueError, 'profile version changed'):
                        reopened.save_version('a', old['resolved_roles'], expected_version=1)
                else:
                    self.assertEqual(reopened.save_version('a', old['resolved_roles'], expected_version=1)['version'], 2)
                self.assertEqual(reopened.latest('a')['version'], 2)
                self.assertEqual(reopened.application_record('p', 'a'), old['applied_profile'])

    def test_journal_faults_never_duplicate_separately_committed_profiles(self):
        for phase in ('before', 'after', 'committed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); current = adapter(root)
                old = current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
                scope = {**CONTEXT, 'project_key': 'next', 'drawing_key': 'next:v1'}
                next_adapter = WorkflowAdapter(current.workflow, context=scope, intake=INTAKE, copies=COPIES,
                                               confirmation_receipts=current._receipts)
                connect = current._receipts._connect
                with patch.object(current._receipts, '_connect', lambda: WriteFault(connect(), 'confirmations', phase)):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        next_adapter.confirm_roles(scope, {**COPIES[1], 'expected_version': 1})
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_IOERR)
                self.assertEqual(current.workflow.store.latest('a')['version'], 2)
                if phase == 'committed':
                    self.assertEqual(next_adapter.confirm_roles(scope, {**COPIES[1], 'expected_version': 1})['applied_profile']['version'], 2)
                else:
                    with self.assertRaisesRegex(ValueError, 'already pinned'):
                        next_adapter.confirm_roles(scope, {**COPIES[1], 'expected_version': 1})
                    self.assertEqual(next_adapter.apply_profile(scope, COPIES[1])['applied_profile']['version'], 2)
                self.assertEqual(current.workflow.store.latest('a')['version'], 2)
                self.assertEqual(current.workflow.store.application_record('p', 'a'), old['applied_profile'])
