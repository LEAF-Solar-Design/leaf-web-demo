from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES
from x07_customer_profile_store import CustomerProfileStore


class SQLiteLengthLimits(unittest.TestCase):
    def profile(self, fresh):
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerProfileStore(Path(directory)/'profiles.sqlite')
            small = {'module': {'block': 'Panel', 'layer': 'M'}}
            first = store.save_version('a', small, expected_version=0)
            pin = store.apply_to_project('old', 'a', expected_version=1)
            original = store._connection
            @contextmanager
            def limited():
                with original() as db:
                    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 512)
                    yield db
            with patch.object(store, '_connection', limited):
                with self.assertRaises(sqlite3.DataError) as caught:
                    store.save_version('fresh' if fresh else 'a', {'module': {'block': 'Panel', 'layer': 'M'+'x'*4000}},
                                       expected_version=0 if fresh else 1)
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_TOOBIG)
            self.assertEqual(store.latest('a'), first); self.assertIsNone(store.latest('fresh'))
            self.assertEqual(store.application_record('old', 'a'), pin)
            with original() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 1)
            result = store.save_version('fresh' if fresh else 'a', small, expected_version=0 if fresh else 1)
            self.assertEqual(result['version'], 1 if fresh else 2)
            self.assertEqual(store.application_record('old', 'a'), pin)

    def test_existing_profile_length_refusal_rolls_back(self): self.profile(False)
    def test_new_customer_length_refusal_rolls_back_both_rows(self): self.profile(True)

    def test_journal_length_refusal_truthfully_recovers_separate_profile_pin_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); current = adapter(root); store = current.workflow.store
            store.save_version('old', {'module': {'block': 'Old', 'layer': 'M'}}, expected_version=0)
            old_pin = store.apply_to_project('old-project', 'old', expected_version=1)
            original = current._receipts._connect
            def limited():
                db = original(); db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 512); return db
            produce = current.workflow.confirm; measured = []
            def observed(*args, **kwargs):
                result = produce(*args, **kwargs); measured.append(len(json.dumps(result))); return result
            request = {**COPIES[0], 'expected_version': 0}
            with patch.object(current._receipts, '_connect', limited), patch.object(current.workflow, 'confirm', observed):
                with self.assertRaises(sqlite3.DataError) as caught: current.confirm_roles(CONTEXT, request)
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_TOOBIG)
            self.assertEqual(len(measured), 1); self.assertGreater(measured[0], 512)
            pin = store.application_record('p', 'a'); self.assertEqual(pin['version'], 1)
            self.assertEqual(store.latest('a')['version'], 1)
            with closing(original()) as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 0)
            with self.assertRaisesRegex(ValueError, 'already pinned'): current.confirm_roles(CONTEXT, request)
            self.assertEqual(current.load_workflow(CONTEXT)['application_record'], pin)
            self.assertEqual(current.apply_profile(CONTEXT, COPIES[0])['applied_profile'], pin)
            self.assertEqual(store.application_record('old-project', 'old'), old_pin)
