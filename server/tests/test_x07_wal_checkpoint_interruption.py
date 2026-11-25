from contextlib import closing
from pathlib import Path
from queue import Queue
import json
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class WALCheckpointInterruption(unittest.TestCase):
    def test_killed_active_checkpoint_reopens_committed_versions_and_pins(self):
        child_script = '''
import sys,sqlite3
from contextlib import closing
sys.path.insert(0,sys.argv[2])
from x07_customer_profile_store import CustomerProfileStore
store=CustomerProfileStore(sys.argv[1])
store.save_version('a',{'module':{'block':'Panel','layer':'M2'}},expected_version=1)
store.apply_to_project('new','a',expected_version=2)
print('COMMITTED',flush=True)
sys.stdin.readline()
with closing(sqlite3.connect(sys.argv[1],timeout=10)) as db:
    db.set_trace_callback(lambda sql:print('CHECKPOINT_ENTER',flush=True) if sql.startswith('PRAGMA wal_checkpoint') else None)
    db.execute('PRAGMA wal_checkpoint(FULL)').fetchone()
'''
        reopen_script = '''
import sys,json
sys.path.insert(0,sys.argv[2])
from x07_customer_profile_store import CustomerProfileStore
store=CustomerProfileStore(sys.argv[1],read_only=True)
with store._connection() as db:
    versions=[row[0] for row in db.execute('SELECT version FROM profile_versions ORDER BY version')]
    integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
print(json.dumps([versions,store.application_record('old','a')['version'],store.application_record('new','a')['version'],integrity]))
'''
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'store.sqlite'
            store = CustomerProfileStore(database)
            store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M1'}}, expected_version=0)
            store.apply_to_project('old', 'a', expected_version=1)
            with closing(sqlite3.connect(database)) as reader, closing(sqlite3.connect(database, timeout=0)) as probe:
                reader.execute('PRAGMA journal_mode=WAL')
                reader.execute('BEGIN')
                reader.execute('SELECT COUNT(*) FROM profile_versions').fetchone()
                child = subprocess.Popen([sys.executable, '-u', '-c', child_script, str(database),
                    str(Path(__file__).resolve().parents[1])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                lines = Queue()
                def collect():
                    for _ in range(2):
                        line = child.stdout.readline()
                        lines.put(line.strip())
                thread = Thread(target=collect, daemon=True)
                thread.start()
                try:
                    self.assertEqual(lines.get(timeout=15), 'COMMITTED')
                    child.stdin.write('CHECKPOINT\n')
                    child.stdin.flush()
                    self.assertEqual(lines.get(timeout=15), 'CHECKPOINT_ENTER')
                    # A second checkpointer returns busy only while the child's
                    # real checkpoint lock is held. The reader keeps it active.
                    deadline = time.monotonic() + 5
                    busy = False
                    while time.monotonic() < deadline:
                        if probe.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()[0] == 1:
                            busy = True
                            break
                    self.assertTrue(busy, 'active checkpoint was not observed')
                    child.terminate()
                    child.communicate(timeout=10)
                finally:
                    if child.poll() is None: child.kill()
                    child.communicate(timeout=10)
                    thread.join(timeout=2)
                    reader.rollback()
            result = subprocess.run([sys.executable, '-c', reopen_script, str(database),
                str(Path(__file__).resolve().parents[1])], capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual(json.loads(result.stdout), [[1, 2], 1, 2, 'ok'])
