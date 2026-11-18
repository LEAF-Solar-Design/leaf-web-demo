import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter, AdapterVersionConflict


class AdapterSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = CustomerProfileStore(Path(self.temp.name) / 'profiles.sqlite')
        self.ctx = {'customer_key': 'a', 'project_key': 'p1', 'drawing_key': 'd1:v1'}
        self.intake = {'inserts': [{'name': '*U2', 'layer': 'MODULES', 'x': 0, 'y': 0}],
                       'polylines': [{'layer': 'ROOF 雪'}, {'layer': 'OBSTRUCTION Ø'}]}
        self.choice = {'block': '*U2', 'layer': 'MODULES', 'design_window': [0, 0, 0, 0], 'oracle_count': 1}
        self.copies = [copy.deepcopy(self.choice)]
        self.aliases = {'*U1': ['*U2']}
        self.store.save_version('a', {'module': {'block': '*U1', 'layer': 'MODULES'}}, expected_version=0)
        self.adapter = WorkflowAdapter(ProfileWorkflow(self.store), context=self.ctx, intake=self.intake,
                                       copies=self.copies, block_aliases=self.aliases)

    def test_caller_intake_alias_and_copy_mutation_cannot_change_snapshot(self):
        ctx = copy.deepcopy(self.ctx)
        self.intake['inserts'][0].update(name='Wrong', x=999)
        self.aliases['*U1'][0] = '*BAD'
        self.copies[0]['design_window'][2] = 999; self.copies[0]['oracle_count'] = 999
        self.ctx['customer_key'] = 'foreign'
        plan = self.adapter.load_workflow(ctx)
        self.assertEqual(plan['compatibility']['roles']['module']['block'], '*U2')
        self.assertEqual(plan['copies'], [self.choice])
        result = self.adapter.apply_profile(ctx, self.choice)
        self.assertEqual(result['selection']['count'], 1)
        self.assertEqual(result['customer_key'], 'a')
        with self.assertRaises(ProfileScopeError):
            self.adapter.load_workflow(self.ctx)

    def test_returned_plan_and_application_are_detached(self):
        plan = self.adapter.load_workflow(self.ctx)
        plan['copies'][0]['design_window'][0] = -999
        plan['compatibility']['roles']['module']['block'] = 'Wrong'
        plan['customer_key'] = 'foreign'
        first = self.adapter.apply_profile(self.ctx, self.choice)
        first['applied_profile']['roles'].clear(); first['selection']['handles'].append('fake')
        second = self.adapter.apply_profile(self.ctx, self.choice)
        self.assertEqual(second['selection']['count'], 1)
        self.assertEqual(second['selection']['handles'], [])
        self.assertEqual(second['applied_profile']['roles']['module']['block'], '*U1')

    def test_editor_optional_roles_create_new_version_without_repinning(self):
        old = self.adapter.apply_profile(self.ctx, self.choice)['applied_profile']
        roles = {'module': {'block': '*U2', 'layer': 'MODULES'},
                 'roof': {'layer': 'ROOF 雪'}, 'obstruction': {'layer': 'OBSTRUCTION Ø'}}
        edited = self.adapter.save_profile('a', roles, expected_version=1)
        self.assertEqual(edited['version'], 2)
        self.assertEqual(edited['roles'], roles)
        self.assertEqual(self.store.application_record('p1', 'a'), old)
        roles['roof']['layer'] = 'MUTATED'; edited['roles'].clear()
        self.assertEqual(self.adapter.load_profile('a')['roles']['roof']['layer'], 'ROOF 雪')

    def test_stale_editor_has_code_and_keeps_records_then_explicit_reload_recovers(self):
        roles = {'module': {'block': '*U2', 'layer': 'MODULES'}}
        self.adapter.save_profile('a', roles, expected_version=1)
        before = self.adapter.load_profile('a')
        with self.assertRaises(AdapterVersionConflict) as error:
            self.adapter.save_profile('a', roles, expected_version=1)
        self.assertEqual(error.exception.code, 'STALE_VERSION')
        self.assertEqual(self.adapter.load_profile('a'), before)
        self.assertEqual(self.adapter.save_profile('a', roles, expected_version=2)['version'], 3)

    def test_editor_scope_and_absent_optional_layer_refuse_without_versions(self):
        before = self.store.latest('a')
        with self.assertRaises(ProfileScopeError):
            self.adapter.save_profile('b', before['roles'], expected_version=0)
        with self.assertRaises(ValueError):
            self.adapter.save_profile('a', {'module': {'block': '*U2', 'layer': 'MODULES'},
                                           'roof': {'layer': 'MISSING'}}, expected_version=1)
        self.assertEqual(self.store.latest('a'), before)
        self.assertIsNone(self.store.latest('b'))
