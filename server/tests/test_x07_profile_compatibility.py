import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_profile_compatibility import check_compatibility
from x07_profile_workflow import ProfileWorkflow
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.intake = {'inserts': [{'name': 'Panel A', 'layer': 'MODULES', 'x': 0, 'y': 0}]}
        self.profile = {'customer_key': 'customer-a', 'profile_id': 'profile-a', 'version': 1,
                        'roles': {'module': {'block': 'Panel A', 'layer': 'MODULES'}}}

    def check(self, **kw):
        return check_compatibility(self.intake, self.profile, customer_key='customer-a', **kw)

    def test_missing_customer_refuses_instead_of_inferring_identity(self):
        with self.assertRaises(ValueError):
            check_compatibility(self.intake, self.profile, customer_key='')

    def test_foreign_profile_proposes_fresh_without_its_id(self):
        result = check_compatibility(self.intake, self.profile, customer_key='customer-b')
        self.assertEqual(result['decision'], 'propose_fresh')
        self.assertIsNone(result['applied_profile'])
        self.assertNotIn('profile-a', json.dumps(result))

    def test_missing_profile_and_incompatible_roles_propose_fresh(self):
        self.assertEqual(check_compatibility(self.intake, None, customer_key='customer-a')['reason'], 'NO_PROFILE')
        self.profile['roles']['module']['layer'] = 'OTHER'
        self.assertEqual(self.check()['reason'], 'ROLE_NOT_FOUND')

    def test_compatible_symbols_are_case_insensitive(self):
        self.profile['roles']['module'] = {'block': 'panel a', 'layer': 'modules'}
        self.assertEqual(self.check()['decision'], 'apply')

    def test_anonymous_alias_is_required_even_for_same_literal_name(self):
        self.intake['inserts'][0]['name'] = '*U1'
        self.profile['roles']['module']['block'] = '*U1'
        self.assertEqual(self.check()['reason'], 'ANONYMOUS_ALIAS_REQUIRED')
        self.assertEqual(self.check(block_aliases={'*U1': '*U1'})['decision'], 'apply')

    def test_explicit_alias_resolves_new_drawing_without_changing_profile(self):
        self.profile['roles']['module']['block'] = '*U1'
        self.intake['inserts'][0]['name'] = '*U2'
        before = copy.deepcopy(self.profile)
        result = self.check(block_aliases={'*U1': '*U2'})
        self.assertEqual(result['roles']['module']['block'], '*U2')
        self.assertEqual(self.profile, before)

    def test_ambiguous_alias_never_picks_arbitrarily(self):
        self.profile['roles']['module']['block'] = '*U1'
        self.intake['inserts'] = [{'name': name, 'layer': 'MODULES', 'x': 0, 'y': 0} for name in ('*U2', '*U3')]
        self.assertEqual(self.check(block_aliases={'*U1': ['*U2', '*U3']})['reason'], 'AMBIGUOUS_ALIAS')

    def test_injected_proposal_cannot_mutate_the_caller_drawing(self):
        before = copy.deepcopy(self.intake)
        def propose(value):
            value.clear()
            return {'fresh': True}
        result = check_compatibility(self.intake, None, customer_key='customer-a', propose=propose)
        self.assertEqual(result['proposal'], {'fresh': True})
        self.assertEqual(self.intake, before)

    def test_layer_only_roof_and_missing_required_module(self):
        self.profile['roles']['roof'] = {'layer': 'ROOF'}
        self.intake['polylines'] = [{'layer': 'ROOF'}]
        self.assertEqual(self.check()['decision'], 'apply')
        self.profile['roles'].pop('module')
        self.assertEqual(self.check()['reason'], 'INVALID_ROLE_MAP')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CustomerProfileStore(Path(self.temp.name) / 'profiles.sqlite')
        self.service = ProfileWorkflow(self.store)
        self.roles = {'module': {'block': 'Panel A', 'layer': 'MODULES'}}
        self.intake = {'inserts': [{'name': 'Panel A', 'layer': 'MODULES', 'x': x, 'y': 0}
                                   for x in [0, 1, 2, 10, 11, 12]]}
        self.ctx = dict(customer_key='a', project_key='a1', drawing_key='drawing1:v1')

    def confirm(self, **extra):
        return self.service.confirm(self.intake, **self.ctx, roles=self.roles,
                                    window=[0, 0, 2, 0], **extra)

    def test_two_customers_confirm_reuse_and_choose_copy(self):
        before = copy.deepcopy(self.intake)
        plan = self.service.inspect(self.intake, **self.ctx)
        self.assertEqual(plan['compatibility']['decision'], 'propose_fresh')
        self.assertIsNone(self.store.latest('a'))
        first = self.confirm(expected_version=0)
        for project in ['a2', 'a3']:
            result = self.service.apply(self.intake, customer_key='a', project_key=project,
                                        drawing_key=project, window=[10, 0, 12, 0])
            self.assertEqual(result['selection']['insert_indices'], [3, 4, 5])
            self.assertEqual(result['applied_profile']['profile_id'], first['applied_profile']['profile_id'])
        second = self.service.confirm(self.intake, customer_key='b', project_key='b1', drawing_key='b1',
                                      roles=self.roles, window=[0, 0, 2, 0], expected_version=0)
        reused = self.service.apply(self.intake, customer_key='b', project_key='b2', drawing_key='b2', window=[0, 0, 2, 0])
        self.assertEqual(reused['selection']['count'], 3)
        self.assertNotEqual(first['applied_profile']['profile_id'], second['applied_profile']['profile_id'])
        self.assertEqual(self.intake, before)

    def test_missing_profile_does_not_pin_or_infer_customer(self):
        result = self.service.apply(self.intake, **self.ctx, window=[0, 0, 2, 0])
        self.assertEqual(result['state'], 'needs_confirmation')
        self.assertIsNone(result['applied_profile'])
        self.assertIsNone(self.store.application_record('a1', 'a'))
        with self.assertRaises(ValueError):
            self.service.inspect(self.intake, **{**self.ctx, 'customer_key': ''})

    def test_invalid_confirmation_and_window_never_write(self):
        for roles, window in [({'module': {'block': 'Absent', 'layer': 'MODULES'}}, [0, 0, 2, 0]),
                              (self.roles, [0, 0, float('inf'), 0])]:
            with self.assertRaises(ValueError):
                self.service.confirm(self.intake, **self.ctx, roles=roles, window=window, expected_version=0)
            self.assertIsNone(self.store.latest('a'))

    def test_foreign_project_refuses_before_profile_write(self):
        self.confirm(expected_version=0)
        with self.assertRaises(ProfileScopeError):
            self.service.confirm(self.intake, **{**self.ctx, 'customer_key': 'b'},
                                 roles=self.roles, window=[0, 0, 2, 0], expected_version=0)
        self.assertIsNone(self.store.latest('b'))

    def test_old_project_reopens_its_pin_and_new_project_uses_new_version(self):
        self.confirm(expected_version=0)
        self.store.save_version('a', self.roles, expected_version=1)
        old = self.service.apply(self.intake, **self.ctx, window=[0, 0, 2, 0])
        new = self.service.apply(self.intake, **{**self.ctx, 'project_key': 'new'}, window=[0, 0, 2, 0])
        self.assertEqual(old['applied_profile']['version'], 1)
        self.assertEqual(new['applied_profile']['version'], 2)

    def test_stale_confirmation_keeps_profile_and_no_pin(self):
        self.store.save_version('a', self.roles, expected_version=0)
        with self.assertRaisesRegex(ValueError, 'version changed'):
            self.confirm(expected_version=0)
        self.assertEqual(self.store.latest('a')['version'], 1)
        self.assertIsNone(self.store.application_record('a1', 'a'))

    def test_change_between_check_and_pin_refuses_unchecked_version(self):
        self.store.save_version('a', self.roles, expected_version=0)
        original = self.store.apply_to_project
        def race(project, customer, **kw):
            self.store.save_version('a', {'module': {'block': 'Other', 'layer': 'OTHER'}}, expected_version=1)
            return original(project, customer, **kw)
        self.store.apply_to_project = race
        with self.assertRaisesRegex(ValueError, 'version changed before project pin'):
            self.service.apply(self.intake, **self.ctx, window=[0, 0, 2, 0])
        self.assertIsNone(self.store.application_record('a1', 'a'))
