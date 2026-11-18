from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, MAX_PROFILE_VERSION, audit_history


class AuditPinTypes(unittest.TestCase):
    def test_invalid_stored_versions_never_healthy_and_sqlite_coercions_are_explicit(self):
        for value, invalid in [(0,True),(-1,True),(1.5,True),('PRIVATE_VERSION',True),(MAX_PROFILE_VERSION+1,True),('1',False),(1.0,False)]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'store.sqlite'
                store = CustomerProfileStore(path)
                saved = store.save_version('a', {'module': {'block':'A','layer':'M'}}, expected_version=0)
                old = store.apply_to_project('old', 'a')
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('PRAGMA foreign_keys=OFF')
                    db.execute('INSERT INTO project_applications VALUES (?,?,?,?)', ('injected','a',saved['profile_id'],value))
                    stored, storage_type = db.execute('SELECT version,typeof(version) FROM project_applications WHERE project_key=?', ('injected',)).fetchone()
                if not invalid:
                    # INTEGER affinity legitimately stores numeric1/1.0 as int1.
                    self.assertEqual((stored,storage_type), (1,'integer'))
                read = CustomerProfileStore(path, read_only=True)
                before = path.read_bytes()
                result = audit_history(read, customer_key='a')
                self.assertEqual(result['healthy'], not invalid)
                self.assertEqual(result['issues']['dangling_pin_versions'], int(invalid))
                self.assertEqual(result['pin_count'], 2)
                self.assertEqual(read.application_record('old','a'), old)
                self.assertEqual(path.read_bytes(), before)
