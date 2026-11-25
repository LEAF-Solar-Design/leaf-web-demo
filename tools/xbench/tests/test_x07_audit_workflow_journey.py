from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'server'))
sys.path.insert(0,str(ROOT/'tools/xbench'))
from x07_profile_history_audit import CustomerProfileStore, audit_history
from x07_workflow_adapter import WorkflowAdapter


class AuditWorkflowJourney(unittest.TestCase):
    def test_two_customer_adapter_edits_lost_replies_and_pinned_reopens_match_model(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_path,receipt_path = Path(directory)/'profiles.sqlite',Path(directory)/'receipts.sqlite'
            CustomerProfileStore(profile_path)
            intake,copies = {'inserts':[]},[]
            for block,layer,start,count in [('A1','MA1',0,2),('A2','MA2',10,3),('B1','MB1',20,1),('B2','MB2',30,2)]:
                intake['inserts'].extend({'name':block,'layer':layer,'x':start+x,'y':0} for x in range(count))
                copies.append({'block':block,'layer':layer,'design_window':[start,0,start+count-1,0],'oracle_count':count})
            model = {customer:{'versions':0,'pins':{}} for customer in ['a','b']}
            old_pins = {}
            def adapter(context):
                return WorkflowAdapter.from_paths(profile_path=profile_path,receipt_path=receipt_path,
                                                  context=context,intake=intake,copies=copies)
            def check():
                read = CustomerProfileStore(profile_path,read_only=True)
                before = profile_path.read_bytes()
                receipt_before = receipt_path.read_bytes() if receipt_path.exists() else None
                for customer,state in model.items():
                    count = state['versions']
                    issues = {'missing_profile':int(count==0),'empty_chain':0,'version_gaps':[],
                              'invalid_versions':0,'foreign_version_profiles':0,'dangling_pin_versions':0,'foreign_pin_profiles':0}
                    expected = {'schema':'leaf.x07.profile-history-audit.v1','customer_key':customer,
                                'profile_found':bool(count),'version_count':count,'pin_count':len(state['pins']),
                                'healthy':bool(count),'issues':issues}
                    self.assertEqual(audit_history(read,customer_key=customer),expected)
                    for project,version in state['pins'].items():
                        self.assertEqual(read.application_record(project,customer)['version'],version)
                    if customer in old_pins:
                        self.assertEqual(read.application_record('old-'+customer,customer),old_pins[customer])
                self.assertEqual(profile_path.read_bytes(),before)
                self.assertEqual(receipt_path.read_bytes() if receipt_path.exists() else None,receipt_before)
            check()
            for customer,index in [('a',0),('b',2)]:
                context = {'customer_key':customer,'project_key':'old-'+customer,'drawing_key':'old-drawing-'+customer}
                bound = adapter(context)
                bound.load_workflow(context); check()
                first = bound.confirm_roles(context,{**copies[index],'expected_version':0})
                self.assertEqual(first['applied_profile']['version'],1)
                model[customer]['versions'] = 1; model[customer]['pins'][context['project_key']] = 1
                old_pins[customer] = deepcopy(first['applied_profile'])
                check()
                edited_roles = {'module':{'block':copies[index+1]['block'],'layer':copies[index+1]['layer']}}
                saved = bound.save_profile(customer,edited_roles,expected_version=1)
                self.assertEqual(saved['version'],2)
                model[customer]['versions'] = 2
                check()
                lost_context = {'customer_key':customer,'project_key':'lost-'+customer,'drawing_key':'lost-drawing-'+customer}
                payload = {**copies[index+1],'expected_version':2}
                def discarded_reply():
                    adapter(lost_context).confirm_roles(lost_context,payload)
                    raise OSError('synthetic discarded successful callback reply')
                with self.assertRaisesRegex(OSError,'discarded successful'):
                    discarded_reply()
                model[customer]['versions'] = 3; model[customer]['pins'][lost_context['project_key']] = 3
                check()
                before_profile,before_receipts = profile_path.read_bytes(),receipt_path.read_bytes()
                replay = adapter(lost_context).confirm_roles(lost_context,payload)
                self.assertEqual(replay['applied_profile']['version'],3)
                self.assertEqual(profile_path.read_bytes(),before_profile)
                self.assertEqual(receipt_path.read_bytes(),before_receipts)
                check()
                reopened = adapter(context)
                reopened.load_workflow(context)
                pinned = reopened.apply_profile(context,copies[index])
                self.assertEqual(pinned['applied_profile'],old_pins[customer])
                check()
            self.assertEqual({key:value['versions'] for key,value in model.items()},{'a':3,'b':3})
