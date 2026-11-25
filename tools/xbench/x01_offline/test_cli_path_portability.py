"""Synthetic CLI evidence from unrelated paths with spaces and Unicode.

Only stdlib subprocess argv arrays are used; every child has a timeout.
"""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "run_offline_checks.py"
VALIDATOR = ROOT / "validate_offline_report.py"
CHECKERS = ("check_cases.py", "check_plot_layers.py", "check_unit_equivalence.py")
VECTORS = ("fill_coverage_cases.json", "module_geometry_cases.json", "tag_overlap_cases.json")
INPUTS = {**{"checker:" + name: ROOT / name for name in CHECKERS},
          **{"vector:" + name: ROOT / name for name in VECTORS}}


def hashes():
    return {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in INPUTS.items()}


class CliPathPortabilityTests(unittest.TestCase):
    def invoke(self, script, args, cwd, expected_code):
        child = subprocess.run([sys.executable, "-B", str(script), *map(str, args)],
                               cwd=cwd, capture_output=True, timeout=45, check=False)
        self.assertEqual(child.returncode, expected_code,
                         child.stderr.decode("utf-8", errors="replace") +
                         child.stdout.decode("utf-8", errors="replace"))
        result = json.loads(child.stdout)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertIs(result["live_grade"], False)
        return result

    def validate(self, path, cwd, code, observed=None, reason=None):
        before = path.read_bytes() if path.exists() else None
        result = self.invoke(VALIDATOR, [path], cwd, code)
        self.assertEqual(result["schema"], "leaf.xbench.x01.offline-report-validation.v1")
        self.assertEqual(result["verdict"], {0: "PASS", 1: "FAIL", 4: "UNREADABLE"}[code])
        self.assertIs(result["valid"], code == 0)
        if observed is not None:
            self.assertEqual(result["observed_verdict"], observed)
        if reason is not None:
            self.assertIn(reason, [item["name"] for item in result["diagnostics"]])
        self.assertEqual(path.read_bytes() if path.exists() else None, before)
        return result

    def test_runner_and_validator_from_unrelated_unicode_cwd(self):
        before = hashes()
        self.assertEqual(len(before), 6)
        evidence = []
        try:
            with tempfile.TemporaryDirectory(prefix="x01 portable \u00e9 \u96ea ") as directory:
                scratch = Path(directory).resolve()
                cwd = scratch / "unrelated working directory \u03a9"
                reports = scratch / "synthetic reports \u96ea"
                cwd.mkdir()
                reports.mkdir()
                normal_path = reports / "normal offline report \u00e9.json"
                normal = self.invoke(RUNNER, ["--timeout-seconds", "10", "--output",
                                              normal_path], cwd, 0)
                self.assertEqual(normal["schema"], "leaf.xbench.x01.offline-run.v1")
                self.assertEqual(normal["verdict"], "PASS")
                self.assertEqual(json.loads(normal_path.read_text(encoding="utf-8")), normal)
                self.assertEqual(list(reports.iterdir()), [normal_path])
                self.assertEqual([row["checker"] for row in normal["checks"]], list(CHECKERS))
                self.assertTrue(all(row["verdict"] == "PASS" and row["exit_code"] == 0
                                    and row["timeout"] is False for row in normal["checks"]))
                self.assertEqual(len(normal["provenance"]), 6)
                self.assertEqual({row["name"]: row["sha256_before"]
                                  for row in normal["provenance"]}, before)
                self.assertEqual({row["name"]: row["sha256_after"]
                                  for row in normal["provenance"]}, before)
                evidence.append(self.validate(normal_path, cwd, 0, "PASS"))

                failed = copy.deepcopy(normal)
                named = dict(name="SYNTHETIC_PORTABILITY_FAILURE", object_ids=["SYNTHETIC/M1"])
                failed["checks"][0].update(verdict="FAIL", exit_code=1,
                                           diagnostics=[named], output=json.dumps(named))
                failed["verdict"] = "FAIL"
                fail_path = reports / "named failure \u03a9.json"
                fail_path.write_text(json.dumps(failed, allow_nan=False), encoding="utf-8")
                evidence.append(self.validate(fail_path, cwd, 0, "FAIL"))
                self.assertEqual(json.loads(fail_path.read_text(encoding="utf-8"))[
                    "checks"][0]["diagnostics"], [named])

                for name, report, reason in (
                        ("invalid live grade", dict(normal, live_grade=True), "LIVE_GRADE_CLAIM"),
                        ("contradictory aggregate", dict(failed, verdict="PASS"), "INCONSISTENT_PARENT")):
                    path = reports / (name + " \u00e9.json")
                    path.write_text(json.dumps(report, allow_nan=False), encoding="utf-8")
                    evidence.append(self.validate(path, cwd, 1, reason=reason))
                missing = reports / "missing synthetic report \u96ea.json"
                evidence.append(self.validate(missing, cwd, 4, reason="INPUT_UNREADABLE"))
                self.assertFalse(missing.exists())
                self.assertEqual({path.name for path in reports.iterdir()}, {
                    normal_path.name, fail_path.name, "invalid live grade \u00e9.json",
                    "contradictory aggregate \u00e9.json"})
                self.assertEqual(list(cwd.iterdir()), [])
                self.assertEqual(hashes(), before)
                print(json.dumps(dict(evidence_kind="synthetic_offline", live_grade=False,
                                      normal_report=normal, named_fail_report=failed,
                                      validations=evidence,
                                      input_hashes_before=before, input_hashes_after=hashes()),
                                 sort_keys=True, allow_nan=False))
            self.assertFalse(scratch.exists())
        finally:
            self.assertEqual(hashes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
