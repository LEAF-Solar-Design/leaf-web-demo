"""Bounded synthetic offline proximity matching measurements; no timing gates."""

import argparse
import contextlib
import copy
from fractions import Fraction
import io
import itertools
import json
import math
import re
import sys
import time
import tracemalloc
import unittest
from unittest.mock import patch

try:
    from . import check_cases as checker
except ImportError:
    import check_cases as checker


SCHEMA = "leaf.xbench.x01.geometry-matching-benchmark.v1"
MAX_SIZE = 32
MAX_SIZES = 3
FAMILIES = ("sparse", "dense", "isolated", "constrained")


class InvalidSize(ValueError):
    pass


class SizeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise treats negative nonfinite tokens as options and
        # exits 2 before they reach the named, machine-readable size rejection.
        raise InvalidSize(message)


def validate_sizes(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_SIZES:
        raise InvalidSize("expected one to three sizes")
    sizes = []
    for value in values:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,2}", value):
            value = int(value)
        if type(value) is not int or not 1 <= value <= MAX_SIZE:
            raise InvalidSize("expected integer sizes in 1..32")
        if (2 * value > checker.MAX_OBJECTS_PER_CASE or
                value * value > checker.MAX_CANDIDATE_PAIRS):
            raise InvalidSize("size exceeds current checker work caps")
        sizes.append(value)
    if len(set(sizes)) != len(sizes):
        raise InvalidSize("duplicate sizes")
    return sizes


def expected_count(family, size):
    if family not in FAMILIES:
        raise ValueError("unknown topology")
    # Disjoint complete components, complete K(n,n), isolated edges, or
    # K(n,n-1) plus an isolated export: independent cardinality formulas.
    return size - 1 if family == "constrained" else size


def fixture(family, size):
    validate_sizes([size])
    count = expected_count(family, size)
    original, exported = [], []
    for index in range(size):
        base = (2 * (index // 2) if family == "sparse" else
                2 * index if family == "isolated" else 0)
        x = base + (index % 2) / 16
        y = base + (index % 2) / 16 + 1 / 32
        if family == "constrained" and index == size - 1:
            y = 100
        original.append(dict(id=f"o{index}", handle=f"original-{index}", point=[x, 0]))
        exported.append(dict(id=f"e{index}", handle=f"export-{index}", point=[y, 0]))
    # These expected literals follow the topology, never a checker invocation.
    expected = dict(world={obj["id"]: obj["point"][:] for obj in original + exported},
                    pairs=[[f"o{i}", f"e{i}"] for i in range(count)],
                    counts=dict(original=size, export=size, matched=count, moved=0,
                                missing=size - count, extra=size - count),
                    diagnostics=[], verdict="PASS" if count == size else "FAIL")
    if count != size:
        expected["diagnostics"] = [dict(name="MISSING", object_ids=[f"o{size - 1}"]),
                                   dict(name="EXTRA", object_ids=[f"e{size - 1}"])]
    return dict(id=f"benchmark-{family}-{size}", original=original, export=exported,
                expected=expected)


def exhaustive_count(case):
    """Tiny injective-assignment oracle, without production matching code."""
    left, right = case["original"], case["export"]
    if max(len(left), len(right)) > 6:
        raise ValueError("exhaustive oracle is capped at six per side")
    limit = Fraction(0.5 + checker.EPS) ** 2
    edges = {(a["id"], b["id"]) for a in left for b in right
             if sum((Fraction(x) - Fraction(y)) ** 2
                    for x, y in zip(a["point"], b["point"])) <= limit}
    for count in range(min(len(left), len(right)), -1, -1):
        for subset in itertools.combinations(left, count):
            for assignment in itertools.permutations(right, count):
                if all((a["id"], b["id"]) in edges for a, b in zip(subset, assignment)):
                    return count


def observed_candidates(case, result):
    """Eligible proximity edges in the actual checker's returned world points."""
    world = result["world"]
    return sum(math.hypot(world[a["id"]][0] - world[b["id"]][0],
                          world[a["id"]][1] - world[b["id"]][1]) <= 0.5 + checker.EPS
               for a in case["original"] for b in case["export"])


def count_agrees(result, expected):
    return result["counts"]["matched"] == len(result["pairs"]) == expected


def measure(family, size):
    case = fixture(family, size)
    checker.validate_case(case, checker.geometry)
    tracemalloc.start()
    try:
        start = time.perf_counter()
        result = checker.geometry(case)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    expected = expected_count(family, size)
    return dict(size=size, family=family, candidate_work_budget=size * size,
                observed_candidate_count=observed_candidates(case, result),
                expected_matched=expected, actual_matched=result["counts"]["matched"],
                count_agrees=count_agrees(result, expected),
                runtime_seconds=elapsed, peak_bytes=peak,
                checker_limits=dict(MAX_OBJECTS_PER_CASE=checker.MAX_OBJECTS_PER_CASE,
                                    MAX_CANDIDATE_PAIRS=checker.MAX_CANDIDATE_PAIRS))


def report(rows):
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline",
                synthetic_offline=True, live_grade=False, rows=rows)


class SelfTests(unittest.TestCase):
    def test_tiny_topologies_and_fixture_validity(self):
        for family in FAMILIES:
            for size in (1, 2, 4, 6):
                with self.subTest(family=family, size=size):
                    case = fixture(family, size)
                    checker.validate_case(case, checker.geometry)
                    self.assertFalse({o["handle"] for o in case["original"]} &
                                     {o["handle"] for o in case["export"]})
                    expected = exhaustive_count(case)
                    self.assertEqual(expected, expected_count(family, size))
                    result = checker.geometry(case)
                    self.assertTrue(count_agrees(result, expected))
                    corrupted = copy.deepcopy(result)
                    corrupted["counts"]["matched"] += 1
                    self.assertFalse(count_agrees(corrupted, expected))
                    corrupted["pairs"].append(["invented-o", "invented-e"])
                    self.assertFalse(count_agrees(corrupted, expected))
        with self.assertRaises(ValueError):
            exhaustive_count(fixture("dense", 7))

    def test_size_limits(self):
        self.assertEqual(validate_sizes([4, 8, 16]), [4, 8, 16])
        self.assertEqual(validate_sizes([32]), [32])
        for values in ([], [True], [False], [0], [-1], [33], [1.0], [float("nan")],
                       [float("inf")], ["nan"], ["True"], ["1.5"], ["9" * 1000],
                       [1, 1], [1, 2, 3, 4]):
            with self.subTest(values=values), self.assertRaises(InvalidSize):
                validate_sizes(values)
        for name, limit in (("MAX_OBJECTS_PER_CASE", 7), ("MAX_CANDIDATE_PAIRS", 15)):
            with patch.object(checker, name, limit), self.assertRaises(InvalidSize):
                validate_sizes([4])

    def test_measurement_output_truth_and_corruption(self):
        for family, edges in (("sparse", 8), ("dense", 16),
                              ("isolated", 4), ("constrained", 12)):
            row = measure(family, 4)
            self.assertEqual(row["observed_candidate_count"], edges)
            self.assertEqual(row["candidate_work_budget"], 16)
            self.assertEqual(row["actual_matched"], expected_count(family, 4))
            self.assertTrue(row["count_agrees"])
            self.assertGreaterEqual(row["runtime_seconds"], 0)
            self.assertGreaterEqual(row["peak_bytes"], 0)
        real_geometry = checker.geometry

        def corrupt(case):
            result = real_geometry(case)
            result["counts"]["matched"] = 0
            return result

        with patch.object(checker, "geometry", side_effect=corrupt):
            # validate_case dispatches by function identity, so validate the
            # fixture separately and bypass only that schema dispatch here.
            with patch.object(checker, "validate_case"):
                row = measure("dense", 2)
        self.assertEqual(row["actual_matched"], 0)
        self.assertFalse(row["count_agrees"])
        output = report([row])
        self.assertFalse(output["live_grade"])
        self.assertTrue(output["synthetic_offline"])
        self.assertEqual(json.loads(json.dumps(output, allow_nan=False)), output)

    def test_invalid_cli_is_named_unreadable(self):
        for argv in (["--sizes", "nan"], ["--sizes", "-1"], ["--sizes", "33"],
                     ["--sizes", "-inf"], ["--sizes", "True"], ["--sizes"],
                     ["--sizes", "4", "4"], ["--sizes", "1", "2", "3", "4"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(argv), 4)
            result = json.loads(output.getvalue())
            self.assertEqual(result["verdict"], "UNREADABLE")
            self.assertEqual(result["diagnostics"][0]["name"], "INVALID_SIZE")

    def test_cli_output_envelope(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--sizes", "1"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertIs(result["live_grade"], False)
        self.assertIs(result["synthetic_offline"], True)
        self.assertEqual([row["family"] for row in result["rows"]], list(FAMILIES))
        self.assertTrue(all(row["count_agrees"] for row in result["rows"]))


def main(argv=None):
    parser = SizeParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", default=[4, 8, 16])
    parser.add_argument("--self-test", action="store_true")
    try:
        args = parser.parse_args(argv)
        sizes = validate_sizes(args.sizes)
    except InvalidSize as error:
        result = report([])
        result.update(verdict="UNREADABLE", diagnostics=[dict(name="INVALID_SIZE", reason=str(error))])
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 4
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    rows = [measure(family, size) for size in sizes for family in FAMILIES]
    print(json.dumps(report(rows), sort_keys=True, allow_nan=False))
    return 0 if all(row["count_agrees"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
