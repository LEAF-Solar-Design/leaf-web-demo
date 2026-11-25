"""Exact/plus-one work boundaries; sentinels prove refusal precedes loops."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import check_cases as checker


FILES = ("module_geometry_cases.json", "fill_coverage_cases.json", "tag_overlap_cases.json")


def geometry_case():
    return dict(id="geometry-budget",
                original=[dict(id="o", handle="a", point=[0, 0])],
                export=[dict(id="e", handle="b", point=[0, 0])])


def fill_case():
    return dict(id="fill-budget", module=dict(id="m", rect=[0, 0, 4, 4]),
                fills=[dict(id="f", outer=[1, 1, 2, 2], holes=[])])


def tag(oid):
    return dict(id=oid, kind="tag", center=[0, 0], width=2, height=2,
                rotation_deg=0, line_count=1, paper_text_height_mm=2)


class WorkLimits(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent
        self.raw = {f: (self.root / f).read_bytes() for f in FILES}

    def tearDown(self):
        for f, raw in self.raw.items():
            self.assertEqual((self.root / f).read_bytes(), raw)

    def refusal(self, call, case, resource, limit, observed, costly):
        with patch.object(checker, costly, side_effect=AssertionError("expensive operation reached")) as sentinel:
            with self.assertRaises(checker.CaseWorkLimitError) as caught:
                call()
            sentinel.assert_not_called()
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic["name"], "CASE_WORK_LIMIT")
        self.assertEqual(diagnostic["case_id"], case.get("id", "<direct>"))
        self.assertEqual(diagnostic["resource"], resource)
        self.assertEqual(diagnostic["limit"], limit)
        self.assertEqual(diagnostic["observed"], observed)
        return diagnostic

    def test_combined_objects_on_every_direct_api(self):
        cases = [(geometry_case(), checker.geometry, "world_point"),
                 (fill_case(), checker.fill_coverage, "fill_area"),
                 (dict(id="tags-budget", objects=[tag("a"), tag("b")]), checker.tag_overlaps, "corners")]
        for case, calculate, costly in cases:
            with self.subTest(api=calculate.__name__):
                with patch.object(checker, "MAX_OBJECTS_PER_CASE", 2):
                    calculate(case)
                with patch.object(checker, "MAX_OBJECTS_PER_CASE", 1):
                    self.refusal(lambda: calculate(case), case, "objects", 1, 2, costly)

    def test_candidate_pairs_before_transforms_or_clipping(self):
        geo = geometry_case()
        geo["original"].append(dict(id="o2", handle="c", point=[1, 0]))
        tags = dict(id="tags-budget", objects=[tag("a"), tag("b"), tag("c")])
        for case, calculate, count, costly in ((geo, checker.geometry, 2, "world_point"),
                                               (tags, checker.tag_overlaps, 3, "corners")):
            with self.subTest(api=calculate.__name__):
                with patch.object(checker, "MAX_CANDIDATE_PAIRS", count):
                    calculate(case)
                with patch.object(checker, "MAX_CANDIDATE_PAIRS", count-1):
                    self.refusal(lambda: calculate(case), case, "candidate_pairs", count-1, count, costly)

    def test_holes_counted_across_fills(self):
        case = fill_case()
        case["fills"][0]["holes"] = [[1, 1, 2, 2]]
        second = copy.deepcopy(case["fills"][0])
        second["id"] = "f2"
        case["fills"].append(second)
        with patch.object(checker, "MAX_HOLES_PER_CASE", 2):
            checker.fill_coverage(case)
        with patch.object(checker, "MAX_HOLES_PER_CASE", 1):
            diagnostic = self.refusal(lambda: checker.fill_coverage(case), case, "holes", 1, 2, "fill_area")
        self.assertEqual(diagnostic["object_ids"], ["m"])

    def test_grid_union_and_per_fill_are_one_budget(self):
        case = fill_case()
        # Four boundaries in each axis: nine cells, each charged one module
        # and one fill visit. The same 18 visits occur in the per-fill pass.
        with patch.object(checker, "MAX_FILL_GRID_WORK", 36):
            checker.fill_coverage(case)
        with patch.object(checker, "MAX_FILL_GRID_WORK", 35):
            diagnostic = self.refusal(lambda: checker.fill_coverage(case), case,
                                      "fill_grid_work", 35, 36, "fill_area")
        self.assertEqual(diagnostic["object_ids"], ["m", "f"])
        second = copy.deepcopy(case["fills"][0])
        second["id"] = "f2"
        case["fills"].append(second)
        # Union: 9*3; two per-fill passes: 2*18. No resetting budgets.
        with patch.object(checker, "MAX_FILL_GRID_WORK", 63):
            checker.fill_coverage(case)
        with patch.object(checker, "MAX_FILL_GRID_WORK", 62):
            self.refusal(lambda: checker.fill_coverage(case), case, "fill_grid_work", 62, 63, "fill_area")

    def test_direct_fill_area_preflight(self):
        case = fill_case()
        with patch.object(checker, "MAX_FILL_GRID_WORK", 18):
            self.assertEqual(checker.fill_area(case["module"]["rect"], case["fills"], ["m"]), 1)
        with patch.object(checker, "MAX_FILL_GRID_WORK", 17):
            self.refusal(lambda: checker.fill_area(case["module"]["rect"], case["fills"], ["m"]),
                         {}, "fill_grid_work", 17, 18, "rectangle")

    def test_cli_unreadable_four(self):
        case = json.loads(self.raw[FILES[0]])["cases"][0]
        output = io.StringIO()
        with patch.object(checker, "load_sets", return_value=[([case], checker.geometry)]), \
                patch.object(checker, "MAX_OBJECTS_PER_CASE", 0), \
                patch("sys.argv", ["check_cases.py"]), redirect_stdout(output):
            code = checker.main()
        self.assertEqual(code, 4)
        result = json.loads(output.getvalue())
        self.assertEqual(result["verdict"], "UNREADABLE")
        self.assertEqual(result["diagnostics"][0]["name"], "CASE_WORK_LIMIT")
        self.assertEqual(result["diagnostics"][0]["case_id"], case["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
