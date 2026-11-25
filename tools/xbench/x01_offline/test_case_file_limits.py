"""Bounded stdlib scratch probes; committed synthetic vectors are read-only."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import check_cases as checker


ROOT = Path(__file__).resolve().parent
FILES = ("module_geometry_cases.json", "fill_coverage_cases.json", "tag_overlap_cases.json")


class CaseFileLimitTests(unittest.TestCase):
    def setUp(self):
        self.original = {name: (ROOT / name).read_bytes() for name in FILES}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, raw in self.original.items():
            (self.root / name).write_bytes(raw)

    def unchanged(self):
        for name, raw in self.original.items():
            self.assertEqual((ROOT / name).read_bytes(), raw, name)

    def tearDown(self):
        self.unchanged()

    def write(self, raw, family=0):
        (self.root / FILES[family]).write_bytes(raw)
        self.unchanged()

    def child(self, limits=None):
        # Patch only the imported checker in an isolated child. No CLI limit
        # switches or environment-dependent defaults are needed by production.
        code = ("import json,sys; sys.path.insert(0,sys.argv[1]); "
                "import check_cases as c; "
                "c.__dict__.update(json.loads(sys.argv[3])); "
                "sys.argv=['check_cases','--cases-dir',sys.argv[2]]; "
                "sys.exit(c.main())")
        child = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT),
                                str(self.root), json.dumps(limits or {})],
                               capture_output=True, text=True, timeout=10)
        self.unchanged()
        self.assertNotIn("Traceback", child.stdout + child.stderr)
        return child

    def file_error(self, reason, limits=None, family=0):
        expected = dict(name="UNREADABLE_CASE_SET", filename=FILES[family], reason=reason)
        with mock.patch.dict(checker.__dict__, limits or {}):
            with self.assertRaises(checker.CaseFileError) as caught:
                checker.load_sets(self.root)
        self.assertEqual(caught.exception.diagnostic, expected)
        child = self.child(limits)
        self.assertEqual(child.returncode, 4, child.stdout + child.stderr)
        self.assertEqual(json.loads(child.stdout),
                         dict(verdict="UNREADABLE", diagnostics=[expected]))

    def accepted(self, limits):
        with mock.patch.multiple(checker, **limits):
            for cases, calculate in checker.load_sets(self.root):
                for case in cases:
                    checker.check_case(case, calculate)
        child = self.child(limits)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertIn("2 negative controls; PASS", child.stdout)

    def test_original_26_and_two_controls(self):
        sets = checker.load_sets(self.root)
        self.assertEqual(sum(len(cases) for cases, _ in sets), 26)
        checker.rejection_checks(sets)
        self.accepted({"MAX_CASE_FILE_BYTES": checker.MAX_CASE_FILE_BYTES})

    def test_byte_boundary_below_exact_above(self):
        cap = max(map(len, self.original.values())) + 128
        limits = dict(MAX_CASE_FILE_BYTES=cap)
        raw = self.original[FILES[0]]
        for size in (cap - 1, cap, cap + 1):
            with self.subTest(size=size):
                # JSON whitespace is valid padding, including at the exact cap.
                self.write(raw + b" " * (size - len(raw)))
                if size <= cap:
                    self.accepted(limits)
                else:
                    self.file_error("FILE_TOO_LARGE", limits)

    def test_read_is_cap_plus_one_and_size_precedes_decode(self):
        limits = dict(MAX_CASE_FILE_BYTES=16)
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = b"\xff" * 17
        with mock.patch.multiple(checker, **limits), mock.patch.object(
                Path, "open", return_value=stream):
            with self.assertRaises(checker.CaseFileError) as caught:
                checker.load_case_file(self.root / FILES[0])
        stream.read.assert_called_once_with(17)
        self.assertEqual(caught.exception.diagnostic["reason"], "FILE_TOO_LARGE")

    def test_depth_boundary_and_quoted_escapes(self):
        cap = 12  # Above all authored vector depths; still small scratch data.
        data = json.loads(self.original[FILES[0]])
        for depth in (cap - 1, cap, cap + 1):
            with self.subTest(depth=depth):
                nested = 0
                for _ in range(depth - 1):
                    nested = [nested]
                data["scratch"] = nested
                data["quoted"] = ('[{}]"\\' * 40)
                self.write(json.dumps(data).encode("utf-8"))
                limits = dict(MAX_JSON_DEPTH=cap)
                if depth <= cap:
                    self.accepted(limits)
                else:
                    self.file_error("JSON_TOO_DEEP", limits)

    def test_depth_rejected_before_recursive_parser(self):
        self.write(b"[" * 1000 + b"0" + b"]" * 1000)
        with mock.patch.object(checker.json, "loads", side_effect=AssertionError("decoded")):
            with self.assertRaises(checker.CaseFileError) as caught:
                checker.load_sets(self.root)
        self.assertEqual(caught.exception.diagnostic["reason"], "JSON_TOO_DEEP")
        self.file_error("JSON_TOO_DEEP")

    def test_case_count_boundary_unique_valid_cases(self):
        cap = 12
        source = json.loads(self.original[FILES[0]])
        for count in (cap - 1, cap, cap + 1):
            with self.subTest(count=count):
                data = copy.deepcopy(source)
                data["cases"] = []
                for index in range(count):
                    case = copy.deepcopy(source["cases"][0])
                    case["id"] = f"scratch-{index}"
                    data["cases"].append(case)
                self.write(json.dumps(data).encode("utf-8"))
                limits = dict(MAX_CASES_PER_SET=cap)
                if count <= cap:
                    self.accepted(limits)
                else:
                    self.file_error("TOO_MANY_CASES", limits)
        # Resource rejection must precede semantic validation of any element.
        self.write(json.dumps(dict(cases=[None] * (cap + 1))).encode())
        self.file_error("TOO_MANY_CASES", dict(MAX_CASES_PER_SET=cap))

    def test_duplicate_keys_at_top_and_nested_depths(self):
        for raw in (b'{"schema":1,"schema":2}',
                    b'{"scratch":{"x":1,"x":2}}',
                    b'{"scratch":[{"x":1,"\\u0078":2}]}'):
            with self.subTest(raw=raw):
                self.write(raw)
                self.file_error("DUPLICATE_JSON_KEY")

    def test_unicode_bom_malformed_and_trailing_payload(self):
        for raw, reason in ((b"\xff", "INVALID_UTF8"),
                            (b'"\xed\xa0\x80"', "INVALID_UTF8"),
                            (b"\xef\xbb\xbf{}", "UTF8_BOM"),
                            (b"{", "MALFORMED_JSON"),
                            (b"{} {}", "MALFORMED_JSON")):
            with self.subTest(raw=raw):
                self.write(raw)
                self.file_error(reason)

    def test_nonfinite_and_oversized_numbers(self):
        for token, reason in (("NaN", "NONFINITE_JSON_NUMBER"),
                              ("Infinity", "NONFINITE_JSON_NUMBER"),
                              ("-Infinity", "NONFINITE_JSON_NUMBER"),
                              ("1e9999", "NONFINITE_JSON_NUMBER"),
                              ("9" * 309, "JSON_INTEGER_TOO_LARGE"),
                              ("1" * 5000, "JSON_INTEGER_TOO_LARGE")):
            with self.subTest(token=token[:20]):
                self.write(('{"scratch":' + token + '}').encode())
                self.file_error(reason)

    def test_missing_file_and_directory_read_failures(self):
        path = self.root / FILES[0]
        path.unlink()
        self.unchanged()
        self.file_error("READ_ERROR")
        path.mkdir()
        self.unchanged()
        self.file_error("READ_ERROR")

    def test_valid_shaped_wrong_expectation_is_fail_one(self):
        data = json.loads(self.original[FILES[0]])
        data["cases"][0]["expected"]["counts"]["matched"] = 99
        self.write(json.dumps(data).encode())
        checker.load_sets(self.root)
        child = self.child()
        self.assertEqual(child.returncode, 1, child.stdout + child.stderr)
        report = json.loads(child.stdout)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(report["diagnostics"][0]["name"], "CASE_CHECK_FAILED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
