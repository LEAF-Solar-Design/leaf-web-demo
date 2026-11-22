import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import x07_capsule_consumer_perf as perf


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerPerf(unittest.TestCase):
    def test_smoke_cli_counts_sizes_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-P', str(Path(perf.__file__)), '--directory', directory, '--smoke'],
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, b'')
            value = json.loads(result.stdout)
            self.assertEqual((value['accepted'], value['refused']), (3, 3))
            self.assertEqual([case['stream_bytes'] for case in value['cases']], [0, 1024, 65536])
            for case in value['cases']:
                self.assertGreaterEqual(case['capture_bytes'], case['stream_bytes'])
                self.assertGreaterEqual(case['elapsed_seconds'], 0)
                self.assertGreaterEqual(case['memory_peak_bytes'], case['memory_current_bytes'])
            self.assertEqual(list(Path(directory).iterdir()), [])
