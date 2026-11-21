"""Synthetic acceptance for generic drawing role ranking and locations."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_drawing_roles import discover_roles


def insert(name, layer, x=0, y=0):
    return {'name': name, 'layer': layer, 'x': x, 'y': y}


class RoleDiscoveryTests(unittest.TestCase):
    def test_named_module_set_outranks_larger_geometric_competitor(self):
        intake = {'inserts': [insert('Panel type A', 'PV MODULES', i, i * 2) for i in range(7)]
                  + [insert('Anonymous', '0', i + 30, i) for i in range(12)],
                  'blocks': {'Anonymous': {'children': [{'closed': True,
                    'pts': [[0, 0], [2, 0], [2, 1], [0, 1]]}]}}}
        result = discover_roles(intake)['module']
        self.assertEqual(result['first_choice']['block'], 'Panel type A')
        self.assertTrue(result['competing'])
        self.assertEqual([r['count'] for r in result['candidates']], [7, 12])
        self.assertEqual(result['first_choice']['insertion_bounds'], [0, 0, 6, 12])

    def test_anonymous_name_is_ranked_from_layer_not_customer(self):
        result = discover_roles({'inserts': [insert('*U42', 'MODULE layer')]})
        self.assertEqual(result['module']['first_choice']['block'], '*U42')
        self.assertNotIn('customer', result)

    def test_roof_and_obstruction_are_separate_roles(self):
        result = discover_roles({'inserts': [insert('Roof outline', 'ROOF'),
                                            insert('Skylight', 'ROOF OBST')]})
        self.assertEqual(result['roof'][0]['block'], 'Roof outline')
        self.assertEqual(result['obstruction'][0]['block'], 'Skylight')
        self.assertIsNone(result['module']['first_choice'])

    def test_malformed_coordinates_are_not_counted_or_used_as_bounds(self):
        result = discover_roles({'inserts': [insert('Panel', 'MODULES', float('nan')),
                                            insert('Panel', 'MODULES', True), None]})
        self.assertIsNone(result['module']['first_choice'])

    def test_input_is_not_changed_and_order_does_not_choose_winner(self):
        intake = {'inserts': [insert('Panel Z', 'MODULES'), insert('Panel A', 'MODULES')]}
        before = copy.deepcopy(intake)
        result = discover_roles(intake)
        self.assertEqual(intake, before)
        intake['inserts'].reverse()
        self.assertEqual(discover_roles(intake), result)

    def test_broken_block_geometry_does_not_create_candidate(self):
        intake = {'inserts': [insert('Other', '0') for _ in range(8)],
                  'blocks': {'Other': {'children': [{'closed': True, 'pts': ['bad']}]}}}
        self.assertIsNone(discover_roles(intake)['module']['first_choice'])


if __name__ == '__main__':
    unittest.main()
