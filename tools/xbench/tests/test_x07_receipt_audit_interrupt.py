from contextlib import contextmanager,redirect_stdout
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit,seed


class ReceiptAuditInterrupt(unittest.TestCase):
    def test_interrupt_each_data_and_policy_query_then_explicit_retry(self):
        profile_queries=['PRAGMA foreign_keys','PRAGMA read_uncommitted','PRAGMA database_list','SELECT name FROM sqlite_temp_master','SELECT * FROM project_applications','SELECT * FROM profile_versions','SELECT profile_id FROM customers']
        journal_queries=['PRAGMA foreign_keys','PRAGMA read_uncommitted','SELECT project_key,result FROM main.confirmations']
        with tempfile.TemporaryDirectory() as directory:
            profile,journal,read,receipts,pins=seed(directory)
            baseline=audit.audit_receipts(read,receipts,customer_key='a');before=(profile.read_bytes(),journal.read_bytes())
            for which,queries in [('profile',profile_queries),('journal',journal_queries)]:
                for prefix in queries:
                    with self.subTest(which=which,query=prefix):
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
                        original_profile=read._connection;original_journal=receipts._connect
                        @contextmanager
                        def connection():
                            with original_profile() as db:
                                try:yield Proxy(db)
                                finally:changes.append(db.total_changes)
                        def journal_connection():return Proxy(original_journal())
                        if which=='profile':read._connection=connection
                        else:receipts._connect=journal_connection
                        try:
                            with self.assertRaises(sqlite3.DatabaseError):audit.audit_receipts(read,receipts,customer_key='a')
                            output=io.StringIO()
                            args=['audit','--database',str(profile),'--journal',str(journal),'--customer','a']
                            with patch.object(sys,'argv',args),patch.object(audit,'CustomerProfileStore',return_value=read),patch.object(audit,'ConfirmationReceiptStore',return_value=receipts),redirect_stdout(output):
                                self.assertEqual(audit.main(),2)
                            self.assertEqual(json.loads(output.getvalue()),{'schema':'leaf.x07.receipt-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                            self.assertNotIn('healthy',output.getvalue());self.assertNotIn('PRIVATE',output.getvalue())
                        finally:read._connection=original_profile;receipts._connect=original_journal
                        self.assertTrue(callbacks)
                        if which=='profile':self.assertEqual(changes,[0,0])
                        self.assertEqual(audit.audit_receipts(read,receipts,customer_key='a'),baseline)
            for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
            self.assertEqual((profile.read_bytes(),journal.read_bytes()),before)
