"""Interrupt actual SQLite VM writes, then retry with the handler removed."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class StoreInterruptRollback(unittest.TestCase):
    def check(self, operation):
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerProfileStore(Path(directory) / 'profile.sqlite')
            roles = {'module': {'block': 'Panel', 'layer': 'M'}}
            first = store.save_version('a', roles, expected_version=0)
            old = store.apply_to_project('old', 'a', expected_version=1)
            if operation == 'pin': store.save_version('a', roles, expected_version=1)
            original = store._connection
            interrupted = []
            @contextmanager
            def connection():
                with original() as db:
                    armed = False
                    def trace(sql):
                        nonlocal armed
                        target = 'project_applications' if operation == 'pin' else 'profile_versions'
                        if sql.startswith('INSERT INTO ' + target): armed = True
                    def progress():
                        if armed: interrupted.append(True); return 1
                        return 0
                    db.set_trace_callback(trace)
                    db.set_progress_handler(progress, 1)
                    try: yield db
                    finally:
                        db.set_progress_handler(None, 0)
                        db.set_trace_callback(None)
            store._connection = connection
            def write():
                if operation == 'pin': return store.apply_to_project('new', 'a', expected_version=2)
                customer = 'fresh' if operation == 'new_customer' else 'a'
                return store.save_version(customer, roles, expected_version=0 if customer == 'fresh' else 1)
            with self.assertRaises(sqlite3.OperationalError) as caught: write()
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
            self.assertTrue(interrupted)
            store._connection = original
            self.assertEqual(store.application_record('old', 'a'), old)
            self.assertIsNone(store.application_record('new', 'a'))
            self.assertIsNone(store.latest('fresh'))
            self.assertEqual(store.latest('a')['version'], 2 if operation == 'pin' else 1)
            with original() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0], 1)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            result = write()
            self.assertEqual(result['version'], 1 if operation == 'new_customer' else 2)
            self.assertEqual(store.application_record('old', 'a'), old)
            self.assertEqual(first['version'], 1)

    def test_existing_version_interrupt_rolls_back_and_retries(self): self.check('existing')
    def test_new_customer_interrupt_rolls_back_customer_and_version(self): self.check('new_customer')
    def test_new_pin_interrupt_rolls_back_and_retries(self): self.check('pin')
