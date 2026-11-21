from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditScopeKeyIdentity(unittest.TestCase):
    def test_exact_keys_match_independent_history_records(self):
        keys = ['tenant:a|b','tenant|a:b','Caf\u00e9','Cafe\u0301','customer\x00one','customer\x00two','case','CASE']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'store.sqlite'
            store = audit.CustomerProfileStore(path)
            models, pins, ids = {}, {}, {}
            for index,key in enumerate(keys):
                count = index%3+1
                for version in range(1,count+1):
                    saved = store.save_version(key,{'module':{'block':f'PRIVATE_ROLE_{index}_{version}','layer':'M'}},expected_version=version-1)
                    ids[key] = saved['profile_id']
                    if version == 1: pins[key] = store.apply_to_project(f'old-{index}',key)
                if index%2: store.apply_to_project(f'new-{index}',key)
                models[key] = {'versions': list(range(1,count+1)), 'pins': 1+index%2}
            with closing(sqlite3.connect(path)) as db, db:
                key = keys[2]
                extra = len(models[key]['versions'])+2
                db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',(key,ids[key],extra,'PRIVATE_UNREAD','synthetic'))
                models[key]['versions'].append(extra)
            read = audit.CustomerProfileStore(path,read_only=True)
            before = path.read_bytes()
            for index,key in enumerate(keys):
                with self.subTest(key=repr(key)):
                    versions = models[key]['versions']
                    absent = sorted(set(range(1,max(versions)+1))-set(versions))
                    gaps = [{'first':v,'last':v} for v in absent]
                    issues = {'missing_profile':0,'empty_chain':0,'version_gaps':gaps,'invalid_versions':0,'foreign_version_profiles':0,'dangling_pin_versions':0,'foreign_pin_profiles':0}
                    expected = {'schema':'leaf.x07.profile-history-audit.v1','customer_key':key,'profile_found':True,'version_count':len(versions),'pin_count':models[key]['pins'],'healthy':not absent,'issues':issues}
                    result = audit.audit_history(read,customer_key=key)
                    self.assertEqual(result,expected)
                    encoded = json.dumps(result)
                    self.assertEqual(json.loads(encoded)['customer_key'],key)
                    self.assertNotIn('PRIVATE',encoded)
                    for profile_id in ids.values(): self.assertNotIn(profile_id,encoded)
                    self.assertEqual(read.application_record(f'old-{index}',key),pins[key])
            self.assertEqual(path.read_bytes(),before)
