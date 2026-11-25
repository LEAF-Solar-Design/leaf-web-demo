"""Read-only, stdlib source-byte custody check for synthetic offline reports.

The explicit source root is the offline helper directory. Exactly the three
default checker names and three default vector names below are required; legacy
or partial provenance cannot establish custody. PASS means current bytes match
both recorded hashes, not that checker findings are true or live grade. No
checkers are imported or executed. Two bounded reads check stream metadata,
pathname identity, and digest agreement. Sources must remain quiescent: this
detects exercised changes, not every transient restored-byte event or changes
after the final observation, and is not a hostile-filesystem snapshot sandbox.
The result's valid field describes report validation; verdict describes custody,
and observed_verdict preserves the report's own checker aggregate.
"""

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import validate_offline_report as validator


SCHEMA = "leaf.xbench.x01.offline-source-check.v1"
VECTORS = ("fill_coverage_cases.json", "module_geometry_cases.json", "tag_overlap_cases.json")
SOURCES = {**{"checker:" + name: name for name in validator.CHECKERS},
           **{"vector:" + name: name for name in VECTORS}}
HASH_BUFFER = 65536


class SourceUnstableError(OSError):
    """The source changed during the bounded custody read."""


def _source_stamp(stat):
    return tuple(getattr(stat, field, None) for field in (
        "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _source_binding(stat):
    # Windows may expose creation time via stat and modification time via
    # fstat's ctime. Compare ctime only within the same API, never across APIs.
    return tuple(getattr(stat, field, None) for field in (
        "st_dev", "st_ino", "st_size", "st_mtime_ns"))


def _hash_source(path):
    """Hash twice against the original identity; never retry or adopt new bytes.

    Each pass reads at most the initial size plus one byte, even if a writer
    keeps appending. Metadata and the second digest detect persistent changes
    even to already-read bytes when mtime is restored. An adversary restoring
    bytes and metadata between observations can evade these finite checks.
    """
    def check_path(expected):
        try:
            current = _source_stamp(path.stat())
        except OSError as exc:
            raise SourceUnstableError("SOURCE_UNSTABLE: " + str(path)) from exc
        if current != expected:
            raise SourceUnstableError("SOURCE_UNSTABLE: " + str(path))

    with path.open("rb") as stream:
        initial = os.fstat(stream.fileno())
        stamp = _source_stamp(initial)
        try:
            initial_path = path.stat()
        except OSError as exc:
            raise SourceUnstableError("SOURCE_UNSTABLE: " + str(path)) from exc
        if _source_binding(initial_path) != _source_binding(initial):
            raise SourceUnstableError("SOURCE_UNSTABLE: " + str(path))
        path_stamp = _source_stamp(initial_path)
        digests = []
        for pass_number in range(2):
            if pass_number:
                stream.seek(0)
            digest = hashlib.sha256()
            remaining = initial.st_size
            while remaining:
                chunk = stream.read(min(HASH_BUFFER, remaining))
                if not chunk:
                    raise SourceUnstableError("SOURCE_UNSTABLE: " + str(path))
                digest.update(chunk)
                remaining -= len(chunk)
            if stream.read(1) or _source_stamp(os.fstat(stream.fileno())) != stamp:
                raise SourceUnstableError("SOURCE_UNSTABLE: " + str(path))
            check_path(path_stamp)
            digests.append(digest.hexdigest())
            if pass_number and digests[0] != digests[1]:
                raise SourceUnstableError("SOURCE_UNSTABLE: " + str(path))
    check_path(path_stamp)
    return digests[0]


def verify_report_sources(report_path, source_root):
    """Return separate custody and report-validity results without changing inputs."""
    report, validation = validator.read_validated_report(report_path)
    result = dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                  verdict="UNREADABLE", valid=validation["valid"],
                  report_validation=validation, diagnostics=[], sources=[])
    if "observed_verdict" in validation:
        result["observed_verdict"] = validation["observed_verdict"]

    def unreadable(reason, source=None):
        diagnostic = dict(name="UNREADABLE", reason=reason)
        if source is not None:
            diagnostic["input"] = source
        result["diagnostics"].append(diagnostic)
        return result

    if not validation["valid"]:
        return unreadable("INVALID_REPORT")
    provenance = report.get("provenance")
    if not provenance:
        return unreadable("MISSING_PROVENANCE")
    # Membership in this exact allowlist rejects all POSIX/Windows absolute,
    # drive-relative, traversal, alternate separator, and unknown-prefix names.
    if {entry["name"] for entry in provenance} != set(SOURCES):
        return unreadable("INVALID_SOURCE_NAMES")
    try:
        if source_root is None:
            return unreadable("INVALID_SOURCE_ROOT")
        root = Path(source_root).resolve(strict=True)
        if not root.is_dir():
            return unreadable("INVALID_SOURCE_ROOT")
        paths = {}
        # Complete resolution of every name BEFORE opening any source content.
        for name, filename in SOURCES.items():
            path = (root / filename).resolve(strict=True)
            if not path.is_relative_to(root):
                return unreadable("SOURCE_OUTSIDE_ROOT", name)
            if not path.is_file():
                return unreadable("SOURCE_NOT_FILE", name)
            paths[name] = path
    except (OSError, ValueError, TypeError, RuntimeError, MemoryError, OverflowError):
        return unreadable("SOURCE_RESOLUTION_ERROR")
    for entry in provenance:
        name = entry["name"]
        source = dict(input=name, **entry, sha256_current=None)
        source[name.split(":", 1)[0]] = SOURCES[name]
        result["sources"].append(source)
        try:
            source["sha256_current"] = _hash_source(paths[name])
        except SourceUnstableError:
            unreadable("SOURCE_UNSTABLE", name)
            continue
        except (OSError, ValueError, MemoryError):
            unreadable("SOURCE_READ_ERROR", name)
            continue
        if entry["sha256_before"] is None or entry["sha256_after"] is None:
            unreadable("MISSING_RECORDED_HASH", name)
        elif not (entry["sha256_before"] == entry["sha256_after"] == source["sha256_current"]):
            result["diagnostics"].append(dict(
                name="SOURCE_CHANGED", **{key: value for key, value in source.items()
                                          if key != "name"}))
    if any(item["name"] == "UNREADABLE" for item in result["diagnostics"]):
        result["verdict"] = "UNREADABLE"
    else:
        result["verdict"] = "FAIL" if result["diagnostics"] else "PASS"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SourceCustodyTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    result = verify_report_sources(args.input, args.source_root)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[result["verdict"]]


class SourceCustodyTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="x01 custody ü ")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "source copies λ"
        self.root.mkdir()
        self.report = validator.ValidatorTests().report("FAIL")
        self.report["provenance"] = []
        for name, filename in SOURCES.items():
            raw = Path(__file__).with_name(filename).read_bytes()
            (self.root / filename).write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            self.report["provenance"].append(dict(
                name=name, sha256_before=digest, sha256_after=digest))
        self.path = Path(self.scratch.name) / "report λ.json"
        self.unrelated = self.root / "unrelated.txt"
        self.unrelated.write_bytes(b"unrelated synthetic sentinel")

    def check(self, verdict):
        raw = json.dumps(self.report).encode("utf-8")
        self.path.write_bytes(raw)
        snapshot = {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        with patch("subprocess.Popen", side_effect=AssertionError("child launch")), \
                patch("os.system", side_effect=AssertionError("shell launch")):
            result = verify_report_sources(self.path, self.root)
        self.assertEqual(result["verdict"], verdict, result)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), hashlib.sha256(raw).digest())
        for path, original in snapshot.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertIs(result["live_grade"], False)
        self.assertEqual(result["schema"], SCHEMA)
        return result

    def test_matching_all_six_and_cli(self):
        result = self.check("PASS")
        self.assertTrue(result["valid"])
        self.assertEqual(result["observed_verdict"], "FAIL")
        self.assertEqual(len(result["sources"]), 6)
        for code in (0, 1, 4):
            if code == 1:
                (self.root / VECTORS[0]).write_bytes(b"changed synthetic vector")
            if code == 4:
                (self.root / VECTORS[0]).unlink()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([str(self.path), "--source-root", str(self.root)]), code)
            self.assertEqual(json.loads(output.getvalue())["schema"], SCHEMA)

    def test_changed_current_and_stale_report(self):
        (self.root / VECTORS[0]).write_bytes(b"new fixture generation")
        result = self.check("FAIL")
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["name"], "SOURCE_CHANGED")
        self.assertEqual(diagnostic["vector"], VECTORS[0])
        self.assertNotEqual(diagnostic["sha256_before"], diagnostic["sha256_current"])
        self.assertEqual(diagnostic["sha256_before"], diagnostic["sha256_after"])

    def test_changed_checker_retains_identity(self):
        checker = validator.CHECKERS[0]
        (self.root / checker).write_bytes(b"raise AssertionError('must never execute')\n")
        result = self.check("FAIL")
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["name"], "SOURCE_CHANGED")
        self.assertEqual(diagnostic["checker"], checker)
        self.assertEqual(diagnostic["input"], "checker:" + checker)
        self.assertEqual(diagnostic["sha256_after"], diagnostic["sha256_before"])

    def test_recorded_mutation_cannot_pass(self):
        entry = self.report["provenance"][0]
        entry["sha256_before"] = "0" * 64
        self.report.update(verdict="UNREADABLE", diagnostics=[dict(name="INPUT_CHANGED", input=entry["name"])])
        self.assertTrue(self.check("FAIL")["valid"])

    def test_missing_read_error_and_recorded_unreadable(self):
        with patch.object(sys.modules[__name__], "_hash_source", side_effect=PermissionError):
            self.assertEqual(self.check("UNREADABLE")["diagnostics"][0]["reason"], "SOURCE_READ_ERROR")
        entry = self.report["provenance"][0]
        entry["sha256_before"] = None
        self.report.update(verdict="UNREADABLE", diagnostics=[dict(name="INPUT_UNREADABLE", input=entry["name"])])
        self.check("UNREADABLE")
        (self.root / VECTORS[0]).unlink()
        self.check("UNREADABLE")

    def test_no_partial_empty_or_legacy_custody(self):
        original = copy.deepcopy(self.report)
        for provenance in (None, [], original["provenance"][:5]):
            self.report = copy.deepcopy(original)
            if provenance is None:
                del self.report["provenance"]
            else:
                self.report["provenance"] = provenance
            self.check("UNREADABLE")

    def test_unsafe_names_preflight_without_hashing(self):
        for name in ("unknown:x", "checker:/tmp/x", "vector:../x", "vector:a/../x",
                     "vector:..\\x", "checker:C:\\x", "checker:C:x", "vector:\\\\host\\x",
                     "/tmp/x", "vector:sub/file.json"):
            self.report["provenance"][-1]["name"] = name
            with patch.object(sys.modules[__name__], "_hash_source", side_effect=AssertionError("hash before preflight")):
                self.check("UNREADABLE")

    def test_symlink_escape_and_invalid_root_or_report(self):
        outside = Path(self.scratch.name) / "outside synthetic.txt"
        outside.write_bytes(b"outside sentinel")
        target = self.root / VECTORS[-1]
        # Deterministic resolution seam works on hosts without symlink rights.
        resolve = Path.resolve

        def escape(path, **kwargs):
            return outside if path == target else resolve(path, **kwargs)

        with patch.object(Path, "resolve", escape), \
                patch.object(sys.modules[__name__], "_hash_source", side_effect=AssertionError("escaped read")):
            result = self.check("UNREADABLE")
        self.assertEqual(result["diagnostics"][0]["reason"], "SOURCE_OUTSIDE_ROOT")
        self.assertEqual(outside.read_bytes(), b"outside sentinel")
        for root in (None, self.path, self.root / "missing", []):
            self.assertEqual(verify_report_sources(self.path, root)["verdict"], "UNREADABLE")
        self.path.write_bytes(b"{}")
        self.assertFalse(verify_report_sources(self.path, self.root)["valid"])
        self.assertEqual(verify_report_sources(None, self.root)["verdict"], "UNREADABLE")


if __name__ == "__main__":
    sys.exit(main())
