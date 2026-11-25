"""Real rollback-journal recovery after observed dirty page spill."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
from queue import Queue
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class HotJournalRecovery(unittest.TestCase):
    def test_terminated_delete_journal_writer_rolls_back_and_allows_retry(self):
        script = '''
import json,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete'
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=8')
db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE')
roles=json.dumps({'module':{'block':'Panel','layer':'M'+'x'*60000}})
for version in range(2,42):
    db.execute('INSERT INTO profile_versions SELECT customer_key,profile_id,?, ?,created_at FROM profile_versions WHERE customer_key=? AND version=1',(version,roles,'a'))
print('SPILLED_UNCOMMITTED',flush=True)
sys.stdin.readline()
raise RuntimeError('parent must terminate before commit')
'''
        reopen = '''
import json,sqlite3,sys
sys.path.insert(0,sys.argv[2])
from x07_customer_profile_store import CustomerProfileStore
store=CustomerProfileStore(sys.argv[1])
with store._connection() as db:
    versions=[r[0] for r in db.execute('SELECT version FROM profile_versions ORDER BY version')]
    integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
print(json.dumps([versions,store.application_record('old','a')['version'],integrity]))
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'
            store = CustomerProfileStore(path)
            store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M'}}, expected_version=0)
            old = store.apply_to_project('old', 'a', expected_version=1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
            original = hashlib.sha256(path.read_bytes()).hexdigest()
            child = subprocess.Popen([sys.executable, '-u', '-c', script, str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = Queue()
            thread = Thread(target=lambda: lines.put(child.stdout.readline().strip()), daemon=True)
            thread.start()
            try:
                self.assertEqual(lines.get(timeout=15), 'SPILLED_UNCOMMITTED')
                journal = Path(str(path) + '-journal')
                self.assertTrue(journal.exists())
                header = journal.read_bytes()[:28]
                self.assertGreater(journal.stat().st_size, 512)
                self.assertEqual(header[:8], bytes.fromhex('d9d505f920a163d7'))
                self.assertNotEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original,
                                    'main database must contain spilled uncommitted pages')
                child.terminate(); child.communicate(timeout=10)
            finally:
                if child.poll() is None: child.kill()
                child.communicate(timeout=10); thread.join(timeout=2)
            fresh = subprocess.run([sys.executable, '-c', reopen, str(path), str(Path(__file__).resolve().parents[1])],
                                   capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual(json.loads(fresh.stdout), [[1], 1, 'ok'])
            self.assertEqual(store.application_record('old', 'a'), old)
            self.assertFalse(Path(str(path) + '-journal').exists())
            store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M2'}}, expected_version=1)
            store.apply_to_project('new', 'a', expected_version=2)
            self.assertEqual(store.application_record('old', 'a')['version'], 1)
            self.assertEqual(store.application_record('new', 'a')['version'], 2)
