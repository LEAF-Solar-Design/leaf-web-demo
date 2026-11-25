"""Output alias refusal uses scratch sources only; no live grading."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_offline_checks as offline
import run_regression_suite as regression


class OutputPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.root = self.directory / "helpers"
        self.root.mkdir()
        (self.root / "nested").mkdir()
        for name in ("run_offline_checks.py", "run_regression_suite.py", "extra_helper.py"):
            (self.root / name).write_bytes(b"# synthetic helper source\n")
        for name in offline.VECTORS:
            (self.root / name).write_bytes(b'{"synthetic": true}\n')
        self.original = {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()}

    def invoke(self, module, output, self_test=False, identity_error=False):
        report = dict(schema=module.SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                      verdict="PASS", diagnostics=[])
        run_name = "run_checks" if module is offline else "run_suites"
        text = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, "ROOT", self.root))
            run = stack.enter_context(patch.object(module, run_name, return_value=report))
            captures = [stack.enter_context(patch.object(item, "capture_child"))
                        for item in (offline, regression)]
            load = stack.enter_context(patch.object(unittest.defaultTestLoader, "loadTestsFromTestCase"))
            write = stack.enter_context(patch.object(module, "write_atomic", wraps=module.write_atomic))
            if identity_error:
                stack.enter_context(patch.object(Path, "samefile", side_effect=OSError("identity denied")))
            stack.enter_context(contextlib.redirect_stdout(text))
            args = ["--output", str(output)] + (["--self-test"] if self_test else [])
            code = module.main(args)
        for capture in captures:
            capture.assert_not_called()
        load.assert_not_called()
        for path, original in self.original.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        return code, json.loads(text.getvalue()), run, write

    def assert_refused(self, module, output, self_test=False, identity_error=False):
        before = sorted(str(path) for path in self.directory.rglob("*"))
        code, report, run, write = self.invoke(module, output, self_test, identity_error)
        self.assertEqual(code, 4)
        self.assertEqual(report["verdict"], "UNREADABLE")
        self.assertEqual(report["evidence_kind"], "synthetic_offline")
        self.assertIs(report["live_grade"], False)
        self.assertEqual(report["diagnostics"][0]["name"],
                         "OUTPUT_PATH_UNREADABLE" if identity_error else "OUTPUT_PROTECTED")
        self.assertEqual(report["checks" if module is offline else "suites"], [])
        run.assert_not_called()
        write.assert_not_called()
        self.assertEqual(sorted(str(path) for path in self.directory.rglob("*")), before)

    def test_direct_sources_and_exact_vectors_before_execution_and_self_tests(self):
        for module in (offline, regression):
            for source in self.original:
                for self_test in (False, True):
                    with self.subTest(runner=module.__name__, source=source.name, self_test=self_test):
                        self.assert_refused(module, source, self_test)

    def test_relative_and_canonical_aliases(self):
        with contextlib.chdir(self.root):
            for module in (offline, regression):
                for source in self.original:
                    for alias in (os.path.relpath(source, start=self.root),
                                  self.root / "nested" / ".." / source.name):
                        with self.subTest(runner=module.__name__, alias=str(alias)):
                            self.assert_refused(module, alias)

    def test_existing_hardlink_aliases(self):
        for index, source in enumerate(self.original):
            alias = self.directory / ("hardlink-" + str(index) + ".json")
            os.link(source, alias)
            for module in (offline, regression):
                for self_test in (False, True):
                    with self.subTest(runner=module.__name__, source=source.name, self_test=self_test):
                        self.assert_refused(module, alias, self_test)

    def test_symlink_aliases(self):
        alias = self.directory / "symlink.json"
        try:
            alias.symlink_to(self.root / "extra_helper.py")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        for module in (offline, regression):
            self.assert_refused(module, alias)

    def test_distinct_json_outputs_are_allowed(self):
        # An arbitrary JSON file in ROOT is not a protected vector.
        for module in (offline, regression):
            for existing in (False, True):
                output = self.root / (module.__name__ + str(existing) + ".json")
                if existing:
                    output.write_bytes(b'{"old": true}\n')
                code, report, run, write = self.invoke(module, output)
                self.assertEqual(code, 0)
                self.assertEqual(report["verdict"], "PASS")
                self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
                run.assert_called_once()
                write.assert_called_once()
                self.assertFalse(list(self.root.glob("." + output.name + ".*")))

    def test_ambiguous_identity_refuses_before_execution_and_self_tests(self):
        output = self.directory / "distinct.json"
        original = b'{"old": true}\n'
        output.write_bytes(original)
        for module in (offline, regression):
            for self_test in (False, True):
                with self.subTest(runner=module.__name__, self_test=self_test):
                    self.assert_refused(module, output, self_test, identity_error=True)
                    self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
