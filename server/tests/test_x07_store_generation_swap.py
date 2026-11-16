"""X07 revalidates paths after disposable database generations are replaced."""
from pathlib import Path
import os
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES, INTAKE
from test_x07_receipt_restore_divergence import backup
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter
from x07_customer_profile_store import CustomerProfileStore


class StoreGenerationSwap(unittest.TestCase):
    def test_swap_after_inspection_revalidates_versions_and_receipt_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            request = {**COPIES[0], 'expected_version': 0}
            old = current.confirm_roles(CONTEXT, request)
            backup(root / 'profiles.sqlite', root / 'baseline.sqlite')
            scope = {**CONTEXT, 'project_key': 'new', 'drawing_key': 'new:v1'}
            next_adapter = WorkflowAdapter(current.workflow, context=scope, intake=INTAKE, copies=COPIES,
                                           confirmation_receipts=current._receipts)
            inspected = next_adapter.load_workflow(scope)
            replacement = root / 'replacement.sqlite'
            backup(root / 'profiles.sqlite', replacement)
            CustomerProfileStore(replacement).save_version('a', old['resolved_roles'], expected_version=1)
            os.replace(replacement, root / 'profiles.sqlite')
            before = [(root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite')]
            with self.assertRaisesRegex(ValueError, 'profile version changed'):
                next_adapter.confirm_roles(scope, {**COPIES[0], 'expected_version': inspected['expected_version'],
                                                  'plan_token': inspected['plan_token']})
            self.assertEqual([(root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite')], before)
            self.assertEqual(next_adapter.load_workflow(scope)['expected_version'], 2)
            settled = next_adapter.confirm_roles(scope, {**COPIES[0], 'expected_version': 2})
            self.assertEqual(settled['applied_profile']['version'], 3)
            # Inspect generation3, then restore an older profile generation while
            # retaining its newer cached receipt. Replay must validate the pin.
            next_adapter.load_workflow(scope)
            backup(root / 'baseline.sqlite', replacement)
            os.replace(replacement, root / 'profiles.sqlite')
            before = [(root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite')]
            with self.assertRaisesRegex(ValueError, 'receipt no longer matches project pin'):
                next_adapter.confirm_roles(scope, {**COPIES[0], 'expected_version': 2})
            self.assertEqual([(root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite')], before)
            self.assertEqual(current.confirm_roles(CONTEXT, request), old)
            # Replacing the journal with a generation lacking that receipt also
            # refuses reconfirmation of an already pinned old project.
            empty = root / 'empty'
            empty.mkdir(); adapter(empty)
            backup(empty / 'receipts.sqlite', replacement)
            current.load_workflow(CONTEXT)
            os.replace(replacement, root / 'receipts.sqlite')
            with self.assertRaisesRegex(ValueError, 'already pinned'):
                current.confirm_roles(CONTEXT, request)
            self.assertEqual(current.workflow.store.application_record('p', 'a'), old['applied_profile'])
