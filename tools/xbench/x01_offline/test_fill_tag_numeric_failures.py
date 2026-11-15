"""Named arithmetic refusals and capped exact interval area controls."""

import copy
from fractions import Fraction
import io
import json
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import check_cases as checker


FILES = ("module_geometry_cases.json", "fill_coverage_cases.json", "tag_overlap_cases.json")


def box(oid, center=(0, 0), width=2, height=2):
    return dict(id=oid, kind="tag", center=list(center), width=width, height=height,
                rotation_deg=0, line_count=1, paper_text_height_mm=2)


def fill_case(rect):
    return dict(id="numeric-fill", module=dict(id="module", rect=rect),
                fills=[dict(id="fill", outer=list(rect), holes=[])])


class NumericControls(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent
        self.raw = {f: (self.root / f).read_bytes() for f in FILES}

    def tearDown(self):
        for f, raw in self.raw.items():
            self.assertEqual((self.root / f).read_bytes(), raw)

    def refuses(self, call, ids, context):
        with self.assertRaises(checker.FillTagNumericError) as caught:
            call()
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic["name"], "INVALID_FILL_TAG_ARITHMETIC")
        self.assertEqual(diagnostic["object_ids"], ids)
        self.assertIn(context, diagnostic["field_path"])
        return diagnostic

    def test_area_overflow_underflow_and_collapsed_corners(self):
        for rect in ([0, 0, 1e200, 1e200], [0, 0, 1e-200, 1e-200]):
            self.refuses(lambda: checker.fill_coverage(fill_case(rect)), ["module"], "area")
        for width in (1e200, 1e-200):
            obj = box("bad", width=width, height=width)
            self.refuses(lambda: checker.tag_overlaps(dict(objects=[obj])), ["bad"], "area")
        obj = box("collapsed", center=(1e100, 1e100))
        self.refuses(lambda: checker.corners(obj), ["collapsed"], "collapsed")
        obj = box("nonfinite", center=(float("inf"), 0))
        self.refuses(lambda: checker.corners(obj), ["nonfinite"], "center")

    def test_nonfinite_clip_and_underflow_ratio_are_named(self):
        self.refuses(lambda: checker.clip_polygon([(1e308, 0), (0, 1e308), (0, 0)],
                                                  [(-1e308, 0), (0, 1), (0, -1)], ["a", "b"]),
                     ["a", "b"], "clip")
        self.refuses(lambda: checker.ratio(1e-300, 1e100, ["a"], "fraction"),
                     ["a"], "fraction")
        self.refuses(lambda: checker.polygon_area([(0, 0), (1e-200, 0), (0, 1e-200)], ["tiny"]),
                     ["tiny"], "polygon")
        obj = box("angle")
        obj["rotation_deg"] = 5e-324
        self.refuses(lambda: checker.corners(obj), ["angle"], "radians")
        obj = box("rotate", width=1e-200, height=1e200)
        obj["rotation_deg"] = 1e-200
        self.refuses(lambda: checker.corners(obj), ["rotate"], "sin_x")

    def test_fraction_controls_large_offsets_and_tiny_positive_boxes(self):
        for offset, side in ((2.0**50, 2.0), (2.0**510, 2.0**460),
                             (0, 2.0**-500), (0, 2.0**-530)):
            with self.subTest(offset=offset, side=side):
                rect = [offset, offset, offset + side, offset + side]
                expected = float((Fraction(rect[2]) - Fraction(rect[0])) ** 2)
                result = checker.fill_coverage(fill_case(rect))
                self.assertEqual(result["module_area"], expected)
                self.assertEqual(result["intersection_area"], expected)
                self.assertEqual(result["fraction"], 1)
                a = box("a", (offset, offset), side, side)
                b = box("b", (offset, offset), side, side)
                result = checker.tag_overlaps(dict(objects=[a, b]))["pairs"][0]
                self.assertEqual(result["intersection_area"], float(Fraction(side)**2))
                self.assertEqual(result["smaller_box_fraction"], 1)

    def test_midpoint_overflow_and_adjacent_endpoints_need_no_midpoint(self):
        # The first x midpoint would overflow; the second has no representable
        # interior float. Both exact positive rectangular areas are valid.
        for x, width, height in ((2.0**1023, 2.0**973, 2.0**-973),
                                  (2.0**50, .25, 2)):
            rect = [x, 0, x + width, height]
            exact = (Fraction(rect[2]) - Fraction(rect[0])) * Fraction(height)
            result = checker.fill_coverage(fill_case(rect))
            self.assertEqual(result["intersection_area"], float(exact))
            self.assertEqual(result["fraction"], 1)
        a = box("a", (2.0**1023, 0), 2.0**973, 2.0**-973)
        b = dict(a, id="b")
        result = checker.tag_overlaps(dict(objects=[a, b]))["pairs"][0]
        self.assertEqual(result["intersection_area"], 1)
        self.assertEqual(result["smaller_box_fraction"], 1)

    def test_separation_and_edge_contact_are_valid_zero(self):
        for x in (2, 3):
            result = checker.tag_overlaps(dict(objects=[box("a"), box("b", (x, 0))]))
            self.assertEqual(result["pairs"][0]["intersection_area"], 0)
            case = fill_case([0, 0, 1, 1])
            case["fills"][0]["outer"] = [x, 0, x+1, 1]
            self.assertEqual(checker.fill_coverage(case)["intersection_area"], 0)

    def test_cli_named_unreadable_four(self):
        # In-memory load seam exercises the real CLI exception mapping without
        # replacing any independently authored expected schema.
        case = copy.deepcopy(json.loads(self.raw[FILES[1]])["cases"][0])
        case["module"]["rect"] = [0, 0, 1e200, 1e200]
        output = io.StringIO()
        with patch.object(checker, "load_sets", return_value=[([case], checker.fill_coverage)]), \
                patch("sys.argv", ["check_cases.py"]), redirect_stdout(output):
            code = checker.main()
        self.assertEqual(code, 4)
        result = json.loads(output.getvalue())
        self.assertEqual(result["verdict"], "UNREADABLE")
        self.assertEqual(result["diagnostics"][0]["object_ids"], [case["module"]["id"]])
        self.assertEqual(result["diagnostics"][0]["name"], "INVALID_FILL_TAG_ARITHMETIC")


if __name__ == "__main__":
    unittest.main(verbosity=2)
