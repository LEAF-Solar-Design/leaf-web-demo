from contextlib import closing
from pathlib import Path
from queue import Queue
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from ._x07_receipt_audit_fixtures import seed
from .test_x07_receipt_audit_schema_failures import invoke


class ReceiptHotJournal(unittest.TestCase):
    def test_crashed_spilled_writers_refuse_then_explicitly_recover(self):
        script = '''
import sqlite3,sys
db=sqlite3.connect(sys.argv[1])
assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete'
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=8')
db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE')
for index in range(40):
    if sys.argv[2]=='profile':
        db.execute('INSERT INTO profile_versions SELECT customer_key,profile_id,?, ?,created_at FROM profile_versions WHERE customer_key=? AND version=1',(index+2,'X'*60000,'a'))
    else:
        db.execute('INSERT INTO confirmations VALUES (?,?,?,?)',('pending-'+str(index),'a','x','X'*60000))
print('SPILLED',flush=True)
sys.stdin.readline()
'''
        for side in ('profile', 'journal'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                profile, journal, read, receipts, pins = seed(directory)
                target = profile if side == 'profile' else journal
                original = target.read_bytes()
                child = subprocess.Popen([sys.executable, '-u', '-P', '-c', script, str(target), side], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                lines = Queue()
                thread = Thread(target=lambda: lines.put(child.stdout.readline().strip()), daemon=True)
                thread.start()
                try:
                    self.assertEqual(lines.get(timeout=15), 'SPILLED')
                    rollback = Path(str(target) + '-journal')
                    self.assertEqual(rollback.read_bytes()[:8], bytes.fromhex('d9d505f920a163d7'))
                    self.assertNotEqual(target.read_bytes(), original)
                    child.terminate(); child.communicate(timeout=10)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10); thread.join(2)
                    child.stdin.close(); child.stdout.close(); child.stderr.close()
                before = {path.name: path.read_bytes() for path in Path(directory).iterdir()}
                self.assertEqual(invoke(profile, journal), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()}, before)
                with closing(sqlite3.connect(target)) as recovery:
                    self.assertEqual(recovery.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(invoke(profile, journal)[0], 0)
                for customer, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + customer, customer), pin)
