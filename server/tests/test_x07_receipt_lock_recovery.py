from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES


class ReceiptLockRecovery(unittest.TestCase):
    def test_locked_journal_refuses_before_profile_write_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            journal = current._receipts
            connect = journal._connect
            def no_wait():
                db = connect()
                db.execute('PRAGMA busy_timeout=0')
                return db
            journal._connect = no_wait
            request = {**COPIES[0], 'expected_version': 0}
            profile_bytes = (root / 'profiles.sqlite').read_bytes()
            with closing(sqlite3.connect(journal.path)) as blocker:
                blocker.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    current.confirm_roles(CONTEXT, request)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                self.assertEqual((root / 'profiles.sqlite').read_bytes(), profile_bytes)
                blocker.rollback()
            first = current.confirm_roles(CONTEXT, request)
            self.assertEqual(adapter(root).confirm_roles(CONTEXT, request), first)
            with current.workflow.store._connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0], 1)
            with closing(sqlite3.connect(journal.path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 1)
