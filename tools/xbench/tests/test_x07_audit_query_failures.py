from contextlib import contextmanager, redirect_stdout
from pathlib import Path
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditQueryFailures(unittest.TestCase):
    def failures(self, mode):
        prefixes = {'customers': 'SELECT profile_id FROM customers', 'profile_versions': 'SELECT version,profile_id FROM profile_versions',
                    'project_applications': 'SELECT version,profile_id FROM project_applications'}
        for table, prefix in prefixes.items():
            with self.subTest(mode=mode, table=table), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'PRIVATE_PATH.sqlite'
                store = audit.CustomerProfileStore(path)
                store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
                pin = store.apply_to_project('old-a', 'a')
                store.save_version('b', {'module': {'block': 'PRIVATE_B', 'layer': 'B'}}, expected_version=0)
                read = audit.CustomerProfileStore(path, read_only=True)
                original = read._connection
                callbacks, changes = [], []
                class Proxy:
                    def __init__(self, db): self.db = db
                    def __getattr__(self, name): return getattr(self.db, name)
                    def execute(self, sql, params=()):
                        if mode == 'interrupt' and sql.startswith(prefix):
                            def progress(): callbacks.append(True); return 1
                            self.db.set_progress_handler(progress, 1)
                            try: return self.db.execute(sql, params)
                            finally: self.db.set_progress_handler(None, 0)
                        return self.db.execute(sql, params)
                @contextmanager
                def failed():
                    with original() as db:
                        if mode == 'deny':
                            def authorize(action, name, *args):
                                if action == sqlite3.SQLITE_READ and name == table:
                                    callbacks.append(True); return sqlite3.SQLITE_DENY
                                return sqlite3.SQLITE_OK
                            db.set_authorizer(authorize)
                        try: yield Proxy(db)
                        finally:
                            db.set_authorizer(None); db.set_progress_handler(None, 0)
                            changes.append(db.total_changes)
                before = path.read_bytes()
                read._connection = failed
                try:
                    with self.assertRaises(sqlite3.DatabaseError): audit.audit_history(read, customer_key='a')
                    text = io.StringIO()
                    with patch.object(sys, 'argv', ['audit', '--database', str(path), '--customer', 'a']), patch.object(audit, 'CustomerProfileStore', return_value=read), redirect_stdout(text):
                        self.assertEqual(audit.main(), 2)
                    self.assertEqual(json.loads(text.getvalue()), {'schema': 'leaf.x07.profile-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'})
                    self.assertNotIn('healthy', text.getvalue()); self.assertNotIn('PRIVATE', text.getvalue())
                    self.assertLess(len(text.getvalue()), 200)
                finally: read._connection = original
                self.assertGreater(len(callbacks), 0); self.assertEqual(changes, [0,0])
                self.assertTrue(audit.audit_history(read, customer_key='a')['healthy'])
                self.assertEqual(read.application_record('old-a', 'a'), pin)
                self.assertEqual(path.read_bytes(), before)

    def test_interrupt_each_select_then_recover(self): self.failures('interrupt')
    def test_deny_each_table_read_then_recover(self): self.failures('deny')
