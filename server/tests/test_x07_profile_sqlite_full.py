from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class SQLiteFullTests(unittest.TestCase):
    def check(self, target):
        with tempfile.TemporaryDirectory() as directory:
            store=CustomerProfileStore(Path(directory)/'store.sqlite')
            roles={'module':{'block':'Panel','layer':'MODULES'}}
            first=store.save_version('a',roles,expected_version=0);pin=store.apply_to_project('old','a')
            original=store._connection
            with original() as db:
                db.execute('CREATE TABLE fixture_pressure (payload BLOB)')
                # Force the real pager limit inside the product transaction.
                db.execute('CREATE TRIGGER fixture_full BEFORE INSERT ON '+target+
                           ' BEGIN INSERT INTO fixture_pressure VALUES (zeroblob(1048576)); END;')
                pages=db.execute('PRAGMA page_count').fetchone()[0]
            limit=[pages]
            @contextmanager
            def limited():
                with original() as db:
                    db.execute('PRAGMA max_page_count='+str(limit[0]));yield db
            store._connection=limited
            operation=(lambda:store.save_version('a',roles,expected_version=1)) if target=='profile_versions' else (lambda:store.apply_to_project('new','a'))
            with self.assertRaises(sqlite3.OperationalError) as error: operation()
            self.assertEqual(error.exception.sqlite_errorcode,sqlite3.SQLITE_FULL)
            self.assertEqual(store.latest('a'),first);self.assertEqual(store.application_record('old','a'),pin)
            self.assertIsNone(store.application_record('new','a'))
            with original() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM fixture_pressure').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0],1)
            limit[0]=pages+1024
            result=operation()
            self.assertEqual(result['version'],2 if target=='profile_versions' else 1)
            with original() as db:
                db.execute('DROP TRIGGER fixture_full');db.execute('DROP TABLE fixture_pressure')
            reopened=CustomerProfileStore(store.path)
            self.assertEqual(reopened.application_record('old','a'),pin)
    def test_full_version_insert_rolls_back_and_retries(self):self.check('profile_versions')
    def test_full_pin_insert_rolls_back_and_retries(self):self.check('project_applications')
