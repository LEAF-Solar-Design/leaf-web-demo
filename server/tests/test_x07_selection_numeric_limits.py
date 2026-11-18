from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import select_modules, SelectionError


def exact(rows, window):
    bounds = [Fraction(value) for value in window]
    return [i for i, row in enumerate(rows) if bounds[0] <= Fraction(row['x']) <= bounds[2]
            and bounds[1] <= Fraction(row['y']) <= bounds[3]]


class SelectionNumericLimitsTests(unittest.TestCase):
    def check(self, positions, window):
        rows = [{'name': 'Panel', 'layer': 'MODULES', 'x': x, 'y': y} for x, y in positions]
        before = deepcopy(rows)
        result = select_modules({'inserts': rows}, block='Panel', layer='MODULES', window=window)
        self.assertEqual(result['insert_indices'], exact(rows, window))
        self.assertEqual(result['count'], len(exact(rows, window)))
        self.assertEqual(rows, before)

    def test_extreme_finite_coordinates_need_no_overflowing_subtraction(self):
        maximum = sys.float_info.max
        self.check([(-maximum, -maximum), (0, 0), (maximum, maximum)], [-maximum, -maximum, maximum, maximum])

    def test_subnormal_boundaries_match_exact_binary_rationals(self):
        tiny = math.ulp(0.0)
        self.check([(0, 0), (tiny, -tiny), (2*tiny, tiny)], [0, -tiny, tiny, 0])

    def test_degenerate_and_signed_zero_windows(self):
        self.check([(0.0, -0.0), (-0.0, 0.0), (math.ulp(0.0), 0)], [-0.0, 0, 0, -0.0])

    def test_next_float_beyond_edge_is_excluded_without_tolerance(self):
        self.check([(1, 1), (math.nextafter(1.0, math.inf), 1), (math.nextafter(1.0, -math.inf), 1)], [1, 1, 1, 1])

    def test_unbounded_integer_conversion_refuses_explicitly(self):
        huge = 10**400
        with self.assertRaises(SelectionError):
            select_modules({'inserts': []}, block='Panel', layer='MODULES', window=[0, 0, huge, 1])
        with self.assertRaises(SelectionError):
            select_modules({'inserts': [{'name': 'Panel', 'layer': 'MODULES', 'x': huge, 'y': 0}]},
                           block='Panel', layer='MODULES', window=[0, 0, 1, 1])
