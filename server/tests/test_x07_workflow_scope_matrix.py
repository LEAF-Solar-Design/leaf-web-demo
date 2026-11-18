"""Bound adapter scope matrix. This is not proof of live authorization."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


class WorkflowScopeMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        self.store = CustomerProfileStore(self.path)
        self.workflow = ProfileWorkflow(self.store)
        self.intake = {'inserts': [{'name': 'Panel', 'layer': 'MODULES', 'x': 0, 'y': 0}]}
        self.copy = {'block': 'Panel', 'layer': 'MODULES', 'design_window': [0, 0, 0, 0], 'oracle_count': 1}
        self.contexts = [{'customer_key': key, 'project_key': 'p-' + key, 'drawing_key': 'd-' + key + ':v1'} for key in ['a', 'b']]
        self.adapters = [WorkflowAdapter(self.workflow, context=ctx, intake=self.intake, copies=[self.copy]) for ctx in self.contexts]
        self.adapters[0].confirm_roles(self.contexts[0], {**self.copy, 'expected_version': 0})
        self.adapters[1].confirm_roles(self.contexts[1], {**self.copy, 'expected_version': 0})

    def snapshot(self):
        db = sqlite3.connect(self.path)
        try:
            return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()
                    for table in ['customers', 'profile_versions', 'project_applications']}
        finally:
            db.close()

    def test_each_swapped_context_key_refuses_each_operation_without_mutation(self):
        before = self.snapshot()
        for index, adapter in enumerate(self.adapters):
            ctx, other = self.contexts[index], self.contexts[1-index]
            for key in ctx:
                swapped = {**ctx, key: other[key]}
                for operation in ['load_workflow', 'confirm_roles', 'apply_profile']:
                    with self.subTest(customer=index, key=key, operation=operation):
                        with self.assertRaises(ProfileScopeError):
                            getattr(adapter, operation)(swapped, *([] if operation == 'load_workflow' else [{**self.copy, 'expected_version': 1}]))
                        self.assertEqual(self.snapshot(), before)

    def test_fully_swapped_context_and_new_drawing_version_refuse(self):
        before = self.snapshot()
        for adapter, ctx, other in [(self.adapters[0], self.contexts[0], self.contexts[1]),
                                    (self.adapters[1], self.contexts[1], self.contexts[0])]:
            for swap in [other, {**ctx, 'drawing_key': ctx['drawing_key'] + ':new'}]:
                with self.assertRaises(ProfileScopeError):
                    adapter.apply_profile(swap, self.copy)
                self.assertEqual(self.snapshot(), before)

    def test_forged_nested_payload_scope_cannot_override_bound_scope(self):
        before = self.snapshot()
        for key in self.contexts[0]:
            payload = {**self.copy, key: self.contexts[1][key], 'expected_version': 1}
            for operation in ['confirm_roles', 'apply_profile']:
                with self.assertRaises(ProfileScopeError):
                    getattr(self.adapters[0], operation)(self.contexts[0], payload)
                self.assertEqual(self.snapshot(), before)

    def test_injected_foreign_project_refuses_underlying_store_before_writes(self):
        before = self.snapshot()
        for index in [0, 1]:
            ctx = {**self.contexts[index], 'project_key': self.contexts[1-index]['project_key']}
            adapter = WorkflowAdapter(self.workflow, context=ctx, intake=self.intake, copies=[self.copy])
            for operation in ['load_workflow', 'confirm_roles', 'apply_profile']:
                with self.assertRaises(ProfileScopeError):
                    getattr(adapter, operation)(ctx, *([] if operation == 'load_workflow' else [{**self.copy, 'expected_version': 1}]))
                self.assertEqual(self.snapshot(), before)

    def test_legitimate_reopen_uses_only_its_customer_pin_and_detaches_context(self):
        for ctx, adapter in zip(self.contexts, self.adapters):
            output = adapter.apply_profile(deepcopy(ctx), self.copy)
            self.assertEqual(output['applied_profile']['customer_key'], ctx['customer_key'])
            other = self.store.latest('b' if ctx['customer_key'] == 'a' else 'a')['profile_id']
            self.assertNotEqual(output['applied_profile']['profile_id'], other)
            self.assertEqual(output['applied_profile']['version'], 1)
