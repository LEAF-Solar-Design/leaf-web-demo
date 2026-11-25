"""Synthetic scratch DXF array controls; all execution has a parent timeout."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from . import check_plot_layers as layers
except ImportError:
    import check_plot_layers as layers


TIMEOUT_SECONDS = 20


def insert(handle="I", block="B", layer="MODULES", columns=None, rows=None, extra=()):
    fields = [(2, block)]
    if columns is not None:
        fields.append((70, columns))
    if rows is not None:
        fields.append((71, rows))
    return layers.entity(handle, layer, "INSERT", fields + list(extra))


def array_data(columns=3, rows=2, children=None, **kwargs):
    return layers.fixture(entities=[insert(columns=columns, rows=rows, **kwargs)],
                          blocks=[("B", [layers.entity("A", "0")]
                                   if children is None else children)])


def token(handle, row, column):
    return f"{handle}[row={row},col={column}]"


class ArrayControls(unittest.TestCase):
    def check_scratch(self, data):
        # Both APIs must leave the exact source bytes intact, including spaces.
        with tempfile.TemporaryDirectory(prefix="array scratch ", dir=SCRATCH) as directory:
            path = Path(directory) / "synthetic array.dxf"
            path.write_bytes(data)
            result = layers.check_file(path)
            self.assertEqual(result, layers.check_bytes(data))
            self.assertEqual(path.read_bytes(), data)
        return result

    def test_ordinary_exact_legacy_result(self):
        for counts in ((None, None), (1, 1), (1, None), (None, 1)):
            self.assertEqual(self.check_scratch(array_data(*counts)), dict(
                schema=layers.SCHEMA, verdict="PASS", inspected_occurrences=1, diagnostics=[]))
            data = array_data(*counts, extra=[(60, 1)])
            expected = dict(schema=layers.SCHEMA, verdict="FAIL", inspected_occurrences=1,
                            diagnostics=[layers.diagnostic("entity_hidden", "A", ["I", "A"],
                                                           "MODULES", "module")])
            self.assertEqual(self.check_scratch(data), expected)
            self.assertEqual(json.dumps(self.check_scratch(data), sort_keys=True),
                             json.dumps(expected, sort_keys=True))

    def test_two_by_three_row_major(self):
        result = self.check_scratch(array_data(extra=[(60, 1), (44, 12.5), (45, -3)]))
        self.assertEqual(result, dict(schema=layers.SCHEMA, verdict="FAIL",
            inspected_occurrences=6, diagnostics=[
                layers.diagnostic("entity_hidden", "A", [token("I", row, col), "A"],
                                  "MODULES", "module")
                for row in range(2) for col in range(3)]))

    def test_nested_arrays_ordinary_and_depth(self):
        data = layers.fixture(entities=[insert(columns=2, rows=2, extra=[(60, 1)])], blocks=[
            ("B", [insert("N", "C", "0", columns=2)]),
            ("C", [insert("O", "D", "0")]), ("D", [layers.entity("A", "0")])])
        with patch.object(layers, "MAX_DEPTH", 3):
            result = self.check_scratch(data)
        self.assertEqual(result["inspected_occurrences"], 8)
        self.assertEqual([d["occurrence_path"] for d in result["diagnostics"]], [
            [token("I", row, col), token("N", 0, inner), "O", "A"]
            for row in range(2) for col in range(2) for inner in range(2)])
        self.assertTrue(all(d["handle"] == "A" for d in result["diagnostics"]))
        with patch.object(layers, "MAX_DEPTH", 2):
            result = self.check_scratch(data)
        self.assertEqual(result["diagnostics"][-1]["reason"], "depth_limit")
        self.assertEqual(result["diagnostics"][-1]["occurrence_path"],
                         [token("I", 0, 0), token("N", 0, 0), "O"])

    def test_inheritance_explicit_layers_and_reason_deduplication(self):
        children = [layers.entity("M", "MODULES", extra=[(60, 1)]),
                    layers.entity("T", "TAGS", "MTEXT"), layers.entity("Z", "0")]
        data = layers.fixture(layers=[("BAD", [(62, -7), (70, 1), (290, 0)])],
            entities=[insert(layer="BAD", columns=2, extra=[(60, 1)])], blocks=[
                ("B", [insert("N", "C", "0", rows=2, extra=[(60, 1)])]), ("C", children)])
        result = layers.check_bytes(data, ["MODULES", "BAD"])
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["inspected_occurrences"], 12)
        expected = []
        for col in range(2):
            for row in range(2):
                for handle, layer, role in (("M", "MODULES", "module"), ("T", "TAGS", "tag"),
                                            ("Z", "BAD", "module")):
                    for reason in ("layer_off", "layer_frozen", "layer_nonplot", "entity_hidden"):
                        expected.append(layers.diagnostic(reason, handle,
                            [token("I", 0, col), token("N", row, 0), handle], layer, role))
        self.assertEqual(result["diagnostics"], expected)
        # Layer zero TEXT inherits the array's tag layer too.
        result = self.check_scratch(array_data(layer="TAGS", children=[layers.entity("T", "0", "TEXT")]))
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["inspected_occurrences"], 6)

    def test_missing_cyclic_and_leaf_context(self):
        for name, blocks, reason, handle, path in (
                ("ABSENT", [], "missing_block", "I", [token("I", 0, 0)]),
                ("B", [("B", [insert("N", "B", "0", rows=2)])], "cyclic_block", "N",
                 [token("I", 0, 0), token("N", 0, 0)])):
            result = self.check_scratch(layers.fixture(
                entities=[insert(block=name, columns=2)], blocks=blocks))
            self.assertEqual(result["verdict"], "UNREADABLE")
            self.assertEqual(result["diagnostics"], [layers.diagnostic(
                reason, handle, path, "MODULES", block=name.casefold())])
        result = self.check_scratch(array_data(children=[layers.entity("A", "UNKNOWN")]))
        self.assertEqual(result["diagnostics"], [layers.diagnostic(
            "unknown_layer", "A", [token("I", 0, 0), "A"], "UNKNOWN")])

    def test_paperspace_skips_expansion(self):
        data = layers.fixture(entities=[layers.entity("A"),
            insert(block="ABSENT", columns=0, rows=-1, extra=[(67, 1)])])
        self.assertEqual(self.check_scratch(data), dict(schema=layers.SCHEMA, verdict="PASS",
                                                       inspected_occurrences=1, diagnostics=[]))
        result = self.check_scratch(array_data(children=[layers.entity("P", "UNKNOWN", extra=[(67, 1)]),
                                                         layers.entity("A", "0")]))
        self.assertEqual(result["inspected_occurrences"], 6)
        self.assertEqual(result["verdict"], "PASS")

    def test_invalid_and_oversize_counts(self):
        for code in (70, 71):
            for value in (0, -1, "1.5", "bad", "", "9" * 100):
                data = array_data(columns=None, rows=None, extra=[(code, value)])
                result = self.check_scratch(data)
                reason = ("invalid_array_count" if value in (0, -1) else
                          "array_cell_limit" if value == "9" * 100 else "malformed_numeric")
                self.assertEqual(result["verdict"], "UNREADABLE")
                self.assertEqual(result["inspected_occurrences"], 0)
                self.assertEqual(result["diagnostics"][-1]["reason"], reason)
            result = self.check_scratch(array_data(columns=None, rows=None, extra=[(code, 1), (code, 2)]))
            self.assertEqual(result["diagnostics"][-1]["reason"], "invalid_required_field")

    def test_per_array_below_exact_above(self):
        with patch.object(layers, "MAX_ARRAY_CELLS", 6):
            for columns, rows, expected in ((5, 1, 5), (3, 2, 6), (7, 1, None), (3, 3, None)):
                result = self.check_scratch(array_data(columns, rows))
                self.assertEqual(result["inspected_occurrences"], expected or 0)
                self.assertEqual(result["verdict"], "PASS" if expected else "UNREADABLE")
                if not expected:
                    self.assertEqual(result["diagnostics"][-1]["reason"], "array_cell_limit")
            # Refuse before examining any child, preserving prior leaf evidence.
            data = layers.fixture(entities=[layers.entity("FIRST"), insert(columns=7)],
                                  blocks=[("B", [layers.entity("BAD", kind="UNSUPPORTED")])])
            result = self.check_scratch(data)
            self.assertEqual(result["inspected_occurrences"], 1)
            self.assertEqual(result["diagnostics"][-1]["reason"], "array_cell_limit")

    def test_total_budget_partial_counts(self):
        # Six INSERT cells and six leaves require exactly twelve visits.
        for limit, inspected in ((13, 6), (12, 6), (11, 5), (10, 5)):
            with patch.object(layers, "MAX_OCCURRENCES", limit):
                result = self.check_scratch(array_data())
            self.assertEqual(result["inspected_occurrences"], inspected)
            self.assertEqual(result["verdict"], "PASS" if limit >= 12 else "UNREADABLE")
            if limit == 11:
                self.assertEqual(result["diagnostics"][-1], layers.diagnostic(
                    "occurrence_limit", path=[token("I", 1, 2)]))
            if limit == 10:
                self.assertEqual(result["diagnostics"][-1], layers.diagnostic(
                    "occurrence_limit", "I", [token("I", 1, 2)], "MODULES"))

    def test_empty_and_repeated_acyclic_blocks_consume_budget(self):
        for limit in (6, 7, 8):
            data = layers.fixture(entities=[layers.entity("A"), insert(columns=3, rows=2)],
                                  blocks=[("B", [])])
            with patch.object(layers, "MAX_OCCURRENCES", limit):
                result = self.check_scratch(data)
            self.assertEqual(result["inspected_occurrences"], 1)
            self.assertEqual(result["verdict"], "UNREADABLE" if limit == 6 else "PASS")
            if limit == 6:
                self.assertEqual(result["diagnostics"][-1], layers.diagnostic(
                    "occurrence_limit", "I", [token("I", 1, 2)], "MODULES"))
        data = layers.fixture(entities=[layers.entity("A"), insert(columns=3)], blocks=[
            ("B", [insert("N", "EMPTY", "0", columns=3)]), ("EMPTY", [])])
        # One leaf + 3 outer cells + 9 empty inner cells = 13 visits.
        for limit in (12, 13, 14):
            with patch.object(layers, "MAX_OCCURRENCES", limit):
                result = self.check_scratch(data)
            self.assertEqual(result["inspected_occurrences"], 1)
            self.assertEqual(result["verdict"], "UNREADABLE" if limit == 12 else "PASS")
        # Ordinary repeated empty INSERTs also retain their visit charge.
        data = layers.fixture(entities=[layers.entity("A")] + [insert(str(i)) for i in range(5)],
                              blocks=[("B", [])])
        with patch.object(layers, "MAX_OCCURRENCES", 5):
            result = self.check_scratch(data)
        self.assertEqual(result["diagnostics"][-1]["reason"], "occurrence_limit")

    def test_cli_exit_codes_and_unchanged_scratch(self):
        with tempfile.TemporaryDirectory(prefix="array cli ", dir=SCRATCH) as directory:
            path = Path(directory) / "synthetic.dxf"
            for data, code in ((array_data(), 0), (array_data(extra=[(60, 1)]), 1),
                               (array_data(columns=0), 4)):
                path.write_bytes(data)
                child = subprocess.run([sys.executable, "-B", str(Path(layers.__file__).resolve()), str(path)],
                                       capture_output=True, timeout=5, check=False)
                self.assertEqual(child.returncode, code, child.stderr.decode(errors="replace"))
                self.assertEqual(json.loads(child.stdout), layers.check_bytes(data))
                self.assertEqual(path.read_bytes(), data)


class BoundedArrayTests(unittest.TestCase):
    def test_controls_in_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="x01 array parent ") as directory:
            child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                                    "--array-worker", directory], capture_output=True,
                                   timeout=TIMEOUT_SECONDS, check=False)
        self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))
        print(child.stderr.decode(errors="replace"), end="")


SCRATCH = None

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--array-worker":
        SCRATCH = sys.argv[2]
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ArrayControls)
        sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
    unittest.main(defaultTest="BoundedArrayTests", verbosity=2)
