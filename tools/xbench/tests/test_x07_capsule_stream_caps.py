from pathlib import Path
import shutil
import tempfile
import unittest
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleStreamCaps(unittest.TestCase):
    def test_exact_and_excess_concurrent_stdout_stderr(self):
        for stdout_extra, stderr_extra in ((0, 0), (1, 0), (0, 1), (1, 1)):
            with self.subTest(stdout=stdout_extra, stderr=stderr_extra), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                check = root / 'x07_stream_cap.cjs'
                cap = capsule.MAX_STREAM_BYTES
                # Use Node's queued stream writes so both pipes drain with
                # backpressure instead of assuming blocking raw descriptors.
                check.write_text("process.stdout.write(Buffer.alloc(" + str(cap + stdout_extra) + ",'S'));" +
                                 "process.stderr.write(Buffer.alloc(" + str(cap + stderr_extra) + ",'E'));\n", encoding='utf-8')
                report = capsule.run_check(check.name, root / 'capture.json', checks_root=root, timeout=10)
                self.assertEqual(report['exit_code'], 0, report['stderr'][-4096:])
                self.assertEqual(report['stdout'], 'S' * cap)
                self.assertEqual(report['stderr'], 'E' * cap)
                self.assertEqual(report['diagnostics_complete'], stdout_extra + stderr_extra == 0)
                self.assertEqual(capsule.exit_code(report), int(stdout_extra + stderr_extra != 0))
                self.assertLess((root / 'capture.json').stat().st_size, 2 * cap + 8192)
