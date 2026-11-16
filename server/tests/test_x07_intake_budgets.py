from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter,IntakeBudgetError


class IntakeBudgetTests(unittest.TestCase):
    def test_exact_cardinality_boundaries_work_excess_refuses_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            store=CustomerProfileStore(Path(directory)/'store.sqlite');workflow=ProfileWorkflow(store)
            ctx={'customer_key':'a','project_key':'p','drawing_key':'d'}
            row={'name':'Panel','layer':'M','x':0,'y':0};copy={'block':'Panel','layer':'M','design_window':[0,0,0,0],'oracle_count':1}
            normal={'context':ctx,'intake':{'inserts':[row]},'copies':[copy],'block_aliases':{'old':'Panel'},'limits':{'inserts':1,'aliases':1,'copies':1}}
            self.assertIsNotNone(WorkflowAdapter(workflow,**normal).load_workflow(ctx))
            for changes in [{'intake':{'inserts':[row,row]}},{'copies':[copy,copy]},{'block_aliases':{'old':'Panel','other':'Panel'}}]:
                with patch('x07_workflow_adapter.deepcopy',side_effect=AssertionError('must refuse before copying')):
                    with self.assertRaises(IntakeBudgetError) as error:WorkflowAdapter(workflow,**{**normal,**changes})
                    self.assertEqual(error.exception.code,'INTAKE_BUDGET_EXCEEDED')
            self.assertIsNone(store.latest('a'));self.assertIsNone(store.application_record('p','a'))

    def test_nesting_cycles_and_large_extra_containers_refuse_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow=ProfileWorkflow(CustomerProfileStore(Path(directory)/'store.sqlite'))
            ctx={'customer_key':'a','project_key':'p','drawing_key':'d'}
            cycle=[];cycle.append(cycle)
            for intake,limits in [({'inserts':[],'nested':[[[[[]]]]]},{'depth':3}),({'inserts':[],'cycle':cycle},{'depth':8}),
                                   ({'inserts':[],'extra':[0]*100},{'nodes':20}),({'inserts':[],'label':'雪'*50},{'string_bytes':30})]:
                with patch('x07_workflow_adapter.deepcopy',side_effect=AssertionError('copy not allowed')):
                    with self.assertRaises(IntakeBudgetError):WorkflowAdapter(workflow,context=ctx,intake=intake,copies=[],limits=limits)
