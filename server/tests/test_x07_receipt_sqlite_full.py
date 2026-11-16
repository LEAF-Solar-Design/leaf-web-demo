from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES
from x07_customer_profile_store import CustomerProfileStore
from x07_workflow_adapter import WorkflowAdapter


class ReceiptSQLiteFull(unittest.TestCase):
    def test_full_receipt_preserves_committed_profile_pin_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            # Real producer output exceeds the fixed journal pager cap. Keep
            # the journal schema intact so schema guards are not bypassed.
            copy = dict(block='Panel', layer='M', design_window=[0, 0, 0, 0], oracle_count=5000)
            current = WorkflowAdapter(current.workflow, context=CONTEXT,
                intake={'inserts': [dict(name='Panel', layer='M', x=0, y=0) for _ in range(5000)]},
                copies=[copy], confirmation_receipts=current._receipts)
            store = current.workflow.store
            store.save_version('old', {'module': {'block': 'Old panel', 'layer': 'M'}}, expected_version=0)
            old_pin = store.apply_to_project('old-project', 'old', expected_version=1)
            journal = current._receipts
            connect = journal._connect
            with closing(connect()) as db, db:
                pages = db.execute('PRAGMA page_count').fetchone()[0]

            def limited():
                db = connect()
                db.execute('PRAGMA max_page_count=' + str(pages))
                return db

            journal._connect = limited
            request = {**copy, 'expected_version': 0}
            with self.assertRaises(sqlite3.OperationalError) as caught:
                current.confirm_roles(CONTEXT, request)
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
            self.assertEqual(store.latest('a')['version'], 1)
            pin = store.application_record('p', 'a')
            self.assertEqual(pin['version'], 1)
            with closing(connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 0)
            with self.assertRaisesRegex(ValueError, 'already pinned'):
                current.confirm_roles(CONTEXT, request)
            self.assertEqual(current.load_workflow(CONTEXT)['application_record'], pin)
            self.assertEqual(current.apply_profile(CONTEXT, copy)['applied_profile'], pin)
            self.assertEqual(store.application_record('old-project', 'old'), old_pin)
            reopened = WorkflowAdapter(current.workflow, context=CONTEXT,
                intake=current._intake, copies=[copy], confirmation_receipts=adapter(root)._receipts)
            with self.assertRaisesRegex(ValueError, 'already pinned'):
                reopened.confirm_roles(CONTEXT, request)
            with CustomerProfileStore(root / 'profiles.sqlite')._connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_versions WHERE customer_key='a'").fetchone()[0], 1)
