"""Offline callback and post-save pin recovery contracts, using disposable SQLite."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


class AdapterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = CustomerProfileStore(Path(self.temp.name) / 'profiles.sqlite')
        self.workflow = ProfileWorkflow(self.store)
        self.context = {'customer_key': 'a', 'project_key': 'project-a', 'drawing_key': 'drawing-a:v1'}
        self.intake = {'inserts': [{'name': 'Panel A', 'layer': 'MODULES', 'x': x, 'y': 0}
                                  for x in [0, 1, 2, 10, 11, 12]] +
                                 [{'name': 'Panel B', 'layer': 'OTHER MODULES', 'x': x, 'y': 5} for x in [0, 1]]}
        self.copies = [{'block': 'Panel A', 'layer': 'MODULES', 'design_window': [x, 0, x + 2, 0],
                        'oracle_count': 3} for x in [0, 10]] + [
                       {'block': 'Panel B', 'layer': 'OTHER MODULES', 'design_window': [0, 5, 1, 5], 'oracle_count': 2}]
        self.adapter = WorkflowAdapter(self.workflow, context=self.context, intake=self.intake, copies=self.copies)

    def choice(self, index=0, version=0):
        return {**self.copies[index], 'expected_version': version}

    def test_proposal_plan_names_competing_sets_and_copies_without_writes(self):
        plan = self.adapter.load_workflow(self.context)
        self.assertEqual(plan['compatibility']['decision'], 'propose_fresh')
        candidates = plan['compatibility']['proposal']['module']['candidates']
        self.assertEqual([(r['block'], r['count']) for r in candidates], [('Panel A', 6), ('Panel B', 2)])
        self.assertEqual(len(plan['copies']), 3)
        self.assertIsNone(self.store.latest('a'))

    def test_confirm_translates_one_competing_copy_payload(self):
        result = self.adapter.confirm_roles(self.context, self.choice(2))
        self.assertEqual(result['selection']['insert_indices'], [6, 7])
        self.assertEqual(result['selection']['count'], 2)
        self.assertEqual(result['applied_profile']['roles']['module']['block'], 'Panel B')
        self.assertEqual(result['drawing_key'], self.context['drawing_key'])

    def test_compatible_reuse_filters_copies_and_needs_only_one_pick(self):
        first = self.adapter.confirm_roles(self.context, self.choice())
        context = {**self.context, 'project_key': 'next', 'drawing_key': 'drawing-next:v1'}
        adapter = WorkflowAdapter(self.workflow, context=context, intake=self.intake, copies=self.copies)
        plan = adapter.load_workflow(context)
        self.assertEqual(plan['compatibility']['decision'], 'apply')
        self.assertEqual(plan['copies'], self.copies[:2])
        second = adapter.apply_profile(context, self.choice(1))
        self.assertEqual(second['selection']['insert_indices'], [3, 4, 5])
        self.assertEqual(second['applied_profile']['profile_id'], first['applied_profile']['profile_id'])
        self.assertEqual(self.store.latest('a')['version'], 1)
        with self.assertRaises(ValueError):
            adapter.apply_profile(context, self.choice(2))

    def test_pin_failure_leaves_saved_version_then_inspect_apply_recovers(self):
        with patch.object(self.store, 'apply_to_project', side_effect=OSError('injected pin unavailable')):
            with self.assertRaises(OSError):
                self.adapter.confirm_roles(self.context, self.choice())
        self.assertEqual(self.store.latest('a')['version'], 1)
        self.assertIsNone(self.store.application_record('project-a', 'a'))
        plan = self.adapter.load_workflow(self.context)
        self.assertEqual(plan['expected_version'], 1)
        self.assertEqual(plan['compatibility']['decision'], 'apply')
        with self.assertRaisesRegex(ValueError, 'version changed'):
            self.adapter.confirm_roles(self.context, self.choice())
        recovered = self.adapter.apply_profile(self.context, self.choice(1))
        self.assertEqual(recovered['applied_profile']['version'], 1)
        self.assertEqual(self.store.latest('a')['version'], 1)

    def test_recovery_preserves_old_project_pin_and_roles(self):
        old = self.adapter.confirm_roles(self.context, self.choice())['applied_profile']
        context = {**self.context, 'project_key': 'new', 'drawing_key': 'new:v1'}
        adapter = WorkflowAdapter(self.workflow, context=context, intake=self.intake, copies=self.copies)
        with patch.object(self.store, 'apply_to_project', side_effect=OSError('pin fail')):
            with self.assertRaises(OSError):
                adapter.confirm_roles(context, self.choice(2, version=1))
        result = adapter.apply_profile(context, self.choice(2))
        self.assertEqual(result['applied_profile']['version'], 2)
        self.assertEqual(self.store.application_record('project-a', 'a'), old)
        self.assertEqual(self.store.latest('a')['version'], 2)

    def test_adapter_snapshots_inputs_and_returns_detached_plans(self):
        before = deepcopy(self.intake)
        self.intake.clear(); self.copies[0]['oracle_count'] = 999
        plan = self.adapter.load_workflow(self.context)
        plan['copies'].clear(); plan['compatibility']['proposal'].clear()
        fresh = self.adapter.load_workflow(self.context)
        self.assertEqual(fresh['copies'][0]['oracle_count'], 3)
        self.assertEqual(fresh['compatibility']['proposal']['module']['first_choice']['count'], 6)
        self.assertEqual(len(before['inserts']), 8)

    def test_unregistered_window_and_false_oracle_refuse_without_writes(self):
        with self.assertRaises(ValueError):
            self.adapter.confirm_roles(self.context, {**self.choice(), 'design_window': [0, 0, 12, 0]})
        with self.assertRaisesRegex(ValueError, 'independent oracle'):
            WorkflowAdapter(self.workflow, context=self.context, intake=self.intake,
                            copies=[{**self.copies[0], 'oracle_count': 99}])
        self.assertIsNone(self.store.latest('a'))
