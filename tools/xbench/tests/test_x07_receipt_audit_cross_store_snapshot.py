from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from ._x07_receipt_audit_fixtures import audit,seed,ProfileWorkflow


class ReceiptAuditCrossStoreSnapshot(unittest.TestCase):
    def test_journal_then_profile_reads_can_observe_separate_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            profile,journal,read,receipts,pins=seed(directory)
            keepers=[sqlite3.connect(path) for path in [profile,journal]]
            original_profile,original_journal=read._connection,receipts._connect
            selected,committed=threading.Event(),threading.Event();errors=[];changes=[];settled=[]
            class JournalProxy:
                def __init__(self,db):self.db=db
                def __getattr__(self,name):return getattr(self.db,name)
                def execute(self,sql,params=()):
                    cursor=self.db.execute(sql,params)
                    if sql.startswith('SELECT project_key,result FROM main.confirmations'):
                        selected.set()
                        if not committed.wait(5):raise AssertionError('writer timeout')
                    return cursor
                def close(self):
                    changes.append(self.db.total_changes);self.db.close()
            @contextmanager
            def tracked_profile():
                with original_profile() as db:
                    try:yield db
                    finally:changes.append(db.total_changes)
            def writer():
                try:
                    if not selected.wait(5):raise AssertionError('journal barrier timeout')
                    store=audit.CustomerProfileStore(profile);writer_journal=audit.ConfirmationReceiptStore(journal)
                    context={'customer_key':'a','project_key':'new-a','drawing_key':'new-drawing'}
                    writer_journal.run(context,'1'*64,lambda:ProfileWorkflow(store).confirm({'inserts':[{'name':'A2','layer':'M','x':0,'y':0}]},**context,roles={'module':{'block':'A2','layer':'M'}},window=[0,0,0,0],expected_version=1),lambda value:None)
                    settled.append((profile.read_bytes(),journal.read_bytes()))
                except BaseException as error:errors.append(error)
                finally:committed.set()
            thread=threading.Thread(target=writer)
            try:
                for keeper in keepers:self.assertEqual(keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
                read._connection=tracked_profile;receipts._connect=lambda:JournalProxy(original_journal())
                thread.start()
                result=audit.audit_receipts(read,receipts,customer_key='a')
                self.assertTrue(result['healthy']);self.assertEqual(result['receipt_count'],1)
                self.assertEqual(result['issues'],{'invalid_receipts':0,'missing_references':0,'inconsistent_references':0})
                self.assertEqual(changes,[0,0])
            finally:
                selected.set();committed.set()
                if thread.ident is not None:thread.join(6)
                read._connection=original_profile;receipts._connect=original_journal
                for keeper in keepers:keeper.close()
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
            # Closing WAL keepers can checkpoint legitimate writer changes.
            before=(profile.read_bytes(),journal.read_bytes())
            reopened=audit.audit_receipts(read,receipts,customer_key='a')
            self.assertTrue(reopened['healthy']);self.assertEqual(reopened['receipt_count'],2)
            self.assertEqual(read.latest('a')['version'],2)
            for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
            self.assertEqual((profile.read_bytes(),journal.read_bytes()),before)
            self.assertEqual(len(settled),1)
