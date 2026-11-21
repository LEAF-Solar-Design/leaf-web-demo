from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_drawing_roles import discover_roles
from x07_role_selection import select_modules


class DiscoverySelectionAgreement(unittest.TestCase):
    def test_seeded_proposals_enclose_exact_matching_insert_identities(self):
        for seed in [701, 702, 703]:
            with self.subTest(seed=seed):
                randomizer = random.Random(seed)
                groups = [('Panel-A', 'MODULES'), ('Panel-B', 'M'), ('Opaque', 'MODULES'), ('Panel-Line', 'M')]
                rows, expected = [], {}
                for group, (block, layer) in enumerate(groups):
                    points = [(Fraction(-2), Fraction(-1)), (Fraction(2), Fraction(1)),
                              (Fraction(-2), Fraction(1)), (Fraction(2), Fraction(-1)),
                              (Fraction(0), Fraction(0)), (Fraction(0), Fraction(0))]
                    points += [(Fraction(randomizer.randrange(-8, 9), 4), Fraction(randomizer.randrange(-4, 5), 4)) for _ in range(20)]
                    if block == 'Panel-Line': points = [(Fraction(3), y) for _, y in points]
                    handles = {f'{seed}-{group}-{i}' for i in range(len(points))}
                    bounds = [min(x for x, _ in points), min(y for _, y in points),
                              max(x for x, _ in points), max(y for _, y in points)]
                    expected[block, layer] = (handles, bounds)
                    rows.extend(dict(name=block, layer=layer, x=float(x), y=float(y), handle=f'{seed}-{group}-{i}')
                                for i, (x, y) in enumerate(points))
                rows += [dict(name='Note', layer='ANNOTATIONS', x=0, y=0, handle=f'note-{i}') for i in range(30)]
                randomizer.shuffle(rows)
                intake = {'inserts': rows}; before = deepcopy(intake)
                proposal = discover_roles(intake)['module']
                self.assertIs(proposal['competing'], True)
                self.assertEqual(len(proposal['candidates']), 4)
                for candidate in proposal['candidates']:
                    handles, bounds = expected[candidate['block'], candidate['layer']]
                    self.assertEqual(candidate['insertion_bounds'], [float(value) for value in bounds])
                    selected = select_modules(intake, block=candidate['block'], layer=candidate['layer'], window=candidate['insertion_bounds'])
                    self.assertEqual(set(selected['handles']), handles)
                    self.assertEqual(selected['count'], len(handles))
                    self.assertEqual(candidate['count'], len(handles))
                    self.assertEqual(selected['insert_indices'], [i for i, row in enumerate(rows) if row['handle'] in handles])
                self.assertEqual(intake, before)
