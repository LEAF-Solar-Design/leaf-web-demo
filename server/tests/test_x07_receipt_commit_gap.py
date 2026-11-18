"""Kill only an owned child after durable profile/pin, before journal insert."""
from pathlib import Path
from contextlib import closing
from queue import Queue
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES

class ReceiptCommitGap(unittest.TestCase):
    def test_restart_inspects_and_applies_instead_of_reconfirming(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); adapter(root)
            store=CustomerProfileStore(root/'profiles.sqlite')
            store.save_version('old',{'module':{'block':'Panel','layer':'M'}},expected_version=0)
            old=store.apply_to_project('old-project','old',expected_version=1)
            script='''
import sys,sqlite3
from pathlib import Path
sys.path.insert(0,sys.argv[2]);sys.path.insert(0,sys.argv[3])
from test_x07_receipt_retry_races import adapter,CONTEXT,COPIES
original=sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        if sql.startswith('INSERT INTO confirmations'):
            print('PROFILE_PIN_COMMITTED',flush=True)
            sys.stdin.readline()
        return super().execute(sql,*args,**kwargs)
sqlite3.connect=lambda *args,**kwargs:original(*args,**dict(kwargs,factory=Connection))
adapter(Path(sys.argv[1])).confirm_roles(CONTEXT,dict(COPIES[0],expected_version=0))
'''
            child=subprocess.Popen([sys.executable,'-u','-c',script,str(root),str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            lines=Queue(); reader=Thread(target=lambda:lines.put(child.stdout.readline()),daemon=True);reader.start()
            try: self.assertEqual(lines.get(timeout=15).strip(),'PROFILE_PIN_COMMITTED')
            finally:
                child.terminate();child.communicate(timeout=10);reader.join(timeout=2)
            current=adapter(root)
            plan=current.load_workflow(CONTEXT)
            self.assertEqual(plan['application_record']['version'],1)
            self.assertEqual(plan['compatibility']['decision'],'apply')
            with self.assertRaisesRegex(ValueError,'already pinned'): current.confirm_roles(CONTEXT,{**COPIES[0],'expected_version':0})
            recovered=current.apply_profile(CONTEXT,COPIES[0])
            self.assertEqual(recovered['applied_profile'],plan['application_record'])
            reopened=CustomerProfileStore(root/'profiles.sqlite')
            self.assertEqual(reopened.application_record('old-project','old'),old)
            with reopened._connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_versions WHERE customer_key='a'").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_applications WHERE customer_key='a'").fetchone()[0],1)
            with closing(sqlite3.connect(root/'receipts.sqlite')) as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0],0)
