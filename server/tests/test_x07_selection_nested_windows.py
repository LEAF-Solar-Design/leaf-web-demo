"""X07 nested inclusive windows use an independent exact Fraction oracle."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import select_modules


class SelectionNestedWindows(unittest.TestCase):
    def test_nested_identity_sets_shrink_through_degenerate_edges(self):
        for seed in (707, 1707, 2707):
            rng = random.Random(seed)
            points = [(Fraction(rng.randrange(-32, 33), 8), Fraction(rng.randrange(-32, 33), 8)) for _ in range(150)]
            radii = [Fraction(4), Fraction(2), Fraction(1), Fraction(1, 2), Fraction(0)]
            points += [(x, y) for radius in radii for x, y in [(radius, 0), (-radius, 0), (0, radius), (0, -radius)]]
            points += [(Fraction(0), Fraction(0)), (Fraction(0), Fraction(0))]
            rows = [dict(name='Panel', layer='M', handle=f'h{i}', x=float(x), y=float(y)) for i, (x, y) in enumerate(points)]
            rows += [dict(name='Other', layer='M', handle='foreign', x=0, y=0),
                     dict(name='Panel', layer='Other', handle='foreign-layer', x=0, y=0)]
            intake = {'inserts': rows}
            before = deepcopy(intake)
            previous = {f'h{i}' for i in range(len(points))}
            for radius in radii:
                with self.subTest(seed=seed, radius=radius):
                    expected = {f'h{i}' for i, (x, y) in enumerate(points) if -radius <= x <= radius and -radius <= y <= radius}
                    selected = select_modules(intake, block='panel', layer='m',
                                              window=[float(-radius), float(-radius), float(radius), float(radius)])
                    actual = set(selected['handles'])
                    self.assertEqual(actual, expected)
                    self.assertEqual(selected['count'], len(expected))
                    self.assertEqual(set(selected['insert_indices']), {int(handle[1:]) for handle in expected})
                    self.assertLessEqual(actual, previous)
                    previous = actual
            self.assertIn('h170', previous)
            self.assertIn('h171', previous)
            self.assertEqual(intake, before)
