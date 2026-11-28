#!/usr/bin/env python3
"""Independent synthetic precision vectors; stdlib only, no fixture or file IO."""

from fractions import Fraction
import math
import sys
import unittest

import check_unit_equivalence as units


class PrecisionBoundaryTests(unittest.TestCase):
    def assert_named(self, name, function, *args):
        with self.assertRaises(units.UnitError) as caught:
            function(*args)
        self.assertEqual(caught.exception.name, name)

    def test_literal_vectors_and_binary_oracle(self):
        # Physical references are literal, independently specified metres.
        vectors = (
            (((0, 0), (1e-200, -2e-200), (-3e-200, 4e-200)),
             ((0, 0), (2.54e-202, -5.08e-202), (-7.62e-202, 1.016e-201)),
             ((0, 0), (2e-200, -4e-200), (-6e-200, 8e-200))),
            (((0, 0), (1e-6, -2e-6), (-3e-6, 4e-6)),
             ((0, 0), (2.54e-8, -5.08e-8), (-7.62e-8, 1.016e-7)),
             ((0, 0), (2e-6, -4e-6), (-6e-6, 8e-6))),
            (((0, 0), (1e12, -5e11), (-2.5e11, 0)),
             ((0, 0), (25400000000, -12700000000), (-6350000000, 0)),
             ((0, 0), (2e12, -1e12), (-5e11, 0))),
        )
        for inch, metre, unitless in vectors:
            for code, scale, points, oracle_scale in (
                    (1, None, inch, .0254), (6, None, metre, 1.0),
                    (0, .0127, unitless, .0127)):
                with self.subTest(code=code, points=points):
                    actual = units.points_metres(points, code, scale)
                    self.assertEqual(len(actual), len(metre))
                    for source, observed, expected in zip(points, actual, metre):
                        for value, result, literal in zip(source, observed, expected):
                            # Exact rational arithmetic on the binary inputs gives
                            # an independently rounded multiplication oracle.
                            oracle = float(Fraction(value) * Fraction(oracle_scale))
                            self.assertEqual(result, oracle)
                            self.assertTrue(math.isclose(result, literal, rel_tol=0,
                                                         abs_tol=units.ABS_TOL))
                    units.require_equivalent(actual, metre)

    def test_absolute_tolerance_adjacent_values(self):
        self.assertEqual(units.ABS_TOL, 1e-12)
        for sign in (-1, 1):
            for delta in (math.nextafter(1e-12, 0), 1e-12):
                units.require_equivalent([(0, 0)], [(sign * delta, 0)])
                units.require_equivalent([(0, sign * delta)], [(0, 0)])
            outside = sign * math.nextafter(1e-12, math.inf)
            self.assert_named("UNIT_EQUIVALENCE_MISMATCH", units.require_equivalent,
                              [(0, 0)], [(outside, 0)])
            self.assert_named("UNIT_EQUIVALENCE_MISMATCH", units.require_equivalent,
                              [(0, outside)], [(0, 0)])
        # A relative tolerance would falsely accept this single-ULP difference.
        large = 25400000000.0
        self.assert_named("UNIT_EQUIVALENCE_MISMATCH", units.require_equivalent,
                          [(large, 0)], [(math.nextafter(large, math.inf), 0)])

    def test_sign_and_order_are_not_discarded(self):
        reference = [(25400000000, -12700000000), (-6350000000, 0)]
        for wrong in (list(reversed(reference)),
                      [(25400000000, 12700000000), (-6350000000, 0)],
                      [(-12700000000, 25400000000), (-6350000000, 0)],
                      reference[:1]):
            self.assert_named("UNIT_EQUIVALENCE_MISMATCH", units.require_equivalent,
                              reference, wrong)

    def test_zero_underflow_and_representable_subnormals(self):
        smallest = math.nextafter(0.0, 1.0)
        for code, scale in ((1, None), (6, None), (0, .5)):
            self.assertEqual(units.points_metres([(0, -0.0)], code, scale), ((0, 0),))
        for sign in (-1, 1):
            for code, scale in ((1, None), (0, .5)):
                for point in ((sign * smallest, 0), (0, sign * smallest)):
                    self.assert_named("INVALID_COORDINATE", units.points_metres,
                                      [point], code, scale)
            for code, scale, value in ((6, None, smallest), (0, 1, smallest),
                                       (1, None, smallest * 32)):
                self.assertEqual(units.points_metres([(sign * value, 0)], code, scale),
                                 ((sign * smallest, 0),))
            self.assertEqual(units.points_metres([(sign * smallest, 0)], 0, 2),
                             ((sign * smallest * 2, 0),))
        self.assertEqual(units.model_height_metres(smallest, 6), smallest)
        self.assertEqual(units.paper_height_mm(smallest, 6, 1), smallest * 1000)
        self.assert_named("INVALID_HEIGHT", units.model_height_metres, smallest, 1)
        self.assert_named("INVALID_HEIGHT", units.model_height_metres, smallest, 0, .5)
        self.assert_named("INVALID_PAPER_HEIGHT", units.paper_height_mm,
                          smallest, 6, .0001)

    def test_finite_checks_and_conversion_overflow(self):
        huge = 10 ** 1000
        for value in (huge, -huge):
            self.assert_named("INVALID_COORDINATE", units.points_metres, [(value, 0)], 6)
            self.assert_named("INVALID_HEIGHT", units.model_height_metres, value, 6)
            self.assert_named("INVALID_UNIT_SCALE", units.metres_per_unit, 0, value)
            self.assert_named("INVALID_SHEET_SCALE", units.paper_height_mm, 1, 6, value)
            self.assert_named("INVALID_PAPER_HEIGHT", units.require_legible, value)
        maximum = sys.float_info.max
        for sign in (-1, 1):
            for point in ((sign * maximum, 0), (0, sign * maximum)):
                self.assert_named("INVALID_COORDINATE", units.points_metres, [point], 0, 2)
        self.assert_named("INVALID_HEIGHT", units.model_height_metres, maximum, 0, 2)
        # Intermediate mm overflow remains a refusal even if the final ratio
        # could mathematically shrink it back into range.
        self.assert_named("INVALID_PAPER_HEIGHT", units.paper_height_mm, maximum, 6, .001)
        self.assert_named("INVALID_PAPER_HEIGHT", units.paper_height_mm, 1, 6, maximum)

    def test_legibility_adjacent_values(self):
        self.assert_named("TEXT_BELOW_MINIMUM", units.require_legible,
                          math.nextafter(1.8, 0))
        self.assertEqual(units.require_legible(1.8), 1.8)
        above = math.nextafter(1.8, math.inf)
        self.assertEqual(units.require_legible(above), above)


if __name__ == "__main__":
    unittest.main(verbosity=2)
