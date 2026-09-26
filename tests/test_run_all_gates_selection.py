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
        for name in ("vitest-leaf.mjs", "playwright-leaf.mjs"):
            shutil.copyfile(ROOT / "scripts/ci/reporters" / name, trusted / name)
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

    def test_suite_trace_env_without_parent_readset_dir_keeps_reporting_only(self):
        suite = RUNNER.Suite("report-only", "report-only fixture", "pytest", self.work,
                             [sys.executable, "-m", "pytest", "test_sample.py"], None)
        parent = self.trusted_env()
        parent.pop("LEAF_READSET_DIR")
        for has_root in (True, False):
            with self.subTest(has_root=has_root):
                if not has_root:
                    parent.pop("LEAF_READSET_ROOT")
                with mock.patch.dict(os.environ, parent, clear=True):
                    env = RUNNER.suite_trace_env(suite, self.logs, 2)
                    self.assertNotIn("LEAF_READSET_DIR", env)
                    self.assertNotIn("LEAF_READSET_ROOT", env)
                    self.assertEqual(env, {
                        "LEAF_READSET_SUITE": suite.id,
                        "LEAF_READSET_ATTEMPT": "2",
                        "LEAF_READSET_RUN": "fixture-run",
                        "LEAF_TEST_REPORT_DIR": str(self.logs.resolve() / "test-reports" / suite.id / "2"),
                    })
                    command = RUNNER.reporting_command(suite, suite.argv, env)
                    self.assertNotIn("pytest_selection", command)  # S8c: plain pytest off tracing builds
                    self.assertEqual(command, [str(a) for a in suite.argv])  # S8c: the original argv, untouched

    def test_tracing_pytest_plugin_options_are_one_token_each(self):
        # S9b: xdist workers pre-parse argv before -p registers --leaf-*, so a two-token value becomes a path.
        suite = RUNNER.Suite("trace-pytest", "tracing pytest fixture", "pytest", self.work,
                             [sys.executable, "-m", "pytest", "test_sample.py"], None)
        env = self.trusted_env()
        with mock.patch.dict(os.environ, env, clear=True):
            trace_env = RUNNER.suite_trace_env(suite, self.logs, 1)
            command = RUNNER.reporting_command(suite, suite.argv, trace_env)
        base = [str(a) for a in suite.argv]
        self.assertEqual(command[:len(base)], base)
        added = command[len(base):]
        output = Path(trace_env["LEAF_TEST_REPORT_DIR"])
        self.assertEqual(added, ["-p", "pytest_selection",
                                 "--leaf-output=" + str(output),
                                 "--leaf-repo=" + env["LEAF_READSET_ROOT"],
                                 "--leaf-selection=" + str(output / "report-only.json"),
                                 "--leaf-catalog=" + str(output / "report-catalog.json")])
        for index, word in enumerate(command):
            if word.startswith("--leaf-"):
                self.assertIn("=", word)
                if index + 1 < len(command):
                    self.assertTrue(command[index + 1].startswith("--leaf-"), command[index + 1])
        self.assertNotIn("--rootdir", command)

    def test_suite_trace_env_passes_through_parent_readset_dir_and_root(self):
        suite = RUNNER.Suite("trace-fixture", "trace fixture", "script", self.work,
                             [sys.executable, "-c", "pass"], None)
        parent = {"LEAF_READSET_DIR": str(self.work / "capture"),
                  "LEAF_READSET_ROOT": str(self.work / "source")}
        with mock.patch.dict(os.environ, parent, clear=True):
            env = RUNNER.suite_trace_env(suite, self.logs, 1)
        self.assertEqual(env["LEAF_READSET_DIR"], parent["LEAF_READSET_DIR"])
        self.assertEqual(env["LEAF_READSET_ROOT"], parent["LEAF_READSET_ROOT"])

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
                self.assertTrue(row["test_report_complete"])
                self.assertEqual(row["test_id_granularity"], "suite")
                self.assertFalse(row["trace_complete"])
                shards = list((self.logs / "readsets" / sid / "1").glob("*.json"))
                self.assertEqual(bool(shards), not isolated)
                if isolated:
                    self.assertIn("isolated_python", row["trace_incomplete_reasons"])

    def test_suite_granularity_requires_a_final_attempt(self):
        for kind in ("script", "tsc", "npm-audit", "vitest"):
            for status in ("PASS", "FAIL", "SKIP", "UNAVAILABLE"):
                with self.subTest(kind=kind, status=status):
                    suite = RUNNER.Suite(kind + "-" + status, "suite fixture", kind,
                                         self.work, [], None)
                    report = RUNNER.read_test_report(suite, self.logs, 1, status)
                    self.assertEqual(report["test_report_complete"], status in ("PASS", "FAIL"))
                    self.assertEqual(report["test_id_granularity"], "suite")
                    self.assertEqual(report["test_ids"], [])
                    self.assertEqual(report["test_report_refs"], [])
                    result = RUNNER.Result(suite, status, "-", 0.0)
                    RUNNER.record_attempt(result, self.logs, 1)
                    row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text())
                    self.assertEqual(row["test_id_granularity"], "suite")
                    self.assertEqual(row["test_report_complete"], status in ("PASS", "FAIL"))

    def test_pytest_requires_completion_documents_even_with_a_generic_report(self):
        suite = RUNNER.Suite("missing-pytest", "pytest fixture", "pytest", self.work, [], None)
        directory = self.logs / "test-reports" / suite.id / "1"
        directory.mkdir(parents=True)
        for generic in (False, True):
            if generic:
                (directory / "tests-main.json").write_text(json.dumps({
                    "schema": "leaf.ci.test-report.v1", "suite_id": suite.id, "attempt": 1,
                    "complete": True, "test_ids": [suite.id + "::test_ok"], "failed_test_ids": []}))
            report = RUNNER.read_test_report(suite, self.logs, 1, "PASS")
            self.assertFalse(report["test_report_complete"])
            self.assertEqual(report["test_id_granularity"], "test")

    def test_vitest_document_does_not_fall_back_to_suite_completeness(self):
        suite = RUNNER.Suite("vitest-document", "vitest fixture", "vitest", self.work, [], None)
        directory = self.logs / "test-reports" / suite.id / "1"
        directory.mkdir(parents=True)
        path = directory / "tests-main.json"
        for complete in (False, True):
            reasons = [] if complete else ["task_without_result:2"]
            path.write_text(json.dumps({
                "schema": "leaf.ci.test-report.v1", "suite_id": suite.id, "attempt": 1,
                "complete": complete, "incomplete_reasons": reasons,
                "test_ids": [suite.id + "::test_ok"], "failed_test_ids": []}))
            report = RUNNER.read_test_report(suite, self.logs, 1, "PASS")
            self.assertEqual(report["test_report_complete"], complete)
            self.assertEqual(report["test_id_granularity"], "test")
            self.assertEqual(report["test_report_reasons"], reasons)
            RUNNER.record_attempt(RUNNER.Result(suite, "PASS", "1", 0.0), self.logs, 1)
            row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text().splitlines()[-1])
            self.assertEqual(row["test_report_complete"], complete)
            self.assertEqual(row["test_report_reasons"], reasons)
        path.write_text("not json")
        self.assertFalse(RUNNER.read_test_report(suite, self.logs, 1, "PASS")["test_report_complete"])

    def test_vitest_renamed_ids_are_carried_into_attempt(self):
        suite = RUNNER.Suite("vitest-renamed", "repeated titles fixture", "vitest", self.work, [], None)
        directory = self.logs / "test-reports" / suite.id / "1"
        directory.mkdir(parents=True)
        path = directory / "tests-vitest-123.json"
        ids = [suite.id + "::case.test.js::same" + suffix for suffix in ("", "#2", "#3")]
        document = {
            "schema": "leaf.ci.test-report.v1", "suite_id": suite.id, "attempt": 1,
            "complete": True, "incomplete_reasons": [], "test_ids": ids,
            "failed_test_ids": [ids[1]], "renamed_duplicate_ids": 2}
        for renamed in (2, 0):
            if renamed == 0:
                document.pop("renamed_duplicate_ids")
            path.write_text(json.dumps(document), encoding="utf-8")
            RUNNER.record_attempt(RUNNER.Result(suite, "PASS", "3", 0.0), self.logs, 1)
            row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text().splitlines()[-1])
            self.assertEqual(row["test_report_renamed_ids"], renamed)
            self.assertIsInstance(row["test_report_renamed_ids"], int)
            self.assertTrue(row["test_report_complete"])
            self.assertEqual(row["test_ids"], ids)
            self.assertEqual(row["failed_test_ids"], [ids[1]])

    def test_process_capture_receipt_fields_reach_the_attempt_record(self):
        suite = RUNNER.Suite("captured-script", "capture fixture", "script", self.work, [], None)
        with mock.patch.dict(os.environ, {"LEAF_PROCESS_CAPTURE": "1"}):
            RUNNER.record_attempt(RUNNER.Result(suite, "PASS", "ok", 0.0), self.logs, 1)
            receipt = (self.logs / "test-reports" / suite.id / "1" / "reports" /
                       ("trace-receipt-" + suite.id + "-1.json"))
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({
                "schema": "leaf.ci.trace-receipt.v1", "capture_group": suite.id + "-1",
                "facility_available": True, "capture_complete": False,
                "capture_errors": ["descendant_outlived_tree"], "tracer_exit_code": 0}), encoding="utf-8")
            RUNNER.record_attempt(RUNNER.Result(suite, "PASS", "ok", 0.0), self.logs, 1)
        missing, present = [json.loads(line) for line in
                            (self.logs / "attempts" / (suite.id + ".jsonl")).read_text().splitlines()]
        self.assertEqual(missing["capture_errors"], ["capture_receipt_missing"])
        self.assertIs(missing["capture_facility_available"], False)
        self.assertEqual({k: present[k] for k in ("capture_facility_available", "capture_complete",
                                                   "capture_errors", "tracer_exit_code")},
                         {"capture_facility_available": True, "capture_complete": False,
                          "capture_errors": ["descendant_outlived_tree"], "tracer_exit_code": 0})
        self.assertTrue(present["test_report_complete"])
        RUNNER.record_attempt(RUNNER.Result(suite, "PASS", "ok", 0.0), self.logs, 2)
        plain = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text().splitlines()[-1])
        self.assertNotIn("capture_errors", plain)

    def test_vitest_skipped_tasks_only_document_is_complete(self):
        suite = RUNNER.Suite("vitest-skips", "skipped tasks fixture", "vitest", self.work, [], None)
        directory = self.logs / "test-reports" / suite.id / "1"
        directory.mkdir(parents=True)
        ids = [suite.id + "::case.test.js::" + name for name in ("skip", "state-skip", "todo")]
        (directory / "tests-vitest-123.json").write_text(json.dumps({
            "schema": "leaf.ci.test-report.v1", "suite_id": suite.id, "attempt": 1,
            "complete": True, "incomplete_reasons": [], "test_ids": ids, "failed_test_ids": []}))
        RUNNER.record_attempt(RUNNER.Result(suite, "PASS", "0", 0.0), self.logs, 1)
        row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text())
        self.assertTrue(row["test_report_complete"])
        self.assertEqual(row["test_report_reasons"], [])
        self.assertEqual(row["test_id_granularity"], "test")
        self.assertEqual(row["test_ids"], ids)
        self.assertEqual(row["failed_test_ids"], [])

    def test_only_skips_produced_by_catalog_gates_are_complete(self):
        for rule in ("db_gated", "opt_in_env", ""):
            with self.subTest(rule=rule):
                suite = RUNNER.Suite("skip-" + (rule or "other"), "skip fixture", "pytest",
                                     self.work, [], None, db_gated=rule == "db_gated",
                                     opt_in_env="LEAF_FIXTURE_OPT_IN" if rule == "opt_in_env" else "")
                with mock.patch.dict(os.environ, {}, clear=True), \
                     mock.patch.object(RUNNER, "probe_platform_db", return_value=(False, "no DB", "")), \
                     mock.patch.object(RUNNER.subprocess, "run", side_effect=AssertionError("must not spawn")):
                    result = (RUNNER.run_suite_guarded(suite, self.logs, 1) if rule else
                              RUNNER.Result(suite, "SKIP", "skip", 0.0, note="not a catalog gate"))
                    RUNNER.record_attempt(result, self.logs, 1)
                row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text())
                self.assertEqual(row["status"], "SKIP")
                self.assertEqual(row["test_report_complete"], bool(rule))
                self.assertEqual(row["test_ids"], [])
                self.assertEqual(row["test_id_granularity"], "test")
                if rule:
                    self.assertEqual(row["skipped_by_gate"], rule)
                else:
                    self.assertNotIn("skipped_by_gate", row)

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
        shards = list((self.logs / "readsets" / "sample" / "1").glob("*.json"))
        self.assertTrue(shards)
        for path in shards:
            ref = json.loads(path.read_text(encoding="utf-8"))["outcomes_ref"]
            self.assertFalse(Path(ref).is_absolute())
            self.assertNotIn("..", Path(ref).parts)
            self.assertTrue(ref.startswith("readsets/sample/1/attempts"))
            rows = [json.loads(line) for line in (self.logs / ref).read_text(
                encoding="utf-8").splitlines()]
            self.assertTrue(rows)
            self.assertTrue(all(row["schema"] == "leaf.ci.test-attempt.v1" for row in rows))
            self.assertTrue(any(row["outcome"] == "failed" for row in rows))
        self.assertTrue(list((self.logs / "readsets" / "sample" / "1").glob("attempts-*.jsonl")))

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

    def test_vitest_reporter_copy_is_inside_suite_cwd_and_overwritten_per_attempt(self):
        env = self.trusted_env()
        source = Path(env["LEAF_TRUSTED_CI_DIR"]) / "vitest-leaf.mjs"
        cwd = self.work / "web"
        suite = RUNNER.Suite("vitest-copy", "copy fixture", "vitest", cwd,
                             ["npm", "test"], None)
        destination = cwd / "node_modules/.leaf-ci/vitest-leaf.mjs"
        with mock.patch.dict(os.environ, env):
            for attempt in (1, 2):
                if attempt == 2:
                    source.write_bytes(source.read_bytes() + b"\n// second attempt\r\n")
                command = RUNNER.reporting_command(
                    suite, suite.argv, RUNNER.suite_trace_env(suite, self.logs, attempt))
                self.assertEqual(command, suite.argv + ["--", "--reporter=default",
                                                       "--reporter=" + str(destination)])
                self.assertNotIn("--reporter=" + str(source), command)
                self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_vitest_reporter_copy_failed_runs_original_and_records_incomplete(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                env = self.trusted_env()
                if missing:
                    (Path(env["LEAF_TRUSTED_CI_DIR"]) / "vitest-leaf.mjs").unlink()
                reason = "reporter_copy_failed:" + ("FileNotFoundError" if missing else "PermissionError")
                suite = RUNNER.Suite("copy-failure-" + str(missing), "copy failure", "vitest",
                                     self.work, ["npm", "test"], 1)
                copy = (contextlib.nullcontext() if missing else
                        mock.patch.object(RUNNER.shutil, "copyfile", side_effect=PermissionError("fixture")))
                with mock.patch.dict(os.environ, env), copy:
                    trace_env = RUNNER.suite_trace_env(suite, self.logs, 1)
                    self.assertEqual(RUNNER.reporting_command(suite, suite.argv, trace_env), suite.argv)
                    self.assertEqual(trace_env["LEAF_TEST_REPORT_INCOMPLETE_REASON"], reason)
                    with mock.patch.object(RUNNER.subprocess, "run", return_value=
                                           subprocess.CompletedProcess(suite.argv, 0, "Tests  1 passed (1)", "")) as spawn, \
                         mock.patch.object(RUNNER, "read_test_report", return_value={"test_report_complete": True}):
                        result = RUNNER.run_suite_guarded(suite, self.logs, 1)
                        RUNNER.record_attempt(result, self.logs, 1)
                    self.assertEqual(spawn.call_args.args[0], suite.argv)
                    self.assertEqual(spawn.call_args.kwargs["env"], RUNNER.clean_env())
                self.assertEqual(result.status, "PASS", result.note)
                row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text(encoding="utf-8"))
                self.assertFalse(row["test_report_complete"])
                self.assertFalse(row["reporting_injected"])
                self.assertEqual(row["test_report_incomplete_reasons"], [reason])

    def test_reporter_startup_failure_retries_uninstrumented_in_both_schedulers(self):
        for jobs in (1, 2):
            for kind, command in (("vitest", ["npm", "test"]),
                                  ("script", ["npx", "playwright", "test", "--reporter=list"])):
                for first_output, startup_failure in (("reporter failed to load", True),
                                                       ("Tests  0 passed (0)", True),
                                                       ("Tests  1 failed (1)", False)):
                    with self.subTest(jobs=jobs, kind=kind, first_output=first_output):
                        log_dir = self.logs / f"{jobs}-{kind}-{first_output.split()[1]}"
                        suite = RUNNER.Suite("reporter-retry", "reporter retry", kind,
                                             self.work, command, 1 if kind == "vitest" else None)
                        processes = [subprocess.CompletedProcess(command, 1, first_output, ""),
                                     subprocess.CompletedProcess(command, 0, "Tests  1 passed (1)", "")]
                        argv = [str(RUNNER_PATH), "--jobs", str(jobs), "--retry", "1",
                                "--log-dir", str(log_dir)]
                        with mock.patch.dict(os.environ, self.trusted_env()), \
                             mock.patch.object(RUNNER, "build_suites", return_value=[suite]), \
                             mock.patch.object(RUNNER.subprocess, "run", side_effect=processes) as spawn, \
                             mock.patch.object(RUNNER, "read_test_report", return_value={"test_report_complete": True}), \
                             mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(RUNNER.main(), 0)
                        self.assertEqual(spawn.call_count, 2)
                        first, retry = [call.args[0] for call in spawn.call_args_list]
                        # Prove injection took before judging the retry behavior.
                        self.assertNotEqual(first, command)
                        self.assertTrue(any("-leaf.mjs" in word for word in first))
                        self.assertEqual(retry, command if startup_failure else first)
                        rows = [json.loads(line) for path in (log_dir / "attempts").glob("*.jsonl")
                                for line in path.read_text(encoding="utf-8").splitlines()]
                        self.assertEqual([row["status"] for row in rows], ["FAIL", "PASS"])
                        if startup_failure:
                            self.assertTrue(rows[0]["reporting_injected"])
                            self.assertFalse(rows[1]["reporting_injected"])
                            for row in rows:
                                self.assertFalse(row["test_report_complete"])
                                self.assertEqual(row["test_report_incomplete_reasons"], ["reporter_startup_failure"])
                        else:
                            self.assertNotIn("reporter_startup_failure", rows[1].get("test_report_incomplete_reasons", []))

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
