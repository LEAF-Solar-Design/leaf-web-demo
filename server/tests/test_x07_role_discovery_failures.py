import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_drawing_roles import discover_roles


class DiscoveryFailureTests(unittest.TestCase):
    def test_empty_and_malformed_containers_are_empty_deterministic_proposals(self):
        empty = discover_roles({})
        for value in (None, [], 'bad', {'inserts': None}, {'inserts': {}},
                      {'inserts': [None, [], {}, {'name': 4, 'layer': 'MODULES', 'x': 0, 'y': 0}]}):
            before = copy.deepcopy(value)
            with self.subTest(value=value):
                self.assertEqual(discover_roles(value), empty)
                self.assertEqual(value, before)

    def test_infinite_coordinates_are_excluded(self):
        rows = [{'name': 'Panel', 'layer': 'MODULES', 'x': value, 'y': 0}
                for value in (float('inf'), float('-inf'))]
        rows.append({'name': 'Panel', 'layer': 'MODULES', 'x': 1, 'y': 2})
        before = copy.deepcopy(rows)
        proposal = discover_roles({'inserts': rows})
        self.assertEqual(proposal['module']['first_choice']['count'], 1)
        self.assertEqual(proposal['module']['first_choice']['insertion_bounds'], [1, 2, 1, 2])
        self.assertEqual(rows, before)

    def test_mixed_case_names_have_same_semantic_rank(self):
        scores = []
        for name, layer in (('PANEL', 'MODULES'), ('panel', 'modules'), ('pAnEl', 'mOdUlEs')):
            data = {'inserts': [{'name': name, 'layer': layer, 'x': 0, 'y': 0}]}
            before = copy.deepcopy(data)
            scores.append(discover_roles(data)['module']['first_choice']['score'])
            self.assertEqual(data, before)
        self.assertEqual(len(set(scores)), 1)

    def test_equal_rank_competitors_remain_visible_and_order_is_stable(self):
        rows = [{'name': name, 'layer': 'MODULES', 'x': x, 'y': 0}
                for name, x in (('Panel Z', 10), ('Panel A', 0))]
        data = {'inserts': rows}
        before = copy.deepcopy(data)
        forward = discover_roles(data)
        backward = discover_roles({'inserts': list(reversed(rows))})
        self.assertEqual(forward, backward)
        self.assertTrue(forward['module']['competing'])
        self.assertEqual(len(forward['module']['candidates']), 2)
        self.assertEqual(data, before)

    def test_malformed_block_container_cannot_turn_geometry_into_a_module(self):
        rows = [{'name': 'Other', 'layer': '0', 'x': x, 'y': 0} for x in range(4)]
        for blocks in (None, [], {'Other': None}, {'Other': {'children': None}},
                       {'Other': {'children': [None, {'closed': True, 'pts': [None] * 4}]}}):
            with self.subTest(blocks=blocks):
                self.assertIsNone(discover_roles({'inserts': rows, 'blocks': blocks})['module']['first_choice'])
