from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_impact_receipt_audit_perf as benchmark


class ImpactReceiptAuditPerf(unittest.TestCase):
    def test_smoke_matrix_reports_independent_counts_and_cleans_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            result = benchmark.benchmark(directory, smoke=True)
            self.assertEqual([(case['scoped_pins'], case['additional_foreign_history']) for case in result['cases']],
                             [(1, 0), (1, 16), (8, 0), (8, 16)])
            for case in result['cases']:
                self.assertEqual(case['scoped_pins'], case['scoped_receipts'])
                self.assertEqual(case['retaining_projects'], case['scoped_pins'])
                self.assertEqual(case['receipt_issues'], {'invalid_receipts': 0, 'missing_references': 0, 'inconsistent_references': 0})
                self.assertGreaterEqual(case['impact_seconds'], 0)
                self.assertGreaterEqual(case['receipt_audit_seconds'], 0)
                self.assertGreaterEqual(case['traced_peak_bytes'], case['traced_current_bytes'])
                self.assertTrue(case['old_pins_preserved'])
                self.assertTrue(case['store_bytes_preserved'])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cli_smoke_and_missing_directory_refusal_are_bounded_json(self):
        with tempfile.TemporaryDirectory() as directory:
            for target, code in [(Path(directory), 0), (Path(directory) / 'missing', 2)]:
                child = subprocess.run([sys.executable, benchmark.__file__, '--directory', str(target), '--smoke'],
                                       capture_output=True, text=True, timeout=15)
                self.assertEqual(child.returncode, code, child.stderr)
                self.assertEqual(child.stderr, '')
                value = json.loads(child.stdout)
                self.assertLess(len(child.stdout), 16384)
                if code == 0:
                    self.assertEqual(len(value['cases']), 4)
                else:
                    self.assertEqual(value, {'schema': 'leaf.x07.impact-receipt-audit-perf.v1', 'error': 'MEASUREMENT_UNAVAILABLE'})
                self.assertEqual(list(Path(directory).iterdir()), [])
