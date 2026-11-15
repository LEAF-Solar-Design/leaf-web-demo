import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_profile_compatibility import check_compatibility


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
