"""X07 online journal backups contain only committed producer receipts."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES, INTAKE
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import ConfirmationReceiptStore, WorkflowAdapter


class ReceiptBackupPending(unittest.TestCase):
    def test_pending_and_committed_backups_reopen_with_valid_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = adapter(root)
            request = {**COPIES[0], 'expected_version': 0}
            settled = first.confirm_roles(CONTEXT, request)
            other = {**CONTEXT, 'project_key': 'second', 'drawing_key': 'second:v1'}
            second = WorkflowAdapter(first.workflow, context=other, intake=INTAKE, copies=COPIES,
                                     confirmation_receipts=first._receipts)
            request2 = {**COPIES[1], 'expected_version': 1}
            settled2 = second.confirm_roles(other, request2)
            journal = root / 'receipts.sqlite'
            with closing(sqlite3.connect(journal)) as writer:
                self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                row = writer.execute('SELECT * FROM confirmations WHERE project_key=?', ('second',)).fetchone()
                writer.execute('DELETE FROM confirmations WHERE project_key=?', ('second',))
                writer.commit()
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO confirmations VALUES (?,?,?,?)', row)
                self.assertEqual(writer.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 2)
                profile_bytes = (root / 'profiles.sqlite').read_bytes()
                for committed in (False, True):
                    if committed:
                        writer.commit()
                    restored = root / ('committed' if committed else 'pending')
                    restored.mkdir()
                    for name in ('profiles.sqlite', 'receipts.sqlite'):
                        with closing(sqlite3.connect(root / name)) as source, closing(sqlite3.connect(restored / name)) as target:
                            source.backup(target)
                    store = CustomerProfileStore(restored / 'profiles.sqlite', read_only=True)
                    receipts = ConfirmationReceiptStore(restored / 'receipts.sqlite', read_only=True)
                    def reopened(scope):
                        return WorkflowAdapter(ProfileWorkflow(store), context=scope, intake=INTAKE, copies=COPIES,
                                               confirmation_receipts=receipts)
                    with closing(sqlite3.connect(restored / 'receipts.sqlite')) as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 2 if committed else 1)
                        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(reopened(CONTEXT).confirm_roles(CONTEXT, request), settled)
                    if committed:
                        self.assertEqual(reopened(other).confirm_roles(other, request2), settled2)
                    else:
                        with self.assertRaisesRegex(ValueError, 'unavailable in read-only journal'):
                            reopened(other).confirm_roles(other, request2)
                    self.assertEqual(store.application_record('p', 'a'), settled['applied_profile'])
                    self.assertEqual(store.application_record('second', 'a'), settled2['applied_profile'])
                    self.assertEqual((root / 'profiles.sqlite').read_bytes(), profile_bytes)
                # The earlier restored generation stays immutable after source commit.
                with closing(sqlite3.connect(root / 'pending/receipts.sqlite')) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 1)
