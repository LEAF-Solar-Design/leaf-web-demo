from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditLockRecovery(unittest.TestCase):
    def test_exclusive_delete_lock_refuses_then_explicit_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'PRIVATE.sqlite'
            store = audit.CustomerProfileStore(path)
            store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
            old = store.apply_to_project('old', 'a')
            read = audit.CustomerProfileStore(path, read_only=True)
            before = path.read_bytes()
            ready, release = threading.Event(), threading.Event()
            errors = []
            def writer():
                try:
                    db = sqlite3.connect(path, timeout=0)
                    try:
                        self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
                        db.execute('BEGIN EXCLUSIVE')
                        ready.set()
                        if not release.wait(10): raise AssertionError('release timeout')
                        db.rollback()
                    finally: db.close()
                except BaseException as error:
                    errors.append(error); ready.set()
            original = read._connection
            @contextmanager
            def no_wait():
                db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=0)
                try:
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA read_uncommitted=OFF')
                    with db: yield db
                finally: db.close()
            thread = threading.Thread(target=writer)
            thread.start()
            try:
                self.assertTrue(ready.wait(5)); self.assertEqual(errors, [])
                read._connection = no_wait
                with self.assertRaises(sqlite3.OperationalError): audit.audit_history(read, customer_key='a')
                code = "import sys;sys.path.insert(0,sys.argv.pop(1));import x07_profile_history_audit as a;c=a.sqlite3.connect\ndef connect(*x,**k):\n k['timeout']=0;return c(*x,**k)\na.sqlite3.connect=connect;raise SystemExit(a.main())"
                child = subprocess.run([sys.executable, '-c', code, str(Path(audit.__file__).parent), '--database', str(path), '--customer', 'a'], capture_output=True, text=True, timeout=5)
                self.assertEqual(child.returncode, 2, child.stderr)
                self.assertEqual(json.loads(child.stdout), {'schema': 'leaf.x07.profile-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'})
                self.assertEqual(child.stderr, ''); self.assertNotIn('PRIVATE', child.stdout)
            finally:
                release.set(); thread.join(5); read._connection = original
            self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
            self.assertTrue(audit.audit_history(read, customer_key='a')['healthy'])
            child = subprocess.run([sys.executable, audit.__file__, '--database', str(path), '--customer', 'a'], capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertTrue(json.loads(child.stdout)['healthy'])
            self.assertEqual(read.application_record('old', 'a'), old)
            self.assertEqual(path.read_bytes(), before)
