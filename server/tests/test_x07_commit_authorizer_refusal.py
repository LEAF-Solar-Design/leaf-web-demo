from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class CommitAuthorizerRefusal(unittest.TestCase):
    def check(self, target):
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerProfileStore(Path(directory) / 'profile.sqlite')
            roles = {'module': {'block': 'Panel', 'layer': 'M'}}
            store.save_version('a', roles, expected_version=0)
            old = store.apply_to_project('old', 'a', expected_version=1)
            if target == 'pin': store.save_version('a', roles, expected_version=1)
            original = store._connection
            seen, denials = [], []
            @contextmanager
            def refused():
                db = sqlite3.connect(store.path)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.set_trace_callback(seen.append)
                def authorize(action, value, *rest):
                    if action == sqlite3.SQLITE_TRANSACTION and value == 'COMMIT':
                        denials.append(value); return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                try:
                    with db: yield db
                finally:
                    db.set_authorizer(None); db.close()
            store._connection = refused
            def write():
                if target == 'pin': return store.apply_to_project('new', 'a', expected_version=2)
                return store.save_version('fresh' if target == 'new_customer' else 'a', roles,
                                          expected_version=0 if target == 'new_customer' else 1)
            with self.assertRaises(sqlite3.DatabaseError) as caught: write()
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_AUTH)
            self.assertEqual(denials, ['COMMIT'])
            table = 'project_applications' if target == 'pin' else 'profile_versions'
            self.assertTrue(any(sql.startswith('INSERT INTO ' + table) for sql in seen))
            self.assertTrue(any(sql == 'ROLLBACK' for sql in seen))
            store._connection = original
            self.assertEqual(store.application_record('old', 'a'), old)
            self.assertIsNone(store.application_record('new', 'a'))
            self.assertIsNone(store.latest('fresh'))
            self.assertEqual(store.latest('a')['version'], 2 if target == 'pin' else 1)
            result = write()
            self.assertEqual(result['version'], 1 if target == 'new_customer' else 2)
            self.assertEqual(store.application_record('old', 'a'), old)

    def test_existing_save_commit_denial_rolls_back(self): self.check('existing')
    def test_new_customer_commit_denial_rolls_back_both_inserts(self): self.check('new_customer')
    def test_pin_commit_denial_rolls_back(self): self.check('pin')
