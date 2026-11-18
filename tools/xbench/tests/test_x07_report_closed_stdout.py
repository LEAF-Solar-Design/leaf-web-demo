"""X07 report handles an owned closed stdout pipe during publication."""
from pathlib import Path
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('closed_stdout_fixtures', root / 'server/tests/test_x07_offline_acceptance_report.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ReportClosedStdout(unittest.TestCase):
    def test_closed_reader_has_bounded_exit_and_atomic_file(self):
        for valid in (True, False):
            for publish in (True, False):
                with self.subTest(valid=valid, publish=publish), tempfile.TemporaryDirectory() as directory:
                    source = Path(directory)
                    paths, _, _ = fixtures.OfflineAcceptanceReport().fixtures(source)
                    if not valid:
                        paths[1].write_text('{}', encoding='utf-8')
                    before = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
                    output = source / 'report.json'
                    # Gate stdin so the parent closes stdout before publication.
                    script = "import runpy,sys;sys.stdin.buffer.read(1);runpy.run_path(sys.argv.pop(1),run_name='__main__')"
                    args = [sys.executable, '-c', script, str(root / 'tools/xbench/x07_offline_acceptance_report.py'),
                            '--source-sha', fixtures.SOURCE, '--source-root', str(source),
                            '--journey', str(paths[0]), '--proof', str(paths[1])]
                    if publish:
                        args += ['--out', str(output)]
                    child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        child.stdout.close()
                        child.stdin.write(b'1')
                        child.stdin.close()
                        child.wait(timeout=15)
                        diagnostics = child.stderr.read()
                        self.assertNotEqual(child.returncode, 0)
                        self.assertLess(len(diagnostics), 1000)
                        self.assertNotIn(b'Traceback', diagnostics)
                        self.assertNotIn(b'Exception ignored', diagnostics)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=5)
                        child.stderr.close()
                    if output.exists():
                        raw = output.read_bytes()
                        self.assertTrue(raw.endswith(b'\n'))
                        result = json.loads(raw)
                        self.assertEqual({step['state'] for step in result['steps']},
                                         {'SYNTHETIC_PASS' if valid else 'UNREADABLE'})
                    self.assertEqual(list(source.glob('.report.json.x07-*')), [])
                    self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
