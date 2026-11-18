from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_compatibility import check_compatibility


class RoleLabelCollisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = CustomerProfileStore(Path(self.temp.name) / 'profiles.sqlite')

    def refuse(self, names, *, layer_names=None, wanted='Panel', aliases=None, reason='AMBIGUOUS_ROLE_LABEL', roof=False):
        layers = layer_names or ['MODULES'] * len(names)
        roles = {'module': {'block': wanted, 'layer': layers[0]}}
        if roof: roles['roof'] = {'layer': 'Roof'}
        profile = self.store.save_version('a', roles)
        rows = [{'name': name, 'layer': layer, 'x': i, 'y': 0} for i, (name, layer) in enumerate(zip(names, layers))]
        intake = {'inserts': rows, 'polylines': [{'layer': 'Roof'}, {'layer': 'ROOF'}] if roof else []}
        original = deepcopy(intake)
        first = check_compatibility(intake, profile, customer_key='a', block_aliases=aliases)
        intake['inserts'].reverse()
        second = check_compatibility(intake, profile, customer_key='a', block_aliases=aliases)
        self.assertEqual(first, second)
        self.assertEqual(first['decision'], 'propose_fresh'); self.assertEqual(first['reason'], reason)
        self.assertIsNone(first['applied_profile'])
        self.assertEqual(self.store.latest('a'), profile)
        self.assertEqual(list(reversed(intake['inserts'])), original['inserts'])

    def test_case_fold_block_collision_refuses_instead_of_combining(self):
        self.refuse(['Panel', 'PANEL'])

    def test_unicode_normalization_collision_refuses(self):
        self.refuse(['Pánel', 'Pa\u0301nel'], wanted='Pánel')

    def test_layer_case_collision_refuses(self):
        self.refuse(['Panel', 'Panel'], layer_names=['MODULES', 'modules'])

    def test_layer_only_roof_collision_refuses(self):
        self.refuse(['Panel'], roof=True)

    def test_conflicting_alias_source_spellings_refuse(self):
        self.refuse(['*U2', '*U3'], wanted='*U1', aliases={'*U1': '*U2', '*u1': '*U3'}, reason='AMBIGUOUS_ALIAS_MAP')

    def test_conflicting_anonymous_targets_refuse(self):
        self.refuse(['*U2', '*U3'], wanted='*U1', aliases={'*U1': ['*U2', '*U3']}, reason='AMBIGUOUS_ALIAS')

    def test_normalization_does_not_implicitly_alias_one_distinct_label(self):
        self.refuse(['Pa\u0301nel'], wanted='Pánel', reason='ROLE_NOT_FOUND')
