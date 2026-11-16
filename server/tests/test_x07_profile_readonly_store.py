from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore,ProfileScopeError


class ReadonlyStoreTests(unittest.TestCase):
    def test_readonly_scope_and_pinned_reopen_work_writes_refuse_then_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'store.sqlite';writer=CustomerProfileStore(path)
            roles={'module':{'block':'Panel','layer':'MODULES'}}
            profile=writer.save_version('a',roles,expected_version=0);pin=writer.apply_to_project('p','a')
            before=path.read_bytes();reader=CustomerProfileStore(path,read_only=True)
            self.assertEqual(reader.latest('a'),profile);self.assertEqual(reader.apply_to_project('p','a',expected_version=1),pin)
            with self.assertRaises(ProfileScopeError):reader.application_record('p','b')
            for operation in [lambda:reader.save_version('a',roles,expected_version=1),lambda:reader.apply_to_project('new','a')]:
                with self.assertRaisesRegex(sqlite3.OperationalError,'read-only'):operation()
                self.assertEqual(path.read_bytes(),before)
            with reader._connection() as db:
                with self.assertRaises(sqlite3.OperationalError):db.execute("INSERT INTO customers VALUES ('b','profile-b')")
            writer=CustomerProfileStore(path)
            self.assertEqual(writer.save_version('a',roles,expected_version=1)['version'],2)
            self.assertEqual(writer.apply_to_project('new','a')['version'],2)
            self.assertEqual(writer.application_record('p','a'),pin)

    def test_readonly_missing_path_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'missing.sqlite'
            with self.assertRaises(sqlite3.OperationalError):CustomerProfileStore(path,read_only=True)
            self.assertFalse(path.exists())
