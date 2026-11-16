import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore

CHILD = '''
import sqlite3,sys
from x07_customer_profile_store import CustomerProfileStore
original=sqlite3.connect
class Paused(sqlite3.Connection):
    def execute(self,sql,*args,**kw):
        result=super().execute(sql,*args,**kw)
        if sql.startswith('INSERT INTO profile_versions'):
            print('INSERTED',flush=True)
            sys.stdin.readline() # parent terminates us while the transaction is open
        return result
def connect(*args,**kw):
    kw['factory']=Paused
    return original(*args,**kw)
sqlite3.connect=connect
store=CustomerProfileStore(sys.argv[1])
store.save_version(sys.argv[2],{'module':{'block':'Panel','layer':'MODULES'}},expected_version=int(sys.argv[3]))
store.apply_to_project('crashed-project',sys.argv[2])
'''


class CrashAtomicityTests(unittest.TestCase):
    def crash(self, path, customer, expected):
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', CHILD, str(path), customer, str(expected)],
                                 cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding='utf-8')
        mailbox = queue.Queue()
        reader = threading.Thread(target=lambda: mailbox.put(child.stdout.readline()), daemon=True); reader.start()
        try:
            self.assertEqual(mailbox.get(timeout=10), 'INSERTED\n')
            child.terminate(); child.wait(timeout=5)
            self.assertNotEqual(child.returncode, 0)
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=5)
            reader.join(timeout=5)
            child.stdin.close(); child.stdout.close(); child.stderr.close()

    def test_crash_after_version_insert_keeps_prior_version_pin_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'; store = CustomerProfileStore(path)
            roles = {'module': {'block': 'Panel', 'layer': 'MODULES'}}
            prior = store.save_version('a', roles, expected_version=0)
            pin = store.apply_to_project('old-project', 'a')
            self.crash(path, 'a', 1)
            reopened = CustomerProfileStore(path)
            self.assertEqual(reopened.latest('a'), prior)
            self.assertEqual(reopened.application_record('old-project', 'a'), pin)
            self.assertIsNone(reopened.application_record('crashed-project', 'a'))
            self.assertEqual(reopened.save_version('a', roles, expected_version=1)['version'], 2)
            self.assertEqual(reopened.apply_to_project('retry-project', 'a')['version'], 2)
            self.assertEqual(reopened.application_record('old-project', 'a'), pin)

    def test_first_customer_crash_leaves_no_root_version_or_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'; CustomerProfileStore(path)
            self.crash(path, 'b', 0)
            reopened = CustomerProfileStore(path)
            self.assertIsNone(reopened.latest('b'))
            self.assertIsNone(reopened.application_record('crashed-project', 'b'))
            with reopened._connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 0)
            self.assertEqual(reopened.save_version('b', {'module': {'block': 'Panel', 'layer': 'MODULES'}}, expected_version=0)['version'], 1)
