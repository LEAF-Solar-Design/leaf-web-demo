from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter,ConfirmationReceiptStore


class ConfirmationReceiptLossTests(unittest.TestCase):
    def test_discard_success_reply_fresh_adapters_replay_one_version_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'profiles.sqlite';journal=Path(directory)/'receipts.sqlite'
            context={'customer_key':'a','project_key':'p','drawing_key':'d:v1'}
            intake={'inserts':[{'name':'Panel','layer':'M','x':x,'y':0} for x in [0,10]]}
            copies=[{'block':'Panel','layer':'M','design_window':[x,0,x,0],'oracle_count':1} for x in [0,10]]
            def adapter():return WorkflowAdapter(ProfileWorkflow(CustomerProfileStore(database)),context=context,intake=intake,copies=copies,
                                                 confirmation_receipts=ConfirmationReceiptStore(journal))
            request={**copies[0],'expected_version':0}
            adapter().confirm_roles(context,request) # successful receipt deliberately discarded
            replay=adapter().confirm_roles(context,request)
            self.assertEqual(adapter().confirm_roles(context,request),replay)
            store=CustomerProfileStore(database)
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0],1)
            for changed in [{**copies[1],'expected_version':0},{**request,'expected_version':1}]:
                with self.assertRaisesRegex(ValueError,'retry payload changed'):adapter().confirm_roles(context,changed)
            self.assertEqual(store.application_record('p','a'),replay['applied_profile'])
