from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES


class ReceiptCommitAuthorizer(unittest.TestCase):
    def test_denied_journal_commit_preserves_profile_pin_and_requires_inspect_apply_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root); store = current.workflow.store
            store.save_version('old', {'module': {'block': 'Old', 'layer': 'M'}}, expected_version=0)
            old_pin = store.apply_to_project('old-project', 'old', expected_version=1)
            connect = current._receipts._connect
            commits, inserts = [], []
            def refused():
                db = connect()
                def authorize(action, value, *rest):
                    if action == sqlite3.SQLITE_TRANSACTION and value == 'COMMIT':
                        commits.append(value); return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                db.set_trace_callback(lambda sql: inserts.append(True) if sql.startswith('INSERT INTO confirmations') else None)
                return db
            current._receipts._connect = refused
            request = {**COPIES[0], 'expected_version': 0}
            with self.assertRaises(sqlite3.DatabaseError) as caught: current.confirm_roles(CONTEXT, request)
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_AUTH)
            self.assertEqual(commits, ['COMMIT']); self.assertEqual(inserts, [True])
            self.assertEqual(store.latest('a')['version'], 1)
            pin = store.application_record('p', 'a'); self.assertEqual(pin['version'], 1)
            with closing(connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 0)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            current._receipts._connect = connect
            with self.assertRaisesRegex(ValueError, 'already pinned'): current.confirm_roles(CONTEXT, request)
            self.assertEqual(current.load_workflow(CONTEXT)['application_record'], pin)
            self.assertEqual(current.apply_profile(CONTEXT, COPIES[0])['applied_profile'], pin)
            self.assertEqual(store.latest('a')['version'], 1)
            self.assertEqual(store.application_record('old-project', 'old'), old_pin)
            with closing(connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 0)
