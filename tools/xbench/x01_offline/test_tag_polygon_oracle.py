"""Bounded right-angle rectangle oracle using exact rational intervals.

No production corners, clipping, or polygon integration constructs expectations.
Only SUBJECT winding is reversed: clip_polygon documents a convex CCW clip;
clockwise clip support is explicitly outside this accepted boundary.
All inputs are synthetic in-memory values; no IO, subprocesses, or fixtures.
"""

import copy
from fractions import Fraction
import itertools
import math
import unittest

try:
    from . import check_cases as checker
except ImportError:  # Direct execution and discovery from this directory.
    import check_cases as checker


EPS = Fraction("1e-10")


def bounds(obj):
    """Right angles exchange side lengths; no trigonometry or vertex rotation."""
    width, height = Fraction(obj["width"]), Fraction(obj["height"])
    if obj["rotation_deg"] % 180 == 90:
        width, height = height, width
    x, y = map(Fraction, obj["center"])
    return x - width / 2, y - height / 2, x + width / 2, y + height / 2


def intersection(a, b):
    return (max(Fraction(0), min(a[2], b[2]) - max(a[0], b[0])) *
            max(Fraction(0), min(a[3], b[3]) - max(a[1], b[1])))


def rectangle_expected(case):
    objects = {o["id"]: o for o in case["objects"]}
    pairs, diagnostics = [], []
    for obj in objects.values():
        if (obj["kind"] == "tag" and
                Fraction(obj["paper_text_height_mm"]) < Fraction("1.8") - EPS):
            diagnostics.append({"name": "TEXT_TOO_SMALL", "object_ids": [obj["id"]]})
    for a_id, b_id in itertools.combinations(sorted(objects), 2):
        a, b = objects[a_id], objects[b_id]
        if a["kind"] == b["kind"] == "module":
            continue
        area = intersection(bounds(a), bounds(b))
        smaller = min(Fraction(o["width"]) * Fraction(o["height"]) for o in (a, b))
        fraction = area / smaller
        pairs.append({"object_ids": [a_id, b_id], "intersection_area": float(area),
                      "smaller_box_fraction": float(fraction)})
        if a["kind"] == b["kind"] == "tag":
            name = "TAG_OVERLAP" if fraction > Fraction("0.1") + EPS else None
        else:
            name = "TAG_MODULE_INTERSECTION" if area > EPS else None
        if name:
            diagnostics.append({"name": name, "object_ids": [a_id, b_id]})
    diagnostics.sort(key=lambda d: (d["name"], d["object_ids"]))
    failed = any(d["name"] in ("TAG_OVERLAP", "TEXT_TOO_SMALL") for d in diagnostics)
    return {"pairs": pairs, "diagnostics": diagnostics,
            "verdict": "FAIL" if failed else "PASS"}


def box(identifier, center=(0, 0), width=2, height=2, rotation=0,
        kind="tag", text=1.8):
    return {"id": identifier, "kind": kind, "center": list(center),
            "width": width, "height": height, "rotation_deg": rotation,
            "line_count": 1 if kind == "tag" else 0, "paper_text_height_mm": text}


def scenarios():
    cases = []

    def add(label, objects):
        cases.append({"id": label, "objects": objects})

    for angle in (0, 90, 180, 270):
        add(f"mixed_{angle}", [
            box("tag/a", (-2, 1), 4, 2, angle),
            box("tag/b", (1, -2), 2, 6, angle),
            box("module/a", (-2, 1), 2, 4, angle, "module"),
            box("module/b", (-2, 1), 2, 4, angle, "module"),
            box("tag/outside", (8, 8), 1, 1, angle)])
        for kind in ("tag", "module"):
            for label, center in (("identical", (0, 0)), ("edge", (2, 0)),
                                  ("corner", (2, 2))):
                add(f"{label}_{kind}_{angle}",
                    [box("a/tag", rotation=angle),
                     box("b/" + kind, center, rotation=angle, kind=kind)])
        # Dyadic dimensions and centers keep very small positive areas
        # representable. Fractions remain order one even below absolute EPS.
        for label, width, height in (("tiny", 2 ** -20, 2 ** -20),
                                      ("narrow", 2 ** -18, 2 ** -8)):
            add(f"{label}_{angle}",
                [box("a/tiny", width=width, height=height, rotation=angle),
                 box("b/tiny", width=width, height=height, rotation=angle),
                 box("c/module", width=width, height=height,
                     rotation=angle, kind="module")])
    for label, offset in (("below", 9 + 2 ** -20), ("equal", 9),
                           ("epsilon_band", 9 - 2 ** -32),
                           ("above", 9 - 2 ** -20)):
        add("threshold_" + label,
            [box("a/tag", width=10), box("b/tag", (offset, 0), width=10)])
    for label, text in (("below", 1.8 - 4e-10), ("equal", 1.8),
                         ("epsilon_band", 1.8 - 0.5e-10), ("above", 1.8 + 4e-10)):
        add("text_" + label, [box("text/" + label, text=text)])
    for exponent in (20, 36):
        add(f"positive_sliver_{exponent}",
            [box("a/sliver"), box("b/sliver", (2 - 2 ** -exponent, 0)),
             box("c/module", (2 - 2 ** -exponent, 0), kind="module")])
    add("only_modules", [box("module/a", kind="module"),
                         box("module/b", kind="module")])
    add("empty", [])
    return cases


class TagPolygonOracle(unittest.TestCase):
    def assert_number(self, actual, expected, context):
        self.assertTrue(math.isfinite(actual), context)
        # Absolute EPS alone would accept zero for the tiny-area controls.
        tolerance = min(float(EPS) / 16, abs(expected) * 1e-8) if expected else float(EPS) / 16
        self.assertAlmostEqual(actual, expected, delta=tolerance, msg=context)

    def assert_result(self, case, actual):
        expected = rectangle_expected(case)
        context = f"{case['id']}: {[o['id'] for o in case['objects']]}"
        self.assertEqual(actual.keys(), expected.keys(), context)
        self.assertEqual(actual["diagnostics"], expected["diagnostics"], context)
        self.assertEqual(actual["verdict"], expected["verdict"], context)
        self.assertEqual(len(actual["pairs"]), len(expected["pairs"]), context)
        for got, want in zip(actual["pairs"], expected["pairs"]):
            self.assertEqual(got.keys(), want.keys(), context)
            self.assertEqual(got["object_ids"], want["object_ids"], context)
            for key in ("intersection_area", "smaller_box_fraction"):
                self.assert_number(got[key], want[key], context + ": " + key)

    def test_bounded_rectangle_scenarios(self):
        cases = scenarios()
        self.assertLessEqual(len(cases), 64)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertLessEqual(len(case["objects"]), 5)
                self.assert_result(case, checker.tag_overlaps(case))

    def test_threshold_and_text_controls(self):
        cases = {c["id"]: c for c in scenarios()}
        for prefix in ("threshold", "text"):
            for label in ("below", "equal", "epsilon_band", "above"):
                case = cases[prefix + "_" + label]
                with self.subTest(case=case["id"]):
                    result = checker.tag_overlaps(case)
                    self.assert_result(case, result)
                    fails = label == ("above" if prefix == "threshold" else "below")
                    self.assertEqual(result["verdict"], "FAIL" if fails else "PASS")

    def test_subject_winding_with_ccw_clip(self):
        def polygon(rect):
            x0, y0, x1, y1 = map(float, rect)
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

        for case in scenarios():
            for a, b in itertools.combinations(case["objects"], 2):
                context = f"{case['id']}: {a['id']}, {b['id']}"
                ar, br = bounds(a), bounds(b)
                subject, clip = polygon(ar), polygon(br)
                area = float((ar[2] - ar[0]) * (ar[3] - ar[1]))
                overlap = float(intersection(ar, br))
                with self.subTest(pair=context):
                    for winding in (subject, subject[::-1]):
                        self.assert_number(checker.polygon_area(winding), area, context)
                        clipped = checker.clip_polygon(winding, clip)
                        self.assert_number(checker.polygon_area(clipped), overlap, context)

    def test_corrupt_overlap_rejected_with_ids(self):
        case = next(c for c in scenarios() if c["id"] == "tiny_0")
        result = checker.tag_overlaps(case)
        self.assert_result(case, result)
        for field in ("intersection_area", "smaller_box_fraction", "diagnostics"):
            corrupt = copy.deepcopy(result)
            if field == "diagnostics":
                corrupt[field][0]["object_ids"].pop()
            else:
                corrupt["pairs"][0][field] = 0.0
            with self.subTest(field=field):
                with self.assertRaises(AssertionError) as caught:
                    self.assert_result(case, corrupt)
                for obj in case["objects"]:
                    self.assertIn(obj["id"], str(caught.exception))


if __name__ == "__main__":
    unittest.main()
