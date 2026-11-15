"""Bounded synthetic arithmetic measurements; no live performance or grade claim."""

import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time
import tracemalloc
import unittest


SCHEMA = "leaf.xbench.x01.offline-benchmark.v1"
MAX_SIZE = 64
MAX_FILL_GRID_CELLS = 258
MAX_TAG_PAIRS = 2016
DEFAULT_SIZES = (4, 8, 16)

# Resolve the sibling by this file's location, including when invoked elsewhere.
_spec = importlib.util.spec_from_file_location(
    "x01_benchmark_cases", Path(__file__).resolve().with_name("check_cases.py"))
cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cases)


def validate_sizes(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("sizes must be a nonempty sequence of increasing integers")
    if any(type(value) is not int or not 1 <= value <= MAX_SIZE for value in values):
        raise ValueError(f"sizes must be positive integers in 1..{MAX_SIZE}; bool is invalid")
    if any(a >= b for a, b in zip(values, values[1:])):
        raise ValueError("sizes must be strictly increasing, without duplicates")
    return tuple(values)


def parse_sizes(text):
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", text):
        raise ValueError("sizes require comma-separated positive integers (no empty entries)")
    return validate_sizes([int(part) for part in text.split(",")])


def validate_repeats(value):
    if type(value) is not int or not 1 <= value <= 3:
        raise ValueError("repeats must be an integer in 1..3; bool is invalid")
    return value


def build_geometry(size):
    validate_sizes([size])
    original = [dict(id=f"o/{i:03d}", handle=f"H{i:03d}", point=[3*i, 0])
                for i in range(size)]
    exported = [dict(id=f"e/{i:03d}", handle=f"H{i:03d}", point=[3*i, 0])
                for i in range(size)]
    expected = dict(
        world={obj["id"]: obj["point"] for obj in original + exported},
        pairs=[[a["id"], b["id"]] for a, b in zip(original, exported)],
        counts=dict(original=size, export=size, matched=size, moved=0, missing=0, extra=0),
        diagnostics=[], verdict="PASS")
    return dict(original=original, export=exported), expected, dict(known_output_count=size)


def build_fill(size):
    validate_sizes([size])
    # Identical 1x1 shapes separated along x; only three y boundaries.
    module = dict(id="m/000", rect=[0, 0, 3*size, 10])
    fills = [dict(id=f"f/{i:03d}", outer=[3*i, 0, 3*i+1, 1], holes=[])
             for i in range(size)]
    rects = [module["rect"]] + [fill["outer"] for fill in fills]
    nx = len({r[j] for r in rects for j in (0, 2)}) - 1
    ny = len({r[j] for r in rects for j in (1, 3)}) - 1
    cases.require(nx * ny <= MAX_FILL_GRID_CELLS, "fill coordinate grid exceeds cap")
    expected = dict(module_area=30*size, fill_union_area_inside_module=size,
                    intersection_area=size, fraction=1/30, threshold=0.05,
                    per_fill_intersection_areas={f["id"]: 1 for f in fills},
                    diagnostics=[], verdict="PASS")
    return dict(module=module, fills=fills), expected, dict(
        known_output_count=size, known_union_area=size, grid_cells=nx*ny)


def build_tags(size):
    validate_sizes([size])
    count = size * (size - 1) // 2
    cases.require(count <= MAX_TAG_PAIRS, "tag pair count exceeds cap")
    objects = [dict(id=f"t/{i:03d}", kind="tag", center=[3*i, 0], width=1,
                    height=1, rotation_deg=0, line_count=1, paper_text_height_mm=2)
               for i in range(size)]
    expected = dict(pairs=[dict(object_ids=[a["id"], b["id"]], intersection_area=0,
                                smaller_box_fraction=0)
                           for i, a in enumerate(objects) for b in objects[i+1:]],
                    diagnostics=[], verdict="PASS")
    return dict(objects=objects), expected, dict(known_output_count=count,
                                               known_overlap_area=0)


FAMILIES = (("geometry", build_geometry, cases.geometry),
            ("fill_coverage", build_fill, cases.fill_coverage),
            ("tag_overlap", build_tags, cases.tag_overlaps))


def benchmark(sizes=DEFAULT_SIZES, repeats=1):
    sizes = validate_sizes(sizes)
    repeats = validate_repeats(repeats)
    records = []
    for size in sizes:
        for family, builder, calculate in FAMILIES:
            workload, expected, known = builder(size)
            # Full analytic comparison is outside the timed/traced interval.
            cases.compare(calculate(workload), expected, f"{family}/{size}")
            for repeat in range(1, repeats + 1):
                tracemalloc.start()
                try:
                    started = time.perf_counter()
                    actual = calculate(workload)
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                cases.compare(actual, expected, f"{family}/{size}/{repeat}")
                cases.require(math.isfinite(elapsed) and elapsed >= 0,
                              "nonfinite or negative elapsed time")
                records.append(dict(family=family, size=size, repeat=repeat,
                                    elapsed_seconds=round(elapsed, 9),
                                    peak_traced_bytes=peak, **known))
                del actual
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline", sizes=list(sizes),
                repeats=repeats, limits=dict(max_size=MAX_SIZE, max_repeats=3,
                max_fill_grid_cells=MAX_FILL_GRID_CELLS, max_tag_pairs=MAX_TAG_PAIRS),
                records=records)


class SelfTests(unittest.TestCase):
    def test_size_validation(self):
        self.assertEqual(parse_sizes("4,8,16"), DEFAULT_SIZES)
        self.assertEqual(parse_sizes("1,64"), (1, 64))
        for text in ("", "1,", ",1", "1,,2", "0", "65", "2,2", "2,1",
                     "True", "nan", "inf", "1.0", "-1"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_sizes(text)
        for values in ([], [True], [False], [float("inf")], [float("nan")], [1.0]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_sizes(values)

    def test_repeat_validation(self):
        for value in (1, 2, 3):
            self.assertEqual(validate_repeats(value), value)
        for value in (0, 4, True, False, 1.0, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_repeats(value)

    def test_deterministic_builders_and_bounds(self):
        for family, builder, calculate in FAMILIES:
            self.assertEqual(builder(4), builder(4))
            workload, expected, known = builder(2)
            cases.compare(calculate(workload), expected, family)
            _, _, largest = builder(MAX_SIZE)
            if family == "fill_coverage":
                self.assertLessEqual(largest["grid_cells"], MAX_FILL_GRID_CELLS)
            if family == "tag_overlap":
                self.assertEqual(largest["known_output_count"], MAX_TAG_PAIRS)

    def test_json_shape_and_tiny_measurement(self):
        result = json.loads(json.dumps(benchmark((1, 2), 1), allow_nan=False))
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertEqual(result["sizes"], [1, 2])
        self.assertEqual(result["repeats"], 1)
        self.assertEqual(len(result["records"]), 6)
        for record, (size, family) in zip(result["records"],
                ((s, f[0]) for s in (1, 2) for f in FAMILIES)):
            self.assertEqual((record["size"], record["family"], record["repeat"]),
                             (size, family, 1))
            self.assertTrue(math.isfinite(record["elapsed_seconds"]))
            self.assertGreaterEqual(record["elapsed_seconds"], 0)
            self.assertIs(type(record["peak_traced_bytes"]), int)
            self.assertGreaterEqual(record["peak_traced_bytes"], 0)
            self.assertIn("known_output_count", record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="4,8,16", help="increasing integers, each 1..64")
    parser.add_argument("--repeats", type=int, default=1, help="1..3 (default 1)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        sizes = parse_sizes(args.sizes)
        repeats = validate_repeats(args.repeats)
    except ValueError as error:
        parser.error(str(error))
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    print(json.dumps(benchmark(sizes, repeats), sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
