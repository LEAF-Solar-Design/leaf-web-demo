"""Opt-in RSS observer checks, with real Linux children and bounded lifetimes."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


def load_memory_runner():
    spec = importlib.util.spec_from_file_location(
        "gate_memory_test_runner", Path(__file__).with_name("run-all-gates.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves annotations here.
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform == "linux", "requires Linux /proc")
class GateSuiteMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_memory_runner()

    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {
            "LEAF_GATE_MEM": "1", "LEAF_PROCESS_CAPTURE_CONTEXT": "",
            "LEAF_PROCESS_CAPTURE_OUT": "", "LEAF_GATE_FAULT_INJECT": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name)

    def suite(self, code, timeout=4):
        return self.runner.Suite(
            "memory-probe", "RSS probe", "script", self.workdir,
            [sys.executable, "-c", code], None, timeout_s=timeout)

    def kwargs(self, timeout=4):
        return dict(cwd=str(self.workdir), env=dict(os.environ), capture_output=True,
                    text=True, encoding="utf-8", errors="replace", shell=False,
                    executable=None, timeout=timeout)

    def record(self, output, attempt=3):
        lines = [line.removeprefix("LEAF_SUITE_MEMORY ")
                 for line in output.splitlines() if line.startswith("LEAF_SUITE_MEMORY ")]
        self.assertEqual(len(lines), 1, output)
        record = json.loads(lines[0])
        self.assertEqual(record["schema"], "leaf.ci.suite-memory.v1")
        self.assertEqual(record["suite"], "memory-probe")
        self.assertEqual(record["attempt"], attempt)
        self.assertEqual(record["platform"], "linux")
        self.assertNotIn("command", record)
        self.assertNotIn("environment", record)
        return record

    def assert_observers_stopped(self):
        self.assertFalse([thread for thread in threading.enumerate()
                          if thread.name == "leaf-suite-memory"])

    def execute(self, suite):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print(" ... memory-probe ", end="")  # Real runner progress has no newline.
            result = self.runner.run_suite(suite, self.workdir, attempt=3,
                                           reporting_disabled=True)
        self.assert_observers_stopped()
        return result, self.record(output.getvalue())

    def test_disabled_preserves_exact_original_spawn(self):
        suite = self.suite("")
        command = suite.argv
        kwargs = self.kwargs()
        spawn = mock.Mock(return_value=subprocess.CompletedProcess(command, 7, "out", "err"))
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"LEAF_GATE_MEM": "0"}), \
                mock.patch.object(self.runner, "_suite_memory_helper") as helper, \
                contextlib.redirect_stdout(output):
            result = self.runner.run_memory_command(suite, 3, spawn, command, **kwargs)
        spawn.assert_called_once_with(command, **kwargs)
        self.assertIs(spawn.call_args.args[0], command)
        self.assertIs(spawn.call_args.kwargs["env"], kwargs["env"])
        helper.assert_not_called()
        self.assertEqual(result.returncode, 7)
        self.assertEqual(output.getvalue(), "")

    def test_real_root_and_child_rss_and_suite_outcome(self):
        def workload(mib):
            child = f"import time; held = bytearray({mib}*1024*1024); time.sleep(0.85)"
            return ("import subprocess, sys; "
                    f"held = bytearray({mib}*1024*1024); "
                    f"child = subprocess.Popen([sys.executable, '-c', {child!r}]); "
                    "child.wait(); print('suite stdout'); "
                    "print('suite stderr', file=sys.stderr); sys.exit(7)")

        _, baseline = self.execute(self.suite(workload(0)))
        result, allocated = self.execute(self.suite(workload(16)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("exit 7", result.note)
        log = result.log_path.read_text()
        self.assertIn("suite stdout", log)
        self.assertIn("suite stderr", log)
        self.assertNotIn("LEAF_SUITE_MEMORY", log)
        self.assertEqual(allocated["exit_code"], 7)
        self.assertGreaterEqual(allocated["sample_count"], 2)
        self.assertTrue(allocated["measurement_caveats"])
        self.assertNotIn("unavailable_reason", allocated)
        # Both allocations are needed to explain this increase.
        self.assertGreater(allocated["sampled_peak_rss_bytes"] -
                           baseline["sampled_peak_rss_bytes"], 26 * 1024 * 1024)

    def test_timeout_retains_partial_output_and_stops_observer(self):
        result, record = self.execute(self.suite(
            "import time; print('before timeout', flush=True); time.sleep(3)", timeout=0.4))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("before timeout", result.log_path.read_text())
        self.assertIn("[TIMEOUT >0.4s]", result.log_path.read_text())
        self.assertEqual(record["exit_code"], 124)

    def test_helper_and_sampler_failures_do_not_change_child_outcome(self):
        class FailingSampler:
            def __init__(self, pid):
                pass

            def sample(self):
                raise OSError("private details must not be emitted")

        class MissingSampler(FailingSampler):
            def sample(self):
                return 0

        class BrokenSampler(FailingSampler):
            def __init__(self, pid):
                raise OSError("private initialization detail")

        for failure in ("helper", "sample", "missing", "start"):
            with self.subTest(failure=failure):
                patch = (mock.patch.object(self.runner, "_suite_memory_helper",
                                           side_effect=ImportError("private path"))
                         if failure == "helper" else
                         mock.patch.object(self.runner, "_suite_memory_helper",
                                           return_value=SimpleNamespace(
                                               CAVEATS=["test caveat"], TreeSampler={
                                                   "sample": FailingSampler,
                                                   "missing": MissingSampler,
                                                   "start": BrokenSampler,
                                               }[failure])))
                with patch:
                    result, record = self.execute(self.suite(
                        "import sys; print('child retained'); sys.exit(9)"))
                self.assertEqual(result.status, "FAIL")
                self.assertIn("child retained", result.log_path.read_text())
                self.assertEqual(record["exit_code"], 9)
                self.assertIsNone(record["sampled_peak_rss_bytes"])
                self.assertEqual(record["unavailable_reason"], {
                    "helper": "helper_unavailable", "sample": "sample_failed",
                    "missing": "no_samples", "start": "monitor_start_failed",
                }[failure])
                self.assertNotIn("private", json.dumps(record))

    def test_spawn_failure_has_unavailable_attempt_record(self):
        suite = self.suite("")
        suite.argv = [str(self.workdir / "missing-command")]
        result, record = self.execute(suite)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("spawn failure", result.note)
        self.assertEqual(record["exit_code"], 127)
        self.assertEqual(record["sample_count"], 0)
        self.assertIsNone(record["sampled_peak_rss_bytes"])

    def test_unsupported_and_custom_spawn_keep_exact_call(self):
        suite = self.suite("")
        kwargs = self.kwargs()
        for platform, reason in (("linux", "custom_spawn"), ("win32", "unsupported_platform")):
            with self.subTest(platform=platform):
                spawn = mock.Mock(return_value=subprocess.CompletedProcess(suite.argv, 0, "yes", ""))
                output = io.StringIO()
                with mock.patch.object(sys, "platform", platform), contextlib.redirect_stdout(output):
                    result = self.runner.run_memory_command(suite, 3, spawn, suite.argv, **kwargs)
                spawn.assert_called_once_with(suite.argv, **kwargs)
                record = json.loads(output.getvalue().lstrip().removeprefix("LEAF_SUITE_MEMORY "))
                self.assertEqual(result.stdout, "yes")
                self.assertIsNone(record["sampled_peak_rss_bytes"])
                self.assertEqual(record["unavailable_reason"], reason)

    def test_capture_grace_and_escalation_are_unchanged(self):
        suite = self.suite("")
        for observed in (False, True):
            for escalate in (False, True):
                with self.subTest(observed=observed, escalate=escalate):
                    proc = mock.MagicMock(pid=999999999, returncode=-15)
                    proc.__enter__.return_value = proc
                    expired = subprocess.TimeoutExpired(suite.argv, 0.1)
                    proc.communicate.side_effect = (
                        [expired, expired, ("grace stdout", "grace stderr")] if escalate else
                        [expired, ("grace stdout", "grace stderr")])
                    output = io.StringIO()
                    with mock.patch.object(self.runner.subprocess, "Popen", return_value=proc), \
                            mock.patch.dict(os.environ, {"LEAF_GATE_MEM": "1" if observed else "0"}), \
                            contextlib.redirect_stdout(output):
                        with self.assertRaises(subprocess.TimeoutExpired) as caught:
                            self.runner.run_memory_command(
                                suite, 3, self.runner.run_capture_command, suite.argv,
                                **self.kwargs(timeout=0.1))
                    self.assertEqual(caught.exception.output, "grace stdout")
                    self.assertEqual(caught.exception.stderr, "grace stderr")
                    calls = [mock.call.communicate(timeout=0.1), mock.call.terminate(),
                             mock.call.communicate(timeout=self.runner.PROCESS_CAPTURE_TERMINATE_GRACE_S)]
                    if escalate:
                        calls += [mock.call.kill(), mock.call.communicate()]
                    self.assertEqual(proc.method_calls, calls)
                    self.assert_observers_stopped()
                    if observed:
                        self.assertEqual(self.record(output.getvalue())["exit_code"], 124)
                    else:
                        self.assertEqual(output.getvalue(), "")

    def test_output_failure_does_not_change_success(self):
        class BrokenSink(io.StringIO):
            def write(self, value):
                raise OSError("sink unavailable")

        suite = self.suite("print('child output')")
        with contextlib.redirect_stdout(BrokenSink()):
            result = self.runner.run_memory_command(
                suite, 3, subprocess.run, suite.argv, **self.kwargs())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "child output\n")
        self.assert_observers_stopped()

    def test_browser_markers_surround_observed_attempt(self):
        suite = self.suite("import sys; print('browser output'); sys.exit(6)")
        kwargs = self.kwargs()
        kwargs["env"]["LEAF_GATE_BROWSER_SUITE"] = suite.id
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.runner.run_browser_command(
                suite, lambda *args, **kw: self.runner.run_memory_command(
                    suite, 3, subprocess.run, *args, **kw), suite.argv, **kwargs)
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("GATE_BROWSER_BEGIN id=memory-probe "))
        self.assertTrue(lines[-1].startswith("GATE_BROWSER_END id=memory-probe "))
        self.assertTrue(lines[-1].endswith(" exit=6"))
        self.assertEqual(result.returncode, 6)
        self.assertEqual(result.stdout, "browser output\n")
        self.assertEqual(self.record(output.getvalue())["exit_code"], 6)
        self.assert_observers_stopped()


if __name__ == "__main__":
    unittest.main()
