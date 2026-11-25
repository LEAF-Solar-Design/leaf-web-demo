"""Static inventory audit; default controls use scratch directories only.

--audit-root names the offline directory containing run_regression_suite.py.
Neither the manifest nor any inventoried test is imported or executed.
"""

import argparse
import ast
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch


SCHEMA = "leaf.xbench.x01.offline-regression-inventory.v1"
FILENAME = re.compile(r"test_[A-Za-z0-9_]+\.py\Z")


def audit_root(root):
    diagnostics = []

    def error(name, entry=""):
        diagnostics.append(dict(name=name, entry=entry))

    tests, exclusions, local = [], {}, []
    try:
        root = Path(root)
        local = sorted(path.name for path in root.iterdir()
                       if path.is_file() and path.match("test_*.py"))
        source = (root / "run_regression_suite.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeError, SyntaxError, ValueError, TypeError) as exc:
        error("INVENTORY_SOURCE_UNREADABLE", type(exc).__name__)
    else:
        values = {}
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign) else
                       [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
            for target in targets:
                if not isinstance(target, ast.Name) or target.id not in ("TESTS", "EXCLUSIONS"):
                    continue
                name = target.id
                if name in values:
                    error("INVENTORY_MULTIPLE_DECLARATIONS", name)
                try:
                    if isinstance(node, ast.AugAssign):
                        raise ValueError
                    value = ast.literal_eval(node.value)
                    # literal_eval would silently collapse duplicate mapping keys.
                    if name == "EXCLUSIONS" and isinstance(node.value, ast.Dict):
                        keys = [ast.literal_eval(key) for key in node.value.keys]
                        if len(keys) != len(set(keys)):
                            error("INVENTORY_DUPLICATE_EXCLUSION", name)
                    values[name] = value
                except (ValueError, TypeError, SyntaxError, RecursionError):
                    values[name] = None
                    error("INVENTORY_NONLITERAL_DECLARATION", name)
        if "TESTS" not in values:
            error("INVENTORY_MISSING_DECLARATION", "TESTS")
        declared = values.get("TESTS")
        if not isinstance(declared, (tuple, list)):
            error("INVENTORY_MALFORMED_TESTS", "TESTS")
        else:
            for index, entry in enumerate(declared):
                if not isinstance(entry, str) or not FILENAME.fullmatch(entry):
                    error("INVENTORY_MALFORMED_ENTRY", str(index))
                elif entry in tests:
                    error("INVENTORY_DUPLICATE_DECLARATION", entry)
                else:
                    tests.append(entry)
        excluded = values.get("EXCLUSIONS", {})
        if not isinstance(excluded, dict):
            error("INVENTORY_MALFORMED_EXCLUSIONS", "EXCLUSIONS")
        else:
            for index, (entry, reason) in enumerate(excluded.items()):
                if not isinstance(entry, str) or not FILENAME.fullmatch(entry):
                    error("INVENTORY_MALFORMED_EXCLUSION", str(index))
                    continue
                exclusions[entry] = reason if isinstance(reason, str) else None
                if not isinstance(reason, str) or not reason.strip():
                    error("INVENTORY_EXCLUSION_REASON_REQUIRED", entry)
        for entry in sorted(set(tests) & set(exclusions)):
            error("INVENTORY_INCLUDED_AND_EXCLUDED", entry)
        for entry in sorted(set(local) - set(tests) - set(exclusions)):
            error("INVENTORY_OMITTED_TEST", entry)
        for entry in sorted(set(tests) - set(local)):
            error("INVENTORY_MISSING_DECLARED_FILE", entry)
        for entry in sorted(set(exclusions) - set(local)):
            error("INVENTORY_MISSING_EXCLUDED_FILE", entry)
    diagnostics.sort(key=lambda item: (item["name"], item["entry"]))
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                verdict="FAIL" if diagnostics else "PASS", diagnostics=diagnostics,
                local_tests=local, declared_tests=tests, exclusions=exclusions)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="x01 inventory ")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def fixture(self, declaration="TESTS = ('test_sentinel.py',)\n", extra=""):
        (self.root / "run_regression_suite.py").write_text(
            declaration + extra + "\nraise RuntimeError('manifest executed')\n", encoding="utf-8")
        (self.root / "test_sentinel.py").write_text(
            "raise RuntimeError('test executed')\n", encoding="utf-8")

    def names(self):
        with patch("subprocess.Popen", side_effect=AssertionError("child launch")), \
                patch("os.system", side_effect=AssertionError("shell launch")):
            result = audit_root(self.root)
            self.assertEqual(result, audit_root(self.root))
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertIs(result["live_grade"], False)
        self.assertEqual(result["verdict"], "FAIL" if result["diagnostics"] else "PASS")
        return [item["name"] for item in result["diagnostics"]]

    def test_static_sentinel_and_optional_exclusions(self):
        self.fixture()
        self.assertEqual(self.names(), [])
        self.fixture(extra="EXCLUSIONS = {}\n")
        self.assertEqual(self.names(), [])

    def test_omitted_local_and_missing_declared(self):
        self.fixture("TESTS = ('test_missing.py',)\n")
        self.assertEqual(self.names(), ["INVENTORY_MISSING_DECLARED_FILE", "INVENTORY_OMITTED_TEST"])

    def test_duplicate_declaration(self):
        self.fixture("TESTS = ('test_sentinel.py', 'test_sentinel.py')\n")
        self.assertEqual(self.names(), ["INVENTORY_DUPLICATE_DECLARATION"])

    def test_malformed_entries(self):
        for entry in (None, 1, [], "../test_other.py", "helper.py", "test_a.py/child"):
            with self.subTest(entry=entry):
                self.fixture("TESTS = ('test_sentinel.py', " + repr(entry) + ")\n")
                self.assertEqual(self.names(), ["INVENTORY_MALFORMED_ENTRY"])

    def test_exclusion_reasons_and_ambiguity(self):
        for reason in (None, "", "  ", 1, [], {}, b"reason", {"reason"}):
            self.fixture("TESTS = ()\n", "EXCLUSIONS = {'test_sentinel.py': " + repr(reason) + "}\n")
            self.assertEqual(self.names(), ["INVENTORY_EXCLUSION_REASON_REQUIRED"])
        self.fixture("TESTS = ()\n", "EXCLUSIONS = {'test_sentinel.py': 'separate bounded control'}\n")
        self.assertEqual(self.names(), [])
        self.fixture(extra="EXCLUSIONS = {'test_sentinel.py': 'reason'}\n")
        self.assertEqual(self.names(), ["INVENTORY_INCLUDED_AND_EXCLUDED"])

    def test_malformed_exclusions(self):
        for value in ([], None, 1):
            self.fixture(extra="EXCLUSIONS = " + repr(value) + "\n")
            self.assertEqual(self.names(), ["INVENTORY_MALFORMED_EXCLUSIONS"])
        self.fixture(extra="EXCLUSIONS = {'../test_x.py': 'reason'}\n")
        self.assertEqual(self.names(), ["INVENTORY_MALFORMED_EXCLUSION"])
        self.fixture(extra="EXCLUSIONS = {'test_missing.py': 'reason'}\n")
        self.assertEqual(self.names(), ["INVENTORY_MISSING_EXCLUDED_FILE"])
        self.fixture("TESTS = ()\n", "EXCLUSIONS = {'test_sentinel.py': 'one', 'test_sentinel.py': 'two'}\n")
        self.assertEqual(self.names(), ["INVENTORY_DUPLICATE_EXCLUSION"])

    def test_nonliteral_missing_and_multiple_declarations(self):
        for declaration, expected in (
                ("", "INVENTORY_MISSING_DECLARATION"),
                ("TESTS = tuple()\n", "INVENTORY_NONLITERAL_DECLARATION"),
                ("TESTS = 'test_sentinel.py'\n", "INVENTORY_MALFORMED_TESTS"),
                ("TESTS = ()\nTESTS = ('test_sentinel.py',)\n", "INVENTORY_MULTIPLE_DECLARATIONS"),
                ("TESTS = ()\nTESTS += ('test_sentinel.py',)\n", "INVENTORY_NONLITERAL_DECLARATION")):
            self.fixture(declaration)
            self.assertIn(expected, self.names())

    def test_unreadable_source(self):
        self.assertEqual(self.names(), ["INVENTORY_SOURCE_UNREADABLE"])
        self.fixture("TESTS = (\n")
        self.assertEqual(self.names(), ["INVENTORY_SOURCE_UNREADABLE"])

    def test_audit_cli_json_and_exit(self):
        self.fixture()
        for declaration, code in (("TESTS = ('test_sentinel.py',)\n", 0), ("TESTS = ()\n", 1)):
            self.fixture(declaration)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--audit-root", str(self.root)]), code)
            self.assertEqual(json.loads(output.getvalue()), audit_root(self.root))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path)
    args = parser.parse_args(argv)
    if args.audit_root is not None:
        result = audit_root(args.audit_root)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result["verdict"] == "PASS" else 1
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InventoryTests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
