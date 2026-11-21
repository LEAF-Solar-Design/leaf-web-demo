from pathlib import Path
import tempfile
import unittest
from test_x07_receipt_retry_races import CONTEXT, COPIES, INTAKE
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter, ConfirmationReceiptStore


class ReceiptScopeNamespace(unittest.TestCase):
    def test_shared_request_id_replays_scoped_projects_and_refuses_project_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def bound(context):
                return WorkflowAdapter(ProfileWorkflow(CustomerProfileStore(root / 'profiles.sqlite')),
                    context=context, intake=INTAKE, copies=COPIES,
                    confirmation_receipts=ConfirmationReceiptStore(root / 'receipts.sqlite'))
            a = CONTEXT
            b = dict(customer_key='b', project_key='b-project', drawing_key='b-drawing')
            request = {**COPIES[0], 'expected_version': 0, 'request_id': 'same-request-id'}
            first = bound(a).confirm_roles(a, request)
            second = bound(b).confirm_roles(b, request)
            self.assertEqual(bound(a).confirm_roles(a, request), first)
            self.assertEqual(bound(b).confirm_roles(b, request), second)
            collision = {**b, 'project_key': a['project_key']}
            before = tuple((root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite'))
            with self.assertRaises(ValueError) as caught:
                bound(collision).confirm_roles(collision, request)
            self.assertNotIn(first['applied_profile']['profile_id'], str(caught.exception))
            self.assertLess(len(str(caught.exception)), 100)
            self.assertEqual(tuple((root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite')), before)
            self.assertEqual(bound(a).workflow.store.application_record('p', 'a'), first['applied_profile'])
            self.assertEqual(bound(b).workflow.store.application_record('b-project', 'b'), second['applied_profile'])
