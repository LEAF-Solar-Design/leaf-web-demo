from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleWorkingDirectory(unittest.TestCase):
    def test_actual_cli_binds_root_and_check_from_unrelated_unicode_directories(self):
        check = capsule.ROOT / 'tools/xbench/tests' / ('x07_cwd_' + uuid4().hex + '.cjs')
        try:
            with check.open('x', encoding='utf-8') as stream: stream.write("console.log('BOUND:'+process.cwd())\n")
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                bindings = []
                for index, name in enumerate(('unrelated space', '雪 and space')):
                    cwd = base / name; cwd.mkdir()
                    output = base / (str(index) + '.json')
                    child = subprocess.run([sys.executable, str(Path(capsule.__file__).resolve()), '--check', check.name, '--out', str(output)], cwd=cwd,
                                           capture_output=True, text=True, timeout=15)
                    self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                    self.assertEqual(child.stderr, '')
                    report = capsule.read_capture(output)
                    self.assertEqual(Path(report['stdout'].strip()[6:]), capsule.ROOT)
                    self.assertEqual(Path(report['command'][1]), check.resolve())
                    self.assertTrue(report['owned_processes_settled'])
                    bindings.append((report['source_sha'], report['source_file_sha256']))
                self.assertEqual(bindings[0], bindings[1])
        finally:
            check.unlink(missing_ok=True)
