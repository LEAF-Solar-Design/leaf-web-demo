#!/usr/bin/env python3
"""Offline synthetic unit contract; no DXF mutation, environment, or grader IO.

INSUNITS 1 = .0254 m/unit; 6 = 1 m/unit; 0 needs an explicit scale.
Sheet scale is dimensionless paper-mm/model-mm, NOT ezdxf custom_scale.
Equivalence uses rel_tol=0 and abs_tol=1e-12 in physical metres or mm.
"""

import argparse
import math
import unittest


ABS_TOL = 1e-12


class UnitError(ValueError):
    """A refusal with a stable diagnostic name."""

    def __init__(self, name, detail):
        self.name = name
        super().__init__(f"{name}: {detail}")


def finite(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnitError(name, "expected a finite number, excluding bool")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise UnitError(name, "number is outside finite range") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise UnitError(name, "expected a positive finite number" if positive
                        else "expected a finite number")
    return result


def metres_per_unit(insunits, explicit=None):
    if type(insunits) is not int or insunits not in (0, 1, 6):
        raise UnitError("UNSUPPORTED_INSUNITS", "expected integer 0, 1, or 6")
    if insunits == 0 and explicit is None:
        raise UnitError("MISSING_UNIT_SCALE", "unitless geometry needs metres/unit")
    scale = None if explicit is None else finite(explicit, "INVALID_UNIT_SCALE", True)
    if insunits == 0:
        return scale
    declared = {1: 0.0254, 6: 1.0}[insunits]
    # A declared header cannot be overridden, even by a nearly equal value.
    if scale is not None and scale != declared:
        raise UnitError("UNIT_SCALE_CONFLICT", "explicit scale conflicts with header")
    return declared


def points_metres(points, insunits, explicit=None):
    """Convert a full array of 2D model-space points, preserving order and signs."""
    scale = metres_per_unit(insunits, explicit)
    if not isinstance(points, (list, tuple)):
        raise UnitError("MALFORMED_POINTS", "expected an array of x/y pairs")
    converted = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise UnitError("MALFORMED_POINT", "expected exactly two coordinates")
        converted.append(tuple(finite(finite(v, "INVALID_COORDINATE") * scale,
                                      "INVALID_COORDINATE") for v in point))
    return tuple(converted)


def model_height_metres(height, insunits, explicit=None):
    return finite(finite(height, "INVALID_HEIGHT", True)
                  * metres_per_unit(insunits, explicit), "INVALID_HEIGHT", True)


def paper_height_mm(height, insunits, sheet_scale, explicit=None):
    """height * metres/unit * 1000 * (paper-mm/model-mm)."""
    ratio = finite(sheet_scale, "INVALID_SHEET_SCALE", True)
    return finite(model_height_metres(height, insunits, explicit) * 1000 * ratio,
                  "INVALID_PAPER_HEIGHT", True)


def require_legible(height_mm):
    """Separate policy: 1.8 mm is accepted; every representable value below fails."""
    height = finite(height_mm, "INVALID_PAPER_HEIGHT", True)
    if height < 1.8:
        raise UnitError("TEXT_BELOW_MINIMUM", "paper height is below 1.8 mm")
    return height


def require_equivalent(left, right):
    """Compare complete physical point arrays, including zeros and shape."""
    left = points_metres(left, 6)
    right = points_metres(right, 6)
    if len(left) != len(right) or any(
            not math.isclose(a, b, rel_tol=0, abs_tol=ABS_TOL)
            for lp, rp in zip(left, right) for a, b in zip(lp, rp)):
        raise UnitError("UNIT_EQUIVALENCE_MISMATCH", "physical point arrays differ")


class UnitContractTests(unittest.TestCase):
    # Hand-authored equivalents, not fixtures derived by the conversion under test.
    cases = (
        (1, None, [(0, 0), (100, -50), (-25, 75), (0, -100)], 10),
        (6, None, [(0, 0), (2.54, -1.27), (-0.635, 1.905), (0, -2.54)], .254),
        (0, .0127, [(0, 0), (200, -100), (-50, 150), (0, -200)], 20),
    )

    def assert_named(self, name, function, *args):
        with self.assertRaises(UnitError) as caught:
            function(*args)
        self.assertEqual(caught.exception.name, name)

    def test_full_positions_and_text(self):
        reference = self.cases[1][2]
        converted = []
        for code, scale, points, height in self.cases:
            with self.subTest(insunits=code):
                actual = points_metres(points, code, scale)
                require_equivalent(actual, reference)
                converted.append(actual)
                self.assertTrue(math.isclose(model_height_metres(height, code, scale),
                                             .254, rel_tol=0, abs_tol=ABS_TOL))
                paper = paper_height_mm(height, code, .01, scale)
                self.assertTrue(math.isclose(paper, 2.54, rel_tol=0, abs_tol=ABS_TOL))
                require_legible(paper)
        for actual in converted:
            require_equivalent(actual, converted[0])

    def test_exact_legibility_threshold_in_all_units(self):
        # These hand-authored model heights give exactly 1.8 mm at ratio 1.
        for code, scale, height in ((1, None, 9 / 127),
                                    (6, None, .0018), (0, .0127, 18 / 127)):
            paper = paper_height_mm(height, code, 1, scale)
            self.assertTrue(math.isclose(paper, 1.8, rel_tol=0, abs_tol=ABS_TOL))
            self.assertEqual(require_legible(paper), paper)
        self.assertEqual(require_legible(1.8), 1.8)
        self.assert_named("TEXT_BELOW_MINIMUM", require_legible,
                          math.nextafter(1.8, 0))

    def test_declared_and_unitless_contract(self):
        self.assertEqual(metres_per_unit(1, .0254), .0254)
        self.assertEqual(metres_per_unit(6, 1), 1)
        self.assert_named("MISSING_UNIT_SCALE", metres_per_unit, 0)
        for code in (None, True, False, 1.0, "6", 2, -1):
            self.assert_named("UNSUPPORTED_INSUNITS", metres_per_unit, code)
        for invalid in (0, -1, True, False, float("nan"), float("inf"),
                        -float("inf"), "0.0127"):
            for code in (0, 1, 6):
                self.assert_named("INVALID_UNIT_SCALE", metres_per_unit, code, invalid)
        for code, explicit in ((1, 1), (6, .0254)):
            self.assert_named("UNIT_SCALE_CONFLICT", metres_per_unit, code, explicit)

    def test_invalid_geometry_and_scales(self):
        for shape in (None, "xy", 1):
            self.assert_named("MALFORMED_POINTS", points_metres, shape, 6)
        for point in ((), (1,), (1, 2, 3), "12", None):
            self.assert_named("MALFORMED_POINT", points_metres, [point], 6)
        for invalid in (None, True, "1", float("nan"), float("inf"), -float("inf")):
            self.assert_named("INVALID_COORDINATE", points_metres, [(0, invalid)], 6)
            self.assert_named("INVALID_COORDINATE", points_metres, [(invalid, 0)], 6)
        for invalid in (None, True, 0, -1, float("nan"), float("inf"), -float("inf")):
            self.assert_named("INVALID_HEIGHT", model_height_metres, invalid, 6)
            self.assert_named("INVALID_SHEET_SCALE", paper_height_mm, 1, 6, invalid)
            self.assert_named("INVALID_PAPER_HEIGHT", require_legible, invalid)
        self.assert_named("INVALID_COORDINATE", points_metres, [(1e308, 0)], 0, 10)
        self.assert_named("INVALID_PAPER_HEIGHT", paper_height_mm, 1e308, 6, 1)

    def test_deliberate_inches_metres_mismatch(self):
        self.assert_named("UNIT_EQUIVALENCE_MISMATCH", require_equivalent,
                          points_metres([(100, 0)], 1),
                          points_metres([(100, 0)], 6))
        for wrong in ([(0, 0)], [(0, 0), (0, 1)]):
            self.assert_named("UNIT_EQUIVALENCE_MISMATCH", require_equivalent,
                              [(0, 0), (0, 0)], wrong)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", required=True)
    parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(UnitContractTests)
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
