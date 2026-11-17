import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import x07_receipt_audit_query_plan as plans


class ReceiptQueryPlan(unittest.TestCase):
    def test_actual_select_smoke_and_independent_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            report = plans.measure_plans(directory, smoke=True)
            self.assertEqual([case['additional_foreign_history'] for case in report['cases']], [0, 16])
            self.assertTrue(report['full_foreign_table_scan'])
            for case in report['cases']:
                self.assertEqual(len(case['plans']), 4)
                self.assertTrue(case['old_pins_preserved'])
                self.assertTrue(case['store_bytes_preserved'])
                journal = [plan for plan in case['plans'] if plan['store'] == 'journal']
                self.assertEqual(len(journal), 1)
                self.assertIn('main.confirmations WHERE customer_key=?', journal[0]['statement'])
                self.assertTrue(journal[0]['foreign_table_scan'])
            self.assertEqual(list(Path(directory).iterdir()), [])
            child = subprocess.run([sys.executable, plans.__file__, '--directory', directory, '--smoke'], capture_output=True, text=True, timeout=20)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertEqual(child.stderr, '')
            self.assertEqual(json.loads(child.stdout), report)
            self.assertLess(len(child.stdout), 16384)
            self.assertEqual(list(Path(directory).iterdir()), [])
