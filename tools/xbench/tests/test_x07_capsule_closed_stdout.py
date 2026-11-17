from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleClosedStdout(unittest.TestCase):
    def test_closed_reader_on_success_and_refusal_is_bounded(self):
        check = capsule.ROOT / 'tools/xbench/tests' / ('x07_cli_' + uuid4().hex + '.cjs')
        try:
            with check.open('x', encoding='utf-8') as stream: stream.write("console.log('DONE')\n")
            with tempfile.TemporaryDirectory() as directory:
                for valid in (True, False):
                    output = Path(directory) / ('valid.json' if valid else 'absent.json')
                    child = subprocess.Popen([sys.executable, str(Path(capsule.__file__).resolve()), '--check', check.name if valid else '../refused.cjs', '--out', str(output)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    child.stdout.close()
                    try:
                        code = child.wait(timeout=15)
                        error = child.stderr.read()
                    finally:
                        if child.poll() is None: child.kill()
                        child.wait(timeout=5); child.stderr.close()
                    self.assertNotEqual(code, 0)
                    self.assertEqual(error, b'')
                    if valid:
                        self.assertTrue(capsule.read_capture(output)['owned_processes_settled'])
                    else:
                        self.assertFalse(output.exists())
        finally:
            check.unlink(missing_ok=True)
