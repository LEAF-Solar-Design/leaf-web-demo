from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES, INTAKE
from x07_workflow_adapter import WorkflowAdapter


class SQLiteVariableLimits(unittest.TestCase):
    def test_profile_insert_variable_limit_rolls_back_then_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            current = adapter(Path(directory)); store = current.workflow.store
            old = current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            connection = store._connection
            @contextmanager
            def limited():
                with connection() as db:
                    db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
                    self.assertEqual(db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER), 4)
                    yield db
            before = store.path.read_bytes()
            with patch.object(store, '_connection', limited):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'too many SQL variables'):
                    store.save_version('a', old['resolved_roles'], expected_version=1)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.latest('a')['version'], 1)
            self.assertEqual(store.save_version('a', old['resolved_roles'], expected_version=1)['version'], 2)
            self.assertEqual(store.application_record('p', 'a'), old['applied_profile'])

    def test_journal_variable_limit_preserves_separate_commit_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            current = adapter(Path(directory))
            old = current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            scope = {**CONTEXT, 'project_key': 'next', 'drawing_key': 'next:v1'}
            next_adapter = WorkflowAdapter(current.workflow, context=scope, intake=INTAKE, copies=COPIES,
                                           confirmation_receipts=current._receipts)
            connect = current._receipts._connect
            def limited():
                db = connect(); db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3); return db
            with patch.object(current._receipts, '_connect', limited):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'too many SQL variables'):
                    next_adapter.confirm_roles(scope, {**COPIES[1], 'expected_version': 1})
            self.assertEqual(current.workflow.store.latest('a')['version'], 2)
            with self.assertRaisesRegex(ValueError, 'already pinned'):
                next_adapter.confirm_roles(scope, {**COPIES[1], 'expected_version': 1})
            self.assertEqual(next_adapter.apply_profile(scope, COPIES[1])['applied_profile']['version'], 2)
            self.assertEqual(current.workflow.store.latest('a')['version'], 2)
            self.assertEqual(current.workflow.store.application_record('p', 'a'), old['applied_profile'])
