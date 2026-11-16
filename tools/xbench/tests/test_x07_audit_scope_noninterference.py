from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, MAX_AUDIT_ROWS, audit_history


class AuditScopeNoninterference(unittest.TestCase):
    def test_foreign_corruption_and_overbudget_pins_cannot_change_target_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = CustomerProfileStore(path)
            store.save_version('a', {'module': {'block':'A','layer':'M'}}, expected_version=0)
            old = store.apply_to_project('old-a','a')
            read = CustomerProfileStore(path, read_only=True)
            expected = audit_history(read, customer_key='a')
            foreign = 'PRIVATE_FOREIGN_CUSTOMER'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('INSERT INTO customers VALUES (?,?)', (foreign,'PRIVATE_FOREIGN_PROFILE'))
                db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)', (foreign,'PRIVATE_WRONG_PROFILE',3,'PRIVATE_ROLE_LABEL','synthetic'))
                db.executemany('INSERT INTO project_applications VALUES (?,?,?,?)',
                               ((f'foreign-{index}',foreign,'PRIVATE_WRONG_PROFILE',0) for index in range(MAX_AUDIT_ROWS+1)))
            before = path.read_bytes()
            result = audit_history(read, customer_key='a')
            self.assertEqual(result, expected)
            self.assertNotIn('PRIVATE', json.dumps(result))
            with self.assertRaisesRegex(ValueError, 'row budget'):
                audit_history(read, customer_key=foreign)
            self.assertEqual(read.application_record('old-a','a'), old)
            self.assertEqual(path.read_bytes(), before)
