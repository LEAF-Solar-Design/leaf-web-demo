from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, audit_history


class AuditVersionTypes(unittest.TestCase):
    def test_invalid_versions_and_independent_safe_integer_gaps_preserve_old_pin(self):
        maximum = (1<<53)-1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'store.sqlite'
            store = CustomerProfileStore(path)
            saved = store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
            old = store.apply_to_project('old','a')
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('PRAGMA ignore_check_constraints=ON')
                values = [3,maximum,0,-1,1.5,'PRIVATE_VERSION',maximum+1]
                db.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)',
                               (('a',saved['profile_id'],version,'{}','synthetic') for version in values))
            read = CustomerProfileStore(path,read_only=True)
            before = path.read_bytes()
            result = audit_history(read,customer_key='a')
            self.assertFalse(result['healthy'])
            self.assertEqual(result['version_count'],8)
            self.assertEqual(result['issues']['invalid_versions'],5)
            self.assertEqual(result['issues']['version_gaps'],[{'first':2,'last':2},{'first':4,'last':maximum-1}])
            self.assertEqual(result['issues']['dangling_pin_versions'],0)
            self.assertEqual(read.application_record('old','a'),old)
            self.assertEqual(path.read_bytes(),before)
            # A numeric text value becomes a legitimate stored integer2.
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',('a',saved['profile_id'],'2','{}','synthetic'))
                self.assertEqual(db.execute('SELECT typeof(version) FROM profile_versions WHERE customer_key=? AND version=2',('a',)).fetchone()[0],'integer')
            before = path.read_bytes()
            result = audit_history(read,customer_key='a')
            self.assertEqual(result['version_count'],9)
            self.assertEqual(result['issues']['invalid_versions'],5)
            self.assertEqual(result['issues']['version_gaps'],[{'first':4,'last':maximum-1}])
            self.assertEqual(read.application_record('old','a'),old)
            self.assertEqual(path.read_bytes(),before)
