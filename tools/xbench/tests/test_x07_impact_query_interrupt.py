from contextlib import contextmanager,redirect_stdout
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit,seed
import x07_profile_change_impact as impact


class ImpactQueryInterrupt(unittest.TestCase):
    def test_interrupt_each_real_select_then_recover_exact_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            profile,_,read,_,pins=seed(directory);store=audit.CustomerProfileStore(profile)
            saved=store.save_version('a',{'module':{'block':'EDIT','layer':'M'}},expected_version=1)
            args={'customer_key':'a','profile_id':saved['profile_id'],'before_version':1,'after_version':2}
            baseline=impact.preview_change(read,**args);before=profile.read_bytes();original=read._connection
            for prefix in ['SELECT profile_id FROM main.customers','SELECT * FROM main.profile_versions','SELECT * FROM main.project_applications']:
                with self.subTest(query=prefix):
                    callbacks=[];changes=[]
                    class Proxy:
                        def __init__(self,db):self.db=db
                        def __getattr__(self,name):return getattr(self.db,name)
                        def execute(self,sql,params=()):
                            if sql.startswith(prefix):
                                def interrupt():callbacks.append(True);return 1
                                self.db.set_progress_handler(interrupt,1)
                                try:return self.db.execute(sql,params)
                                finally:self.db.set_progress_handler(None,0)
                            return self.db.execute(sql,params)
                    @contextmanager
                    def failed():
                        with original() as db:
                            try:yield Proxy(db)
                            finally:changes.append(db.total_changes)
                    read._connection=failed
                    try:
                        with self.assertRaises(sqlite3.DatabaseError):impact.preview_change(read,**args)
                        output=io.StringIO()
                        argv=['impact','--database',str(profile),'--customer','a','--profile-id',saved['profile_id'],'--before','1','--after','2']
                        with patch.object(sys,'argv',argv),patch.object(impact,'CustomerProfileStore',return_value=read),redirect_stdout(output):self.assertEqual(impact.main(),2)
                        self.assertEqual(json.loads(output.getvalue()),{'schema':'leaf.x07.profile-change-impact.v1','error':'SCOPED_PREVIEW_UNAVAILABLE'})
                        self.assertNotIn('retaining',output.getvalue());self.assertNotIn('PRIVATE',output.getvalue())
                    finally:read._connection=original
                    self.assertTrue(callbacks);self.assertEqual(changes,[0,0])
                    self.assertEqual(impact.preview_change(read,**args),baseline)
            for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
            self.assertEqual(profile.read_bytes(),before)
