from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore,ProfileScopeError


class PersistedPinCorruptionTests(unittest.TestCase):
    def test_dangling_and_mismatched_links_refuse_without_foreign_data_or_repairs(self):
        for customer,profile,version in [('a','profile-missing',1),('a','placeholder',99),('missing','placeholder',1)]:
            with self.subTest(pin=(customer,profile,version)),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'store.sqlite';store=CustomerProfileStore(path)
                for key in ['a','b']:store.save_version(key,{'module':{'block':'Panel '+key,'layer':'M'}})
                foreign=store.latest('b')['profile_id']
                db=sqlite3.connect(path)
                try:
                    db.execute('PRAGMA foreign_keys=OFF')
                    db.execute('INSERT INTO project_applications VALUES (?,?,?,?)',('bad',customer,foreign if profile=='placeholder' else profile,version));db.commit()
                finally:db.close()
                before=path.read_bytes();reopened=CustomerProfileStore(path)
                for operation in [reopened.application_record,reopened.apply_to_project]:
                    with self.assertRaisesRegex(ValueError,'stored project pin is invalid'):operation('bad',customer)
                    with self.assertRaises(ProfileScopeError):operation('bad','b' if customer!='b' else 'a')
                    self.assertEqual(path.read_bytes(),before)
