import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


class AdapterPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        self.store = CustomerProfileStore(self.path); self.workflow = ProfileWorkflow(self.store)
        self.ctx = {'customer_key': 'a', 'project_key': 'p1', 'drawing_key': 'd1:v1'}
        self.intake = {'inserts': [{'name': 'Panel', 'layer': 'MODULES', 'x': 0, 'y': 0}]}
        self.choice = {'block': 'Panel', 'layer': 'MODULES', 'design_window': [0, 0, 0, 0], 'oracle_count': 1}
        self.adapter = WorkflowAdapter(self.workflow, context=self.ctx, intake=self.intake, copies=[self.choice])

    def snapshot(self):
        db = sqlite3.connect(self.path)
        try:
            return {name: db.execute('SELECT * FROM ' + name + ' ORDER BY 1,2').fetchall()
                    for name in ['customers', 'profile_versions', 'project_applications']}
        finally:
            db.close()

    def test_null_scalar_and_malformed_choices_refuse_deterministically(self):
        before = self.snapshot()
        for payload in [None, 0, True, 'choice', [], {}, {'block': 4}, {**self.choice, 'layer': None},
                        {**self.choice, 'design_window': [0, 0, 1, 1]}]:
            for method in ['confirm_roles', 'apply_profile']:
                with self.subTest(payload=payload, method=method):
                    errors = []
                    for _ in range(2):
                        with self.assertRaises(ValueError) as error:
                            getattr(self.adapter, method)(self.ctx, payload)
                        errors.append((type(error.exception), str(error.exception)))
                    self.assertEqual(errors[0], errors[1]); self.assertEqual(self.snapshot(), before)

    def test_missing_boolean_scalar_and_negative_versions_never_save(self):
        before = self.snapshot()
        for version in [None, True, False, '0', -1, 0.0, [], {}]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.adapter.confirm_roles(self.ctx, {**self.choice, 'expected_version': version})
            self.assertEqual(self.snapshot(), before)

    def test_oracle_mismatch_and_invalid_candidate_shape_refuse_before_write(self):
        before = self.snapshot()
        for candidates in [None, {}, [None], [{**self.choice, 'oracle_count': True}],
                           [{**self.choice, 'oracle_count': 9}], [{**self.choice, 'design_window': [0, 0, float('inf'), 0]}]]:
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                WorkflowAdapter(self.workflow, context=self.ctx, intake=self.intake, copies=candidates)
            self.assertEqual(self.snapshot(), before)

    def test_bad_editor_roles_and_versions_preserve_existing_pin_and_history(self):
        self.adapter.confirm_roles(self.ctx, {**self.choice, 'expected_version': 0})
        before = self.snapshot()
        for roles in [None, [], 9, {}, {'module': None}, {'module': {'block': 'Missing', 'layer': 'MODULES'}}]:
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                self.adapter.save_profile('a', roles, expected_version=1)
            self.assertEqual(self.snapshot(), before)
        for version in [True, False, None, -1, 1.0]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.adapter.save_profile('a', {'module': {'block': 'Panel', 'layer': 'MODULES'}}, expected_version=version)
            self.assertEqual(self.snapshot(), before)

    def test_forged_payload_scope_cannot_create_other_customer(self):
        before = self.snapshot()
        payload = {**self.choice, 'customer_key': 'b', 'expected_version': 0}
        with self.assertRaises(ValueError):
            self.adapter.confirm_roles(copy.deepcopy(self.ctx), payload)
        self.assertEqual(self.snapshot(), before)
