import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SERVER = Path(__file__).resolve().parents[1]
WRITER = '''
import json,sys
from x07_customer_profile_store import CustomerProfileStore
store=CustomerProfileStore(sys.argv[1])
roles={'module':{'block':'Panel 雪','layer':'MODULES'}}
a=store.save_version('a',roles,expected_version=0)
old=store.apply_to_project('old-a','a',expected_version=1)
store.save_version('a',roles,expected_version=1)
store.save_version('b',roles,expected_version=0)
store.apply_to_project('old-b','b',expected_version=1)
print(json.dumps({'old':old,'a_id':a['profile_id']}))
'''
READER = '''
import json,sys,sqlite3
from x07_customer_profile_store import CustomerProfileStore,ProfileScopeError
store=CustomerProfileStore(sys.argv[1])
old=store.apply_to_project('old-a','a',expected_version=1)
new=store.apply_to_project('new-a','a',expected_version=2)
try: store.application_record('old-a','b')
except ProfileScopeError: isolated=True
else: isolated=False
db=sqlite3.connect(sys.argv[1])
try:
    try: db.execute("UPDATE profile_versions SET version=99 WHERE customer_key='a'")
    except sqlite3.IntegrityError: immutable=True
    else: immutable=False
finally: db.close()
print(json.dumps({'old':old,'new':new,'isolated':isolated,'immutable':immutable,
                  'latest':store.latest('a'),'b':store.latest('b')}))
'''


class ProcessRestartTests(unittest.TestCase):
    def run_process(self, code, path):
        process = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(path)], cwd=SERVER,
                                 capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_closed_writer_fresh_reader_retains_versions_scope_and_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'
            written = self.run_process(WRITER, path)  # writer exits before reader starts
            read = self.run_process(READER, path)
            self.assertEqual(read['old'], written['old'])
            self.assertEqual(read['old']['version'], 1)
            self.assertEqual(read['new']['version'], 2)
            self.assertEqual(read['latest']['profile_id'], written['a_id'])
            self.assertNotEqual(read['b']['profile_id'], written['a_id'])
            self.assertTrue(read['isolated']); self.assertTrue(read['immutable'])

    def test_second_fresh_reader_reopens_without_new_profile_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'
            self.run_process(WRITER, path)
            first, second = self.run_process(READER, path), self.run_process(READER, path)
            self.assertEqual(first, second)
