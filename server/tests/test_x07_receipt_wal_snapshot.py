from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
from threading import Event, Thread
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES, INTAKE
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import ConfirmationReceiptStore, WorkflowAdapter


class ReceiptWALSnapshot(unittest.TestCase):
    def test_independent_readers_exclude_pending_rows_and_restart_into_committed_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = adapter(root)
            request = {**COPIES[0], 'expected_version': 0}
            result1 = first.confirm_roles(CONTEXT, request)
            scope2 = {**CONTEXT, 'project_key': 'p2', 'drawing_key': 'd2:v1'}
            second = WorkflowAdapter(first.workflow, context=scope2, intake=INTAKE, copies=COPIES,
                                     confirmation_receipts=first._receipts)
            request2 = {**COPIES[1], 'expected_version': 1}
            result2 = second.confirm_roles(scope2, request2)
            journal = root / 'receipts.sqlite'
            # Retain a genuine producer row, then stage that committed journal
            # generation again using an independent WAL writer.
            with closing(sqlite3.connect(journal)) as db, db:
                self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                row2 = db.execute('SELECT * FROM confirmations WHERE project_key=?', ('p2',)).fetchone()
                db.execute('DELETE FROM confirmations WHERE project_key=?', ('p2',))
            profile_bytes = (root / 'profiles.sqlite').read_bytes()
            pending, allow_commit, committed = Event(), Event(), Event()
            failures = []
            def writer():
                try:
                    with closing(sqlite3.connect(journal)) as db, db:
                        db.execute('BEGIN IMMEDIATE')
                        db.execute('INSERT INTO confirmations VALUES (?,?,?,?)', row2)
                        pending.set()
                        if not allow_commit.wait(10): raise AssertionError('commit permission not received')
                    committed.set()
                except BaseException as error: failures.append(error); pending.set(); committed.set()
            def readonly(scope):
                return WorkflowAdapter(ProfileWorkflow(CustomerProfileStore(root / 'profiles.sqlite', read_only=True)),
                    context=scope, intake=INTAKE, copies=COPIES,
                    confirmation_receipts=ConfirmationReceiptStore(journal, read_only=True))
            with closing(sqlite3.connect(journal)) as reader:
                reader.execute('BEGIN')
                initial = reader.execute('SELECT result FROM confirmations ORDER BY project_key').fetchall()
                self.assertEqual([json.loads(row[0]) for row in initial], [result1])
                worker = Thread(target=writer, daemon=True); worker.start()
                try:
                    self.assertTrue(pending.wait(10)); self.assertEqual(failures, [])
                    self.assertEqual(readonly(CONTEXT).confirm_roles(CONTEXT, request), result1)
                    with self.assertRaisesRegex(ValueError, 'unavailable in read-only journal'):
                        readonly(scope2).confirm_roles(scope2, request2)
                    self.assertEqual(reader.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 1)
                    allow_commit.set(); self.assertTrue(committed.wait(10)); self.assertEqual(failures, [])
                    self.assertEqual(reader.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 1)
                    self.assertEqual(readonly(scope2).confirm_roles(scope2, request2), result2)
                    reader.commit(); reader.execute('BEGIN')
                    rows = reader.execute('SELECT result FROM confirmations ORDER BY project_key').fetchall()
                    self.assertEqual([json.loads(row[0]) for row in rows], [result1, result2])
                    reader.commit()
                    self.assertEqual(first.workflow.store.application_record('p', 'a')['version'], 1)
                    self.assertEqual(first.workflow.store.application_record('p2', 'a')['version'], 2)
                    with self.assertRaises(ValueError) as caught:
                        readonly(scope2).confirm_roles({**scope2, 'customer_key': 'foreign'}, request2)
                    self.assertNotIn('profile_id', str(caught.exception))
                    self.assertEqual((root / 'profiles.sqlite').read_bytes(), profile_bytes)
                finally:
                    allow_commit.set(); worker.join(timeout=11)
                self.assertFalse(worker.is_alive()); self.assertEqual(failures, [])
