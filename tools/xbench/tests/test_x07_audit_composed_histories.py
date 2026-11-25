from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit
from x07_profile_change_impact import preview_change
from x07_workflow_adapter import WorkflowAdapter


class AuditComposedHistories(unittest.TestCase):
    def test_settled_two_customer_commands_match_independent_value_model(self):
        with tempfile.TemporaryDirectory() as directory:
            profile,journal=Path(directory)/'profiles.sqlite',Path(directory)/'journal.sqlite'
            audit.CustomerProfileStore(profile);audit.ConfirmationReceiptStore(journal)
            intake={'inserts':[]};copies=[]
            for block,layer,start,count in [('A1','MA1',0,2),('A2','MA2',10,3),('B1','MB1',20,1),('B2','MB2',30,2)]:
                intake['inserts'].extend({'name':block,'layer':layer,'x':start+i,'y':0} for i in range(count))
                copies.append({'block':block,'layer':layer,'design_window':[start,0,start+count-1,0],'oracle_count':count})
            model={key:{'roles':[],'pins':{},'receipts':0} for key in ['a','b']};old={}
            def roles(index):return {'module':{'block':copies[index]['block'],'layer':copies[index]['layer']}}
            def adapter(context):return WorkflowAdapter.from_paths(profile_path=profile,receipt_path=journal,context=context,intake=intake,copies=copies)
            checks=[]
            def check(command):
                read=audit.CustomerProfileStore(profile,read_only=True);receipts=audit.ConfirmationReceiptStore(journal,read_only=True)
                before=(profile.read_bytes(),journal.read_bytes())
                for customer,state in model.items():
                    expected={'schema':'leaf.x07.receipt-history-audit.v1','customer_key':customer,'receipt_count':state['receipts'],'healthy':True,'issues':{'invalid_receipts':0,'missing_references':0,'inconsistent_references':0}}
                    self.assertEqual(audit.audit_receipts(read,receipts,customer_key=customer),expected)
                    with read._connection() as db:
                        records=db.execute('SELECT version,roles_json FROM profile_versions WHERE customer_key=? ORDER BY version',(customer,)).fetchall()
                    self.assertEqual([(row['version'],json.loads(row['roles_json'])) for row in records],list(enumerate(state['roles'],1)))
                    if not state['roles']:continue
                    first,last=state['roles'][0],state['roles'][-1]
                    result=preview_change(read,customer_key=customer,profile_id=read.latest(customer)['profile_id'],before_version=1,after_version=len(state['roles']))
                    self.assertEqual(result['added'],{k:last[k] for k in last.keys()-first.keys()})
                    self.assertEqual(result['removed'],{k:first[k] for k in first.keys()-last.keys()})
                    self.assertEqual(result['changed'],{k:{'before':first[k],'after':last[k]} for k in first.keys()&last.keys() if first[k]!=last[k]})
                    expected_pins=[]
                    for project,version in sorted(state['pins'].items()):
                        pinned=state['roles'][version-1]
                        different=sorted(k for k in pinned.keys()|last.keys() if pinned.get(k)!=last.get(k))
                        if different:expected_pins.append({'project_key':project,'version':version,'different_roles':different})
                        record=read.application_record(project,customer)
                        self.assertEqual((record['version'],record['roles']),(version,pinned))
                    self.assertEqual(result['retaining_different_roles'],expected_pins)
                    if customer in old:self.assertEqual(read.application_record('old-'+customer,customer),old[customer])
                self.assertEqual((profile.read_bytes(),journal.read_bytes()),before);checks.append(command)
            check('empty settled stores')
            for customer,index in [('a',0),('b',2)]:
                context={'customer_key':customer,'project_key':'old-'+customer,'drawing_key':'old-drawing-'+customer}
                bound=adapter(context);bound.load_workflow(context);check('proposal '+customer)
                result=bound.confirm_roles(context,{**copies[index],'expected_version':0})
                model[customer]['roles'].append(roles(index));model[customer]['pins'][context['project_key']]=1;model[customer]['receipts']+=1
                old[customer]=deepcopy(result['applied_profile']);check('confirm '+customer)
                bound.save_profile(customer,roles(index+1),expected_version=1)
                model[customer]['roles'].append(roles(index+1));check('edit '+customer)
                lost_context={'customer_key':customer,'project_key':'lost-'+customer,'drawing_key':'lost-drawing-'+customer}
                payload={**copies[index+1],'expected_version':2}
                def discarded_reply():
                    adapter(lost_context).confirm_roles(lost_context,payload)
                    raise OSError('synthetic lost successful reply')
                with self.assertRaisesRegex(OSError,'lost successful'):discarded_reply()
                model[customer]['roles'].append(roles(index+1));model[customer]['pins'][lost_context['project_key']]=3;model[customer]['receipts']+=1
                check('discarded reply '+customer)
                before=(profile.read_bytes(),journal.read_bytes())
                replay=adapter(lost_context).confirm_roles(lost_context,payload)
                self.assertEqual(replay['applied_profile']['version'],3)
                self.assertEqual((profile.read_bytes(),journal.read_bytes()),before);check('cached retry '+customer)
                reopened=adapter(context);reopened.load_workflow(context);check('reload '+customer)
                self.assertEqual(reopened.apply_profile(context,copies[index])['applied_profile'],old[customer]);check('pinned reopen '+customer)
            self.assertEqual(len(checks),15)
