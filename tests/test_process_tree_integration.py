"""S15a-web: per-suite process-tree capture in the gate runner, offline.

subprocess.run is patched wherever a supervisor would start; only the pytest plugin and the
Node reporter run for real, with capture on and no tracer, to prove their suites files and the
diagnostics split.
"""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
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
CI = ROOT / "scripts" / "ci"
RUNNER_PATH = ROOT / "scripts/run-all-gates.py"
COPIES = ("trace_process_tree.py", "trace_supervisor.py", "external_inventory.py", "select_tests.py")
RUN_ID = "codebuild:fixture"
SHA = "a" * 40


def load(name, path, extra_path=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = list(sys.path)
    try:
        if extra_path:
            sys.path.insert(0, str(extra_path))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


RUNNER = load("leaf_process_tree_gate_runner", RUNNER_PATH)
MANIFEST = load("leaf_process_tree_full_run_manifest", CI / "full_run_manifest.py", CI)
SELECT = sys.modules["select_tests"]
TREE = load("leaf_process_tree_decoder", CI / "trace_process_tree.py", CI)
SUPERVISOR = load("leaf_process_tree_supervisor", CI / "trace_supervisor.py", CI)


def completed(argv, code=0):
    return subprocess.CompletedProcess(argv, code, stdout="fixture output\n", stderr="")


class CaptureFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leaf-process-tree-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.logs = self.work / "logs"
        self.logs.mkdir()
        self.trusted = self.work / "trusted"
        self.trusted.mkdir()
        for name in ("sitecustomize.py", "trace_reads.py", "pytest_selection.py") + COPIES:
            shutil.copyfile(CI / name, self.trusted / name)
        for name in ("vitest-leaf.mjs", "playwright-leaf.mjs"):
            shutil.copyfile(CI / "reporters" / name, self.trusted / name)
        self.context_dir = self.trusted / "capture"
        self.context_dir.mkdir()
        self.base = {"run_id": RUN_ID, "source_sha": SHA, "source_tree": "b" * 40, "capture_sha": "c" * 40,
                     "catalog_sha256": "d" * 64, "source_root": "/src", "initial_cwd": "/src",
                     "inventory": {"files": ["web/a.js"], "symlinks": {}},
                     "external_roots": {"/usr/bin": {"class": "os-image", "origin_category": "os-image"}},
                     "toolchain_fingerprint": "e" * 64, "image_manifest_digest": None}
        (self.context_dir / "base-context.json").write_text(json.dumps(self.base), encoding="utf-8")

    def env(self, capture=True):
        env = {"LEAF_TRUSTED_CI_DIR": str(self.trusted), "LEAF_READSET_RUN": RUN_ID,
               "LEAF_READSET_DIR": str(self.logs / "readsets"), "LEAF_READSET_ROOT": str(self.work)}
        if capture:
            env.update(LEAF_PROCESS_CAPTURE="1", LEAF_CAPTURE_CONTEXT_DIR=str(self.context_dir))
        return env

    def suite(self, sid="fixture-suite", argv=None, kind="script"):
        cwd = self.work / sid
        cwd.mkdir(exist_ok=True)
        return RUNNER.Suite(sid, sid + " fixture", kind, cwd, argv or [sys.executable, "-c", "pass"], None)

    def spawn(self, suite, capture=True, code=0, valid=True):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return completed(argv, code)
        with mock.patch.dict(os.environ, self.env(capture)), \
             mock.patch.object(RUNNER, "validate_capture_context", return_value=valid), \
             mock.patch.object(RUNNER.subprocess, "run", side_effect=fake_run):
            result = RUNNER.run_suite(suite, self.logs, 1)
        self.assertEqual(len(calls), 1)
        return result, calls[0]


class SpawnWrapTests(CaptureFixture):
    def test_capture_wraps_the_exact_argv_with_unchanged_spawn_options(self):
        suite = self.suite()
        _, (plain, plain_kwargs) = self.spawn(suite, capture=False)
        result, (argv, kwargs) = self.spawn(suite)
        report_dir = self.logs.resolve() / "test-reports" / suite.id / "1"
        self.assertEqual(argv[:6], [sys.executable, "-I", "-B", "-c", RUNNER.CAPTURE_WRAPPER, str(self.trusted)])
        self.assertEqual(argv[6:14], ["run", "--context", str(report_dir / "capture-context.json"),
                                      "--out", str(report_dir), "--suites-file",
                                      str(report_dir / "capture-suites.json"), "--"])
        self.assertEqual(argv[14:], [str(word) for word in suite.argv])
        self.assertEqual(argv[14:], plain)
        self.assertIs(kwargs["shell"], False)
        self.assertIsNone(kwargs["executable"])
        for key in ("cwd", "timeout", "capture_output", "text", "encoding", "errors"):
            self.assertEqual(kwargs[key], plain_kwargs[key], key)
        # Only the two capture variables ci.sh exports reach the child in addition.
        self.assertEqual({k: v for k, v in kwargs["env"].items()
                          if k not in ("LEAF_PROCESS_CAPTURE", "LEAF_CAPTURE_CONTEXT_DIR")}, plain_kwargs["env"])
        self.assertEqual(result.status, "PASS")
        context = json.loads((report_dir / "capture-context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["capture_group"], suite.id + "-1")
        self.assertEqual(context["suites"], [])
        self.assertIs(context["suites_deferred"], True)
        self.assertEqual(context["seed_fds"], {"1": {"kind": "supervisor"}, "2": {"kind": "supervisor"}})
        self.assertEqual(context["initial_cwd"], str(suite.cwd.resolve()))
        self.assertEqual({k: context[k] for k in self.base if k != "initial_cwd"},
                         {k: v for k, v in self.base.items() if k != "initial_cwd"})

    def test_shell_command_is_wrapped_as_its_shell_form_without_shell(self):
        suite = self.suite()
        with mock.patch.object(RUNNER, "normalize_spawn_command",
                               return_value=("fixture.cmd --flag", True, "/custom/sh")):
            _, (argv, kwargs) = self.spawn(suite)
        self.assertEqual(argv[argv.index("--") + 1:], ["/custom/sh", "-c", "fixture.cmd --flag"])
        self.assertIs(kwargs["shell"], False)
        self.assertIsNone(kwargs["executable"])

    def test_without_capture_the_spawn_is_identical_to_main(self):
        suite = self.suite()
        _, (argv, kwargs) = self.spawn(suite, capture=False)
        self.assertEqual(argv, [str(word) for word in suite.argv])
        self.assertEqual(set(kwargs), {"cwd", "env", "capture_output", "text", "timeout", "shell",
                                       "executable", "encoding", "errors"})
        self.assertIs(kwargs["shell"], False)
        self.assertIsNone(kwargs["executable"])
        self.assertEqual(kwargs["timeout"], suite.timeout_s)
        self.assertEqual(kwargs["cwd"], str(suite.cwd))
        self.assertNotIn("LEAF_PROCESS_CAPTURE", kwargs["env"])
        self.assertFalse(list(self.logs.rglob("capture-context.json")))

    def test_refused_or_missing_context_fails_open_to_the_untraced_spawn(self):
        suite = self.suite()
        _, (argv, _) = self.spawn(suite, valid=False)
        self.assertEqual(argv, [str(word) for word in suite.argv])
        (self.context_dir / "base-context.json").unlink()
        _, (argv, _) = self.spawn(suite)
        self.assertEqual(argv, [str(word) for word in suite.argv])

    def test_concurrent_suites_get_distinct_groups_and_context_files(self):
        suites = [self.suite("suite-a"), self.suite("suite-b")]
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return completed(argv)
        with mock.patch.dict(os.environ, self.env()), \
             mock.patch.object(RUNNER, "validate_capture_context", return_value=True), \
             mock.patch.object(RUNNER.subprocess, "run", side_effect=fake_run), \
             ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda suite: RUNNER.run_suite(suite, self.logs, 1), suites))
        contexts = sorted({argv[argv.index("--context") + 1] for argv in calls})
        self.assertEqual(len(contexts), 2)
        groups = sorted(json.loads(Path(path).read_text(encoding="utf-8"))["capture_group"] for path in contexts)
        self.assertEqual(groups, ["suite-a-1", "suite-b-1"])

    def test_supervisor_exit_code_is_the_suite_rc(self):
        result, _ = self.spawn(self.suite(), code=5)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.note, "exit 5")

    def test_process_shards_move_beside_the_read_sets(self):
        suite = self.suite()
        report_dir = self.logs.resolve() / "test-reports" / suite.id / "1"

        def fake_run(argv, **kwargs):
            shard = report_dir / "readsets" / suite.id / "1" / "tree-process.json"
            shard.parent.mkdir(parents=True)
            shard.write_text("{}", encoding="utf-8")
            (report_dir / "reports").mkdir()
            (report_dir / "reports" / ("process-tree-" + suite.id + "-1.json")).write_text("{}", encoding="utf-8")
            return completed(argv)
        with mock.patch.dict(os.environ, self.env()), \
             mock.patch.object(RUNNER, "validate_capture_context", return_value=True), \
             mock.patch.object(RUNNER.subprocess, "run", side_effect=fake_run):
            RUNNER.run_suite(suite, self.logs, 1)
        self.assertTrue((self.logs / "readsets" / suite.id / "1" / "tree-process.json").is_file())
        self.assertFalse(list((report_dir / "readsets").rglob("*.json")))
        self.assertTrue((report_dir / "reports" / ("process-tree-" + suite.id + "-1.json")).is_file())

    def test_trusted_validator_accepts_a_posix_context_and_refuses_a_bad_group(self):
        context = dict(self.base, capture_group="suite-1", suites=[], suites_deferred=True,
                       seed_fds={"1": {"kind": "supervisor"}, "2": {"kind": "supervisor"}})
        self.assertTrue(RUNNER.validate_capture_context(self.trusted, context))
        self.assertFalse(RUNNER.validate_capture_context(self.trusted, dict(context, capture_group="a%2Fb-1")))
        self.assertFalse(RUNNER.validate_capture_context(self.trusted, dict(context, initial_cwd="C:\\src")))


class ReceiptTests(CaptureFixture):
    def receipt(self, suite, **fields):
        path = (self.logs / "test-reports" / suite.id / "1" / "reports" /
                ("trace-receipt-" + suite.id + "-1.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"schema": "leaf.ci.trace-receipt.v1", "capture_group": suite.id + "-1",
               "facility_available": True, "capture_complete": True, "capture_errors": [],
               "tracer_exit_code": 0}
        doc.update(fields)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def test_read_test_report_carries_receipt_fields_and_names_a_missing_receipt(self):
        suite = self.suite()
        with mock.patch.dict(os.environ, {"LEAF_PROCESS_CAPTURE": "1"}):
            missing = RUNNER.read_test_report(suite, self.logs, 1, "PASS")
            self.assertEqual(missing["capture_errors"], ["capture_receipt_missing"])
            self.assertIs(missing["capture_complete"], False)
            self.assertIs(missing["test_report_complete"], True)
            self.receipt(suite)
            report = RUNNER.read_test_report(suite, self.logs, 1, "PASS")
            self.assertEqual({k: report[k] for k in ("capture_facility_available", "capture_complete",
                                                      "capture_errors", "tracer_exit_code")},
                             {"capture_facility_available": True, "capture_complete": True,
                              "capture_errors": [], "tracer_exit_code": 0})
            self.receipt(suite, capture_complete=False, tracer_exit_code=137,
                         capture_errors=["e%02d" % n for n in range(20)])
            report = RUNNER.read_test_report(suite, self.logs, 1, "FAIL")
            self.assertEqual(len(report["capture_errors"]), RUNNER.CAPTURE_ERROR_LIMIT + 1)
            self.assertEqual(report["capture_errors"][-1], "capture_errors_truncated")
            self.assertEqual(report["tracer_exit_code"], 137)
            self.receipt(suite, capture_group="other-1")
            self.assertEqual(RUNNER.read_test_report(suite, self.logs, 1, "PASS")["capture_errors"],
                             ["capture_receipt_mismatch"])
            RUNNER.record_attempt(RUNNER.Result(suite, "PASS", "ok", 0.0), self.logs, 1)
        row = json.loads((self.logs / "attempts" / (suite.id + ".jsonl")).read_text(encoding="utf-8"))
        self.assertEqual(row["capture_errors"], ["capture_receipt_mismatch"])
        self.assertNotIn("capture_errors", RUNNER.read_test_report(suite, self.logs, 1, "PASS"))


class SuitesFileTests(CaptureFixture):
    def test_pytest_plugin_writes_suites_file_and_readsets_hold_no_python_shards(self):
        (self.work / "sample").mkdir()
        (self.work / "sample" / "test_sample.py").write_text(
            "def test_one():\n    pass\n\ndef test_two():\n    pass\n", encoding="utf-8")
        suite = RUNNER.Suite("sample", "pytest fixture", "pytest", self.work / "sample",
                             [sys.executable, "-B", "-m", "pytest", "test_sample.py", "-q",
                              "-p", "no:cacheprovider"], 2)
        env = dict(self.env(capture=False), LEAF_PROCESS_CAPTURE="1", PYTHONPATH=str(self.trusted),
                   PYTHONSAFEPATH="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="")
        with mock.patch.dict(os.environ, env):
            result = RUNNER.run_suite_guarded(suite, self.logs, 1)
        self.assertEqual(result.status, "PASS", result.log_path.read_text(encoding="utf-8"))
        rows = json.loads((self.logs / "test-reports" / "sample" / "1" / "capture-suites.json").read_text(
            encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({k: row[k] for k in ("suite_id", "attempt", "worker")},
                         {"suite_id": "sample", "attempt": 1, "worker": "tree"})
        self.assertEqual(row["test_ids"], ["sample::sample/test_sample.py::test_one",
                                           "sample::sample/test_sample.py::test_two"])
        self.assertTrue(row["outcomes_ref"].startswith("readsets/sample/1/attempts-"))
        self.assertTrue((self.logs / row["outcomes_ref"]).is_file())
        path = self.logs / "test-reports" / "sample" / "1" / "capture-suites.json"
        self.assertEqual(SUPERVISOR.load_suites(path), rows)
        # The diagnostics split: Python read sets beside readsets/, never inside it.
        self.assertFalse([p for p in (self.logs / "readsets").rglob("*.json")])
        self.assertTrue(list((self.logs / "diagnostics" / "readsets" / "sample" / "1").glob("*.json")))

    def test_capture_suites_helper_shape(self):
        plugin = load("leaf_process_tree_pytest_selection", CI / "pytest_selection.py", CI)
        rows = plugin.capture_suites("s", 2, ["b.py::t", "a.py::t", "a.py::t"], "readsets/s/2/attempts-main-1.jsonl")
        self.assertEqual(rows, [{"suite_id": "s", "attempt": 2, "worker": "tree",
                                 "test_ids": ["s::a.py::t", "s::b.py::t"],
                                 "outcomes_ref": "readsets/s/2/attempts-main-1.jsonl"}])
        TREE.validate_suites(rows)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the reporter row writer")
    def test_vitest_reporter_writes_its_own_document_as_outcomes_ref(self):
        code = ("import Vitest from " + json.dumps((CI / "reporters" / "vitest-leaf.mjs").as_uri()) + ";\n"
                "import { resolve } from 'node:path';\n"
                "const root = process.env.LEAF_READSET_ROOT; const v = new Vitest(); v.onInit({config: {root}});\n"
                "v.onFinished([{filepath: resolve(root, 'case.test.js'), tasks: [\n"
                "  {name: 'same', type: 'test', result: {state: 'pass'}},\n"
                "  {name: 'same', type: 'test', result: {state: 'fail'}}]}], []);\n")
        reports = self.work / "reports"
        env = dict(os.environ, LEAF_READSET_ROOT=str(self.work), LEAF_READSET_SUITE="web.vitest",
                   LEAF_READSET_ATTEMPT="1", LEAF_TEST_REPORT_DIR=str(reports), LEAF_PROCESS_CAPTURE="1")
        proc = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code],
                              cwd=self.work, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = list(reports.glob("tests-vitest-*.json"))
        self.assertEqual(len(documents), 1)
        document = json.loads(documents[0].read_text(encoding="utf-8"))
        rows = json.loads((reports / "capture-suites.json").read_text(encoding="utf-8"))
        self.assertEqual(rows, [{"suite_id": "web.vitest", "attempt": 1, "worker": "tree",
                                 "test_ids": document["test_ids"],
                                 "outcomes_ref": "reports/web%2Evitest/1/" + documents[0].name}])
        self.assertEqual(len(set(rows[0]["test_ids"])), 2)
        self.assertEqual(RUNNER.encoded_suite_id("web.vitest"), "web%2Evitest")
        env.pop("LEAF_PROCESS_CAPTURE")
        shutil.rmtree(reports)
        subprocess.run([shutil.which("node"), "--input-type=module", "-e", code],
                       cwd=self.work, env=env, capture_output=True, text=True, timeout=30, check=True)
        self.assertFalse((reports / "capture-suites.json").exists())


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leaf-process-manifest-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.catalog = {"schema": "leaf.ci.test-catalog.v1", "suites": [{"id": "s", "test_ids": []}]}
        _, self.catalog["catalog_sha256"] = SELECT.catalog_info(self.catalog)
        self.bindings = {"run_id": RUN_ID, "source_sha": SHA, "source_tree": "b" * 40, "capture_sha": "c" * 40,
                         "catalog_sha256": self.catalog["catalog_sha256"]}

    def shard(self, epoch="f" * 64, **extra):
        doc = dict(self.bindings, schema="leaf.ci.readset.v1", suite_id="s", worker="tree", attempt=1,
                   trace_kind="linux-process-tree", python_only=False, capture_epoch=epoch,
                   outcomes_ref="readsets/s/1/attempts-main-1.jsonl")
        doc.update(extra)
        return doc

    def certificate(self, epoch="f" * 64):
        return dict(self.bindings, schema="leaf.ci.process-tree.v1", capture_group="s-1", capture_epoch=epoch,
                    capture_epoch_manifest={"schema": "leaf.ci.capture-epoch.v1"})

    def inputs(self):
        return {"repo": "leaf-web-demo", "run_id": RUN_ID, "source_sha": SHA, "source_tree": "b" * 40,
                "capture_sha": "c" * 40, "image": "img", "execution_mode": "full",
                "full_run_complete": True, "test_id_reporting_complete": True}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")

    def test_partitioner_keeps_capture_reports_skips_diagnostics_and_rejects_foreign_shards(self):
        readsets = self.work / "readsets"
        self.write(readsets / "s" / "1" / "tree-process.json", self.shard())
        self.write(readsets / "s" / "1" / "attempts-main-1.jsonl", "{}\n")
        self.write(readsets / "s" / "1" / "foreign-process.json", dict(self.shard(), run_id="other"))
        self.write(readsets / "diagnostics" / "readsets" / "s" / "1" / "main-1.json", {"run_id": "other"})
        self.write(readsets / "reports" / "process-tree-s-1.json", self.certificate())
        self.write(readsets / "reports" / "trace-receipt-s-1.json", {"capture_group": "s-1"})
        rejected = self.work / "rejected"
        self.assertEqual(MANIFEST.partition_readsets(readsets, rejected, self.catalog, RUN_ID), 1)
        for kept in ("s/1/tree-process.json", "s/1/attempts-main-1.jsonl", "diagnostics/readsets/s/1/main-1.json",
                     "reports/process-tree-s-1.json", "reports/trace-receipt-s-1.json"):
            self.assertTrue((readsets / kept).is_file(), kept)
        self.assertEqual(sorted(p.relative_to(rejected).as_posix() for p in rejected.rglob("*") if p.is_file()),
                         ["s/1/foreign-process.json"])

    def test_manifest_binds_the_agreed_epoch_and_names_a_mixed_one(self):
        manifest = MANIFEST.build_manifest(self.inputs(), self.catalog, [self.shard()], {},
                                           [self.certificate(), self.certificate()])
        self.assertEqual(manifest["capture_epoch"], "f" * 64)
        self.assertEqual(manifest["capture_epoch_manifest"], {"schema": "leaf.ci.capture-epoch.v1"})
        self.assertEqual(manifest["provider_binding"]["capture_epoch"], "f" * 64)
        self.assertEqual(manifest["provider_binding"]["toolchain_fingerprint"], manifest["toolchain_fingerprint"])
        self.assertEqual(manifest["toolchain_fingerprint"], MANIFEST.toolchain_fingerprint("img", "c" * 40))
        self.assertEqual(manifest["workers_by_suite"], {"s": ["tree"]})
        mixed = MANIFEST.build_manifest(self.inputs(), self.catalog, [], {},
                                        [self.certificate(), self.certificate("0" * 64)])
        self.assertNotIn("capture_epoch", mixed)
        self.assertEqual(mixed["completeness_reasons"], ["mixed_capture_epoch"])
        self.assertIs(mixed["full_run_complete"], False)
        plain = MANIFEST.build_manifest(self.inputs(), self.catalog, [], {})
        self.assertNotIn("capture_epoch", plain)
        self.assertNotIn("completeness_reasons", plain)
        with self.assertRaisesRegex(ValueError, "process_tree_binding_mismatch"):
            MANIFEST.build_manifest(self.inputs(), self.catalog, [], {}, [dict(self.certificate(), run_id="x")])
        with self.assertRaisesRegex(ValueError, "shard_epoch_mismatch"):
            MANIFEST.build_manifest(self.inputs(), self.catalog, [self.shard("0" * 64)], {}, [self.certificate()])

    def test_manifest_main_reads_certificates_and_ignores_diagnostics(self):
        readsets = self.work / "logs" / "readsets"
        self.write(readsets / "s" / "1" / "tree-process.json", self.shard())
        self.write(self.work / "logs" / "diagnostics" / "readsets" / "s" / "1" / "main-1.json", {"bad": True})
        self.write(readsets / "diagnostics" / "stray.json", {"bad": True})
        self.write(self.work / "sel" / "reports" / "process-tree-s-1.json", self.certificate())
        self.write(self.work / "catalog.json", self.catalog)
        output = self.work / "full-run.json"
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.inputs()))), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            code = MANIFEST.main(["--catalog", str(self.work / "catalog.json"), "--readsets", str(readsets),
                                  "--reports", str(self.work / "sel" / "reports"), "--output", str(output)])
        self.assertEqual(code, 0, errors.getvalue())
        manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["capture_epoch"], "f" * 64)
        self.assertEqual(manifest["workers_by_suite"], {"s": ["tree"]})


class CatalogAndCopyTests(unittest.TestCase):
    def test_catalog_declares_capture_without_touching_trace_kind_or_python_only(self):
        with mock.patch.dict(os.environ, {"LEAF_PROCESS_CAPTURE": ""}):
            plain = RUNNER.selection_catalog(ROOT)
        with mock.patch.dict(os.environ, {"LEAF_PROCESS_CAPTURE": "1"}):
            captured = RUNNER.selection_catalog(ROOT)
        self.assertEqual(plain["catalog_sha256"], captured["catalog_sha256"])
        self.assertTrue(all("capture" not in row for row in plain["suites"]))
        self.assertTrue(all(row["capture"] == "linux-process-tree" for row in captured["suites"]))
        self.assertTrue(all(row["python_only"] is False for row in captured["suites"]))
        self.assertEqual([{k: v for k, v in row.items() if k != "capture"} for row in captured["suites"]],
                         plain["suites"])

    def test_helper_copies_equal_the_frozen_tools_files(self):
        ref = os.environ.get("LEAF_TOOLS_REF")
        if not ref:
            self.skipTest("LEAF_TOOLS_REF is unset; the planner's verify compares the copies")
        for name in COPIES:
            ours = (CI / name).read_bytes().replace(b"\r\n", b"\n")
            theirs = (Path(ref) / "selector" / name).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(ours).hexdigest(), hashlib.sha256(theirs).hexdigest(), name)


if __name__ == "__main__":
    unittest.main()
