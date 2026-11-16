from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES
from x07_customer_profile_store import CustomerProfileStore


class ReceiptSQLiteFull(unittest.TestCase):
    def test_full_receipt_preserves_committed_profile_pin_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            store = current.workflow.store
            store.save_version('old', {'module': {'block': 'Old panel', 'layer': 'M'}}, expected_version=0)
            old_pin = store.apply_to_project('old-project', 'old', expected_version=1)
            journal = current._receipts
            connect = journal._connect
            with closing(connect()) as db, db:
                db.execute('CREATE TABLE fixture_pressure (payload BLOB)')
                db.execute('CREATE TRIGGER fixture_full BEFORE INSERT ON confirmations '
                    'BEGIN INSERT INTO fixture_pressure VALUES (zeroblob(1048576)); END;')
                pages = db.execute('PRAGMA page_count').fetchone()[0]

            def limited():
                db = connect()
                db.execute('PRAGMA max_page_count=' + str(pages))
                return db

            journal._connect = limited
            request = {**COPIES[0], 'expected_version': 0}
            with self.assertRaises(sqlite3.OperationalError) as caught:
                current.confirm_roles(CONTEXT, request)
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
            self.assertEqual(store.latest('a')['version'], 1)
            pin = store.application_record('p', 'a')
            self.assertEqual(pin['version'], 1)
            with closing(connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM fixture_pressure').fetchone()[0], 0)
            with self.assertRaisesRegex(ValueError, 'already pinned'):
                current.confirm_roles(CONTEXT, request)
            self.assertEqual(current.load_workflow(CONTEXT)['application_record'], pin)
            self.assertEqual(current.apply_profile(CONTEXT, COPIES[0])['applied_profile'], pin)
            self.assertEqual(store.application_record('old-project', 'old'), old_pin)
            with closing(connect()) as db, db:
                db.execute('DROP TRIGGER fixture_full')
                db.execute('DROP TABLE fixture_pressure')
            reopened = adapter(root)
            with self.assertRaisesRegex(ValueError, 'already pinned'):
                reopened.confirm_roles(CONTEXT, request)
            with CustomerProfileStore(root / 'profiles.sqlite')._connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_versions WHERE customer_key='a'").fetchone()[0], 1)
