"""Bounded raw-wire probes for the read-only synthetic report validator."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import validate_offline_report as validator


class ValidatorWireBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="x01 wire ü ")
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "report λ.json"
        self.report = validator.ValidatorTests().report("FAIL")

    def raw_report(self, **extra):
        return json.dumps(dict(self.report, **extra), ensure_ascii=False).encode("utf-8")

    def probe(self, raw, verdict, name=None, cli=False, **limits):
        self.assertLessEqual(len(raw), validator.MAX_BYTES + 1)
        self.path.write_bytes(raw)
        result = validator.validate_file(self.path, **limits)
        self.assertEqual(result["verdict"], verdict, result)
        if name:
            self.assertIn(name, [item["name"] for item in result["diagnostics"]])
        self.assertLessEqual(len(json.dumps(result)), 65536)
        self.assertEqual(self.path.read_bytes(), raw)
        if cli:
            child = subprocess.run(
                [sys.executable, "-B", str(Path(validator.__file__)), str(self.path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
            self.assertEqual(child.returncode, {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[verdict])
            self.assertEqual(child.stderr, b"")
            self.assertLessEqual(len(child.stdout), 65536)
            parsed = json.loads(child.stdout)
            self.assertEqual(parsed["verdict"], verdict)
            if name:
                self.assertIn(name, [item["name"] for item in parsed["diagnostics"]])
            self.assertEqual(self.path.read_bytes(), raw)
        return result

    def test_byte_below_at_over_and_hard_cap(self):
        raw = self.raw_report()
        cap = len(raw) + 8
        for size in (cap - 1, cap, cap + 1):
            self.probe(raw + b" " * (size - len(raw)),
                       "UNREADABLE" if size > cap else "PASS",
                       "INPUT_BYTE_LIMIT" if size > cap else None, max_bytes=cap)
        for size in (validator.MAX_BYTES - 1, validator.MAX_BYTES, validator.MAX_BYTES + 1):
            self.probe(raw + b" " * (size - len(raw)),
                       "UNREADABLE" if size > validator.MAX_BYTES else "PASS",
                       "INPUT_BYTE_LIMIT" if size > validator.MAX_BYTES else None,
                       max_bytes=validator.MAX_BYTES * 2)

    def test_depth_below_at_over_and_hard_cap(self):
        base = self.raw_report()
        for cap in (8, validator.MAX_DEPTH):
            for depth in (cap - 1, cap, cap + 1):
                # Root object contributes one lexical container level.
                raw = base[:-1] + b',"extension":' + b"[" * (depth - 1) + b"0" + b"]" * (depth - 1) + b"}"
                requested = cap if cap == 8 else cap * 2
                self.probe(raw, "UNREADABLE" if depth > cap else "PASS",
                           "INVALID_JSON" if depth > cap else None, max_depth=requested)

    def test_escaped_strings_do_not_count_as_containers(self):
        text = ('[{\\"quoted\\"}]' + '"' + "\\" + "λ") * 100
        self.probe(self.raw_report(extension=text), "PASS", max_depth=8)
        self.report["checks"][0]["output"] = text
        self.probe(self.raw_report(), "PASS", cli=True)

    def test_utf8_and_boms_are_explicitly_rejected(self):
        # Wire contract is UTF-8 without BOM; UTF-16 is never auto-detected.
        raw = self.raw_report(extension="λ")
        self.probe(raw, "PASS")
        for bad in (b"\xff", raw[:-1] + b',"bad":"\xff"}',
                    b"\xef\xbb\xbf" + raw, b"\xff\xfe" + raw.decode().encode("utf-16-le"),
                    b"\xfe\xff" + raw.decode().encode("utf-16-be")):
            self.probe(bad, "UNREADABLE", "INVALID_JSON", cli=True)

    def test_duplicates_trailing_and_truncated_payloads(self):
        raw = self.raw_report()
        for bad in (raw[:-1] + b',"verdict":"FAIL"}',
                    raw[:-1] + b',"extension":{"a":1,"a":2}}',
                    raw + b"{}", raw + b"null", raw[:-1],
                    b'{"checks":[{"checker":"a","checker":"b"}]}'):
            self.probe(bad, "UNREADABLE", "INVALID_JSON", cli=True)
        self.probe(raw + b"\r\n\t ", "PASS")

    def test_huge_integer_wire_and_supported_numeric_fields(self):
        base = self.raw_report()
        # Construct digit strings, never an unlimited Python integer.
        for digits in (1025, 100000):
            raw = base[:-1] + b',"extension":' + b"9" * digits + b"}"
            self.probe(raw, "UNREADABLE", "INVALID_JSON", cli=True)
        bounded = b"9" * 400
        self.probe(base[:-1] + b',"extension":' + bounded + b"}", "PASS", cli=True)
        raw = base.replace(b'"duration_seconds": 0.25', b'"duration_seconds": ' + bounded, 1)
        self.assertNotEqual(raw, base)
        self.probe(raw, "FAIL", "INVALID_DURATION", cli=True)
        raw = base.replace(b'"exit_code": 1', b'"exit_code": ' + bounded, 1)
        self.assertNotEqual(raw, base)
        self.probe(raw, "FAIL", "INCONSISTENT_CHILD", cli=True)
        self.report["checks"][0]["duration_seconds"] = int("9" * 400)
        result = validator.validate_report(self.report)
        self.assertIn("INVALID_DURATION", [item["name"] for item in result["diagnostics"]])

    def test_nonfinite_numbers_anywhere(self):
        raw = self.raw_report()
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"):
            self.probe(raw[:-1] + b',"extension":' + token + b"}",
                       "UNREADABLE", "INVALID_JSON", cli=True)
            self.probe(raw.replace(b'"duration_seconds": 0.25', b'"duration_seconds": ' + token, 1),
                       "UNREADABLE", "INVALID_JSON")

    def test_read_parser_resource_errors_and_invalid_limits(self):
        raw = self.raw_report()
        self.path.write_bytes(raw)
        for error in (OSError, MemoryError, OverflowError):
            with patch.object(Path, "open", side_effect=error):
                result = validator.validate_file(self.path)
            self.assertEqual(result["verdict"], "UNREADABLE")
            self.assertEqual(result["diagnostics"], [dict(name="INPUT_UNREADABLE", path="$")])
            self.assertEqual(self.path.read_bytes(), raw)
        for error in (MemoryError, RecursionError, OverflowError):
            with patch.object(validator.json, "loads", side_effect=error):
                result = validator.validate_file(self.path)
            self.assertEqual(result["diagnostics"], [dict(name="INVALID_JSON", path="$")])
            self.assertEqual(self.path.read_bytes(), raw)
        for limits in (dict(max_bytes=-1), dict(max_bytes=True), dict(max_depth=0),
                       dict(max_depth=True), dict(max_depth="32")):
            self.probe(raw, "UNREADABLE", "INVALID_LIMIT", **limits)
        self.assertEqual(validator.validate_file(self.path.parent)["verdict"], "UNREADABLE")
        self.assertEqual(validator.validate_file(self.path.with_suffix(".missing"))["verdict"], "UNREADABLE")

    def test_named_diagnostics_are_bounded(self):
        raw = self.raw_report()
        self.probe(raw, "PASS", cli=True)
        self.probe(b"{}", "FAIL", "UNKNOWN_SCHEMA", cli=True)
        # Invalid nested claims must not copy a huge attacker-controlled key.
        nested = {"x" * 20000: {"live_grade": True}}
        result = self.probe(self.raw_report(extension=nested), "FAIL", "LIVE_GRADE_CLAIM", cli=True)
        self.assertLessEqual(len(result["diagnostics"][0]["path"]), 256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
