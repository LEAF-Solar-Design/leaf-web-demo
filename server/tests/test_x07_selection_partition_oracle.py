"""Inclusive partitions share boundary INSERTs, not coordinate identities."""
import copy
from fractions import Fraction
from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import select_modules


class SelectionPartitionOracle(unittest.TestCase):
    def test_adjacent_window_unions_and_shared_edges(self):
        randomizer = random.Random(710)
        points = [(Fraction(randomizer.randrange(-12, 13), 4),
                   Fraction(randomizer.randrange(-12, 13), 4)) for _ in range(120)]
        points += [(Fraction(0), Fraction(y)) for y in [-2, -1, 0, 0, 1, 2]]
        points += [(Fraction(x), Fraction(0)) for x in [-2, -1, 0, 0, 1, 2]]
        rows = [dict(name='Panel', layer='M', handle=f'h{i}', x=float(x), y=float(y))
                for i, (x, y) in enumerate(points)]
        rows += [dict(name='Other', layer='M', x=0, y=0), dict(name='Panel', layer='Other', x=0, y=0)]
        intake = {'inserts': rows}
        before = copy.deepcopy(intake)
        for axis in [0, 1]:
            for cut in [Fraction(-2), Fraction(-1, 2), Fraction(0), Fraction(1, 2), Fraction(2)]:
                with self.subTest(axis=axis, cut=cut):
                    enclosing = [-2, -2, 2, 2]
                    left, right = enclosing.copy(), enclosing.copy()
                    left[axis + 2] = float(cut)
                    right[axis] = float(cut)
                    def oracle(bounds):
                        return {f'h{i}' for i, (x, y) in enumerate(points)
                                if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]}
                    def selected(bounds):
                        value = select_modules(intake, block='panel', layer='m', window=bounds)
                        self.assertEqual(value['count'], len(value['handles']))
                        return set(value['handles'])
                    a, b, whole = selected(left), selected(right), selected(enclosing)
                    edge = {f'h{i}' for i, p in enumerate(points)
                            if p[axis] == cut and -2 <= p[1-axis] <= 2}
                    self.assertEqual(a, oracle(left)); self.assertEqual(b, oracle(right))
                    self.assertEqual(whole, oracle(enclosing))
                    self.assertEqual(a | b, whole)
                    self.assertEqual(a & b, edge)
        self.assertEqual(intake, before)
