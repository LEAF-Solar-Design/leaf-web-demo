import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import x07_capsule_resource_soak as soak
import x07_capsule_capture_perf as perf


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleMeasurements(unittest.TestCase):
    def test_resource_cleanup_and_bounded_cli_smoke(self):
        with tempfile.TemporaryDirectory() as directory:
            report = soak.measure(directory, iterations=1)
            self.assertEqual(report['launches'], 3)
            self.assertTrue(report['owned_resources_settled'])
            self.assertEqual([case['mode'] for case in report['cases']], ['success', 'failure', 'timeout'])
            self.assertTrue(all(case['readers_alive'] == case['workers_alive'] == 0 for case in report['cases']))
            self.assertEqual(list(Path(directory).iterdir()), [])
            child = subprocess.run([sys.executable, soak.__file__, '--directory', directory, '--smoke'], capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertEqual(child.stderr, '')
            self.assertEqual(json.loads(child.stdout)['launches'], 6)
            self.assertLess(len(child.stdout), 16384)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_capture_bytes_timings_memory_and_bounded_cli_smoke(self):
        with tempfile.TemporaryDirectory() as directory:
            report = perf.benchmark(directory, smoke=True)
            self.assertEqual([case['bytes_per_stream'] for case in report['cases']], [0, 1024, perf.MAX_STREAM_BYTES])
            self.assertTrue(all(case['complete'] and case['owned_processes_settled'] for case in report['cases']))
            self.assertTrue(all(case['memory_peak_bytes'] >= case['memory_current_bytes'] >= 0 for case in report['cases']))
            self.assertEqual(list(Path(directory).iterdir()), [])
            child = subprocess.run([sys.executable, perf.__file__, '--directory', directory, '--smoke'], capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertEqual(child.stderr, '')
            self.assertEqual(len(json.loads(child.stdout)['cases']), 3)
            self.assertLess(len(child.stdout), 8192)
            self.assertEqual(list(Path(directory).iterdir()), [])
