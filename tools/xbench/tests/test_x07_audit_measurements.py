from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_audit_query_plan as plan
import x07_audit_resource_soak as soak


class AuditMeasurements(unittest.TestCase):
    def test_query_plan_smoke_flags_foreign_pin_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            result=plan.measure_plans(directory,smoke=True)
            self.assertEqual([c['foreign_rows'] for c in result['cases']],[0,16])
            self.assertTrue(result['full_foreign_table_scan'])
            for case in result['cases']:
                self.assertTrue(case['audit']['healthy'])
                self.assertEqual((case['audit']['version_count'],case['audit']['pin_count']),(1,1))
                self.assertFalse(case['plans']['customers']['full_table_scan'])
                self.assertFalse(case['plans']['profile_versions']['full_table_scan'])
                self.assertTrue(case['plans']['project_applications']['full_table_scan'])
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_resource_smoke_counts_closed_connections_and_retained_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            result=soak.measure_resources(directory,iterations=3)
            self.assertEqual(result['connections'],{'opened':9,'closed':9,'active':0})
            self.assertTrue(result['pins_preserved'])
            self.assertEqual([s['iteration'] for s in result['memory_samples']],[1,3])
            for sample in result['memory_samples']:
                self.assertEqual(sample['active_connections'],0)
                self.assertGreaterEqual(sample['memory_peak_bytes'],sample['memory_current_bytes'])
            if result['handles_before'] is not None:
                self.assertLessEqual(result['handles_after_cleanup'],result['handles_before']+2)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_both_smoke_clis_emit_bounded_json_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            for tool in [plan,soak]:
                child=subprocess.run([sys.executable,tool.__file__,'--directory',directory,'--smoke'],capture_output=True,text=True,timeout=15)
                self.assertEqual(child.returncode,0,child.stderr); self.assertEqual(child.stderr,'')
                self.assertLess(len(child.stdout),16384)
                self.assertIn('schema',json.loads(child.stdout))
                self.assertEqual(list(Path(directory).iterdir()),[])
