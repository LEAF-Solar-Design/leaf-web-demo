from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


class CapsuleNumericArguments(unittest.TestCase):
    def test_invalid_api_budgets_refuse_before_any_process_or_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for timeout in (float('nan'), float('inf'), -float('inf'), 0, -1, True, 10 ** 1000):
                with self.subTest(timeout=repr(timeout)), patch.object(capsule.subprocess, 'Popen', side_effect=AssertionError('spawned')):
                    with self.assertRaises(ValueError):
                        capsule.run_check('x07_missing.cjs', root / 'absent.json', timeout=timeout, checks_root=root)
            for limit in (True, False, 0, -1, 1.5, capsule.MAX_STREAM_BYTES + 1):
                with self.subTest(limit=limit), patch.object(capsule.subprocess, 'Popen', side_effect=AssertionError('spawned')):
                    with self.assertRaises(ValueError):
                        capsule.run_check('x07_missing.cjs', root / 'absent.json', stream_limit=limit, checks_root=root)
            self.assertEqual(list(root.iterdir()), [])

    def test_cli_nonfinite_timeouts_are_bounded_generic_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'absent.json'
            for token in ('nan', 'inf', '-inf', '0', '-1'):
                stdout = io.StringIO()
                with self.subTest(token=token), patch.object(sys, 'argv', ['capsule', '--check', 'x07_missing.cjs', '--out', str(output), '--timeout=' + token]), redirect_stdout(stdout):
                    self.assertEqual(capsule.main(), 2)
                self.assertEqual(json.loads(stdout.getvalue())['error'], 'CAPSULE_UNAVAILABLE')
                self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which('node'), 'node unavailable')
    def test_supported_stream_boundaries_retry_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_boundary.cjs'; check.write_text("process.stdout.write('S')\n", encoding='utf-8')
            for limit in (1, capsule.MAX_STREAM_BYTES):
                report = capsule.run_check(check.name, root / (str(limit) + '.json'), checks_root=root, stream_limit=limit, timeout=5)
                self.assertEqual(report['stdout_bytes'], 1)
                self.assertEqual(capsule.exit_code(report), 0)
