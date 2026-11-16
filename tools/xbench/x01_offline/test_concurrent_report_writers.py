"""Bounded concurrent use of the production atomic writer on scratch reports."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import run_offline_checks as runner


WRITERS = 3
REPLACEMENTS = 12


def payload(token):
    return dict(token=token, padding=(token + "|") * 4096,
                evidence_kind="synthetic_offline", live_grade=False)


# Each child parks BETWEEN staging/fsync and replacement, on every round.
# The parent observes all parked writers before releasing a round together.
CHILD = r'''
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from run_offline_checks import write_atomic
import run_offline_checks as runner

path, signals = Path(sys.argv[2]), Path(sys.argv[3])
writer, rounds, fail = int(sys.argv[4]), int(sys.argv[5]), sys.argv[6] == "fail"
deadline = time.monotonic() + 7
replace = runner.os.replace
staged = None
sharing_refusal = None

def parked_replace(source, destination):
    global staged, sharing_refusal
    staged = Path(source)
    (signals / ("ready-%d-%d" % (writer, step))).touch()
    while not (signals / ("release-%d" % step)).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("parent handshake deadline")
        time.sleep(.001)
    if fail:
        raise OSError("injected before replace")
    try:
        replace(source, destination)
    except PermissionError as error:
        # Ordinary readers may deny replacement on Windows. Only this
        # explicit refusal at the real replace call is an allowed failure.
        if sys.platform == "win32":
            sharing_refusal = (type(error).__name__ + ": " + str(error))[:512]
        raise

try:
    with patch.object(runner.os, "replace", side_effect=parked_replace):
        for step in range(rounds):
            staged, sharing_refusal = None, None
            token = "%d:%d" % (writer, step)
            report = dict(token=token, padding=(token + "|") * 4096,
                          evidence_kind="synthetic_offline", live_grade=False)
            outcome = dict(status="replaced", diagnostic="")
            try:
                write_atomic(path, report)
            except PermissionError:
                if fail or sharing_refusal is None:
                    raise
                # Parent validates the destination after all round outcomes.
                outcome = dict(status="sharing-refused", diagnostic=sharing_refusal)
            except OSError as error:
                if not fail or str(error) != "injected before replace":
                    raise
                outcome = dict(status="injected-failure", diagnostic=str(error))
                (signals / "failure-observed").touch()
            else:
                if fail:
                    raise AssertionError("injected failure was not raised")
            if staged is None or staged.exists():
                raise AssertionError("staged temporary file was not cleaned up")
            # Publish the completed diagnostic before the done marker.
            (signals / ("outcome-%d-%d" % (writer, step))).write_text(
                json.dumps(outcome), encoding="utf-8")
            (signals / ("done-%d-%d" % (writer, step))).touch()
except Exception as error:
    (signals / ("error-%d" % writer)).write_text(
        (type(error).__name__ + ": " + str(error))[:512], encoding="utf-8")
    sys.exit(1)
'''


class ConcurrentReportWriterTests(unittest.TestCase):
    def scenario(self, fail=False):
        started = time.monotonic()
        deadline = started + 7.5
        children = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signals = root / "signals"
            signals.mkdir()
            path = root / "report.json"
            old = payload("old")
            runner.write_atomic(path, old)
            old_bytes = path.read_bytes()
            allowed = {"old": old}
            writers, rounds = (1, 1) if fail else (WRITERS, REPLACEMENTS)
            for writer in range(writers):
                for step in range(rounds):
                    token = "%d:%d" % (writer, step)
                    allowed[token] = payload(token)

            observations = dict(complete_reads=0, sharing_refused_reads=0)

            def read_complete(permitted, allow_sharing_refusal=False):
                # Successful reads must be complete. Windows can explicitly refuse
                # an open during replacement; this is not a successful validation.
                try:
                    raw = path.read_text(encoding="utf-8")
                except PermissionError:
                    if not allow_sharing_refusal or sys.platform != "win32":
                        raise
                    observations["sharing_refused_reads"] += 1
                    return False
                report = json.loads(raw)
                self.assertIsInstance(report, dict)
                token = report.get("token")
                self.assertIn(token, permitted)
                self.assertEqual(report, allowed[token])
                observations["complete_reads"] += 1
                return True

            def await_markers(names, permitted):
                while True:
                    read_complete(permitted, allow_sharing_refusal=True)
                    if all((signals / name).exists() for name in names):
                        return
                    for writer, child in enumerate(children):
                        code = child.poll()
                        if code is not None and (code != 0 or not (signals / names[writer]).exists()):
                            error = signals / ("error-%d" % writer)
                            diagnostic = (error.read_text(encoding="utf-8")[:512]
                                          if error.exists() else "no child error marker")
                            self.fail("writer %d exited %d: %s" % (writer, code, diagnostic))
                    self.assertLess(time.monotonic(), deadline, "writer handshake deadline")
                    time.sleep(.001)

            try:
                for writer in range(writers):
                    children.append(subprocess.Popen(
                        [sys.executable, "-B", "-c", CHILD, str(runner.ROOT), str(path),
                         str(signals), str(writer), str(rounds), "fail" if fail else "write"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL))
                visible = {"old"}
                replacements = 0
                for step in range(rounds):
                    await_markers(["ready-%d-%d" % (writer, step) for writer in range(writers)],
                                  visible)
                    # All writers have staged full reports, but none has replaced
                    # this round yet. In round zero the old bytes must survive.
                    read_complete(visible)
                    if step == 0:
                        self.assertEqual(path.read_bytes(), old_bytes)
                    if not fail:
                        visible.update("%d:%d" % (writer, step) for writer in range(writers))
                    (signals / ("release-%d" % step)).touch()
                    await_markers(["done-%d-%d" % (writer, step) for writer in range(writers)],
                                  visible)
                    for writer in range(writers):
                        outcome = json.loads((signals / ("outcome-%d-%d" % (writer, step)))
                                             .read_text(encoding="utf-8"))
                        self.assertIn(outcome["status"], {"injected-failure"} if fail else
                                      {"replaced", "sharing-refused"})
                        if outcome["status"] == "replaced":
                            replacements += 1
                        else:
                            self.assertTrue(outcome["diagnostic"])
                            self.assertLessEqual(len(outcome["diagnostic"]), 512)
                            if not fail:
                                self.assertEqual(sys.platform, "win32")
                            read_complete(visible)
                for child in children:
                    self.assertEqual(child.wait(timeout=max(.001, deadline - time.monotonic())), 0)
                read_complete(visible)
                if fail:
                    self.assertTrue((signals / "failure-observed").is_file())
                    self.assertEqual(path.read_bytes(), old_bytes)
                else:
                    self.assertGreater(replacements, 0, "no actual replacement succeeded")
                    self.assertNotEqual(json.loads(path.read_text(encoding="utf-8"))["token"], "old")
                # Only the valid report and handshake directory remain.
                self.assertEqual(set(root.iterdir()), {path, signals})
            finally:
                # Terminate every outstanding child before waiting on any one.
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                for child in children:
                    try:
                        child.wait(timeout=.2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                for child in children:
                    child.wait(timeout=max(.001, started + 9.5 - time.monotonic()))
        self.assertGreater(observations["complete_reads"], rounds)
        self.last_observations = observations
        self.assertLess(time.monotonic() - started, 10)

    def test_concurrent_replacements_are_complete_allowed_payloads(self):
        self.scenario()

    def test_child_failure_before_replace_preserves_report_and_removes_temp(self):
        self.scenario(fail=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
