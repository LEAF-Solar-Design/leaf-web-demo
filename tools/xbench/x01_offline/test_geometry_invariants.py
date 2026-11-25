"""Deterministic synthetic invariants; no drawing grade or readiness claim."""

import copy
import itertools
import json
import math
from pathlib import Path
import unittest

try:
    from . import check_cases as checker
except ImportError:  # Direct execution and unittest discovery from this directory.
    import check_cases as checker


ROOT = Path(__file__).resolve().parent


def literals(filename):
    with (ROOT / filename).open(encoding="utf-8") as stream:
        return json.load(stream)["cases"]


def translate(point):
    return [point[0] + 7, point[1] - 3]


def quarter_turn(point):
    return [-point[1], point[0]]


def flattened_point(obj):
    """Independent complex-number evaluation of the input's nested inserts."""
    value = complex(*obj["point"])
    for insert in obj.get("inserts", []):
        offset = value - complex(*insert["base"])
        scaled = complex(offset.real * insert["scale"][0],
                         offset.imag * insert["scale"][1])
        radians = insert["rotation_deg"] * math.pi / 180
        value = scaled * complex(math.cos(radians), math.sin(radians))
        value += complex(*insert["insertion"])
    return [value.real, value.imag]


def map_rectangle(rect, transform):
    points = [transform([x, y]) for x in (rect[0], rect[2])
              for y in (rect[1], rect[3])]
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


class GeometryInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geometry_cases = literals("module_geometry_cases.json")
        cls.fill_cases = literals("fill_coverage_cases.json")
        cls.tag_cases = literals("tag_overlap_cases.json")

    def geometry_transform(self, transform):
        for source in self.geometry_cases:
            # Greedy edge ordering can change under floating-point rotations of
            # equal distances. Only test the declared ID policy for ties below.
            if source["id"] == "equal_distance_id_tie":
                continue
            with self.subTest(case=source["id"]):
                checker.check_case(source, checker.geometry)
                case = copy.deepcopy(source)
                for side in ("original", "export"):
                    for obj in case[side]:
                        obj["point"] = transform(flattened_point(obj))
                        obj.pop("inserts", None)
                # Transform literal world expectations, never computed results.
                case["expected"]["world"] = {
                    key: transform(point)
                    for key, point in source["expected"]["world"].items()}
                checker.check_case(case, checker.geometry)

    def test_geometry_translation(self):
        self.geometry_transform(translate)

    def test_geometry_rotation_90_degrees(self):
        self.geometry_transform(quarter_turn)

    def test_geometry_input_order(self):
        for source in self.geometry_cases:
            for original, export in itertools.product((False, True), repeat=2):
                with self.subTest(case=source["id"], original=original, export=export):
                    case = copy.deepcopy(source)
                    if original:
                        case["original"].reverse()
                    if export:
                        case["export"].reverse()
                    checker.check_case(case, checker.geometry)

    def test_tie_uses_declared_id_policy(self):
        source = next(c for c in self.geometry_cases
                      if c["id"] == "equal_distance_id_tie")
        self.assertEqual(source["expected"]["pairs"],
                         [["o/a", "e/a"], ["o/b", "e/b"]])
        checker.check_case(source, checker.geometry)

    def fill_transform(self, transform):
        for source in self.fill_cases:
            with self.subTest(case=source["id"]):
                checker.check_case(source, checker.fill_coverage)
                case = copy.deepcopy(source)
                case["module"]["rect"] = map_rectangle(case["module"]["rect"], transform)
                for fill in case["fills"]:
                    fill["outer"] = map_rectangle(fill["outer"], transform)
                    fill["holes"] = [map_rectangle(hole, transform)
                                     for hole in fill["holes"]]
                # Rigid transforms preserve union, hole and per-ID areas.
                checker.check_case(case, checker.fill_coverage)

    def test_fill_translation(self):
        self.fill_transform(translate)

    def test_fill_rotation_90_degrees(self):
        self.fill_transform(quarter_turn)

    def test_fill_input_order(self):
        for source in self.fill_cases:
            for fills in itertools.permutations(source["fills"]):
                with self.subTest(case=source["id"], ids=[f["id"] for f in fills]):
                    case = copy.deepcopy(source)
                    case["fills"] = copy.deepcopy(list(fills))
                    for fill in case["fills"]:
                        fill["holes"].reverse()
                    checker.check_case(case, checker.fill_coverage)

    def tag_transform(self, transform, angle):
        for source in self.tag_cases:
            with self.subTest(case=source["id"]):
                checker.check_case(source, checker.tag_overlaps)
                case = copy.deepcopy(source)
                for obj in case["objects"]:
                    obj["center"] = transform(obj["center"])
                    obj["rotation_deg"] += angle
                checker.check_case(case, checker.tag_overlaps)

    def test_tag_translation(self):
        self.tag_transform(translate, 0)

    def test_tag_rotation_90_degrees(self):
        self.tag_transform(quarter_turn, 90)

    def test_tag_input_order(self):
        for source in self.tag_cases:
            for objects in itertools.permutations(source["objects"]):
                with self.subTest(case=source["id"], ids=[o["id"] for o in objects]):
                    case = copy.deepcopy(source)
                    case["objects"] = copy.deepcopy(list(objects))
                    checker.check_case(case, checker.tag_overlaps)

    def test_changed_world_point_is_detected(self):
        case = copy.deepcopy(self.geometry_cases[0])
        checker.check_case(case, checker.geometry)
        case["export"][0]["point"][0] += 20
        with self.assertRaises(AssertionError):
            checker.check_case(case, checker.geometry)

    def test_changed_fill_area_is_detected(self):
        case = copy.deepcopy(next(c for c in self.fill_cases
                                  if c["id"] == "exact_five_percent"))
        checker.check_case(case, checker.fill_coverage)
        case["fills"][0]["outer"][2] = 2
        with self.assertRaises(AssertionError):
            checker.check_case(case, checker.fill_coverage)

    def test_changed_tag_box_is_detected(self):
        case = copy.deepcopy(next(c for c in self.tag_cases
                                  if c["id"] == "multiline_height_independent"))
        checker.check_case(case, checker.tag_overlaps)
        case["objects"][1]["center"] = [100, 100]
        with self.assertRaises(AssertionError):
            checker.check_case(case, checker.tag_overlaps)


if __name__ == "__main__":
    unittest.main()
