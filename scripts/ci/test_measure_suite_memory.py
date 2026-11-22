"""Focused Linux subprocess integration checks for the standalone RSS wrapper."""

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import textwrap
import time
import unittest


SCRIPT = Path(__file__).with_name("measure_suite_memory.py")
PREFIX = "LEAF_SUITE_MEMORY "


@unittest.skipUnless(sys.platform == "linux", "requires Linux /proc")
class SuiteMemoryTests(unittest.TestCase):
    def invoke(self, code, timeout="5", extra=()):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--suite", "memory-integration",
             "--timeout", timeout, *extra, "--", sys.executable, "-c", code],
            capture_output=True, text=True, timeout=9,
        )

    def record(self, result):
        records = [line[len(PREFIX):] for line in result.stdout.splitlines()
                   if line.startswith(PREFIX)]
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        record = json.loads(records[0])
        self.assertEqual(record["schema"], "leaf.ci.suite-memory.v1")
        self.assertEqual(record["suite"], "memory-integration")
        self.assertEqual(record["platform"], "linux")
        self.assertGreater(record["sample_count"], 0)
        self.assertGreaterEqual(record["elapsed_seconds"], 0)
        self.assertTrue(record["measurement_caveats"])
        self.assertNotIn("command", record)
        self.assertNotIn("environment", record)
        return record

    def test_child_and_grandchild_allocations_are_both_included(self):
        def workload(mib):
            grandchild = (
                "import time; "
                f"allocation = bytearray({mib} * 1024 * 1024); "
                "time.sleep(0.7)"
            )
            child = textwrap.dedent(f"""
                import ctypes, subprocess, sys
                # Exercise stat comm parsing with both spaces and parentheses.
                ctypes.CDLL(None).prctl(15, b'leaf ) rss (x', 0, 0, 0)
                allocation = bytearray({mib} * 1024 * 1024)
                child = subprocess.Popen([sys.executable, '-c', {grandchild!r}])
                child.wait()
            """)
            return (
                "import subprocess, sys; "
                f"subprocess.run([sys.executable, '-c', {child!r}], check=True)"
            )

        baseline = self.invoke(workload(0))
        allocated = self.invoke(workload(24))
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertEqual(allocated.returncode, 0, allocated.stderr)
        before = self.record(baseline)
        after = self.record(allocated)
        # One 24 MiB allocation alone cannot explain this increase.
        self.assertGreaterEqual(
            after["sampled_peak_rss_bytes"] - before["sampled_peak_rss_bytes"],
            40 * 1024 * 1024,
        )
        self.assertFalse(after["timed_out"])

    def test_nonzero_exit_and_inherited_output(self):
        result = self.invoke(
            "import sys; print('visible stdout', end=''); "
            "print('visible stderr', file=sys.stderr); sys.exit(7)"
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("visible stdout", result.stdout)
        self.assertIn("visible stderr", result.stderr)
        record = self.record(result)
        self.assertEqual(record["exit_code"], 7)
        self.assertFalse(record["timed_out"])

    def assert_group_gone(self, pgid):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        self.fail(f"owned process group {pgid} survived cleanup")

    def test_timeout_reaps_descendant_and_removes_group(self):
        code = textwrap.dedent("""
            import os, signal, subprocess, sys, time
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            def finish(signum, frame):
                child.wait(timeout=2)
                sys.exit(0)
            signal.signal(signal.SIGTERM, finish)
            print('GROUP', os.getpgrp(), flush=True)
            time.sleep(30)
        """)
        result = self.invoke(code, timeout="0.8")
        self.assertEqual(result.returncode, 124, result.stderr)
        record = self.record(result)
        self.assertTrue(record["timed_out"])
        pgid = int(next(line.split()[1] for line in result.stdout.splitlines()
                        if line.startswith("GROUP ")))
        self.assert_group_gone(pgid)

    def test_timeout_kills_command_ignoring_term(self):
        result = self.invoke(
            "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('GROUP', os.getpgrp(), flush=True); time.sleep(30)",
            timeout="0.8",
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertTrue(self.record(result)["timed_out"])
        pgid = int(next(line.split()[1] for line in result.stdout.splitlines()
                        if line.startswith("GROUP ")))
        self.assert_group_gone(pgid)

    def test_interrupt_cleans_only_command_group(self):
        code = (
            "import os, time; print('GROUP', os.getpgrp(), flush=True); "
            "time.sleep(30)"
        )
        wrapper = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--suite", "memory-integration",
             "--timeout", "30", "--interval", "20", "--",
             sys.executable, "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        pgid = None
        try:
            ready, _, _ = select.select([wrapper.stdout], [], [], 3)
            self.assertTrue(ready, "command did not become ready")
            first_line = wrapper.stdout.readline()
            self.assertTrue(first_line.startswith("GROUP "), first_line)
            pgid = int(first_line.split()[1])
            self.assertNotEqual(pgid, os.getpgrp())
            wrapper.send_signal(signal.SIGINT)
            stdout, stderr = wrapper.communicate(timeout=3)
            result = subprocess.CompletedProcess(
                wrapper.args, wrapper.returncode, first_line + stdout, stderr
            )
            self.assertEqual(result.returncode, 130, stderr)
            self.assertFalse(self.record(result)["timed_out"])
            self.assert_group_gone(pgid)
        finally:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if wrapper.poll() is None:
                wrapper.kill()
            wrapper.communicate(timeout=3)

    def test_invalid_options_fail_cleanly_before_spawn(self):
        cases = [
            ("--timeout", "nan"), ("--timeout", "inf"), ("--timeout", "0"),
            ("--timeout", "nope"), ("--interval", "-1"),
            ("--sample-interval", "nan"), ("--suite", "bad label"),
        ]
        for option, value in cases:
            with self.subTest(option=option, value=value):
                result = self.invoke("print('SHOULD_NOT_RUN')", extra=(option, value))
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("SHOULD_NOT_RUN", result.stdout)
                self.assertNotIn(PREFIX, result.stdout)


if __name__ == "__main__":
    unittest.main()
