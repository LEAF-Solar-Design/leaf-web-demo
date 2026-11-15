"""Bounded synthetic unit-cell oracle; no fixtures, file IO, or drawing grades.

Exactly 64 deterministic cases, coordinates in [-8, 8], at most four fills
and two holes per fill. Each rectangle occupies at most 256 integer cells.
Expected coverage uses set algebra, never checker.fill_area or edge grids.
"""

import copy
from fractions import Fraction
import random
import unittest

try:
    from . import check_cases as checker
except ImportError:  # Direct execution and discovery from this directory.
    import check_cases as checker


EPS = Fraction("1e-10")


def cells(rect):
    left, bottom, right, top = rect
    return {(x, y) for x in range(left, right) for y in range(bottom, top)}


def occupancy_expected(case):
    module = cells(case["module"]["rect"])
    union, contributions = set(), {}
    for fill in case["fills"]:
        occupied = cells(fill["outer"])
        for hole in fill["holes"]:
            occupied.difference_update(cells(hole))
        contributions[fill["id"]] = len(occupied & module)
        union.update(occupied)
    area = len(union & module)
    fraction = Fraction(area, len(module))
    offenders = sorted(key for key, value in contributions.items() if value)
    failed = fraction > Fraction(1, 20) + EPS
    return {
        "module_area": len(module), "fill_union_area_inside_module": area,
        "intersection_area": area, "fraction": float(fraction),
        "threshold": 0.05, "per_fill_intersection_areas": contributions,
        "diagnostics": [{"name": "FILL_OVERLAP",
                         "object_ids": [case["module"]["id"], *offenders]}]
        if failed else [], "verdict": "FAIL" if failed else "PASS",
    }


def scenario(label, module, fills):
    return {"id": label, "module": {"id": "m/" + label, "rect": module},
            "fills": [{"id": f"f/{label}/{i}", "outer": outer, "holes": holes}
                      for i, (outer, holes) in enumerate(fills)]}


def scenarios():
    cases = [
        scenario("empty", [-4, -4, 4, 4], []),
        scenario("complete", [-4, -4, 4, 4], [([-8, -8, 8, 8], [])]),
        scenario("outside_contact", [-2, -2, 2, 2],
                 [([2, -2, 4, 2], []), ([3, 3, 5, 5], []),
                  ([2, 2, 4, 4], [])]),
        scenario("overlapping_fills", [-4, -4, 4, 4],
                 [([-3, -3, 2, 2], []), ([-2, -2, 3, 3], [])]),
        scenario("overlapping_holes", [-4, -4, 4, 4],
                 [([-4, -4, 4, 4], [[-3, -3, 1, 1], [-1, -1, 3, 3]])]),
        scenario("refilled_hole", [-4, -4, 4, 4],
                 [([-4, -4, 4, 4], [[-2, -2, 2, 2]]),
                  ([-2, -2, 2, 2], [])]),
        scenario("fully_holed", [-4, -4, 4, 4],
                 [([-4, -4, 4, 4], [[-4, -4, 4, 4]])]),
        scenario("threshold_equal", [-5, -2, 5, 0], [([0, -1, 1, 0], [])]),
        scenario("threshold_above", [-5, -2, 5, 0], [([0, -1, 2, 0], [])]),
        scenario("threshold_below", [-8, -8, 8, 8], [([0, 0, 1, 1], [])]),
    ]
    rng = random.Random(0xF111)

    def rect_within(bounds):
        x0, x1 = sorted(rng.sample(range(bounds[0], bounds[2] + 1), 2))
        y0, y1 = sorted(rng.sample(range(bounds[1], bounds[3] + 1), 2))
        return [x0, y0, x1, y1]

    while len(cases) < 64:
        fills = []
        for _ in range(rng.randrange(5)):
            outer = rect_within([-8, -8, 8, 8])
            holes = [rect_within(outer) for _ in range(rng.randrange(3))]
            fills.append((outer, holes))
        cases.append(scenario(f"seeded_{len(cases):02d}",
                              rect_within([-8, -8, 8, 8]), fills))
    return cases


class FillGridOracle(unittest.TestCase):
    def assert_result(self, case, actual):
        # Include all source IDs even when a corrupted diagnostic drops one.
        context = repr([case["module"]["id"]] + [f["id"] for f in case["fills"]])
        expected = occupancy_expected(case)
        self.assertEqual(actual.keys(), expected.keys(), context)
        for key in expected:
            if key == "fraction":
                self.assertAlmostEqual(actual[key], expected[key],
                                       delta=float(EPS) / 16, msg=context)
            else:
                self.assertEqual(actual[key], expected[key], f"{context}: {key}")

    def test_capped_grid_cases(self):
        cases = scenarios()
        self.assertEqual(len(cases), 64)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertLessEqual(len(case["fills"]), 4)
                rects = [case["module"]["rect"]]
                for fill in case["fills"]:
                    self.assertLessEqual(len(fill["holes"]), 2)
                    rects.extend([fill["outer"], *fill["holes"]])
                self.assertTrue(all(-8 <= v <= 8 for r in rects for v in r))
                self.assert_result(case, checker.fill_coverage(case))

    def test_threshold_controls(self):
        cases = {c["id"]: c for c in scenarios()}
        for label, fraction, verdict in (("threshold_below", 1 / 256, "PASS"),
                                          ("threshold_equal", 0.05, "PASS"),
                                          ("threshold_above", 0.1, "FAIL")):
            with self.subTest(case=label):
                result = checker.fill_coverage(cases[label])
                self.assert_result(cases[label], result)
                self.assertEqual(result["fraction"], fraction)
                self.assertEqual(result["verdict"], verdict)

    def test_corrupt_results_rejected_with_offender_ids(self):
        case = next(c for c in scenarios() if c["id"] == "overlapping_fills")
        result = checker.fill_coverage(case)
        self.assert_result(case, result)
        for field in ("intersection_area", "diagnostics"):
            corrupt = copy.deepcopy(result)
            if field == "intersection_area":
                corrupt[field] += 1
            else:
                corrupt[field][0]["object_ids"].pop()
            with self.subTest(field=field):
                with self.assertRaises(AssertionError) as caught:
                    self.assert_result(case, corrupt)
                for obj in [case["module"], *case["fills"]]:
                    self.assertIn(obj["id"], str(caught.exception))


if __name__ == "__main__":
    unittest.main()
