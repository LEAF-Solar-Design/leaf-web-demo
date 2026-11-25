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


class AuditSchemaFailures(unittest.TestCase):
    def test_supported_missing_empty_legacy_future_and_altered_schemas_are_nonrepairing(self):
        for kind in ['supported','missing','empty','legacy','future','altered']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'PRIVATE_SCHEMA.sqlite'
                pins = None
                if kind not in ['missing','empty']:
                    store = audit.CustomerProfileStore(path)
                    store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
                    store.apply_to_project('old','a')
                    with closing(sqlite3.connect(path)) as db, db:
                        if kind=='legacy': db.execute('PRAGMA user_version=0')
                        elif kind=='future': db.execute('PRAGMA user_version=99')
                        elif kind=='altered': db.execute('CREATE TABLE PRIVATE_ALTERED (value TEXT)')
                        pins = db.execute('SELECT * FROM project_applications ORDER BY project_key').fetchall()
                elif kind=='empty':
                    with closing(sqlite3.connect(path)): pass
                before = path.read_bytes() if path.exists() else None
                child = subprocess.run([sys.executable,audit.__file__,'--database',str(path),'--customer','a'],capture_output=True,text=True,timeout=10)
                self.assertEqual(child.stderr,'')
                if kind=='supported':
                    self.assertEqual(child.returncode,0)
                    self.assertTrue(json.loads(child.stdout)['healthy'])
                else:
                    self.assertEqual(child.returncode,2)
                    self.assertEqual(json.loads(child.stdout),{'schema':'leaf.x07.profile-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                    self.assertLess(len(child.stdout),200); self.assertNotIn('PRIVATE',child.stdout)
                self.assertEqual(path.read_bytes() if path.exists() else None,before)
                if pins is not None:
                    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                        self.assertEqual(db.execute('SELECT * FROM project_applications ORDER BY project_key').fetchall(),pins)
