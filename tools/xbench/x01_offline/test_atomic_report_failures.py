"""Fault injection for synthetic offline report replacement; stdlib only."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


_SPEC = importlib.util.spec_from_file_location(
    "x01_atomic_runner", Path(__file__).with_name("run_offline_checks.py"))
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


def synthetic_report(failed=False):
    rows = [dict(checker=name, verdict="FAIL" if failed and index == 0 else "PASS",
                 exit_code=1 if failed and index == 0 else 0, timeout=False,
                 duration_seconds=0.0, diagnostics=[dict(name="SYNTHETIC_TEST")],
                 output="", output_truncated=False)
            for index, name in enumerate(runner.CHECKERS)]
    return dict(schema=runner.SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                verdict=runner.aggregate(rows), checks=rows, diagnostics=[])


class AtomicReportFailureTests(unittest.TestCase):
    def test_failures_preserve_prior_or_absent_report_and_remove_temps(self):
        for operation in ("serialization", "fsync", "replace"):
            for prior in (False, True):
                with self.subTest(operation=operation, prior=prior), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "report.json"
                    sentinel = b"prior report\x00\xff\n"
                    if prior:
                        path.write_bytes(sentinel)
                    if operation == "serialization":
                        def fail_dump(result, stream, **kwargs):
                            stream.write('{"partial":')
                            raise TypeError("injected serialization error")
                        fault = patch.object(runner.json, "dump", side_effect=fail_dump)
                        error = TypeError
                    else:
                        fault = patch.object(runner.os, operation,
                                             side_effect=OSError("injected " + operation))
                        error = OSError
                    with fault, self.assertRaises(error):
                        runner.write_atomic(path, synthetic_report())
                    if prior:
                        self.assertEqual(path.read_bytes(), sentinel)
                    else:
                        self.assertFalse(path.exists())
                    self.assertEqual(list(Path(directory).iterdir()), [path] if prior else [])

    def test_happy_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_bytes(b"sentinel")
            report = synthetic_report()
            runner.write_atomic(path, report)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_main_output_error_and_fail_precedence(self):
        for failed in (False, True):
            for error in (OSError("save failed"), TypeError("serialization failed"),
                          ValueError("serialization failed")):
                with self.subTest(failed=failed, error=type(error).__name__):
                    output = io.StringIO()
                    with patch.object(runner, "run_checks", return_value=synthetic_report(failed)), \
                            patch.object(runner, "write_atomic", side_effect=error), \
                            contextlib.redirect_stdout(output):
                        code = runner.main(["--output", "unused-synthetic-report.json"])
                    report = json.loads(output.getvalue())
                    self.assertEqual(code, 1 if failed else 4)
                    self.assertEqual(report["verdict"], "FAIL" if failed else "UNREADABLE")
                    self.assertEqual(report["diagnostics"][0]["name"], "OUTPUT_WRITE_ERROR")
                    self.assertEqual(len(report["checks"]), 3)
                    self.assertEqual(report["evidence_kind"], "synthetic_offline")
                    self.assertIs(report["live_grade"], False)

    def test_main_serialization_failure_does_not_serialize_unsafe_child_data(self):
        # Unsafe child JSON is refused before it can enter the saved report.
        diagnostics, reason = runner.structured_diagnostics(
            b'{"diagnostics": [{"name": "unsafe", "value": NaN}]}')
        self.assertEqual(diagnostics, [])
        self.assertEqual(reason, "CHILD_OUTPUT_UNREADABLE")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_bytes(b"sentinel")
            output = io.StringIO()
            with patch.object(runner, "run_checks", return_value=synthetic_report()), \
                    patch.object(runner.json, "dump", side_effect=ValueError("injected dump failure")), \
                    contextlib.redirect_stdout(output):
                code = runner.main(["--output", str(path)])
            self.assertEqual(code, 4)
            report = json.loads(output.getvalue())
            self.assertEqual(report["diagnostics"][0]["name"], "OUTPUT_WRITE_ERROR")
            self.assertEqual(path.read_bytes(), b"sentinel")
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main(verbosity=2)
