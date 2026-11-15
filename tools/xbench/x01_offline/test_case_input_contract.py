"""Stdlib contracts for the real synthetic fixture shapes, using temporary copies."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("x01_case_input_checker", ROOT / "check_cases.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
FILES = ("module_geometry_cases.json", "fill_coverage_cases.json", "tag_overlap_cases.json")
DELETE = object()


class CaseInputContractTests(unittest.TestCase):
    def setUp(self):
        self.original = {name: (ROOT / name).read_bytes() for name in FILES}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, content in self.original.items():
            (self.root / name).write_bytes(content)

    def tearDown(self):
        for name, content in self.original.items():
            self.assertEqual((ROOT / name).read_bytes(), content, name)

    def mutate(self, family, path, value):
        # Start each mutation with an exact copy of the committed file.
        data = json.loads(self.original[FILES[family]])
        node = data
        for key in path[:-1]:
            node = node[key]
        if value is DELETE:
            del node[path[-1]]
        else:
            node[path[-1]] = copy.deepcopy(value)
        (self.root / FILES[family]).write_text(json.dumps(data), encoding="utf-8")
        return data

    def rejected(self, family, path, value, field, case_index=0, name="INVALID_CASE_INPUT"):
        data = self.mutate(family, path, value)
        cid = "<top-level>" if case_index is None else data["cases"][case_index].get("id", "<top-level>")
        if not isinstance(cid, str) or not cid:
            cid = "<top-level>"
        with self.assertRaises(checker.CaseInputError) as caught:
            checker.load_sets(self.root)
        self.assertEqual(caught.exception.diagnostic,
                         dict(name=name, case_id=cid, field_path=field))
        # Restore this family so other-family mutations remain independent.
        (self.root / FILES[family]).write_bytes(self.original[FILES[family]])

    def test_every_original_case_recomputes_and_negative_controls_reject(self):
        sets = checker.load_sets(self.root)
        self.assertEqual(sum(len(cases) for cases, _ in sets), 26)
        for cases, calculate in sets:
            for case in cases:
                with self.subTest(case=case["id"]):
                    checker.check_case(case, calculate)
        checker.rejection_checks(sets)
        for name, content in self.original.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def test_top_level_contracts_for_all_families(self):
        for family in range(3):
            mutations = [(("schema",), DELETE, "schema", "INVALID_SCHEMA"),
                         (("schema",), "unknown", "schema", "INVALID_SCHEMA"),
                         (("units",), DELETE, "units", "INVALID_CASE_INPUT"),
                         (("units",), "metres", "units", "INVALID_CASE_INPUT"),
                         (("conventions",), [], "conventions", "INVALID_CASE_INPUT"),
                         (("conventions", "comparison_epsilon"), True, "conventions.comparison_epsilon", "INVALID_CASE_INPUT"),
                         (("conventions", "comparison_epsilon"), float("nan"), "conventions.comparison_epsilon", "INVALID_CASE_INPUT"),
                         (("cases",), DELETE, "cases", "INVALID_CASE_INPUT"),
                         (("cases",), {}, "cases", "INVALID_CASE_INPUT"),
                         (("cases",), [], "cases", "INVALID_CASE_INPUT")]
            for path, value, field, name in mutations:
                with self.subTest(family=family, path=path, value=value):
                    self.rejected(family, path, value, field, None, name)
            for value in (None, [], 1):
                (self.root / FILES[family]).write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(checker.CaseInputError) as caught:
                    checker.load_sets(self.root)
                self.assertEqual(caught.exception.case_id, "<top-level>")
                (self.root / FILES[family]).write_bytes(self.original[FILES[family]])

    def test_missing_required_fields_and_duplicate_case_ids(self):
        family_fields = (("original", "export"), ("module", "fills"), ("objects",))
        for family, fields in enumerate(family_fields):
            for field in ("id", "expected") + fields:
                with self.subTest(family=family, field=field):
                    self.rejected(family, ("cases", 0, field), DELETE,
                                  "case." + field, name="MISSING_FIELD")
            data = json.loads(self.original[FILES[family]])
            self.rejected(family, ("cases", 1, "id"), data["cases"][0]["id"],
                          "id", 1, "DUPLICATE_CASE_ID")
            field = fields[-1]
            self.rejected(family, ("cases", 0, field), {}, field)

    def test_invalid_ids_objects_and_convention_fields(self):
        object_paths = (("original", 0), ("module",), ("objects", 0))
        object_fields = ("original[0]", "module", "objects[0]")
        convention_fields = (("point", "tolerance_inch"), ("rectangle", "threshold"),
                             ("box", "overlap_threshold", "minimum_text_height_mm"))
        for family, objpath in enumerate(object_paths):
            for value in (True, 1, [], ""):
                self.rejected(family, ("cases", 0, "id"), value, "id")
                self.rejected(family, ("cases", 0) + objpath + ("id",), value,
                              object_fields[family] + ".id")
            self.rejected(family, ("cases", 0) + objpath, None, object_fields[family])
            for key in convention_fields[family]:
                self.rejected(family, ("conventions", key), DELETE,
                              "conventions." + key, None)
                self.rejected(family, ("conventions", key), True,
                              "conventions." + key, None)

    def test_geometry_points_ids_handles_and_inserts(self):
        for side in ("original", "export"):
            for value in (None, "12", [0], [0, 1, 2], [True, 0], [0, float("inf")],
                          [0, float("nan")], [0, -float("inf")], ["0", 0]):
                field = side + "[0].point"
                suffix = "[0]" if isinstance(value, list) and len(value) == 2 and value[0] in (True, "0") else "[1]" if isinstance(value, list) and len(value) == 2 else ""
                with self.subTest(side=side, value=value):
                    self.rejected(0, ("cases", 0, side, 0, "point"), value, field + suffix)
            for key in ("id", "handle", "point"):
                self.rejected(0, ("cases", 0, side, 0, key), DELETE,
                              side + "[0]." + key, name="MISSING_FIELD")
            self.rejected(0, ("cases", 0, side, 0, "handle"), True, side + "[0].handle")
            self.rejected(0, ("cases", 0, side, 0, "inserts"), {}, side + "[0].inserts")
        data = json.loads(self.original[FILES[0]])
        self.rejected(0, ("cases", 0, "original", 1, "id"), data["cases"][0]["original"][0]["id"],
                      "original[1].id", name="DUPLICATE_OBJECT_ID")
        self.rejected(0, ("cases", 0, "export", 0, "id"), data["cases"][0]["original"][0]["id"],
                      "export[0].id", name="DUPLICATE_OBJECT_ID")
        prefix = ("cases", 2, "original", 0, "inserts", 0)
        for key in ("base", "scale", "insertion", "rotation_deg"):
            field = "original[0].inserts[0]." + key
            self.rejected(0, prefix + (key,), DELETE, field, 2, "MISSING_FIELD")
            values = (True, float("nan"), "90") if key == "rotation_deg" else ([], [0, True], [0, float("inf")])
            for value in values:
                suffix = "[1]" if isinstance(value, list) and len(value) == 2 else ""
                self.rejected(0, prefix + (key,), value, field + suffix, 2)
        self.rejected(0, prefix + ("scale",), [0, 1], "original[0].inserts[0].scale", 2)

    def test_fill_rectangles_holes_and_ids(self):
        locations = ((0, ("module", "rect"), "module.rect"),
                     (1, ("fills", 0, "outer"), "fills[0].outer"),
                     (4, ("fills", 0, "holes", 0), "fills[0].holes[0]"))
        for index, path, field in locations:
            for value in (None, "rect", [0, 0, 1], [0, 0, 0, 1], [0, 0, -1, 1],
                          [False, 0, 1, 1], [0, 0, float("inf"), 1]):
                suffix = "[0]" if isinstance(value, list) and value and value[0] is False else "[2]" if isinstance(value, list) and len(value) == 4 and value[2] == float("inf") else ""
                with self.subTest(path=path, value=value):
                    self.rejected(1, ("cases", index) + path, value, field + suffix, index)
        for path, field in ((("module", "id"), "module.id"), (("module", "rect"), "module.rect"),
                            (("fills", 0, "outer"), "fills[0].outer"), (("fills", 0, "holes"), "fills[0].holes")):
            self.rejected(1, ("cases", 1) + path, DELETE, field, 1, "MISSING_FIELD")
        self.rejected(1, ("cases", 1, "fills", 0, "holes"), {}, "fills[0].holes", 1)
        self.rejected(1, ("cases", 4, "fills", 0, "holes", 0), [-1, 1, 9, 9], "fills[0].holes[0]", 4)
        self.rejected(1, ("cases", 1, "fills", 0, "id"), "m/1", "fills[0].id", 1, "DUPLICATE_OBJECT_ID")

    def test_tag_boxes_dimensions_and_line_count(self):
        prefix = ("cases", 0, "objects", 0)
        for key in ("id", "kind", "center", "width", "height", "rotation_deg", "line_count", "paper_text_height_mm"):
            self.rejected(2, prefix + (key,), DELETE, "objects[0]." + key, name="MISSING_FIELD")
        self.rejected(2, prefix + ("kind",), "unknown", "objects[0].kind")
        for value in ([], [0], [0, 0, 0], "xy", None):
            self.rejected(2, prefix + ("center",), value, "objects[0].center")
        for value in (True, "1", float("nan"), float("inf"), -float("inf")):
            self.rejected(2, prefix + ("center",), [0, value], "objects[0].center[1]")
            for key in ("width", "height", "rotation_deg", "line_count", "paper_text_height_mm"):
                self.rejected(2, prefix + (key,), value, "objects[0]." + key)
        for key in ("width", "height", "line_count", "paper_text_height_mm"):
            for value in (0, -1):
                self.rejected(2, prefix + (key,), value, "objects[0]." + key)
        self.rejected(2, prefix + ("line_count",), 1.5, "objects[0].line_count")
        self.rejected(2, ("cases", 0, "objects", 1, "id"), "t/a", "objects[1].id", name="DUPLICATE_OBJECT_ID")

    def test_expected_shape_bool_and_nonfinite_for_each_family(self):
        leaves = (("world", "o/block1/module", 0), ("fraction",), ("pairs", 0, "intersection_area"))
        for family, leaf in enumerate(leaves):
            field = "expected." + {0: "world.o/block1/module[0]", 1: "fraction", 2: "pairs[0].intersection_area"}[family]
            for value in (True, False, float("nan"), float("inf"), -float("inf"), "0"):
                with self.subTest(family=family, value=value):
                    self.rejected(family, ("cases", 0, "expected") + leaf, value, field)
            for value in (None, [], "PASS"):
                self.rejected(family, ("cases", 0, "expected"), value, "expected")
            self.rejected(family, ("cases", 0, "expected", "verdict"), DELETE,
                          "expected.verdict", name="MISSING_FIELD")
            self.rejected(family, ("cases", 0, "expected", "diagnostics"), {}, "expected.diagnostics")

    def test_valid_input_changes_are_expectation_failures(self):
        # Each change alters a variable output map/list as well as values.
        # Loading and validation must not use the calculation as a shape oracle.
        mutations = ((0, ("cases", 0, "export", 0, "point"), [100, 100]),
                     (1, ("cases", 0, "fills"),
                      [dict(id="f/new", outer=[0, 0, 10, 10], holes=[])]),
                     (2, ("cases", 0, "objects"),
                      json.loads(self.original[FILES[2]])["cases"][0]["objects"][:1]))
        for family, path, value in mutations:
            with self.subTest(family=family):
                data = self.mutate(family, path, value)
                cases, calculate = checker.load_sets(self.root)[family]
                case = cases[0]
                checker.validate_case(case, calculate)
                with self.assertRaises(AssertionError):
                    checker.check_case(case, calculate)
                child = subprocess.run([sys.executable, str(ROOT / "check_cases.py"),
                                        "--cases-dir", str(self.root)], cwd=self.root,
                                       capture_output=True, text=True, timeout=10)
                self.assertEqual(child.returncode, 1, child.stdout + child.stderr)
                self.assertNotIn("Traceback", child.stdout + child.stderr)
                diagnostic = json.loads(child.stdout)["diagnostics"][0]
                self.assertEqual(diagnostic["name"], "CASE_CHECK_FAILED")
                self.assertEqual(diagnostic["case_id"], data["cases"][0]["id"])
                self.assertEqual(diagnostic["field_path"], "expected")
                (self.root / FILES[family]).write_bytes(self.original[FILES[family]])

    def test_well_formed_wrong_expectations_reach_compare(self):
        mutations = ((0, ("world",), {}), (0, ("pairs",), []),
                     (0, ("counts", "matched"), 99),
                     (1, ("per_fill_intersection_areas",), {"f/wrong": 1}),
                     (1, ("fraction",), 0.5), (2, ("pairs",), []))
        for family, path, value in mutations:
            with self.subTest(family=family, path=path):
                self.mutate(family, ("cases", 0, "expected") + path, value)
                cases, calculate = checker.load_sets(self.root)[family]
                with self.assertRaises(AssertionError):
                    checker.check_case(cases[0], calculate)
                (self.root / FILES[family]).write_bytes(self.original[FILES[family]])
        for family in range(3):
            for path, value in ((("diagnostics",), [dict(name="WRONG", object_ids=["absent"])]),
                                (("verdict",), "WRONG")):
                with self.subTest(family=family, path=path):
                    self.mutate(family, ("cases", 0, "expected") + path, value)
                    cases, calculate = checker.load_sets(self.root)[family]
                    with self.assertRaises(AssertionError):
                        checker.check_case(cases[0], calculate)
                    (self.root / FILES[family]).write_bytes(self.original[FILES[family]])

    def test_expected_nested_schema_is_checked_without_observed_shapes(self):
        malformed = ((0, ("pairs",), [["only-one-id"]], "expected.pairs[0]"),
                     (0, ("world",), {"o/new": [0]}, "expected.world.o/new"),
                     (0, ("counts", "moved"), [], "expected.counts.moved"),
                     (1, ("per_fill_intersection_areas",), {"f/new": "zero"},
                      "expected.per_fill_intersection_areas.f/new"),
                     (2, ("pairs", 0, "object_ids"), ["one"], "expected.pairs[0].object_ids"))
        for family, path, value, field in malformed:
            with self.subTest(family=family, path=path):
                self.rejected(family, ("cases", 0, "expected") + path, value, field)
        for family in range(3):
            self.rejected(family, ("cases", 0, "expected", "diagnostics"),
                          [dict(name="WRONG", object_ids=True)], "expected.diagnostics[0].object_ids")
            self.rejected(family, ("cases", 0, "expected", "diagnostics"),
                          [dict(name="WRONG")], "expected.diagnostics[0].object_ids", name="MISSING_FIELD")

    def test_cli_exact_case_field_and_no_traceback(self):
        mutations = ((0, ("cases", 0, "original", 0, "point"), [True, 0], "original[0].point[0]"),
                     (1, ("cases", 0, "module", "rect"), [0, 0, 0, 1], "module.rect"),
                     (2, ("cases", 0, "objects", 0, "width"), float("nan"), "objects[0].width"))
        for family, path, value, field in mutations:
            data = self.mutate(family, path, value)
            child = subprocess.run([sys.executable, str(ROOT / "check_cases.py"),
                                    "--cases-dir", str(self.root)], cwd=self.root,
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 1)
            self.assertNotIn("Traceback", child.stdout + child.stderr)
            self.assertEqual(json.loads(child.stdout)["diagnostics"], [dict(
                name="INVALID_CASE_INPUT", case_id=data["cases"][0]["id"], field_path=field)])
            (self.root / FILES[family]).write_bytes(self.original[FILES[family]])

    def test_cli_bad_json_and_valid_copy(self):
        path = self.root / FILES[0]
        path.write_text("{", encoding="utf-8")
        command = [sys.executable, str(ROOT / "check_cases.py"), "--cases-dir", str(self.root)]
        child = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(json.loads(child.stdout)["diagnostics"], [dict(
            name="UNREADABLE_CASE_SET", case_id="<top-level>", field_path=FILES[0])])
        self.assertNotIn("Traceback", child.stderr)
        path.write_bytes(self.original[FILES[0]])
        child = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertIn("2 negative controls; PASS", child.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
