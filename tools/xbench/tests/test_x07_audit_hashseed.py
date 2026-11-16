from contextlib import closing
from pathlib import Path
import itertools
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditHashseed(unittest.TestCase):
    def test_cli_bytes_match_across_corrupt_insertion_orders_and_child_seeds(self):
        outputs = []
        for order in itertools.permutations([3,4,7]):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'store.sqlite'
                store = audit.CustomerProfileStore(path)
                a = store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
                old = store.apply_to_project('old-a','a')
                b = store.save_version('b',{'module':{'block':'PRIVATE_B','layer':'B'}},expected_version=0)
                foreign = store.apply_to_project('old-b','b')
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('PRAGMA foreign_keys=OFF')
                    for version in order:
                        db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',('a',b['profile_id'] if version==4 else a['profile_id'],version,'PRIVATE_ROLE','synthetic'))
                    pins = {3:('bad-missing','a',a['profile_id'],9),4:('bad-foreign','a',b['profile_id'],3),7:('new-a','a',a['profile_id'],7)}
                    for version in order: db.execute('INSERT INTO project_applications VALUES (?,?,?,?)',pins[version])
                before = path.read_bytes()
                for seed in ['0','1','4294967295']:
                    child = subprocess.run([sys.executable,audit.__file__,'--database',str(path),'--customer','a'],
                                           env={'PYTHONHASHSEED':seed,'PYTHONUTF8':'1'},capture_output=True,timeout=10)
                    self.assertEqual(child.returncode,1,child.stderr)
                    self.assertEqual(child.stderr,b'')
                    self.assertNotIn(b'PRIVATE',child.stdout)
                    self.assertNotIn(b['profile_id'].encode(),child.stdout)
                    outputs.append(child.stdout)
                self.assertEqual(store.application_record('old-a','a'),old)
                self.assertEqual(store.application_record('old-b','b'),foreign)
                self.assertEqual(path.read_bytes(),before)
        self.assertTrue(all(value==outputs[0] for value in outputs))
        result = json.loads(outputs[0])
        self.assertEqual(result['issues']['version_gaps'],[{'first':2,'last':2},{'first':5,'last':6}])
        self.assertEqual(result['issues']['foreign_version_profiles'],1)
