"""Sequential stdlib checks of synthetic offline support only."""

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCHEMA = "leaf.xbench.x01.offline-run.v1"
ROOT = Path(__file__).resolve().parent
TEXT_LIMIT = 16384
CHECKERS = ("check_cases.py", "check_plot_layers.py", "check_unit_equivalence.py")


def timeout_seconds(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or not 0 < result <= 300:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timeout must be positive, finite and at most 300 seconds") from None
    return result


def aggregate(rows):
    """Explicit verdict precedence; rows remain available to callers."""
    verdicts = {row["verdict"] for row in rows}
    return "FAIL" if "FAIL" in verdicts else "UNREADABLE" if "UNREADABLE" in verdicts else "PASS"


def run_checks(commands=None, timeout=30):
    """Run (checker name, argv) pairs; command injection is an internal test seam."""
    timeout = timeout_seconds(timeout)
    if commands is None:
        commands = [(name, [sys.executable, str(ROOT / name), "--self-test"])
                    for name in CHECKERS]
    rows = []
    for name, command in commands:
        started = time.monotonic()
        code, timed_out, output, failure = None, False, "", None
        # A file avoids blocking on an unread pipe. Saved raw text is bounded,
        # including on timeout; structured JSON diagnostics retain their shape.
        with tempfile.TemporaryFile() as captured:
            try:
                child = subprocess.Popen(command, stdout=captured, stderr=subprocess.STDOUT)
                try:
                    code = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    child.kill()
                    code = child.wait()
                    failure = "CHILD_TIMEOUT"
            except OSError:
                failure = "CHILD_LAUNCH_ERROR"
            captured.seek(0)
            raw = captured.read(TEXT_LIMIT + 1)
            truncated = len(raw) > TEXT_LIMIT
            output = raw[:TEXT_LIMIT].decode("utf-8", errors="replace")
            captured.seek(0)
            try:
                payload = json.load(captured)
            except (ValueError, TypeError):
                payload = None
        verdict = {0: "PASS", 1: "FAIL", 4: "UNREADABLE"}.get(code, "UNREADABLE")
        if failure:
            verdict = "UNREADABLE"
        elif code not in (0, 1, 4):
            failure = "CHILD_UNEXPECTED_EXIT"
        diagnostics = []
        if isinstance(payload, dict) and isinstance(payload.get("diagnostics"), list):
            diagnostics = [item for item in payload["diagnostics"] if isinstance(item, dict)]
        if failure:
            diagnostics.append(dict(name=failure, checker=name))
        if not diagnostics:
            diagnostics.append(dict(name="CHILD_OUTPUT", checker=name, text=output,
                                    truncated=truncated))
        rows.append(dict(checker=name, verdict=verdict, exit_code=code,
                         timeout=timed_out, duration_seconds=time.monotonic() - started,
                         diagnostics=diagnostics, output=output, output_truncated=truncated))
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                verdict=aggregate(rows), checks=rows)


def write_atomic(path, result):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as stream:
            temporary = stream.name
            json.dump(result, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


class RunnerTests(unittest.TestCase):
    def stub(self, source):
        return [sys.executable, "-c", source]

    def run_stub(self, source, timeout=2):
        return run_checks([("stub", self.stub(source))], timeout)

    def test_all_pass(self):
        result = run_checks([(name, self.stub("pass")) for name in CHECKERS])
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(len(result["checks"]), 3)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertIs(result["live_grade"], False)

    def test_named_failure_and_all_children_run(self):
        named = {"name": "EXACT_FAILURE", "object_ids": ["o/1"]}
        source = "import sys; print(" + repr(json.dumps(dict(diagnostics=[named]))) + "); sys.exit(1)"
        result = run_checks([("bad", self.stub(source)),
                             ("unreadable", self.stub("raise SystemExit(4)")),
                             ("last", self.stub("pass"))])
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(len(result["checks"]), 3)
        self.assertEqual(result["checks"][0]["diagnostics"], [named])
        self.assertEqual(result["checks"][-1]["verdict"], "PASS")

    def test_exit_four(self):
        self.assertEqual(self.run_stub("raise SystemExit(4)")["verdict"], "UNREADABLE")

    def test_timeout(self):
        result = self.run_stub("import time; time.sleep(5)", .05)
        row = result["checks"][0]
        self.assertEqual(result["verdict"], "UNREADABLE")
        self.assertTrue(row["timeout"])
        self.assertIsNotNone(row["exit_code"])
        self.assertEqual(row["diagnostics"][0]["name"], "CHILD_TIMEOUT")
        self.assertLess(row["duration_seconds"], 2)

    def test_missing_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            row = run_checks([("missing", [str(Path(directory) / "absent")])])["checks"][0]
        self.assertEqual(row["verdict"], "UNREADABLE")
        self.assertEqual(row["diagnostics"][0], dict(name="CHILD_LAUNCH_ERROR", checker="missing"))

    def test_unexpected_and_negative_exit(self):
        for source in ("raise SystemExit(7)", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"):
            with self.subTest(source=source):
                row = self.run_stub(source)["checks"][0]
                self.assertEqual(row["verdict"], "UNREADABLE")
                self.assertEqual(row["diagnostics"][0]["name"], "CHILD_UNEXPECTED_EXIT")
        self.assertEqual(aggregate([dict(verdict="PASS"), dict(verdict="UNREADABLE")]), "UNREADABLE")

    def test_plain_unittest_output(self):
        # An empty unittest suite exits 5 on newer Python versions. Exercise
        # actual successful unittest output rather than the no-tests policy.
        source = ("import unittest\n"
                  "class Smoke(unittest.TestCase):\n"
                  "    def test_smoke(self):\n"
                  "        self.assertEqual(2 + 2, 4)\n"
                  "unittest.main(argv=['stub'])\n")
        row = self.run_stub(source)["checks"][0]
        self.assertEqual(row["exit_code"], 0, row)
        self.assertEqual(row["verdict"], "PASS", row)
        self.assertEqual(row["diagnostics"][0]["checker"], "stub")
        self.assertIn("Ran 1 test", row["diagnostics"][0]["text"])
        self.assertIn("OK", row["diagnostics"][0]["text"])

    def test_invalid_timeout(self):
        for value in (0, -1, 301, True, None, "bad", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_checks([], value)

    def test_json_pass_does_not_override_exit(self):
        row = self.run_stub('print(\'{"verdict":"PASS","diagnostics":[]}\'); raise SystemExit(1)')["checks"][0]
        self.assertEqual(row["verdict"], "FAIL")

    def test_output_is_bounded(self):
        row = self.run_stub("print('x' * 50000)")["checks"][0]
        self.assertLessEqual(len(row["output"]), TEXT_LIMIT)
        self.assertTrue(row["output_truncated"])

    def test_named_diagnostic_survives_long_json_output(self):
        source = 'import json; print(json.dumps({"padding": "x" * 20000, "diagnostics": [{"name": "NAMED", "object_ids": ["a"]}]}))'
        row = self.run_stub(source)["checks"][0]
        self.assertEqual(row["diagnostics"], [dict(name="NAMED", object_ids=["a"])])
        self.assertTrue(row["output_truncated"])

    def test_atomic_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            result = self.run_stub("pass")
            write_atomic(path, result)
            self.assertEqual(json.loads(path.read_text()), result)
            self.assertEqual(list(Path(directory).iterdir()), [path])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--timeout-seconds", default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        timeout = timeout_seconds(args.timeout_seconds)
    except ValueError as error:
        print(json.dumps(dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                              verdict="UNREADABLE", checks=[], diagnostics=[dict(
                                  name="INVALID_TIMEOUT", message=str(error))])))
        return 4
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(RunnerTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    result = run_checks(timeout=timeout)
    if args.output:
        try:
            write_atomic(args.output, result)
        except OSError as error:
            result["diagnostics"] = [dict(name="OUTPUT_WRITE_ERROR", message=str(error))]
            result["verdict"] = aggregate(result["checks"] + [dict(verdict="UNREADABLE")])
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
