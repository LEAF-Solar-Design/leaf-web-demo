"""S3 catalog, child attribution, and first-attempt evidence contracts."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/run-all-gates.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("leaf_selection_gate_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


RUNNER = load_runner()


class SelectionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leaf-selection-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.logs = self.work / "logs"
        self.logs.mkdir()

    def trusted_env(self):
        trusted = self.work / "trusted"
        trusted.mkdir(exist_ok=True)
        for name in ("sitecustomize.py", "trace_reads.py", "pytest_selection.py", "select_tests.py"):
            shutil.copyfile(ROOT / "scripts/ci" / name, trusted / name)
        return {"LEAF_TRUSTED_CI_DIR": str(trusted), "PYTHONPATH": str(trusted),
                "PYTHONSAFEPATH": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "LEAF_READSET_RUN": "fixture-run", "LEAF_READSET_ROOT": str(self.work),
                "LEAF_READSET_DIR": str(self.logs / "readsets"), "PYTEST_ADDOPTS": ""}

    def test_list_is_side_effect_free_and_preserves_catalog_fingerprint(self):
        with mock.patch.object(RUNNER.subprocess, "run", side_effect=AssertionError("subprocess")), \
             mock.patch.object(RUNNER, "probe_platform_db", side_effect=AssertionError("DB probe")), \
             mock.patch.object(RUNNER, "reset_authored_tools", side_effect=AssertionError("reset")), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")), \
             mock.patch.object(sys, "argv", [str(RUNNER_PATH), "--list", "--catalog-root", str(ROOT),
                                            "--jobs", "auto", "--log-dir", str(self.work / "forbidden")]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(RUNNER.main(), 0)
        doc = json.loads(output.getvalue())
        self.assertEqual(doc["schema"], "leaf.ci.catalog.v1")
        self.assertGreater(len(doc["suites"]), 400)
        self.assertEqual(doc["catalog_sha256"], RUNNER.catalog_fingerprint(RUNNER.build_suites()))
        self.assertEqual(len(doc["suites"]), len({row["id"] for row in doc["suites"]}))
        self.assertTrue(all({"id", "command", "cwd", "skip_rules", "collection_identity"} <= set(row)
                            for row in doc["suites"]))
        self.assertFalse((self.work / "forbidden").exists())
        # The extracted runner must be runnable outside its original scripts dir.
        extracted = self.work / "run-all-gates.py"
        shutil.copyfile(RUNNER_PATH, extracted)
        (self.work / "platform.py").write_text("raise AssertionError('candidate import')\n")
        proc = subprocess.run([sys.executable, "-I", "-B", str(extracted), "--list",
                               "--catalog-root", str(ROOT)], cwd=self.work, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), doc)

    def test_first_failure_survives_retry_in_both_schedulers(self):
        suite = RUNNER.Suite("fixture-retry", "retry fixture", "script", self.work,
                             [sys.executable, "-c", "pass"], None)
        tid = "fixture-retry::test_sample.py::test_case[red]"
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                log_dir = self.logs / str(jobs)
                result_path = self.work / (str(jobs) + ".json")
                reports = [{"failed_test_ids": [tid], "test_ids": [tid],
                            "collection_ids_sha256": "fixture-digest", "test_report_complete": True},
                           {"failed_test_ids": [], "test_ids": [tid],
                            "collection_ids_sha256": "fixture-digest", "test_report_complete": True}]
                results = [RUNNER.Result(suite, "FAIL", "err", 2.0),
                           RUNNER.Result(suite, "PASS", "ok", 3.0)]
                argv = [str(RUNNER_PATH), "--jobs", str(jobs), "--retry", "1",
                        "--log-dir", str(log_dir), "--result-json", str(result_path)]
                with mock.patch.object(RUNNER, "build_suites", return_value=[suite]), \
                     mock.patch.object(RUNNER, "run_suite_guarded", side_effect=results), \
                     mock.patch.object(RUNNER, "read_test_report", side_effect=reports), \
                     mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(RUNNER.main(), 0)
                rows = [json.loads(line) for path in (log_dir / "attempts").glob("*.jsonl")
                        for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([row["status"] for row in rows], ["FAIL", "PASS"])
                self.assertEqual([row["attempt"] for row in rows], [1, 2])
                self.assertEqual([row["seconds"] for row in rows], [2.0, 3.0])
                self.assertEqual(rows[0]["failed_test_ids"], [tid])
                final = json.loads(result_path.read_text(encoding="utf-8"))["results"][0]
                self.assertEqual(final["status"], "PASS")
                self.assertEqual(final["seconds"], 5.0)

    def test_child_trace_environment_and_isolated_incompleteness(self):
        child = "import json,os; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith('LEAF_READSET_')}))"
        with mock.patch.dict(os.environ, self.trusted_env()):
            for isolated in (False, True):
                sid = "child-isolated" if isolated else "child-python"
                argv = [sys.executable] + (["-I"] if isolated else []) + ["-B", "-c", child]
                suite = RUNNER.Suite(sid, sid, "script", self.work, argv, None)
                result = RUNNER.run_suite_guarded(suite, self.logs, 1)
                self.assertEqual(result.status, "PASS", result.note)
                RUNNER.record_attempt(result, self.logs, 1)
                lines = result.log_path.read_text(encoding="utf-8").splitlines()
                env = json.loads(next(line for line in lines if line.startswith('{"LEAF_')))
                self.assertEqual(env["LEAF_READSET_SUITE"], sid)
                self.assertEqual(env["LEAF_READSET_ATTEMPT"], "1")
                self.assertEqual(env["LEAF_READSET_RUN"], "fixture-run")
                self.assertEqual(env["LEAF_READSET_ROOT"], str(self.work))
                ledger = self.logs / "attempts" / (sid + ".jsonl")
                row = json.loads(ledger.read_text(encoding="utf-8"))
                self.assertFalse(row["test_report_complete"])
                self.assertFalse(row["trace_complete"])
                shards = list((self.logs / "readsets" / sid / "1").glob("*.json"))
                self.assertEqual(bool(shards), not isolated)
                if isolated:
                    self.assertIn("isolated_python", row["trace_incomplete_reasons"])

    def test_pytest_plugin_reports_exact_parameterized_failure(self):
        (self.work / "test_sample.py").write_text(
            "import pytest\n@pytest.mark.parametrize('value', ['good', 'bad'])\n"
            "def test_case(value):\n    assert value == 'good'\n", encoding="utf-8")
        suite = RUNNER.Suite("sample", "pytest fixture", "pytest", self.work,
                             [sys.executable, "-B", "-m", "pytest", "test_sample.py", "-q",
                              "-p", "no:cacheprovider"], 2)
        with mock.patch.dict(os.environ, self.trusted_env()):
            result = RUNNER.run_suite_guarded(suite, self.logs, 1)
            RUNNER.record_attempt(result, self.logs, 1)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(result.test_report["test_report_complete"], result.log_path.read_text(encoding="utf-8"))
        self.assertEqual(result.test_report["failed_test_ids"], ["sample::test_sample.py::test_case[bad]"])
        self.assertEqual(len(result.test_report["test_ids"]), 2)

    @unittest.skipUnless(shutil.which("node"), "Node is required for reporter contracts")
    def test_node_reporters_preserve_ids_and_first_failure(self):
        code = r'''
import Vitest from __VITEST__;
import Playwright from __PLAYWRIGHT__;
import { resolve } from 'node:path';
const root = process.env.LEAF_READSET_ROOT;
const vitest = new Vitest();
vitest.onInit({config: {root}});
vitest.onFinished([{filepath: resolve(root, 'case.test.js'), tasks: [
  {name: 'case[red]', type: 'test', result: {state: 'pass', retryCount: 1}},
  {name: 'case[green]', type: 'test', result: {state: 'pass'}}
]}], []);
const reporter = new Playwright();
const test = {id: 'case-red', location: {file: resolve(root, 'case.spec.js')},
              titlePath: () => ['chromium', 'case[red]']};
reporter.onBegin({rootDir: root}, {allTests: () => [test]});
reporter.onTestEnd(test, {status: 'failed', retry: 0});
reporter.onTestEnd(test, {status: 'passed', retry: 1});
reporter.onEnd({status: 'passed'});
'''
        code = code.replace("__VITEST__", json.dumps((ROOT / "scripts/ci/reporters/vitest-leaf.mjs").as_uri()))
        code = code.replace("__PLAYWRIGHT__", json.dumps((ROOT / "scripts/ci/reporters/playwright-leaf.mjs").as_uri()))
        env = dict(os.environ, LEAF_READSET_ROOT=str(self.work), LEAF_READSET_SUITE="node-fixture",
                   LEAF_READSET_ATTEMPT="1", LEAF_TEST_REPORT_DIR=str(self.work / "reports"))
        proc = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code],
                              cwd=self.work, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        docs = [json.loads(path.read_text(encoding="utf-8")) for path in (self.work / "reports").glob("tests-*.json")]
        self.assertEqual(len(docs), 2)
        for doc in docs:
            self.assertTrue(doc["complete"])
            self.assertTrue(all(tid.startswith("node-fixture::") for tid in doc["test_ids"]))
            self.assertEqual(len(doc["failed_test_ids"]), 1)
            self.assertIn("case[red]", doc["failed_test_ids"][0])

    def test_initial_map_keeps_all_unclassified_suites_mandatory(self):
        mapping = json.loads((ROOT / "scripts/ci/test-selection-map.json").read_text(encoding="utf-8"))
        self.assertIs(mapping["selection_enabled"], False)
        self.assertEqual(mapping["phase"], "shadow")
        self.assertEqual(set(mapping["mandatory_suite_ids"]), {suite.id for suite in RUNNER.build_suites()})
        self.assertEqual(mapping["unresolved_mandatory"], [])

    def test_suite_environment_scrubs_pythonsafepath_and_keeps_pythonpath(self):
        suite = RUNNER.Suite("env-fixture", "environment fixture", "script", self.work,
                             [sys.executable, "-c", "pass"], None)
        with mock.patch.dict(os.environ, {"PYTHONSAFEPATH": "1", "PYTHONPATH": "capture-path"}), \
             mock.patch.object(RUNNER.subprocess, "run", return_value=
                               subprocess.CompletedProcess(suite.argv, 0, "", "")) as spawn:
            self.assertNotIn("PYTHONSAFEPATH", RUNNER.clean_env())
            self.assertEqual(RUNNER.clean_env()["PYTHONPATH"], "capture-path")
            result = RUNNER.run_suite_guarded(suite, self.logs, 1)
        self.assertEqual(result.status, "PASS", result.note)
        self.assertNotIn("PYTHONSAFEPATH", spawn.call_args.kwargs["env"])
        self.assertEqual(spawn.call_args.kwargs["env"]["PYTHONPATH"], "capture-path")

    def test_timeout_decodes_partial_bytes_and_preserves_fail_note(self):
        for kind in ("script", "pytest", "vitest", "tsc"):
            with self.subTest(kind=kind):
                suite = RUNNER.Suite("timeout-" + kind, "timeout fixture", kind, self.work,
                                     [sys.executable, "-c", "pass"], None, timeout_s=1)
                timeout = subprocess.TimeoutExpired(suite.argv, 1, output=b"partial \xff", stderr=b"err")
                with mock.patch.object(RUNNER.subprocess, "run", side_effect=timeout):
                    result = RUNNER.run_suite_guarded(suite, self.logs, 1)
                self.assertEqual(result.status, "FAIL")
                self.assertIn("[TIMEOUT >1s]", result.note)
                self.assertIn("partial \ufffd\nerr", result.log_path.read_text(encoding="utf-8"))

    def test_audit_timeout_and_spawn_output_decode_bytes(self):
        argv = ["npm", "audit"]
        timeout = subprocess.TimeoutExpired(argv, 1, output=b"partial \xff", stderr=b"err")
        log = io.StringIO()
        with mock.patch.object(RUNNER.subprocess, "run", side_effect=timeout):
            rc, stdout, stderr, timed_out, spawn_err = RUNNER._run_npm_audit_attempt(
                argv, self.work, log, 1)
        self.assertEqual(rc, 124)
        self.assertEqual(stdout, "partial \ufffd")
        self.assertIn("err\n[TIMEOUT >", stderr)
        self.assertTrue(timed_out)
        self.assertEqual(spawn_err, "")
        suite = RUNNER.Suite("bytes-output", "bytes output fixture", "script", self.work,
                             [sys.executable, "-c", "pass"], None)
        with mock.patch.object(RUNNER.subprocess, "run", return_value=
                               subprocess.CompletedProcess(suite.argv, 0, b"partial \xff", b"err")):
            result = RUNNER.run_suite_guarded(suite, self.logs, 1)
        self.assertEqual(result.status, "PASS", result.note)
        self.assertIn("partial \ufffd\nerr", result.log_path.read_text(encoding="utf-8"))

    def test_reporting_normalizes_playwright_bytes_and_paths(self):
        commands = [
            ["npx", "playwright", "test", "--reporter", b"list,html"],
            [Path("npx"), Path("playwright"), Path("test"), Path("--reporter"), Path("list")],
            [b"npx", b"playwright", b"test", b"--reporter=list"],
        ]
        with mock.patch.dict(os.environ, self.trusted_env()):
            for argv in commands:
                with self.subTest(argv=argv):
                    original = list(argv)
                    suite = RUNNER.Suite("reporter-fixture", "reporter fixture", "script",
                                         self.work, argv, None)
                    command = RUNNER.reporting_command(
                        suite, argv, RUNNER.suite_trace_env(suite, self.logs, 1))
                    self.assertEqual(argv, original)
                    self.assertTrue(all(isinstance(word, str) for word in command))
                    reporter = next(word for word in command if word.startswith("--reporter="))
                    self.assertTrue(reporter.startswith("--reporter=list"))
                    self.assertIn("playwright-leaf.mjs", reporter)
                    if b"list,html" in original:
                        self.assertIn("list,html,", reporter)

    def test_reporting_preserves_vitest_separator_and_isolated_pytest(self):
        with mock.patch.dict(os.environ, self.trusted_env()):
            for kind, argv in (
                ("vitest", ["npm", "test", "--", "--run"]),
                ("pytest", [sys.executable, "-I", "-m", "pytest", "test_sample.py"]),
            ):
                with self.subTest(kind=kind):
                    suite = RUNNER.Suite("flags-fixture", "flags fixture", kind, self.work, argv, None)
                    command = RUNNER.reporting_command(
                        suite, argv, RUNNER.suite_trace_env(suite, self.logs, 1))
                    if kind == "vitest":
                        self.assertEqual(command.count("--"), 1)
                        self.assertIn("--reporter=default", command)
                    else:
                        self.assertEqual(command, argv)

    def test_reporting_injection_failure_runs_original_and_records_incomplete(self):
        def broken_reporting(suite, argv, env):
            argv.append("--must-not-reach-child")
            env["REPORTING_PARTIAL"] = "1"
            raise TypeError("reporter fixture")

        for helper, failure in (("reporting_command", broken_reporting),
                                ("suite_trace_env", OSError("environment fixture"))):
            with self.subTest(helper=helper):
                suite = RUNNER.Suite("fallback-" + helper, "fallback fixture", "script", self.work,
                                     [sys.executable, "-c", "pass"], None)
                reason = "reporting_injection_failed:" + ("TypeError" if callable(failure) else "OSError")
                original_env = RUNNER.clean_env()
                with mock.patch.object(RUNNER, helper, side_effect=failure), \
                     mock.patch.object(RUNNER.subprocess, "run", return_value=
                                       subprocess.CompletedProcess(suite.argv, 0, "", "")) as spawn, \
                     mock.patch.object(RUNNER, "read_test_report", return_value={"test_report_complete": True}):
                    result = RUNNER.run_suite_guarded(suite, self.logs, 1)
                    RUNNER.record_attempt(result, self.logs, 1)
                self.assertEqual(result.status, "PASS", result.note)
                self.assertEqual(spawn.call_args.args[0], suite.argv)
                self.assertEqual(spawn.call_args.kwargs["env"], original_env)
                self.assertEqual(result.log_path.read_text(encoding="utf-8").count(reason), 1)
                row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text(encoding="utf-8"))
                self.assertFalse(row["test_report_complete"])
                self.assertEqual(row["test_report_incomplete_reasons"], [reason])

    def test_ci_unsets_pythonsafepath_before_gate(self):
        lines = (ROOT / ".codebuild/ci.sh").read_text(encoding="utf-8").splitlines()
        self.assertFalse(any(line.startswith("export PYTHONSAFEPATH") for line in lines))
        unset_index = lines.index("unset PYTHONSAFEPATH")
        gate_index = next(index for index, line in enumerate(lines)
                          if line.startswith("python scripts/run-all-gates.py"))
        self.assertLess(unset_index, gate_index)


if __name__ == "__main__":
    unittest.main()
