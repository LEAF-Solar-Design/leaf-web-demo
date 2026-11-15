"""Bounded sequential execution of the fixed synthetic offline regression slice."""

import argparse
import contextlib
import io
import json
import math
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

try:
    from .run_offline_checks import (capture_child, structured_diagnostics,
                                     timeout_seconds, write_atomic, TEXT_LIMIT,
                                     MAX_DIAGNOSTICS, MAX_DIAGNOSTIC_BYTES)
except ImportError:
    from run_offline_checks import (capture_child, structured_diagnostics,
                                    timeout_seconds, write_atomic, TEXT_LIMIT,
                                    MAX_DIAGNOSTICS, MAX_DIAGNOSTIC_BYTES)


SCHEMA = "leaf.xbench.x01.offline-regression.v1"
ROOT = Path(__file__).resolve().parent
PYTHON = str(Path(sys.executable).resolve())
TESTS = (
    "test_array_insert_layers.py",
    "test_atomic_report_failures.py",
    "test_case_file_limits.py",
    "test_case_input_contract.py",
    "test_case_work_limits.py",
    "test_child_lifecycle.py",
    "test_cli_path_portability.py",
    "test_dxf_mutation_corpus.py",
    "test_fill_grid_oracle.py",
    "test_fill_tag_numeric_failures.py",
    "test_geometry_invariants.py",
    "test_geometry_matching_oracle.py",
    "test_geometry_numeric_failures.py",
    "test_layer_resource_limits.py",
    "test_oblique_tag_oracle.py",
    "test_regression_inventory.py",
    "test_report_comparison_algebra.py",
    "test_source_read_mutation.py",
    "test_synthetic_workflow.py",
    "test_tag_polygon_oracle.py",
    "test_unit_precision_boundaries.py",
    "test_validator_wire_boundaries.py",
)
EXCLUSIONS = {}  # Every local test_*.py script is explicitly included.
SELF_TESTS = (
    "check_cases.py", "check_plot_layers.py", "check_unit_equivalence.py",
    "run_offline_checks.py", "validate_offline_report.py",
    "verify_report_sources.py", "benchmark_synthetic.py",
)
SUITES = TESTS + SELF_TESTS
DEFAULT_TIMEOUT = 30
DEFAULT_TOTAL = 300
MAX_TOTAL = 3600
EXIT = {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}


def envelope(rows, diagnostics=None):
    verdicts = {row["verdict"] for row in rows}
    verdict = ("FAIL" if "FAIL" in verdicts else "UNREADABLE"
               if not rows or "UNREADABLE" in verdicts or diagnostics else "PASS")
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                verdict=verdict, suites=rows, diagnostics=diagnostics or [])


def total_seconds(value):
    try:
        result = float(value)
        if isinstance(value, bool) or not math.isfinite(result) or not 0 < result <= MAX_TOTAL:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("total budget must be positive, finite and at most 3600 seconds") from None
    return result


def manifest(names=SUITES):
    return [dict(name=name, argv=[PYTHON, str(ROOT / name)] +
                 (["--self-test"] if name in SELF_TESTS else []),
                 parse_json=False) for name in names]


def run_suites(commands=None, timeout=DEFAULT_TIMEOUT, total_budget=DEFAULT_TOTAL):
    """Commands are an internal stub seam; CLI selection always uses the whitelist."""
    timeout, total_budget = timeout_seconds(timeout), total_seconds(total_budget)
    commands = manifest() if commands is None else list(commands)
    started = time.monotonic()
    deadline = started + total_budget
    rows = []
    for entry in commands:
        name, command = entry["name"], entry["argv"]
        before = time.monotonic()
        remaining = deadline - before
        if remaining <= 0:
            rows.append(dict(name=name, argv=command, verdict="UNREADABLE",
                             exit_code=None, timeout=False, skipped=True,
                             duration_seconds=0, output="", output_truncated=False,
                             failure="REGRESSION_DEADLINE_EXCEEDED", diagnostics=[dict(
                                 name="REGRESSION_DEADLINE_EXCEEDED", suite=name)]))
            continue
        code, timed_out, raw, failure = capture_child(command, min(timeout, remaining))
        # Unittest logs may contain JSON examples followed by ordinary text.
        # Their exit code is authoritative. Structured output is an explicit
        # internal command option, never inferred from a log's first byte.
        diagnostics, parse_failure = (structured_diagnostics(raw)
                                      if not failure and entry.get("parse_json", False)
                                      else ([], None))
        failure = failure or parse_failure
        output = raw[:TEXT_LIMIT].decode("utf-8", errors="replace")
        encoded = output.encode("utf-8")
        truncated = len(raw) > TEXT_LIMIT or len(encoded) > TEXT_LIMIT
        output = encoded[:TEXT_LIMIT].decode("utf-8", errors="ignore")
        verdict = {0: "PASS", 1: "FAIL", 4: "UNREADABLE"}.get(code, "UNREADABLE")
        if failure:
            verdict = "UNREADABLE"
        elif code not in (0, 1, 4):
            failure = "CHILD_UNEXPECTED_EXIT"
        if failure:
            diagnostics.append(dict(name=failure, suite=name))
        if not diagnostics:
            diagnostics.append(dict(name="SUITE_FAILED" if code == 1 else
                                    "SUITE_UNREADABLE" if code == 4 else "CHILD_OUTPUT",
                                    suite=name, text=output[:2048], truncated=truncated))
        if (len(diagnostics) > MAX_DIAGNOSTICS or
                len(json.dumps(diagnostics).encode("utf-8")) > MAX_DIAGNOSTIC_BYTES):
            verdict, failure = "UNREADABLE", "CHILD_DIAGNOSTIC_LIMIT"
            diagnostics = [dict(name=failure, suite=name)]
        rows.append(dict(name=name, argv=command, verdict=verdict, exit_code=code,
                         timeout=timed_out, skipped=False, failure=failure,
                         duration_seconds=time.monotonic() - before,
                         diagnostics=diagnostics, output=output, output_truncated=truncated))
    result = envelope(rows, [] if commands else [dict(name="NO_SUITES_SELECTED")])
    result.update(timeout_seconds=timeout, total_budget_seconds=total_budget,
                  duration_seconds=time.monotonic() - started)
    return result


class InvalidCLI(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidCLI(message)


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--suite", action="append")
    parser.add_argument("--timeout-seconds", default=DEFAULT_TIMEOUT)
    parser.add_argument("--total-budget-seconds", default=DEFAULT_TOTAL)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    try:
        args = parser.parse_args(argv)
        timeout = timeout_seconds(args.timeout_seconds)
        budget = total_seconds(args.total_budget_seconds)
        names = SUITES if args.suite is None else args.suite
        unknown = [name for name in names if name not in SUITES]
        if unknown:
            raise InvalidCLI("unknown suite: " + ", ".join(unknown))
        if args.self_test and (args.list or args.suite or args.output):
            raise InvalidCLI("--self-test cannot be combined with --list, --suite or --output")
    except (InvalidCLI, ValueError) as error:
        result = envelope([], [dict(name="INVALID_ARGUMENT", message=str(error))])
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 4
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(RegressionTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.list:
        result = dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                      mode="manifest", suites=manifest(names), timeout_seconds=timeout,
                      total_budget_seconds=budget)
    else:
        result = run_suites(manifest(names), timeout, budget)
    if args.output:
        try:
            write_atomic(args.output, result)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
            result.setdefault("diagnostics", []).append(dict(
                name="OUTPUT_WRITE_ERROR", message=str(error)))
            if result.get("verdict") != "FAIL":
                result["verdict"] = "UNREADABLE"
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return EXIT.get(result.get("verdict"), 0)


class RegressionTests(unittest.TestCase):
    def stub(self, name, source):
        return dict(name=name, argv=[PYTHON, "-c", source], parse_json=True)

    def test_mixed_unittest_logs_preserve_exit_status(self):
        entry = self.stub("mixed", 'print(\'{"control": true}\', flush=True); print("Ran 1 test\\nOK")')
        entry["parse_json"] = False
        result = run_suites([entry], 5, 10)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["suites"][0]["exit_code"], 0)
        self.assertIsNone(result["suites"][0]["failure"])
        self.assertIn("Ran 1 test", result["suites"][0]["output"])
        entry["parse_json"] = True
        refused = run_suites([entry], 5, 10)
        self.assertEqual(refused["verdict"], "UNREADABLE")
        self.assertEqual(refused["suites"][0]["failure"], "CHILD_OUTPUT_UNREADABLE")

    def test_statuses_order_and_named_failure(self):
        named = dict(name="SYNTHETIC_NAMED_FAILURE", object_ids=["synthetic/1"])
        source = "print(" + repr(json.dumps(dict(diagnostics=[named]))) + "); raise SystemExit(1)"
        commands = [self.stub("pass", "pass"), self.stub("fail", source),
                    self.stub("unreadable", "raise SystemExit(4)"),
                    self.stub("unexpected", "raise SystemExit(7)"), self.stub("last", "pass")]
        result = run_suites(commands, 5, 30)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual([r["name"] for r in result["suites"]], [c["name"] for c in commands])
        self.assertEqual([r["verdict"] for r in result["suites"]],
                         ["PASS", "FAIL", "UNREADABLE", "UNREADABLE", "PASS"])
        self.assertEqual(result["suites"][1]["diagnostics"], [named])
        self.assertEqual(result["suites"][3]["failure"], "CHILD_UNEXPECTED_EXIT")
        self.assertFalse(result["suites"][3]["timeout"])

    def test_pass_and_empty(self):
        self.assertEqual(run_suites([self.stub("pass", "pass")], 5, 10)["verdict"], "PASS")
        self.assertEqual(run_suites([])["verdict"], "UNREADABLE")

    def test_timeout_and_global_deadline(self):
        result = run_suites([self.stub("slow", "import time; time.sleep(10)"),
                             self.stub("later", "raise SystemExit(1)")], .05, .05)
        first, later = result["suites"]
        self.assertTrue(first["timeout"])
        self.assertEqual(first["failure"], "CHILD_TIMEOUT")
        self.assertEqual(later["failure"], "REGRESSION_DEADLINE_EXCEEDED")
        self.assertTrue(later["skipped"])
        self.assertIsNone(later["exit_code"])
        self.assertEqual(result["verdict"], "UNREADABLE")

    def test_manifest_and_invalid_cli_never_capture(self):
        with patch(__name__ + ".capture_child") as capture:
            for argv in (["--suite", "unknown"], ["--timeout-seconds", "nan"],
                         ["--timeout-seconds", "301"], ["--total-budget-seconds", "0"],
                         ["--total-budget-seconds", "inf"], ["--total-budget-seconds", "3601"],
                         ["--unknown"], ["--suite"]):
                with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(argv), 4)
                    self.assertEqual(json.loads(output.getvalue())["diagnostics"][0]["name"],
                                     "INVALID_ARGUMENT")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--list"]), 0)
            listed = json.loads(output.getvalue())
            self.assertEqual([row["name"] for row in listed["suites"]], list(SUITES))
            self.assertNotIn(Path(__file__).name, SUITES)
            self.assertEqual(len(TESTS), 22)
            for row in listed["suites"]:
                self.assertTrue(Path(row["argv"][0]).is_absolute())
                self.assertTrue(Path(row["argv"][1]).is_absolute())
            capture.assert_not_called()


if __name__ == "__main__":
    sys.exit(main())
