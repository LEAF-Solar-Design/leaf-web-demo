from pathlib import Path
import os
import tempfile
import unittest
from test_x07_receipt_retry_races import CONTEXT, COPIES, INTAKE
from x07_customer_profile_store import CustomerProfileStore
from x07_workflow_adapter import WorkflowAdapter


class StorePathAliasing(unittest.TestCase):
    def build(self, profile, receipt):
        return WorkflowAdapter.from_paths(profile_path=profile, receipt_path=receipt,
            context=CONTEXT, intake=INTAKE, copies=COPIES)

    def test_identical_and_resolved_paths_refuse_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'nested').mkdir()
            path = root / 'new.sqlite'
            for alias in [path, root / 'nested' / '..' / 'new.sqlite']:
                with self.subTest(alias=str(alias)):
                    with self.assertRaisesRegex(ValueError, 'distinct files'):
                        self.build(path, alias)
                    self.assertFalse(path.exists())

    def test_hardlink_alias_refuses_without_changing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'profiles.sqlite'
            store = CustomerProfileStore(path)
            saved = store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project('p', 'a', expected_version=1)
            alias = root / 'receipt-alias.sqlite'
            os.link(path, alias)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'distinct files'):
                self.build(path, alias)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(alias.read_bytes(), before)
            self.assertEqual(store.latest('a'), saved)
            self.assertEqual(store.application_record('p', 'a'), pin)

    def test_distinct_paths_support_regular_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self.build(root / 'profiles.sqlite', root / 'receipts.sqlite')
            first = current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            self.assertEqual(first['applied_profile']['version'], 1)
