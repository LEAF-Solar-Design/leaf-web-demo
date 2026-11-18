from fractions import Fraction
import math
from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import select_modules, SelectionError


class SelectionScaleOracle(unittest.TestCase):
    def test_power_of_two_scaling_preserves_exact_fraction_membership(self):
        randomizer = random.Random(707)
        points = [(Fraction(x, 8), Fraction(y, 8)) for x, y in [(-8, -4), (8, 4), (0, 0), (-8, 4), (8, -4)]]
        points += [(Fraction(randomizer.randint(-16, 16), 8), Fraction(randomizer.randint(-16, 16), 8)) for _ in range(100)]
        window = [Fraction(-1), Fraction(-1, 2), Fraction(1), Fraction(1, 2)]
        expected = [index for index, (x, y) in enumerate(points) if window[0] <= x <= window[2] and window[1] <= y <= window[3]]
        for exponent in [-1020, -100, -1, 0, 1, 100, 1020]:
            with self.subTest(exponent=exponent):
                factor = Fraction(2) ** exponent
                bounds = [float(value * factor) for value in window]
                rows = [{'name': 'Panel', 'layer': 'M', 'handle': str(index), 'x': float(x * factor), 'y': float(y * factor)} for index, (x, y) in enumerate(points)]
                self.assertTrue(all(math.isfinite(row[key]) for row in rows for key in ['x', 'y']))
                result = select_modules({'inserts': rows}, block='Panel', layer='M', window=bounds)
                self.assertEqual(result['insert_indices'], expected)
                self.assertEqual(result['count'], len(expected))

    def test_nonfinite_scaled_origins_and_windows_refuse(self):
        for value in [float('inf'), float('-inf'), float('nan')]:
            with self.subTest(value=value):
                with self.assertRaises(SelectionError):
                    select_modules({'inserts': [{'name': 'Panel', 'layer': 'M', 'x': value, 'y': 0}]}, block='Panel', layer='M', window=[-1, -1, 1, 1])
                with self.assertRaises(SelectionError):
                    select_modules({'inserts': []}, block='Panel', layer='M', window=[-1, -1, value, 1])
