"""Independent expected competing groups across input orders and noise."""
import copy
import itertools
from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_drawing_roles import discover_roles


class DiscoveryOrderOracle(unittest.TestCase):
    def test_competing_rank_counts_and_bounds_ignore_permutation_and_noise(self):
        # Distinct name/layer scores, then count and spelling ties.
        groups = [('Panel', 'MODULES', 3, 9), ('Module', 'MODULES', 3, 9),
                  ('Opaque', 'MODULES', 5, 6), ('PanelLow', 'DRAWING', 7, 3)]
        expected = []
        for name, layer, count, score in groups:
            expected.append(dict(block=name, layer=layer, count=count, score=score,
                                 basis='names', insertion_bounds=[0, 0, count-1, 2*(count-1)]))
        # This literal order independently encodes score, count, layer, block ties.
        expected = [expected[i] for i in [1, 0, 2, 3]]
        for order in itertools.permutations(groups):
            rows = [dict(name=name, layer=layer, x=i, y=2*i)
                    for name, layer, count, _ in order for i in range(count)]
            random.Random(711).shuffle(rows)
            for noise in [[], [dict(name='Dimension', layer='NOTES', x=i, y=-i) for i in range(100)]]:
                with self.subTest(order=[row[0] for row in order], noise=len(noise)):
                    intake = {'inserts': rows + noise, 'blocks': {}}
                    before = copy.deepcopy(intake)
                    value = discover_roles(intake)
                    self.assertEqual(value['module']['candidates'], expected)
                    self.assertEqual(value['module']['first_choice'], expected[0])
                    self.assertIs(value['module']['competing'], True)
                    self.assertEqual(value['roof'], [])
                    self.assertEqual(value['obstruction'], [])
                    self.assertEqual(intake, before)

    def test_zero_and_single_group_ambiguity_decisions(self):
        for count in [0, 1, 8]:
            with self.subTest(count=count):
                intake = {'inserts': [dict(name='Panel', layer='M', x=i, y=0) for i in range(count)]}
                before = copy.deepcopy(intake)
                value = discover_roles(intake)['module']
                self.assertIs(value['competing'], False)
                self.assertEqual(len(value['candidates']), int(count > 0))
                if count: self.assertEqual(value['first_choice']['count'], count)
                else: self.assertIsNone(value['first_choice'])
                self.assertEqual(intake, before)
