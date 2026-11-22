from contextlib import closing, contextmanager
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditNoWriteAuthorizer(unittest.TestCase):
    def test_clean_and_corrupt_audits_attempt_only_metadata_reads(self):
        actions = ['INSERT','UPDATE','DELETE','CREATE_INDEX','CREATE_TABLE','CREATE_TEMP_INDEX','CREATE_TEMP_TABLE','CREATE_TEMP_TRIGGER','CREATE_TEMP_VIEW','CREATE_TRIGGER','CREATE_VIEW','DROP_INDEX','DROP_TABLE','DROP_TEMP_INDEX','DROP_TEMP_TABLE','DROP_TEMP_TRIGGER','DROP_TEMP_VIEW','DROP_TRIGGER','DROP_VIEW','ALTER_TABLE','REINDEX','ANALYZE','ATTACH','DETACH']
        forbidden = {getattr(sqlite3,'SQLITE_'+name) for name in actions}
        for corrupt in [False,True]:
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'store.sqlite'
                store = audit.CustomerProfileStore(path)
                a = store.save_version('a',{'module':{'block':'PRIVATE_A','layer':'M'}},expected_version=0)
                old = store.apply_to_project('old-a','a')
                b = store.save_version('b',{'module':{'block':'PRIVATE_B','layer':'B'}},expected_version=0)
                foreign = store.apply_to_project('old-b','b')
                if corrupt:
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',('a',b['profile_id'],3,'PRIVATE_UNREAD','synthetic'))
                        db.execute('INSERT INTO project_applications VALUES (?,?,?,?)',('bad','a',a['profile_id'],9))
                read = audit.CustomerProfileStore(path,read_only=True)
                original = read._connection
                attempts, changes = [], []
                @contextmanager
                def guarded():
                    with original() as db:
                        def authorize(action,table,column,*rest):
                            if action in forbidden or (action == sqlite3.SQLITE_READ and column == 'roles_json') or (action == sqlite3.SQLITE_PRAGMA and column is not None):
                                attempts.append((action,table,column)); return sqlite3.SQLITE_DENY
                            return sqlite3.SQLITE_OK
                        db.set_authorizer(authorize)
                        try: yield db
                        finally:
                            db.set_authorizer(None); changes.append(db.total_changes)
                before = path.read_bytes()
                read._connection = guarded
                try: result = audit.audit_history(read,customer_key='a')
                finally: read._connection = original
                issues = {'missing_profile':0,'empty_chain':0,'version_gaps':[{'first':2,'last':2}] if corrupt else [],'invalid_versions':0,'foreign_version_profiles':int(corrupt),'dangling_pin_versions':int(corrupt),'foreign_pin_profiles':0}
                self.assertEqual(result,{'schema':'leaf.x07.profile-history-audit.v1','customer_key':'a','profile_found':True,'version_count':1+int(corrupt),'pin_count':1+int(corrupt),'healthy':not corrupt,'issues':issues})
                self.assertEqual(attempts,[]); self.assertEqual(changes,[0])
                self.assertNotIn('PRIVATE',json.dumps(result))
                self.assertEqual(read.application_record('old-a','a'),old)
                self.assertEqual(read.application_record('old-b','b'),foreign)
                self.assertEqual(path.read_bytes(),before)
