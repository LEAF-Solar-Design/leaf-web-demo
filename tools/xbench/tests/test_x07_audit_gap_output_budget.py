from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditGapOutputBudget(unittest.TestCase):
    def test_maximum_fragmented_chain_has_linear_bounded_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'store.sqlite'
            store = audit.CustomerProfileStore(path)
            saved = store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
            old = store.apply_to_project('old','a')
            count = audit.MAX_AUDIT_ROWS
            stride = (audit.MAX_PROFILE_VERSION-1)//(count-1)
            versions = [1+stride*index for index in range(count)]
            with closing(sqlite3.connect(path)) as db, db:
                db.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)',(('a',saved['profile_id'],v,'{}','synthetic') for v in versions[1:]))
            read = audit.CustomerProfileStore(path,read_only=True)
            before = path.read_bytes()
            # The child timeout also bounds a regression that enumerates huge absent ranges.
            child = subprocess.run([sys.executable,audit.__file__,'--database',str(path),'--customer','a'],capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,1,child.stderr); self.assertEqual(child.stderr,'')
            self.assertLess(len(child.stdout.encode('utf-8')),1024*1024)
            result = json.loads(child.stdout)
            gaps = [{'first':left+1,'last':right-1} for left,right in zip(versions,versions[1:])]
            self.assertEqual(len(gaps),count-1)
            self.assertEqual(result['issues'],{'missing_profile':0,'empty_chain':0,'version_gaps':gaps,'invalid_versions':0,'foreign_version_profiles':0,'dangling_pin_versions':0,'foreign_pin_profiles':0})
            self.assertFalse(result['healthy']); self.assertEqual(result['version_count'],count)
            self.assertEqual(result['pin_count'],1)
            self.assertEqual(read.application_record('old','a'),old)
            self.assertEqual(path.read_bytes(),before)
