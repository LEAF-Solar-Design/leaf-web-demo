"""Finite synthetic inputs, named overflow failures and an exact cancellation oracle."""

import copy
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import check_cases as checker


ROOT = Path(__file__).resolve().parent
FILES = ("module_geometry_cases.json", "fill_coverage_cases.json", "tag_overlap_cases.json")


def insert(base=(0, 0), scale=(1, 1), rotation=0, insertion=(0, 0)):
    return dict(base=list(base), scale=list(scale), rotation_deg=rotation,
                insertion=list(insertion))


def occurrence(point, inserts=(), oid="o/nested"):
    return dict(id=oid, handle="A", point=list(point), inserts=list(inserts))


class GeometryNumericTests(unittest.TestCase):
    def setUp(self):
        self.original = {name: (ROOT / name).read_bytes() for name in FILES}

    def unchanged(self):
        for name, raw in self.original.items():
            self.assertEqual((ROOT / name).read_bytes(), raw, name)

    def tearDown(self):
        self.unchanged()

    def assert_numeric(self, call, ids, field):
        with self.assertRaises(checker.GeometryNumericError) as caught:
            call()
        expected = dict(name="NONFINITE_GEOMETRY", object_ids=ids, field_path=field)
        self.assertEqual(caught.exception.diagnostic, expected)
        self.unchanged()
        return expected

    def overflow_objects(self):
        # Identity first: failures must retain the nested insert index. Later
        # shrink/cancellation cannot make a nonfinite intermediate acceptable.
        return [
            (occurrence([1.7e308, 0], [insert(),
                         insert(base=(-1.7e308, 0), scale=(1e-308, 1)),
                         insert(insertion=(-3.4, 0))]),
             "inserts[1].base.subtract[0]"),
            (occurrence([0, -1.7e308], [insert(),
                         insert(base=(0, 1.7e308), scale=(1, 1e-308))]),
             "inserts[1].base.subtract[1]"),
            (occurrence([1e308, 0], [insert(), insert(scale=(2, 1)),
                         insert(scale=(1e-308, 1))]),
             "inserts[1].scale[0]"),
            (occurrence([0, 1e308], [insert(), insert(scale=(1, 2)),
                         insert(scale=(1, 1e-308))]),
             "inserts[1].scale[1]"),
            (occurrence([1.7e308, -1.7e308], [insert(), insert(rotation=45),
                         insert(scale=(1e-308, 1e-308))]),
             "inserts[1].rotation.subtract[0]"),
            (occurrence([1.7e308, 1.7e308], [insert(), insert(rotation=45),
                         insert(scale=(1e-308, 1e-308))]),
             "inserts[1].rotation.add[1]"),
            (occurrence([1.7e308, 0], [insert(), insert(insertion=(1.7e308, 0)),
                         insert(insertion=(-1.7e308, 0))]),
             "inserts[1].insertion[0]"),
            (occurrence([0, 1.7e308], [insert(), insert(insertion=(0, 1.7e308)),
                         insert(insertion=(0, -1.7e308))]),
             "inserts[1].insertion[1]"),
        ]

    def test_nested_transform_arithmetic_names_each_first_bad_operation(self):
        for obj, field in self.overflow_objects():
            with self.subTest(field=field):
                # All authored coordinates and transforms are finite.
                for value in obj["point"]:
                    self.assertTrue(math.isfinite(value))
                for transform in obj["inserts"]:
                    for key in ("base", "scale", "insertion"):
                        self.assertTrue(all(math.isfinite(v) for v in transform[key]))
                    self.assertTrue(math.isfinite(transform["rotation_deg"]))
                self.assert_numeric(lambda: checker.world_point(obj), [obj["id"]], field)
                case = dict(original=[obj], export=[])
                self.assert_numeric(lambda: checker.geometry(case), [obj["id"]], field)

    def test_pair_subtraction_and_norm_overflow_keep_both_ids(self):
        for a, b, field in (([1.7e308, 0], [-1.7e308, 0], "distance.delta[0]"),
                            ([0, 1.7e308], [0, -1.7e308], "distance.delta[1]"),
                            ([1.3e308, 1.3e308], [0, 0], "distance")):
            for shared_handle in (True, False):
                with self.subTest(field=field, shared_handle=shared_handle):
                    original = occurrence(a, oid="o/pair")
                    exported = occurrence(b, oid="e/pair")
                    if not shared_handle:
                        exported["handle"] = "B"
                    case = dict(original=[original], export=[exported])
                    self.assert_numeric(lambda: checker.geometry(case),
                                        ["o/pair", "e/pair"], field)

    def test_valid_large_finite_world_and_distance_are_retained(self):
        # Powers of two make this large finite control exactly representable.
        unit = 2.0 ** 898
        obj = occurrence([4 * unit, -4 * unit],
                         [insert(base=(unit, -unit), scale=(0.5, 0.5)),
                          insert(insertion=(unit, -unit))])
        expected = [2.5 * unit, -2.5 * unit]
        self.assertEqual(checker.world_point(obj), expected)
        a = occurrence([1e200, 1e200], oid="o/large")
        b = occurrence([0, 0], oid="e/large")
        observed = checker.geometry(dict(original=[a], export=[b]))
        self.assertEqual(observed["world"], {"o/large": [1e200, 1e200], "e/large": [0, 0]})
        self.assertEqual(observed["diagnostics"],
                         [dict(name="MOVED", object_ids=["o/large", "e/large"])])

    def test_finite_cancellation_is_not_silently_exact_under_compare(self):
        obj = occurrence([1.0, 0.0],
                         [insert(base=(-1e16, 0)), insert(base=(1e16, 0))])
        # Exact rational oracle for these zero-rotation affine transforms.
        oracle = [Fraction(value) for value in obj["point"]]
        for transform in obj["inserts"]:
            self.assertEqual(transform["rotation_deg"], 0)
            oracle = [(value - Fraction(base)) * Fraction(scale) + Fraction(offset)
                      for value, base, scale, offset in zip(
                          oracle, transform["base"], transform["scale"], transform["insertion"])]
        self.assertEqual(oracle, [Fraction(1), Fraction(0)])
        actual = checker.world_point(obj)
        self.assertTrue(all(math.isfinite(value) for value in actual))
        self.assertGreater(abs(Fraction(actual[0]) - oracle[0]), Fraction(checker.EPS))
        with self.assertRaises(AssertionError):
            checker.compare(actual, [1, 0], "independent-exact-world")
        # Literal schema and expectation are independent of the observed world.
        case = dict(id="finite-cancellation", original=[obj], export=[], expected=dict(
            world={"o/nested": [1, 0]}, pairs=[],
            counts=dict(original=1, export=0, matched=0, moved=0, missing=1, extra=0),
            diagnostics=[dict(name="MISSING", object_ids=["o/nested"])], verdict="FAIL"))
        with self.assertRaises(AssertionError):
            checker.check_case(case, checker.geometry)

    def test_cli_numeric_errors_are_unreadable_four(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, raw in self.original.items():
                (root / name).write_bytes(raw)
                self.unchanged()
            for obj, field in self.overflow_objects():
                with self.subTest(field=field):
                    data = json.loads(self.original[FILES[0]])
                    # Preserve an independently authored valid expected schema.
                    data["cases"][0]["original"] = [copy.deepcopy(obj)]
                    (root / FILES[0]).write_text(json.dumps(data), encoding="utf-8")
                    self.unchanged()
                    child = subprocess.run([sys.executable, "-B", str(ROOT / "check_cases.py"),
                                            "--cases-dir", str(root)],
                                           capture_output=True, text=True, timeout=10)
                    self.unchanged()
                    self.assertEqual(child.returncode, 4, child.stdout + child.stderr)
                    self.assertNotIn("Traceback", child.stdout + child.stderr)
                    self.assertEqual(json.loads(child.stdout), dict(verdict="UNREADABLE", diagnostics=[
                        dict(name="NONFINITE_GEOMETRY", object_ids=[obj["id"]], field_path=field)]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
