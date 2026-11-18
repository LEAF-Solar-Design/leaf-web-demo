"""Independent offline adapters share a journal, never a connection."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter, ConfirmationReceiptStore

CONTEXT = dict(customer_key='a', project_key='p', drawing_key='d:v1')
INTAKE = {'inserts':[dict(name='Panel',layer='M',x=x,y=0) for x in (0,10)]}
COPIES = [dict(block='Panel',layer='M',design_window=[x,0,x,0],oracle_count=1) for x in (0,10)]

def adapter(root):
    return WorkflowAdapter(ProfileWorkflow(CustomerProfileStore(root/'profiles.sqlite')),
        context=CONTEXT,intake=INTAKE,copies=COPIES,
        confirmation_receipts=ConfirmationReceiptStore(root/'receipts.sqlite'))

class ReceiptRetryRaces(unittest.TestCase):
    def test_identical_retry_and_conflicting_payloads(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); adapter(root); barrier=Barrier(2)
                def worker(index):
                    current=adapter(root)
                    barrier.wait(timeout=10)
                    try: return current.confirm_roles(CONTEXT,{**COPIES[index if conflict else 0],'expected_version':0})
                    except ValueError as error: return str(error)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(worker,index) for index in range(2)]
                    results=[future.result(timeout=15) for future in futures]
                if conflict:
                    self.assertEqual(sum(isinstance(item,dict) for item in results),1)
                    self.assertIn('confirmation retry payload changed',results)
                else: self.assertEqual(results[0],results[1])
                store=CustomerProfileStore(root/'profiles.sqlite')
                with store._connection() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0],1)
                self.assertEqual(store.application_record('p','a')['version'],1)
