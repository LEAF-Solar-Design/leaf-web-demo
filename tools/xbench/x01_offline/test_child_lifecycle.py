"""Bounded synthetic subprocess controls; never invoke the live grader."""

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import run_offline_checks as runner
import validate_offline_report as validator


# Include interpreter startup (and two descendant startups) on Windows hosts.
# The outer watchdog still bounds all three captures plus their finite cleanup.
CAPTURE_TIMEOUT = 5
OUTER_TIMEOUT = 30


def recorded_pids(path):
    if not path.exists():
        return []
    return [int(line) for line in path.read_text().splitlines()]


def pid_alive(pid):
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        api.OpenProcess.restype = ctypes.c_void_p
        api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
                return False
            raise OSError("cannot inspect recorded PID")
        try:
            code = ctypes.c_ulong()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise OSError("cannot inspect process exit")
            return code.value == 259  # STILL_ACTIVE
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True  # Zombies count as present: Linux must actually reap them.
    except ProcessLookupError:
        return False


def cleanup_pid(pid):
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        api.OpenProcess.restype = ctypes.c_void_p
        api.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenProcess(1, False, pid)
        if handle:
            try:
                api.TerminateProcess(handle, 1)
            finally:
                api.CloseHandle(handle)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def child_source(mode, path):
    record = ("import os, subprocess, sys, time\n"
              "fd = os.open(" + repr(str(path)) + ", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
              "os.write(fd, (str(os.getpid()) + '\\n').encode()); os.close(fd)\n")
    leaf = record + "time.sleep(15)\n"
    spawn_leaf = "subprocess.Popen([sys.executable, '-c', " + repr(leaf) + "])\n"
    if mode == "orphan":
        return record + spawn_leaf + "os._exit(0)\n"
    if mode in ("grandchild", "flood"):
        descendant = record + spawn_leaf
        if mode == "flood":
            # Ensure the grandchild has started before the output cap fires.
            descendant += ("from pathlib import Path\n"
                           "ready_deadline = time.monotonic() + 3\n"
                           "while len(Path(" + repr(str(path)) + ").read_text().splitlines()) < 3 and time.monotonic() < ready_deadline:\n"
                           "    time.sleep(.005)\n"
                           "while True: os.write(1, b'x' * 4096)\n")
        else:
            descendant += "time.sleep(15)\n"
        return (record + "subprocess.Popen([sys.executable, '-c', " + repr(descendant) + "])\n"
                + "os._exit(0)\n")
    if mode == "alive":
        return record + spawn_leaf + "time.sleep(15)\n"
    if mode == "closed":
        return record + "os.close(1); os.close(2); time.sleep(15)\n"
    return record + "print('synthetic normal output')\n"


def worker(mode, path):
    command = [sys.executable, "-c", child_source(mode, path)]
    commands = [(runner.CHECKERS[0], command)]
    commands += [(name, [sys.executable, "-c", "print('OK')"])
                 for name in runner.CHECKERS[1:]]
    if mode == "containment":
        from unittest.mock import patch
        with patch.object(runner._WindowsJob, "start", side_effect=OSError("synthetic assignment failure")):
            report = runner.run_checks(commands, timeout=CAPTURE_TIMEOUT)
    else:
        report = runner.run_checks(commands, timeout=CAPTURE_TIMEOUT)
    print(json.dumps(dict(report=report, validation=validator.validate_report(report),
                          readers=[thread.name for thread in threading.enumerate()
                                   if thread.name.startswith("x01-capture")])))


class ChildLifecycleTests(unittest.TestCase):
    def control(self, mode, reason, count):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pids.txt"
            outer = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                      "--worker", mode, str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            started = time.monotonic()
            try:
                output, errors = outer.communicate(timeout=OUTER_TIMEOUT)
                self.assertEqual(outer.returncode, 0, errors.decode(errors="replace"))
                payload = json.loads(output)
                report = payload["report"]
                self.assertEqual(payload["validation"]["verdict"], "PASS", payload)
                self.assertEqual(payload["readers"], [])
                self.assertEqual([row["checker"] for row in report["checks"]], list(runner.CHECKERS))
                for other in report["checks"][1:]:
                    self.assertEqual(other["verdict"],
                                     "UNREADABLE" if mode == "containment" else "PASS", other)
                    self.assertFalse(other["timeout"], other)
                    if mode == "containment":
                        self.assertEqual(other["diagnostics"], [dict(
                            name="CHILD_CONTAINMENT_ERROR", checker=other["checker"])])
                row = report["checks"][0]
                self.assertIsInstance(row["exit_code"], int)
                self.assertEqual(row["timeout"], reason == "CHILD_TIMEOUT", row)
                self.assertEqual(row["verdict"], "UNREADABLE" if reason else "PASS", row)
                self.assertEqual(report["verdict"], row["verdict"])
                self.assertLessEqual(len(row["output"].encode()), runner.TEXT_LIMIT)
                self.assertLess(row["duration_seconds"], CAPTURE_TIMEOUT + 2)
                if reason:
                    self.assertEqual(row["diagnostics"], [dict(name=reason, checker=runner.CHECKERS[0])])
                else:
                    self.assertIn("synthetic normal output", row["output"])
                pids = recorded_pids(path)
                self.assertEqual(len(pids), count, pids)
                reap_deadline = time.monotonic() + 1
                while any(pid_alive(pid) for pid in pids) and time.monotonic() < reap_deadline:
                    time.sleep(.01)
                self.assertEqual([pid for pid in pids if pid_alive(pid)], [], pids)
                self.assertLess(time.monotonic() - started, OUTER_TIMEOUT + 2)
            finally:
                if outer.poll() is None:
                    outer.kill()
                outer.communicate(timeout=2)
                for pid in recorded_pids(path):
                    if pid_alive(pid):
                        cleanup_pid(pid)

    def test_immediate_parent_exit_descendant_holds_pipe(self):
        self.control("orphan", "CHILD_TIMEOUT", 2)

    def test_immediate_parent_exit_grandchild_holds_pipe(self):
        self.control("grandchild", "CHILD_TIMEOUT", 3)

    def test_parent_remains_alive_on_timeout(self):
        self.control("alive", "CHILD_TIMEOUT", 2)

    def test_closed_pipe_does_not_complete_running_child(self):
        self.control("closed", "CHILD_TIMEOUT", 1)

    def test_flooding_descendant(self):
        self.control("flood", "CHILD_OUTPUT_LIMIT", 3)

    def test_successful_command(self):
        self.control("normal", None, 1)

    @unittest.skipUnless(os.name == "nt", "Windows suspended-child containment")
    def test_assignment_failure_never_runs_child(self):
        self.control("containment", "CHILD_CONTAINMENT_ERROR", 0)

    def test_containment_failure_is_named_without_spawn(self):
        from unittest.mock import patch
        with patch.object(runner.os, "name", "nt"), patch.object(
                runner, "_WindowsJob", side_effect=OSError("synthetic containment failure")), patch.object(
                runner.subprocess, "Popen") as spawn:
            result = runner.capture_child([sys.executable, "-c", "pass"], 1)
        spawn.assert_not_called()
        self.assertEqual(result, (None, False, b"", "CHILD_CONTAINMENT_ERROR"))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
