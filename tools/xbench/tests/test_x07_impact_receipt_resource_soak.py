import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import x07_impact_receipt_resource_soak as soak


class ImpactReceiptResources(unittest.TestCase):
    def test_smoke_measures_closed_owned_connections_and_retained_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            report = soak.measure_resources(directory, iterations=3)
            self.assertEqual(report['connections']['active'], 0)
            self.assertEqual(report['connections']['opened'], report['connections']['closed'])
            self.assertGreater(report['connections']['opened'], 20)
            self.assertTrue(report['pins_preserved'])
            self.assertTrue(report['store_bytes_preserved'])
            self.assertEqual([s['iteration'] for s in report['memory_samples']], [1, 3])
            self.assertEqual(list(Path(directory).iterdir()), [])
            child = subprocess.run([sys.executable, soak.__file__, '--directory', directory, '--smoke'],
                                   capture_output=True, text=True, timeout=20)
            self.assertEqual(child.returncode, 0, child.stderr + child.stdout)
            self.assertEqual(child.stderr, '')
            self.assertEqual(json.loads(child.stdout)['connections']['active'], 0)
            self.assertLess(len(child.stdout), 4096)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_iteration_budget_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            for count in (0, -1, True, 1001, 1.5):
                with self.subTest(count=count), self.assertRaises(ValueError):
                    soak.measure_resources(directory, iterations=count)
            self.assertEqual(list(Path(directory).iterdir()), [])
