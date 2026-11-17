from contextlib import closing, contextmanager
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, audit_history


class AuditPayloadBlindness(unittest.TestCase):
    def test_metadata_audit_never_reads_or_decodes_role_payload_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = CustomerProfileStore(path)
            saved = store.save_version('a', {'module': {'block':'A','layer':'M'}}, expected_version=0)
            old = store.apply_to_project('old','a')
            with closing(sqlite3.connect(path)) as db, db:
                db.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)',
                               [('a',saved['profile_id'],2,'PRIVATE_MALFORMED_JSON','synthetic'),
                                ('a',saved['profile_id'],3,json.dumps({'module':{'block':'PRIVATE_'+'x'*262144,'layer':'M'}}),'synthetic')])
            read = CustomerProfileStore(path, read_only=True)
            original = read._connection
            attempted, columns = [], []
            @contextmanager
            def blind():
                with original() as db:
                    def authorize(action, table, column, *args):
                        if action == sqlite3.SQLITE_READ:
                            columns.append((table,column))
                            if column == 'roles_json':
                                attempted.append(True); return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK
                    db.set_authorizer(authorize)
                    try: yield db
                    finally: db.set_authorizer(None)
            before = path.read_bytes()
            read._connection = blind
            try: result = audit_history(read, customer_key='a')
            finally: read._connection = original
            self.assertTrue(result['healthy'])
            self.assertEqual(result['version_count'], 3)
            self.assertEqual(attempted, [])
            self.assertIn(('profile_versions','version'), columns)
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertEqual(read.application_record('old','a'), old)
            self.assertEqual(path.read_bytes(), before)
