"""Read-only composition of saved synthetic report validity, custody and comparison.

Both reports and both explicit local source roots must remain quiescent across
all helper calls. This is not an atomic multi-file snapshot. No checker is
imported or executed, no stale source is adopted, and no live grade is asserted.
"""

import argparse
import builtins
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import compare_offline_reports as comparison
import validate_offline_report as validator
import verify_report_sources as custody


SCHEMA = "leaf.xbench.x01.offline-evidence-bundle.v1"
EXIT_CODES = {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}


def inspect_evidence_bundle(old_report, new_report, old_source_root, new_source_root):
    """Compose bounded existing readers; observed FAIL does not fail inspection."""
    reports = {}
    diagnostics = []
    for side, report, root in (("old", old_report, old_source_root),
                               ("new", new_report, new_source_root)):
        checked = custody.verify_report_sources(report, root)
        validation = checked["report_validation"]
        reports[side] = dict(validity=validation, custody=checked,
                             observed_verdict=checked.get("observed_verdict"))
        for stage, items in (("validity", validation["diagnostics"]),
                             ("custody", checked["diagnostics"])):
            for item in items:
                report_problem = stage == "validity" or item.get("reason") in {
                    "INVALID_REPORT", "MISSING_PROVENANCE", "INVALID_SOURCE_NAMES",
                    "MISSING_RECORDED_HASH"}
                diagnostics.append(dict(item, side=side, stage=stage,
                                        argument=side + ("_report" if report_problem
                                                         else "_source_root")))
    compared = comparison.compare_reports(old_report, new_report)
    for item in compared["diagnostics"]:
        diagnostics.append(dict(item, stage="comparison",
                                argument=item["side"] + "_report"))
    valid = (all(row["validity"]["valid"] for row in reports.values())
             and compared["valid"])
    verdicts = [row["custody"]["verdict"] for row in reports.values()]
    verdicts.append(compared["verdict"])
    if not valid or any(value not in ("PASS", "FAIL") for value in verdicts):
        verdict = "UNREADABLE"
    else:
        verdict = "FAIL" if "FAIL" in verdicts else "PASS"
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                verdict=verdict, valid=valid, reports=reports, comparison=compared,
                diagnostics=diagnostics)


class ArgumentError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("old_report", nargs="?", type=Path)
    parser.add_argument("new_report", nargs="?", type=Path)
    parser.add_argument("--old-source-root", type=Path)
    parser.add_argument("--new-source-root", type=Path)
    parser.add_argument("--self-test", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.self_test:
            if any(value is not None for value in (args.old_report, args.new_report,
                                                   args.old_source_root, args.new_source_root)):
                raise ArgumentError("--self-test cannot be combined with bundle inputs")
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(BundleTests)
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        missing = [name for name in ("old_report", "new_report", "old_source_root",
                                    "new_source_root") if getattr(args, name) is None]
        if missing:
            raise ArgumentError("required arguments: " + ", ".join(missing))
    except ArgumentError as exc:
        result = dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                      verdict="UNREADABLE", valid=False, reports={}, comparison=None,
                      diagnostics=[dict(name="ARGUMENT_ERROR", reason=str(exc))])
    else:
        result = inspect_evidence_bundle(args.old_report, args.new_report,
                                         args.old_source_root, args.new_source_root)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return EXIT_CODES[result["verdict"]]


class BundleTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="x01 bundle ü ")
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.roots = [self.base / (side + " sources λ") for side in ("old", "new")]
        self.paths = [self.base / (side + " report λ.json") for side in ("old", "new")]
        self.reports = []
        for index, root in enumerate(self.roots):
            root.mkdir()
            rows = [dict(checker=name, verdict="PASS", exit_code=0, timeout=False,
                         duration_seconds=0.25, diagnostics=[], output="OK",
                         output_truncated=False) for name in validator.CHECKERS]
            report = dict(schema=validator.REPORT_SCHEMA, evidence_kind="synthetic_offline",
                          live_grade=False, verdict="PASS", checks=rows, diagnostics=[],
                          provenance=[])
            for name, filename in custody.SOURCES.items():
                # Poison checker fixtures are hashed as data, never imported.
                raw = (b"raise AssertionError('checker executed')\n" if filename.endswith(".py")
                       else b"synthetic vector\n")
                (root / filename).write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                report["provenance"].append(dict(name=name, sha256_before=digest,
                                                sha256_after=digest))
            self.reports.append(report)
            self.save(index)

    def save(self, index):
        self.paths[index].write_bytes(json.dumps(self.reports[index]).encode("utf-8"))

    def probe(self, verdict="PASS"):
        snapshots = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in self.base.rglob("*") if path.is_file()}
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.rsplit(".", 1)[-1] in {
                    *(Path(item).stem for item in validator.CHECKERS), "run_offline_checks"}:
                raise AssertionError("forbidden checker/runner import: " + name)
            return original_import(name, *args, **kwargs)

        with patch("subprocess.Popen", side_effect=AssertionError("child launch")), \
                patch("os.system", side_effect=AssertionError("shell launch")), \
                patch("builtins.__import__", side_effect=guarded_import):
            result = inspect_evidence_bundle(*self.paths, *self.roots)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main([str(path) for path in self.paths] + [
                    "--old-source-root", str(self.roots[0]),
                    "--new-source-root", str(self.roots[1])])
            self.assertEqual(code, EXIT_CODES[verdict])
            self.assertEqual(json.loads(output.getvalue()), result)
        self.assertEqual(result["verdict"], verdict, result)
        self.assertEqual(result["schema"], SCHEMA)
        self.assertIs(result["live_grade"], False)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertEqual(snapshots, {path: hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in self.base.rglob("*") if path.is_file()})
        return result

    def test_fresh_and_separate_roots(self):
        self.assertNotEqual(self.roots[0], self.roots[1])
        result = self.probe()
        for row in result["reports"].values():
            self.assertTrue(row["validity"]["valid"])
            self.assertEqual(row["custody"]["verdict"], "PASS")
            self.assertEqual(len(row["custody"]["sources"]), 6)

    def test_observed_fail_is_valid_and_current(self):
        self.reports[1]["verdict"] = "FAIL"
        self.reports[1]["checks"][0].update(verdict="FAIL", exit_code=1)
        self.save(1)
        result = self.probe()
        self.assertTrue(result["valid"])
        self.assertEqual(result["reports"]["new"]["observed_verdict"], "FAIL")
        self.assertEqual(result["comparison"]["observed_verdicts"], dict(old="PASS", new="FAIL"))

    def test_stale_named_hash_failure(self):
        (self.roots[1] / custody.VECTORS[0]).write_bytes(b"changed\n")
        result = self.probe("FAIL")
        self.assertEqual(result["comparison"]["verdict"], "PASS")
        self.assertTrue(result["valid"])
        diagnostic = result["reports"]["new"]["custody"]["diagnostics"][0]
        self.assertEqual(diagnostic["name"], "SOURCE_CHANGED")
        self.assertEqual(diagnostic["input"], "vector:" + custody.VECTORS[0])

    def test_malformed_and_unknown_report(self):
        for raw, name in ((b"{", "INVALID_JSON"), (b"{}", "UNKNOWN_SCHEMA")):
            self.paths[1].write_bytes(raw)
            result = self.probe("UNREADABLE")
            self.assertFalse(result["valid"])
            self.assertIn(name, [item["name"] for item in
                                result["reports"]["new"]["validity"]["diagnostics"]])

    def test_missing_source_and_report(self):
        (self.roots[1] / custody.VECTORS[0]).unlink()
        result = self.probe("UNREADABLE")
        self.assertTrue(result["valid"])
        self.assertEqual(result["comparison"]["verdict"], "PASS")
        self.paths[0].unlink()
        self.assertFalse(self.probe("UNREADABLE")["valid"])

    def test_different_source_generations(self):
        raw = b"different generation\n"
        (self.roots[1] / custody.VECTORS[0]).write_bytes(raw)
        entry = next(item for item in self.reports[1]["provenance"]
                     if item["name"] == "vector:" + custody.VECTORS[0])
        entry.update(sha256_before=hashlib.sha256(raw).hexdigest(),
                     sha256_after=hashlib.sha256(raw).hexdigest())
        self.save(1)
        self.assertEqual(len(self.probe()["comparison"]["provenance_changes"]), 1)

    def test_unreadable_precedes_stale(self):
        (self.roots[0] / custody.VECTORS[0]).write_bytes(b"stale\n")
        self.paths[1].write_bytes(b"{")
        self.probe("UNREADABLE")

    def test_required_arguments_and_unknown_option(self):
        for args in ([], [str(path) for path in self.paths], ["--output", "x.json"],
                     ["--self-test", str(self.paths[0])]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args), 4)
            result = json.loads(output.getvalue())
            self.assertEqual(result["diagnostics"][0]["name"], "ARGUMENT_ERROR")
            self.assertEqual(result["verdict"], "UNREADABLE")


if __name__ == "__main__":
    sys.exit(main())
