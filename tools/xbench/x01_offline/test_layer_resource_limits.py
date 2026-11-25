"""Small synthetic parser/expansion boundary checks, entirely in memory."""

import math
import unittest
from unittest.mock import patch

try:
    from . import check_plot_layers as layers
except ImportError:  # Direct execution and unittest discovery from this directory.
    import check_plot_layers as layers


LIMITS = ("MAX_BYTES", "MAX_PAIRS", "MAX_RECORDS", "MAX_DEPTH", "MAX_OCCURRENCES")


def chain(depth):
    """A depth of N has N INSERT ancestors and one classified leaf."""
    if depth == 0:
        return layers.fixture(entities=[layers.entity("A")])
    blocks = []
    for index in range(depth):
        child = (layers.entity("A", "0") if index == depth - 1 else
                 layers.entity(f"I{index + 1}", "0", "INSERT", [(2, f"B{index + 1}")]))
        blocks.append((f"B{index}", [child]))
    return layers.fixture(entities=[layers.entity("R", "MODULES", "INSERT", [(2, "B0")])],
                          blocks=blocks)


def repeated_dag(levels):
    """Linear source, exponential occurrences; no block references itself."""
    blocks = [("B0", [layers.entity("A", "0")])]
    for level in range(1, levels + 1):
        blocks.append((f"B{level}", [
            layers.entity(f"L{level}", "0", "INSERT", [(2, f"B{level - 1}")]),
            layers.entity(f"R{level}", "0", "INSERT", [(2, f"B{level - 1}")])]))
    return layers.fixture(entities=[layers.entity("ROOT", "MODULES", "INSERT",
                                                [(2, f"B{levels}")])], blocks=blocks)


class LayerResourceLimits(unittest.TestCase):
    def setUp(self):
        self.original_limits = {name: getattr(layers, name) for name in LIMITS}

    def tearDown(self):
        self.assertEqual({name: getattr(layers, name) for name in LIMITS},
                         self.original_limits)

    def assert_accepted(self, data, inspected=1):
        result = layers.check_bytes(data)
        self.assertEqual(result, dict(schema=layers.SCHEMA, verdict="PASS",
                                     inspected_occurrences=inspected, diagnostics=[]))

    def assert_unreadable(self, data, reason, path=(), inspected=0, **context):
        result = layers.check_bytes(data)
        self.assertEqual(result, dict(schema=layers.SCHEMA, verdict="UNREADABLE",
                                     inspected_occurrences=inspected,
                                     diagnostics=[layers.diagnostic(reason, path=path, **context)]))

    def test_real_constants_are_finite_positive_integers(self):
        for name in LIMITS:
            with self.subTest(limit=name):
                value = getattr(layers, name)
                self.assertIs(type(value), int)
                self.assertTrue(math.isfinite(value))
                self.assertGreater(value, 0)

    def parser_boundary(self, name, usage, reason):
        data = layers.fixture(entities=[layers.entity("A")])
        for offset in (1, 0, -1):
            with self.subTest(limit=name, source_relative_to_limit=-offset):
                with patch.object(layers, name, usage + offset):
                    if offset >= 0:
                        self.assert_accepted(data)
                    else:
                        # These guards run before an offending entity is read;
                        # no entity handle or occurrence path is available.
                        self.assert_unreadable(data, reason)

    def test_byte_boundary(self):
        data = layers.fixture(entities=[layers.entity("A")])
        self.parser_boundary("MAX_BYTES", len(data), "byte_limit")

    def test_pair_boundary(self):
        data = layers.fixture(entities=[layers.entity("A")])
        self.parser_boundary("MAX_PAIRS", len(data.splitlines()) // 2, "pair_limit")

    def test_record_boundary(self):
        data = layers.fixture(entities=[layers.entity("A")])
        record_count = sum(code == b"0" for code in data.splitlines()[::2])
        self.parser_boundary("MAX_RECORDS", record_count, "record_limit")

    def test_depth_boundary(self):
        with patch.object(layers, "MAX_DEPTH", 3):
            for depth in (2, 3):
                with self.subTest(depth=depth):
                    self.assert_accepted(chain(depth))
            # Depth is checked on entry to the block, before its leaf handle.
            self.assert_unreadable(chain(4), "depth_limit", ["R", "I1", "I2", "I3"])

    def test_occurrence_boundary(self):
        with patch.object(layers, "MAX_OCCURRENCES", 4):
            for count in (3, 4, 5):
                data = layers.fixture(entities=[layers.entity(f"A{i}") for i in range(count)])
                with self.subTest(count=count):
                    if count <= 4:
                        self.assert_accepted(data, count)
                    else:
                        # The cap precedes reading A4, so only the parent path
                        # (empty in modelspace) is available, not its handle.
                        self.assert_unreadable(data, "occurrence_limit", inspected=4)

    def test_repeated_acyclic_dag_boundary_counts_inserts(self):
        data = repeated_dag(3)
        # ROOT + 2 + 4 + 8 INSERTs + 8 classified leaves = 23 visits.
        for limit in (24, 23, 22):
            with self.subTest(limit=limit):
                with patch.object(layers, "MAX_OCCURRENCES", limit):
                    if limit >= 23:
                        self.assert_accepted(data, 8)
                    else:
                        self.assert_unreadable(data, "occurrence_limit",
                                               ["ROOT", "R3", "R2", "R1"], inspected=7)

    def test_large_repeated_dag_stops_at_occurrence_cap(self):
        small, large = repeated_dag(10), repeated_dag(20)
        count_records = lambda data: sum(code == b"0" for code in data.splitlines()[::2])
        # Each extra level adds exactly one BLOCK, two INSERTs and ENDBLK.
        self.assertEqual(count_records(large) - count_records(small), 40)
        self.assertLess(len(large), 4096)
        with patch.object(layers, "MAX_OCCURRENCES", 22):
            # ROOT, 20 nested INSERTs, then the first classified leaf;
            # the next branch is refused before reading its handle.
            self.assert_unreadable(large, "occurrence_limit",
                                   ["ROOT"] + [f"L{i}" for i in range(20, 1, -1)],
                                   inspected=1)

    def test_numeric_refusal_remains_distinct_from_limits(self):
        data = layers.fixture(entities=[layers.entity("BAD", extra=[(10, "nan")])])
        lines = data.splitlines()
        value_line = next(i + 2 for i in range(0, len(lines), 2)
                          if lines[i:i + 2] == [b"10", b"nan"])
        # Exact parser boundaries must admit parsing far enough to diagnose the
        # malformed numeric value and retain its already-read handle/layer.
        with patch.multiple(layers, MAX_BYTES=len(data), MAX_PAIRS=len(lines) // 2,
                            MAX_RECORDS=sum(code == b"0" for code in lines[::2]),
                            MAX_DEPTH=1, MAX_OCCURRENCES=1):
            self.assert_unreadable(data, "malformed_numeric", handle="BAD",
                                   layer="MODULES", group=10, line=value_line)
        with patch.object(layers, "MAX_BYTES", len(data) - 1):
            self.assert_unreadable(data, "byte_limit")


if __name__ == "__main__":
    unittest.main()
