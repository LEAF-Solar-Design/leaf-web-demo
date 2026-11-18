from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditLockRecovery(unittest.TestCase):
    def test_locks_on_either_store_refuse_then_retry_exactly(self):
        for side in ['profile', 'journal']:
            with self.subTest(store=side), tempfile.TemporaryDirectory() as directory:
                profile, journal, read, receipts, pins = seed(directory)
                baseline = audit.audit_receipts(read, receipts, customer_key='a')
                before = profile.read_bytes(), journal.read_bytes()
                ready, release = threading.Event(), threading.Event()
                errors = []

                def locker():
                    try:
                        with closing(sqlite3.connect(profile if side == 'profile' else journal)) as connection:
                            self.assertEqual(connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
                            connection.execute('BEGIN EXCLUSIVE')
                            ready.set()
                            if not release.wait(10):
                                raise AssertionError('lock release timeout')
                            connection.rollback()
                    except BaseException as error:
                        errors.append(error)
                        ready.set()

                real_connect = sqlite3.connect

                def no_wait(*args, **kwargs):
                    kwargs['timeout'] = 0
                    return real_connect(*args, **kwargs)

                thread = threading.Thread(target=locker)
                thread.start()
                try:
                    self.assertTrue(ready.wait(5))
                    self.assertEqual(errors, [])
                    with patch.object(sqlite3, 'connect', side_effect=no_wait):
                        with self.assertRaises(sqlite3.DatabaseError):
                            audit.audit_receipts(read, receipts, customer_key='a')
                    code = "import sys;sys.path.insert(0,sys.argv.pop(1));import x07_receipt_history_audit as a;c=a.sqlite3.connect\ndef connect(*x,**k):\n k['timeout']=0;return c(*x,**k)\na.sqlite3.connect=connect;raise SystemExit(a.main())"
                    child = subprocess.run([sys.executable, '-c', code, str(Path(audit.__file__).parent),
                                            '--database', str(profile), '--journal', str(journal), '--customer', 'a'],
                                           capture_output=True, text=True, timeout=5)
                    self.assertEqual(child.returncode, 2, child.stderr)
                    self.assertEqual(child.stderr, '')
                    self.assertEqual(json.loads(child.stdout), {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'})
                    self.assertNotIn('healthy', child.stdout)
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(audit.audit_receipts(read, receipts, customer_key='a'), baseline)
                for key, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + key, key), pin)
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
