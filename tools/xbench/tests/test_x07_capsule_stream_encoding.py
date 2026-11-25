import base64
from pathlib import Path
import shutil
import tempfile
import unittest
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleStreamEncoding(unittest.TestCase):
    def test_invalid_bytes_have_lossless_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_encoding.cjs'
            check.write_text("process.stdout.write(Buffer.from([255,0,195]));process.stderr.write(Buffer.from([192,175,254]))\n", encoding='utf-8')
            report = capsule.run_check(check.name, root / 'capture.json', checks_root=root, timeout=5)
            self.assertEqual(base64.b64decode(report['stdout_base64']), bytes([255, 0, 195]))
            self.assertEqual(base64.b64decode(report['stderr_base64']), bytes([192, 175, 254]))
            self.assertEqual(report['stdout_encoding'], 'binary-base64')
            self.assertEqual(report['stderr_encoding'], 'binary-base64')
            self.assertTrue(report['diagnostics_complete'])
            self.assertTrue(report['owned_processes_settled'])

    def test_split_valid_multibyte_streams_reassemble_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_split.cjs'
            check.write_text("process.stdout.write(Buffer.from([233]));process.stderr.write(Buffer.from([240,159]));setTimeout(()=>{process.stdout.write(Buffer.from([155,170]));process.stderr.write(Buffer.from([154,128]))},30)\n", encoding='utf-8')
            report = capsule.run_check(check.name, root / 'capture.json', checks_root=root, timeout=5)
            self.assertEqual(report['stdout'], '雪')
            self.assertEqual(report['stderr'], '🚀')
            self.assertEqual((report['stdout_bytes'], report['stderr_bytes']), (3, 4))
            self.assertEqual(report['stdout_encoding'], 'utf-8')
            self.assertNotIn('stdout_base64', report)
            self.assertTrue(report['diagnostics_complete'])
