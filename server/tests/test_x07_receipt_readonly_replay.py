"""Read-only journals replay committed requests and refuse production of new ones."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES, INTAKE
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import ConfirmationReceiptStore, WorkflowAdapter


class ReceiptReadonlyReplay(unittest.TestCase):
    def test_replay_unseen_refusal_and_writable_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = adapter(root).confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            store = CustomerProfileStore(root / 'profiles.sqlite')
            journal = root / 'receipts.sqlite'
            before = (journal.read_bytes(), (root / 'profiles.sqlite').read_bytes())
            def bound(context, read_only):
                return WorkflowAdapter(ProfileWorkflow(store), context=context, intake=INTAKE, copies=COPIES,
                    confirmation_receipts=ConfirmationReceiptStore(journal, read_only=read_only))
            self.assertEqual(bound(CONTEXT, True).confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0}), result)
            unseen = {**CONTEXT, 'project_key': 'unseen'}
            with self.assertRaisesRegex(ValueError, 'unavailable in read-only journal'):
                bound(unseen, True).confirm_roles(unseen, {**COPIES[0], 'expected_version': 1})
            self.assertEqual((journal.read_bytes(), (root / 'profiles.sqlite').read_bytes()), before)
            fresh = bound(unseen, False).confirm_roles(unseen, {**COPIES[0], 'expected_version': 1})
            self.assertEqual(fresh['applied_profile']['version'], 2)
            self.assertEqual(store.application_record('p', 'a'), result['applied_profile'])
            self.assertEqual(bound(unseen, True).confirm_roles(unseen, {**COPIES[0], 'expected_version': 1}), fresh)
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0], 2)
            # The same known legacy table can replay without a schema write.
            with closing(sqlite3.connect(journal)) as db, db:
                db.execute('PRAGMA user_version=0')
            legacy_bytes = journal.read_bytes()
            self.assertEqual(bound(unseen, True).confirm_roles(unseen, {**COPIES[0], 'expected_version': 1}), fresh)
            self.assertEqual(journal.read_bytes(), legacy_bytes)

    def test_missing_readonly_journal_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'missing.sqlite'
            with self.assertRaises(sqlite3.OperationalError):
                ConfirmationReceiptStore(journal, read_only=True)
            self.assertFalse(journal.exists())
