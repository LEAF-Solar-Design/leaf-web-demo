"""X07 SQLite bindings preserve embedded NUL identities without prefix collisions."""
from pathlib import Path
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter
from x07_workflow_adapter import WorkflowAdapter


class EmbeddedNulKeys(unittest.TestCase):
    def test_customer_project_drawing_and_role_round_trips_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            base = adapter(Path(directory))
            observed = []
            for suffix in ('', '\0one', '\0two'):
                context = dict(customer_key='customer' + suffix, project_key='project' + suffix, drawing_key='drawing' + suffix)
                block, layer = 'Panel' + suffix, 'MODULES' + suffix
                intake = {'inserts': [dict(name=block, layer=layer, handle='h1', x=0, y=0)]}
                copies = [dict(block=block, layer=layer, design_window=[0, 0, 0, 0], oracle_count=1)]
                current = WorkflowAdapter(base.workflow, context=context, intake=intake, copies=copies,
                                          confirmation_receipts=base._receipts)
                result = current.confirm_roles(context, {**copies[0], 'expected_version': 0})
                for key, value in context.items(): self.assertEqual(result[key], value)
                self.assertEqual(result['resolved_roles']['module'], {'block': block, 'layer': layer})
                self.assertEqual(current.confirm_roles(context, {**copies[0], 'expected_version': 0}), result)
                self.assertEqual(base.workflow.store.latest(context['customer_key'])['roles']['module'], {'block': block, 'layer': layer})
                observed.append((context, result['applied_profile']))
                for prior_context, prior_pin in observed:
                    self.assertEqual(base.workflow.store.application_record(prior_context['project_key'], prior_context['customer_key']), prior_pin)
            self.assertEqual(len({pin['profile_id'] for _, pin in observed}), 3)
