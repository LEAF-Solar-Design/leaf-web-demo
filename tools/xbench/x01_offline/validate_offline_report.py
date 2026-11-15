"""Read-only validation of leaf.xbench.x01.offline-run.v1 reports.

Only normal aggregates of the three known synthetic offline checkers are
supported, including older reports without provenance. This validates report
consistency, never live-grade evidence or the truth of a checker's conclusions.
"""

import argparse
import contextlib
import copy
import io
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import unittest


REPORT_SCHEMA = "leaf.xbench.x01.offline-run.v1"
SCHEMA = "leaf.xbench.x01.offline-report-validation.v1"
CHECKERS = ("check_cases.py", "check_plot_layers.py", "check_unit_equivalence.py")
MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
TEXT_LIMIT = 16384
DIAGNOSTIC_LIMIT = 64
DIAGNOSTIC_BYTES = 16384
ROW_REASONS = frozenset((
    "CHILD_TIMEOUT", "CHILD_OUTPUT_LIMIT", "CHILD_DIAGNOSTIC_LIMIT",
    "CHILD_LAUNCH_ERROR", "CHILD_UNEXPECTED_EXIT", "CHILD_OUTPUT_UNREADABLE",
    "CHILD_DIAGNOSTICS_INVALID",
    "CHILD_CONTAINMENT_ERROR",
))
# The two explicit parsing exceptions are CHILD_OUTPUT_UNREADABLE and
# CHILD_DIAGNOSTICS_INVALID. Arbitrary diagnostic names do not override exits.
PARSE_OR_LIMIT_REASONS = ROW_REASONS - {
    "CHILD_TIMEOUT", "CHILD_LAUNCH_ERROR", "CHILD_UNEXPECTED_EXIT"}
SOURCE_REASONS = frozenset(("INPUT_CHANGED", "INPUT_UNREADABLE"))


def _result(verdict, diagnostics, observed=None):
    result = dict(schema=SCHEMA, evidence_kind="synthetic_offline",
                  live_grade=False, verdict=verdict, valid=verdict == "PASS",
                  diagnostics=[{key: value[:256] if isinstance(value, str) else value
                                for key, value in item.items()}
                               for item in diagnostics[:DIAGNOSTIC_LIMIT]])
    if observed in ("PASS", "FAIL", "UNREADABLE"):
        result["observed_verdict"] = observed
    return result


def _tree_problem(value, max_depth=MAX_DEPTH):
    """Bound traversal before any recursive serialization, including API inputs."""
    pending = [(value, "$", 0)]
    while pending:
        node, path, depth = pending.pop()
        if depth > max_depth:
            return "DEPTH_LIMIT", path
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                return "INVALID_JSON_TYPE", path
            for key, child in node.items():
                child_path = path + "." + key
                if key == "live_grade" and child is not False:
                    return "LIVE_GRADE_CLAIM", child_path
                pending.append((child, child_path, depth + 1))
        elif isinstance(node, list):
            pending.extend((child, path + "[" + str(i) + "]", depth + 1)
                           for i, child in enumerate(node))
        elif type(node) is float:
            if not math.isfinite(node):
                return "NONFINITE_NUMBER", path
        elif node is not None and type(node) not in (str, int, bool):
            return "INVALID_JSON_TYPE", path
    return None


def validate_report(value):
    """Return a separate validation result; observed FAIL can validate as PASS."""
    errors = []

    def error(name, path, checker=None):
        item = dict(name=name, path=path)
        if checker is not None:
            item["checker"] = checker
        errors.append(item)

    problem = _tree_problem(value)
    if problem:
        error(*problem)
        return _result("FAIL", errors)
    if not isinstance(value, dict):
        return _result("FAIL", [dict(name="REPORT_NOT_OBJECT", path="$")])

    def diagnostics(items, path, checker=None):
        if not isinstance(items, list):
            error("INVALID_DIAGNOSTICS", path, checker)
            return set()
        try:
            oversized = (len(items) > DIAGNOSTIC_LIMIT or
                         _tree_problem(items, 8) or
                         len(json.dumps(items, ensure_ascii=True).encode("utf-8")) > DIAGNOSTIC_BYTES)
        except (ValueError, OverflowError):
            oversized = True
        if oversized:
            error("DIAGNOSTIC_LIMIT", path, checker)
        names = set()
        for i, item in enumerate(items):
            if (not isinstance(item, dict) or
                    not isinstance(item.get("name"), str) or not item["name"]):
                error("INVALID_DIAGNOSTIC", path + "[" + str(i) + "]", checker)
            else:
                names.add(item["name"])
        return names

    if value.get("schema") != REPORT_SCHEMA:
        error("UNKNOWN_SCHEMA", "$.schema")
    if value.get("evidence_kind") != "synthetic_offline":
        error("INVALID_EVIDENCE_KIND", "$.evidence_kind")
    if value.get("live_grade") is not False:
        error("INVALID_LIVE_GRADE", "$.live_grade")
    parent = value.get("verdict")
    if parent not in ("PASS", "FAIL", "UNREADABLE"):
        error("INVALID_VERDICT", "$.verdict")
    top = value.get("diagnostics", [])
    top_names = diagnostics(top, "$.diagnostics")
    if isinstance(top, list):
        for i, item in enumerate(top):
            if (isinstance(item, dict) and isinstance(item.get("name"), str) and
                    item["name"] in SOURCE_REASONS and
                    (not isinstance(item.get("input"), str) or not item["input"].strip())):
                error("INVALID_SOURCE_DIAGNOSTIC", "$.diagnostics[" + str(i) + "]")
    rows = value.get("checks")
    if not isinstance(rows, list):
        error("INVALID_CHECKS", "$.checks")
        rows = []
    if len(rows) != 3:
        error("CHECKER_COUNT", "$.checks")
    seen = set()
    verdicts = []
    for i, row in enumerate(rows):
        path = "$.checks[" + str(i) + "]"
        if not isinstance(row, dict):
            error("INVALID_CHECKER_ROW", path)
            continue
        checker = row.get("checker")
        if not isinstance(checker, str) or checker not in CHECKERS:
            error("UNKNOWN_CHECKER", path + ".checker")
            checker = None
        elif checker in seen:
            error("DUPLICATE_CHECKER", path + ".checker", checker)
        else:
            seen.add(checker)
        verdict = row.get("verdict")
        if verdict not in ("PASS", "FAIL", "UNREADABLE"):
            error("INVALID_VERDICT", path + ".verdict", checker)
        else:
            verdicts.append(verdict)
        code = row.get("exit_code")
        code_valid = "exit_code" in row and (code is None or type(code) is int)
        if not code_valid:
            error("INVALID_EXIT_CODE", path + ".exit_code", checker)
        timeout = row.get("timeout")
        if type(timeout) is not bool:
            error("INVALID_TIMEOUT", path + ".timeout", checker)
        duration = row.get("duration_seconds")
        if (type(duration) not in (int, float) or duration < 0 or
                (type(duration) is int and duration.bit_length() > 1023)):
            error("INVALID_DURATION", path + ".duration_seconds", checker)
        output = row.get("output")
        if not isinstance(output, str):
            error("INVALID_OUTPUT", path + ".output", checker)
        elif len(output.encode("utf-8", errors="surrogatepass")) > TEXT_LIMIT:
            error("OUTPUT_LIMIT", path + ".output", checker)
        if type(row.get("output_truncated")) is not bool:
            error("INVALID_OUTPUT_TRUNCATED", path + ".output_truncated", checker)
        reasons = diagnostics(row.get("diagnostics"), path + ".diagnostics", checker)
        exceptions = reasons & ROW_REASONS
        if code_valid and type(timeout) is bool:
            consistent = True
            if timeout != ("CHILD_TIMEOUT" in reasons):
                consistent = False
            if "CHILD_LAUNCH_ERROR" in reasons and (code is not None or timeout):
                consistent = False
            if "CHILD_UNEXPECTED_EXIT" in reasons and (code is None or code in (0, 1, 4)):
                consistent = False
            if timeout and code is None:
                consistent = False
            if code is None:
                expected = "UNREADABLE"
                consistent = consistent and bool(reasons & {
                    "CHILD_LAUNCH_ERROR", "CHILD_CONTAINMENT_ERROR"})
            elif exceptions:
                expected = "UNREADABLE"
                if not (timeout or reasons & PARSE_OR_LIMIT_REASONS or
                        "CHILD_UNEXPECTED_EXIT" in reasons):
                    consistent = False
            else:
                expected = {0: "PASS", 1: "FAIL", 4: "UNREADABLE"}.get(code)
            if not consistent or verdict != expected:
                error("INCONSISTENT_CHILD", path, checker)
    for checker in CHECKERS:
        if checker not in seen:
            error("MISSING_CHECKER", "$.checks", checker)

    if "provenance" in value:
        provenance = value["provenance"]
        events = set()
        names = set()
        if not isinstance(provenance, list):
            error("INVALID_PROVENANCE", "$.provenance")
            provenance = []
        for i, entry in enumerate(provenance):
            path = "$.provenance[" + str(i) + "]"
            if not isinstance(entry, dict):
                error("INVALID_PROVENANCE", path)
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                error("INVALID_PROVENANCE_NAME", path + ".name")
                continue
            if name in names:
                error("DUPLICATE_PROVENANCE", path + ".name")
            names.add(name)
            hashes = []
            for field in ("sha256_before", "sha256_after"):
                digest = entry.get(field)
                if field not in entry or (digest is not None and (
                        not isinstance(digest, str) or
                        re.fullmatch(r"[0-9a-f]{64}", digest) is None)):
                    error("INVALID_PROVENANCE_HASH", path + "." + field)
                hashes.append(digest)
            before, after = hashes
            reason = ("INPUT_UNREADABLE" if None in hashes else
                      "INPUT_CHANGED" if before != after else None)
            if reason:
                events.add((reason, name))
        recorded = set()
        if isinstance(top, list):
            for i, item in enumerate(top):
                if (isinstance(item, dict) and isinstance(item.get("name"), str) and
                        item["name"] in SOURCE_REASONS):
                    name = item.get("input")
                    if not isinstance(name, str) or (item["name"], name) not in events:
                        error("INVALID_SOURCE_DIAGNOSTIC", "$.diagnostics[" + str(i) + "]")
                    else:
                        recorded.add((item["name"], name))
        for reason, name in sorted(events - recorded):
            error("MISSING_SOURCE_DIAGNOSTIC", "$.provenance")

    expected_parent = ("FAIL" if "FAIL" in verdicts else
                       "UNREADABLE" if "UNREADABLE" in verdicts else "PASS")
    if top_names & SOURCE_REASONS:
        expected_parent = "UNREADABLE"
    elif "OUTPUT_WRITE_ERROR" in top_names and expected_parent == "PASS":
        expected_parent = "UNREADABLE"
    if parent != expected_parent:
        error("INCONSISTENT_PARENT", "$.verdict")
    return _result("FAIL" if errors else "PASS", errors, parent)


def _check_depth(text, max_depth):
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError("JSON depth exceeds limit")
        elif char in "]}":
            depth -= 1


def _reject_constant(value):
    raise ValueError("nonfinite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _bounded_int(value):
    # Independent of the host's optional integer-string conversion limit.
    if len(value.lstrip("-")) > 1024:
        raise ValueError("JSON integer exceeds limit")
    return int(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_validated_report(path, max_bytes=MAX_BYTES, max_depth=MAX_DEPTH):
    """Return (parsed snapshot, validation); read once, at most the cap plus one."""
    if (type(max_bytes) is not int or max_bytes < 0 or
            type(max_depth) is not int or max_depth < 1):
        return None, _result("UNREADABLE", [dict(name="INVALID_LIMIT", path="$")])
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(min(max_bytes, MAX_BYTES) + 1)
    except (OSError, ValueError, TypeError, MemoryError, OverflowError):
        return None, _result("UNREADABLE", [dict(name="INPUT_UNREADABLE", path="$")])
    if len(raw) > min(max_bytes, MAX_BYTES):
        return None, _result("UNREADABLE", [dict(name="INPUT_BYTE_LIMIT", path="$")])
    try:
        text = raw.decode("utf-8")
        _check_depth(text, min(max_depth, MAX_DEPTH))
        value = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float,
                           parse_int=_bounded_int,
                           object_pairs_hook=_unique_object)
        validation = validate_report(value)
    except (UnicodeError, ValueError, RecursionError, OverflowError, MemoryError):
        return None, _result("UNREADABLE", [dict(name="INVALID_JSON", path="$")])
    return value, validation


def validate_file(path, max_bytes=MAX_BYTES, max_depth=MAX_DEPTH):
    """Read at most the byte cap plus one; never rewrite the source file."""
    return read_validated_report(path, max_bytes, max_depth)[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ValidatorTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.input is None:
        parser.error("INPUT.json is required unless --self-test is used")
    result = validate_file(args.input)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[result["verdict"]]


class ValidatorTests(unittest.TestCase):
    def report(self, verdict="PASS"):
        rows = [dict(checker=name, verdict="PASS", exit_code=0, timeout=False,
                     duration_seconds=0.25, diagnostics=[dict(name="CHILD_OUTPUT")],
                     output="OK", output_truncated=False) for name in CHECKERS]
        rows[0].update(verdict=verdict, exit_code={"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[verdict])
        return dict(schema=REPORT_SCHEMA, evidence_kind="synthetic_offline",
                    live_grade=False, verdict=verdict, checks=rows)

    def assert_valid(self, report):
        result = validate_report(report)
        self.assertEqual(result["verdict"], "PASS", result)
        self.assertTrue(result["valid"])
        self.assertEqual(result["schema"], SCHEMA)
        self.assertIs(result["live_grade"], False)

    def assert_invalid(self, report, name=None):
        result = validate_report(report)
        self.assertEqual(result["verdict"], "FAIL", result)
        if name:
            self.assertIn(name, [item["name"] for item in result["diagnostics"]])

    def provenance(self, report, before="a" * 64, after="a" * 64):
        report["provenance"] = [dict(name="vector:fixture.json", sha256_before=before,
                                     sha256_after=after)]

    def test_valid_observed_verdicts_and_no_mutation(self):
        for verdict in ("PASS", "FAIL", "UNREADABLE"):
            report = self.report(verdict)
            snapshot = copy.deepcopy(report)
            self.assert_valid(report)
            self.assertEqual(report, snapshot)

    def test_checker_membership(self):
        for mode, diagnostic in (("duplicate", "DUPLICATE_CHECKER"),
                                 ("missing", "MISSING_CHECKER"),
                                 ("unknown", "UNKNOWN_CHECKER")):
            report = self.report()
            if mode == "duplicate":
                report["checks"][1]["checker"] = CHECKERS[0]
            elif mode == "missing":
                report["checks"].pop()
            else:
                report["checks"][0]["checker"] = "unknown.py"
            self.assert_invalid(report, diagnostic)

    def test_schema_and_synthetic_claims(self):
        for field, bad in (("schema", "leaf.xbench.grade.v1"),
                           ("evidence_kind", "live"), ("live_grade", True),
                           ("live_grade", 0)):
            report = self.report()
            report[field] = bad
            self.assert_invalid(report)
        for bad in (True, 0, None, "false"):
            report = self.report()
            report["checks"][0]["diagnostics"][0]["nested"] = {"live_grade": bad}
            self.assert_invalid(report, "LIVE_GRADE_CLAIM")

    def test_parent_child_contradictions(self):
        report = self.report("FAIL")
        report["verdict"] = "PASS"
        self.assert_invalid(report, "INCONSISTENT_PARENT")
        for code in (0, 1, 4, 7, None):
            report = self.report()
            report["checks"][0].update(exit_code=code, verdict="UNREADABLE")
            report["verdict"] = "UNREADABLE"
            report["checks"][0]["diagnostics"] = [dict(name="UNKNOWN_EXCEPTION")]
            if code == 4:
                self.assert_valid(report)
            else:
                self.assert_invalid(report, "INCONSISTENT_CHILD")

    def test_exception_policies(self):
        for reason in sorted(ROW_REASONS):
            with self.subTest(reason=reason):
                report = self.report("UNREADABLE")
                row = report["checks"][0]
                code = None if reason == "CHILD_LAUNCH_ERROR" else 7 if reason == "CHILD_UNEXPECTED_EXIT" else 1
                row.update(exit_code=code, timeout=reason == "CHILD_TIMEOUT",
                           diagnostics=[dict(name=reason)])
                self.assert_valid(report)
                row["verdict"] = "FAIL"
                report["verdict"] = "FAIL"
                self.assert_invalid(report, "INCONSISTENT_CHILD")
        for reason in ("CHILD_TIMEOUT", "CHILD_LAUNCH_ERROR", "CHILD_UNEXPECTED_EXIT"):
            report = self.report("UNREADABLE")
            report["checks"][0].update(exit_code=0, diagnostics=[dict(name=reason)])
            self.assert_invalid(report, "INCONSISTENT_CHILD")
        report = self.report("UNREADABLE")
        report["checks"][0]["timeout"] = True
        self.assert_invalid(report, "INCONSISTENT_CHILD")

    def test_containment_failure_without_spawn(self):
        report = self.report("UNREADABLE")
        report["checks"][0].update(exit_code=None, diagnostics=[dict(name="CHILD_CONTAINMENT_ERROR")])
        self.assert_valid(report)
        report["checks"][0]["timeout"] = True
        self.assert_invalid(report, "INCONSISTENT_CHILD")

    def test_types_and_nonfinite(self):
        for field, bad in (("exit_code", True), ("duration_seconds", False),
                           ("duration_seconds", -1), ("duration_seconds", float("nan")),
                           ("duration_seconds", float("inf")), ("timeout", 0),
                           ("output", []), ("output_truncated", 1),
                           ("diagnostics", ["bad"]), ("diagnostics", [{}])):
            report = self.report()
            report["checks"][0][field] = bad
            self.assert_invalid(report)
        report = self.report()
        del report["checks"][0]["exit_code"]
        self.assert_invalid(report, "INVALID_EXIT_CODE")

    def test_retained_limits(self):
        for field, bad in (("diagnostics", [dict(name="X")] * 65),
                           ("diagnostics", [dict(name="X", text="x" * 16384)]),
                           ("output", "x" * (TEXT_LIMIT + 1))):
            report = self.report()
            report["checks"][0][field] = bad
            self.assert_invalid(report)

    def test_malformed_containers(self):
        for value in (None, [], 1, "report"):
            self.assert_invalid(value, "REPORT_NOT_OBJECT")
        for field, bad in (("checks", None), ("checks", [None, {}, []]),
                           ("verdict", []), ("diagnostics", [dict(name=[])]),
                           ("provenance", [None]), ("provenance", None)):
            report = self.report()
            report[field] = bad
            self.assert_invalid(report)
        report = self.report()
        self.provenance(report)
        report["diagnostics"] = [dict(name=[])]
        self.assert_invalid(report, "INVALID_DIAGNOSTIC")

    def test_provenance_hashes_and_names(self):
        report = self.report()
        self.provenance(report)
        self.assert_valid(report)
        for bad in ("A" * 64, "a" * 63, True, 12):
            self.provenance(report, bad)
            self.assert_invalid(report, "INVALID_PROVENANCE_HASH")
        self.provenance(report)
        report["provenance"].append(copy.deepcopy(report["provenance"][0]))
        self.assert_invalid(report, "DUPLICATE_PROVENANCE")

    def test_provenance_overrides_fail_and_requires_diagnostic(self):
        for before, after, reason in (("a" * 64, "b" * 64, "INPUT_CHANGED"),
                                      (None, "a" * 64, "INPUT_UNREADABLE"),
                                      (None, None, "INPUT_UNREADABLE")):
            report = self.report("FAIL")
            self.provenance(report, before, after)
            self.assert_invalid(report, "MISSING_SOURCE_DIAGNOSTIC")
            report["diagnostics"] = [dict(name=reason, input="vector:fixture.json")]
            self.assert_invalid(report, "INCONSISTENT_PARENT")
            report["verdict"] = "UNREADABLE"
            self.assert_valid(report)
            report["diagnostics"][0]["input"] = "wrong"
            self.assert_invalid(report, "INVALID_SOURCE_DIAGNOSTIC")

    def test_atomic_output_error_parent_policy(self):
        for verdict in ("PASS", "FAIL", "UNREADABLE"):
            report = self.report(verdict)
            report["diagnostics"] = [dict(name="OUTPUT_WRITE_ERROR")]
            if verdict == "PASS":
                self.assert_invalid(report, "INCONSISTENT_PARENT")
                report["verdict"] = "UNREADABLE"
            self.assert_valid(report)

    def test_file_controls_and_unchanged_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            self.assertEqual(validate_file(path)["verdict"], "UNREADABLE")
            raw = json.dumps(self.report("FAIL")).encode("utf-8")
            path.write_bytes(raw)
            self.assertEqual(validate_file(path)["verdict"], "PASS")
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(validate_file(path, max_bytes=10)["verdict"], "UNREADABLE")
            for raw in (b"{bad", b"\xff", b'{"a":NaN}', b'{"a":1e999}',
                        b'{"a":1,"a":2}', b'{"diagnostics":[],"diagnostics":[]}',
                        b'{"diagnostics":[{"name":"a","name":"b"}]}',
                        b"[" * 40 + b"0" + b"]" * 40):
                path.write_bytes(raw)
                self.assertEqual(validate_file(path)["verdict"], "UNREADABLE", raw)
            path.write_bytes(b"[[[0]]]")
            self.assertEqual(validate_file(path, max_depth=2)["verdict"], "UNREADABLE")
            path.write_bytes(json.dumps(self.report()).encode("utf-8"))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())["schema"], SCHEMA)
            path.write_bytes(b"{}")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(path)]), 1)
                self.assertEqual(main([str(path) + ".missing"]), 4)

    def test_depth_in_memory_and_string_braces(self):
        report = self.report()
        nested = []
        report["extra"] = nested
        for _ in range(MAX_DEPTH + 1):
            child = []
            nested.append(child)
            nested = child
        self.assert_invalid(report, "DEPTH_LIMIT")
        _check_depth(json.dumps({"text": '["\\' * 100}), 2)


if __name__ == "__main__":
    sys.exit(main())
