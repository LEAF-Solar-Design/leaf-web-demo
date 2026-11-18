from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


class DrawingRevisionReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=CustomerProfileStore(Path(self.temp.name)/'store.sqlite');self.workflow=ProfileWorkflow(self.store)
        self.ctx={'customer_key':'a','project_key':'p','drawing_key':'same:v1'}
        self.intake={'revision':'one','inserts':[{'name':'Panel','layer':'MODULES','x':0,'y':0}]}
        self.copy={'block':'Panel','layer':'MODULES','design_window':[0,0,0,0],'oracle_count':1}
        self.original=WorkflowAdapter(self.workflow,context=self.ctx,intake=self.intake,copies=[self.copy])
        self.token=self.original.load_workflow(self.ctx)['plan_token']

    def test_saved_plan_refuses_changed_revision_roles_or_bounds_before_confirmation(self):
        changed_roles=deepcopy(self.intake);changed_roles['inserts'][0]['name']='Other'
        variants=[({**self.intake,'revision':'two'},self.copy),
                  (changed_roles,{**self.copy,'block':'Other'}),
                  (self.intake,{**self.copy,'design_window':[-1,-1,1,1]})]
        for intake,copy in variants:
            adapter=WorkflowAdapter(self.workflow,context=self.ctx,intake=intake,copies=[copy])
            for method in ['confirm_roles','apply_profile']:
                with self.assertRaisesRegex(ValueError,'snapshot changed'):
                    getattr(adapter,method)(self.ctx,{**self.copy,'plan_token':self.token,'expected_version':0})
            self.assertIsNone(self.store.latest('a'));self.assertIsNone(self.store.application_record('p','a'))

    def test_matching_snapshot_token_succeeds_and_stale_reopen_preserves_pin(self):
        pin=self.original.confirm_roles(self.ctx,{**self.copy,'plan_token':self.token,'expected_version':0})['applied_profile']
        adapter=WorkflowAdapter(self.workflow,context=self.ctx,intake={**self.intake,'revision':'two'},copies=[self.copy])
        with self.assertRaisesRegex(ValueError,'snapshot changed'):
            adapter.apply_profile(self.ctx,{**self.copy,'plan_token':self.token})
        self.assertEqual(self.store.application_record('p','a'),pin)
        fresh=adapter.load_workflow(self.ctx)
        self.assertEqual(adapter.apply_profile(self.ctx,{**self.copy,'plan_token':fresh['plan_token']})['applied_profile'],pin)
