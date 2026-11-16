"""A borrowed active connection is refused, not committed by a store method."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class StoreOuterTransaction(unittest.TestCase):
    def check(self, operation):
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerProfileStore(Path(directory) / 'profile.sqlite')
            roles = {'module': {'block': 'Panel', 'layer': 'M'}}
            original = store.save_version('a', roles, expected_version=0)
            pin = store.apply_to_project('old', 'a', expected_version=1)
            with closing(sqlite3.connect(store.path)) as caller:
                caller.row_factory = sqlite3.Row
                caller.execute('PRAGMA foreign_keys=ON')
                caller.execute('BEGIN IMMEDIATE')
                caller.execute('INSERT INTO customers VALUES (?,?)', ('caller', 'caller-profile'))
                @contextmanager
                def borrowed():
                    # The caller retains transaction ownership; the injected
                    # seam must not wrap the connection in another commit scope.
                    yield caller
                with patch.object(store, '_connection', borrowed):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'within a transaction'):
                        if operation == 'save': store.save_version('a', roles, expected_version=1)
                        else: store.apply_to_project('new', 'a', expected_version=1)
                self.assertTrue(caller.in_transaction)
                self.assertEqual(caller.execute("SELECT COUNT(*) FROM customers WHERE customer_key='caller'").fetchone()[0], 1)
                with closing(sqlite3.connect(store.path)) as observer:
                    self.assertEqual(observer.execute("SELECT COUNT(*) FROM customers WHERE customer_key='caller'").fetchone()[0], 0)
                    self.assertEqual(observer.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 1)
                caller.rollback()
                self.assertFalse(caller.in_transaction)
            self.assertEqual(store.latest('a'), original)
            self.assertEqual(store.application_record('old', 'a'), pin)
            self.assertIsNone(store.application_record('new', 'a'))
            self.assertIsNone(store.latest('caller'))

    def test_save_refuses_borrowed_active_transaction(self): self.check('save')
    def test_pin_refuses_borrowed_active_transaction(self): self.check('pin')
