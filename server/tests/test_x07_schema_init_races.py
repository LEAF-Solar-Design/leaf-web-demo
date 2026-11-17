import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore

CHILD = '''
import json,sqlite3,sys,time
from x07_customer_profile_store import CustomerProfileStore
if sys.argv[3]=='short':
    original=sqlite3.connect
    def connect(*args,**kw):
        kw['timeout']=0.05
        return original(*args,**kw)
    sqlite3.connect=connect
print('READY',flush=True)
sys.stdin.readline()
start=time.monotonic()
try:
    store=CustomerProfileStore(sys.argv[1])
    customer='customer-'+sys.argv[2]
    store.save_version(customer,{'module':{'block':'Panel','layer':'MODULES'}},expected_version=0)
    pin=store.apply_to_project('project-'+sys.argv[2],customer)
    print(json.dumps({'state':'ok','schema':store.schema_status,'pin':pin}))
except sqlite3.OperationalError as error:
    print(json.dumps({'state':'busy','message':str(error),'elapsed':time.monotonic()-start}))
'''


class SchemaInitRaceTests(unittest.TestCase):
    def spawn(self, path, index, mode='normal'):
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', CHILD, str(path), str(index), mode],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8')
        self.addCleanup(self.close, child)
        mailbox = queue.Queue()
        reader = threading.Thread(target=lambda: mailbox.put(child.stdout.readline()), daemon=True); reader.start()
        self.assertEqual(mailbox.get(timeout=10), 'READY\n'); reader.join(timeout=5)
        return child

    def close(self, child):
        if child.poll() is None: child.kill(); child.wait(timeout=5)
        for stream in [child.stdin, child.stdout, child.stderr]:
            if not stream.closed: stream.close()

    def finish(self, child):
        stdout, stderr = child.communicate('go\n', timeout=15)
        self.assertEqual(child.returncode, 0, stderr)
        return json.loads(stdout)

    def test_independent_processes_initialize_one_coherent_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'
            children = [self.spawn(path, i) for i in range(2)]
            # Both processes reach READY before either is released.
            for child in children: child.stdin.write('go\n'); child.stdin.flush()
            results = []
            for child in children:
                stdout, stderr = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, stderr); results.append(json.loads(stdout))
            self.assertTrue(all(row['state'] == 'ok' for row in results))
            self.assertEqual(sum(row['schema']['decision'] == 'initialize' for row in results), 1)
            reopened = CustomerProfileStore(path)
            self.assertEqual(reopened.schema_status['decision'], 'supported')
            with reopened._connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 4)
            for index in range(2):
                self.assertEqual(reopened.application_record('project-'+str(index), 'customer-'+str(index))['version'], 1)

    def test_bounded_contention_refuses_then_fresh_process_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'; CustomerProfileStore(path)
            lock = sqlite3.connect(path); lock.execute('BEGIN IMMEDIATE')
            try:
                result = self.finish(self.spawn(path, 1, 'short'))
                self.assertEqual(result['state'], 'busy'); self.assertIn('locked', result['message'])
                self.assertLess(result['elapsed'], 10)
            finally: lock.rollback(); lock.close()
            self.assertEqual(self.finish(self.spawn(path, 1))['state'], 'ok')
