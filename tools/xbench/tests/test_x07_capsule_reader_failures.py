from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleReaderFailures(unittest.TestCase):
    def test_each_reader_failure_refuses_without_unjoined_worker(self):
        for side in ('stdout', 'stderr'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                check = root / 'x07_reader.cjs'
                check.write_text("process.stdout.write('READY');process.stderr.write('READY');setInterval(()=>{},1000)\n", encoding='utf-8')
                original = subprocess.Popen

                class BrokenReader:
                    def __init__(self, stream): self.stream, self.calls = stream, 0
                    def __getattr__(self, name): return getattr(self.stream, name)
                    def read1(self, size):
                        self.calls += 1
                        if self.calls > 1: raise OSError('synthetic reader refusal')
                        return self.stream.read1(size)

                def popen(command, *args, **kwargs):
                    child = original(command, *args, **kwargs)
                    if str(command[0]) != 'git':
                        setattr(child, side, BrokenReader(getattr(child, side)))
                    return child

                with patch.object(subprocess, 'Popen', side_effect=popen):
                    report = capsule.run_check(check.name, root / 'failed.json', checks_root=root, timeout=5)
                self.assertEqual(report['error'], 'READER_FAILED')
                self.assertFalse(report['diagnostics_complete'])
                self.assertEqual(report['reader_errors'], [side])
                self.assertEqual(report['resources']['readers_alive'], 0)
                self.assertTrue(report['owned_processes_settled'])
                self.assertEqual(capsule.exit_code(report), 2)
                self.assertEqual(capsule.read_capture(root / 'failed.json'), report)
                check.write_text("console.log('RETRY')\n", encoding='utf-8')
                self.assertEqual(capsule.exit_code(capsule.run_check(check.name, root / 'retry.json', checks_root=root, timeout=5)), 0)
