"""Bounded synthetic offline INSERT traversal measurements, with no timing gates.

All graphs are acyclic ordinary INSERTs built in memory. Results describe this
checker only; they are neither live grades nor speedup claims.
"""

import argparse
import contextlib
import io
import json
import sys
import time
import tracemalloc
import unittest
from unittest.mock import patch

try:
    from . import check_plot_layers as layers
except ImportError:
    import check_plot_layers as layers


SCHEMA = "leaf.xbench.x01.layer-expansion-benchmark.v1"
LIMIT_NAMES = ("MAX_BYTES", "MAX_PAIRS", "MAX_RECORDS", "MAX_OCCURRENCES",
               "MAX_DEPTH", "MAX_ARRAY_CELLS")
MAX_WIDTH = 64
MAX_LEAVES = 16
MAX_BENCH_DEPTH = 48
MAX_REPEATS = 3
MAX_SIZES = 3


def wide_graph(width, leaves):
    """One root, width INSERT children, and leaves leaves per child."""
    blocks = [("LEAF", [layers.entity(f"L{i}", "0") for i in range(leaves)]),
              ("WIDE", [layers.entity(f"I{i}", "0", "INSERT", [(2, "LEAF")])
                        for i in range(width)])]
    return layers.fixture(entities=[layers.entity("ROOT", "MODULES", "INSERT",
                                                 [(2, "WIDE")])], blocks=blocks)


def deep_graph(depth):
    """A single chain: depth INSERT ancestors and exactly one leaf."""
    blocks = []
    for index in range(depth):
        child = (layers.entity("LEAF", "0") if index == depth - 1 else
                 layers.entity(f"I{index + 1}", "0", "INSERT", [(2, f"B{index + 1}")]))
        blocks.append((f"B{index}", [child]))
    return layers.fixture(entities=[layers.entity("ROOT", "MODULES", "INSERT",
                                                 [(2, "B0")])], blocks=blocks)


def bounded_integer(maximum):
    def convert(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("expected an integer") from None
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f"expected 1..{maximum}")
        return number
    return convert


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--widths", nargs="+", type=bounded_integer(MAX_WIDTH), default=[2, 8, 24])
    parser.add_argument("--depths", nargs="+", type=bounded_integer(
        min(MAX_BENCH_DEPTH, layers.MAX_DEPTH - 1)), default=[2, 8, 24])
    parser.add_argument("--leaves", type=bounded_integer(MAX_LEAVES), default=4)
    parser.add_argument("--repeats", type=bounded_integer(MAX_REPEATS), default=1)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if len(args.widths) > MAX_SIZES or len(args.depths) > MAX_SIZES:
        parser.error(f"at most {MAX_SIZES} widths and depths")
    if any(depth >= layers.MAX_DEPTH for depth in args.depths):
        parser.error("depth must be below checker MAX_DEPTH")
    return args


def measure(shape, size, leaves, repetition):
    # Construction is excluded from traversal time and peak allocation metrics.
    data = wide_graph(size, leaves) if shape == "wide" else deep_graph(size)
    expected = size * leaves if shape == "wide" else 1
    records = layers.records(data)
    graph = dict(source_bytes=len(data), records=len(records),
                 blocks=sum(kind == "BLOCK" for kind, _ in records),
                 inserts=sum(kind == "INSERT" for kind, _ in records),
                 leaves=sum(kind == "LINE" for kind, _ in records))
    tracemalloc.start()
    try:
        start = time.perf_counter()
        result = layers.check_bytes(data)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return dict(shape=shape, size=size, repetition=repetition, graph_size=graph,
                expected_leaf_count=expected,
                inspected_count=result["inspected_occurrences"], verdict=result["verdict"],
                diagnostics=result["diagnostics"], elapsed_seconds=elapsed, peak_bytes=peak,
                checker_limits={name: getattr(layers, name) for name in LIMIT_NAMES})


class SelfTests(unittest.TestCase):
    def test_independent_formulas(self):
        for width, leaves in ((1, 1), (2, 3), (MAX_WIDTH, MAX_LEAVES)):
            result = layers.check_bytes(wide_graph(width, leaves))
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["inspected_occurrences"], width * leaves)
        for depth in (1, 3, min(MAX_BENCH_DEPTH, layers.MAX_DEPTH - 1)):
            result = layers.check_bytes(deep_graph(depth))
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["inspected_occurrences"], 1)

    def test_cli_bounds(self):
        args = arguments([])
        self.assertLessEqual((len(args.widths) + len(args.depths)) * args.repeats, 9)
        for argv in (["--widths", "0"], ["--widths", str(MAX_WIDTH + 1)],
                     ["--depths", str(layers.MAX_DEPTH)], ["--depths", "-1"],
                     ["--leaves", str(MAX_LEAVES + 1)], ["--repeats", "0"],
                     ["--repeats", str(MAX_REPEATS + 1)], ["--widths", "bad"],
                     ["--widths", "1", "2", "3", "4"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    arguments(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_named_limit_control(self):
        with patch.object(layers, "MAX_OCCURRENCES", 2):
            result = layers.check_bytes(wide_graph(2, 1))
        self.assertEqual(result["verdict"], "UNREADABLE")
        self.assertEqual(result["inspected_occurrences"], 0)
        self.assertEqual(result["diagnostics"][0]["reason"], "occurrence_limit")


def main(argv=None):
    args = arguments(argv)
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    rows = [measure(shape, size, args.leaves, repetition)
            for shape, sizes in (("wide", args.widths), ("deep", args.depths))
            for size in sizes for repetition in range(args.repeats)]
    print(json.dumps(dict(schema=SCHEMA, evidence_kind="synthetic_offline",
                          synthetic_offline=True, live_grade=False, rows=rows),
                     sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
