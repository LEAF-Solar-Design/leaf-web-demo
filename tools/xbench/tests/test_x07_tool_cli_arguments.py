from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest


class ToolCliArguments(unittest.TestCase):
    def test_preview_and_measurement_argument_errors_are_generic_json(self):
        tools = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = ['--database', str(root / 'PRIVATE_DB.sqlite'), '--customer', 'a', '--profile-id', 'fixture',
                       '--before', 'PRIVATE_VALUE', '--after', '1']
            measurement = ['--directory', str(root / 'PRIVATE_DIRECTORY'), '--PRIVATE_UNKNOWN', 'PRIVATE_VALUE']
            cases = [('x07_profile_change_impact.py', 'leaf.x07.profile-change-impact.v1', preview),
                     ('x07_audit_query_plan.py', 'leaf.x07.audit-query-plan.v1', measurement),
                     ('x07_audit_resource_soak.py', 'leaf.x07.audit-resource-soak.v1', measurement),
                     ('x07_impact_receipt_audit_perf.py', 'leaf.x07.impact-receipt-audit-perf.v1', measurement)]
            for filename, schema, invalid_arguments in cases:
                for arguments in [[], invalid_arguments]:
                    with self.subTest(tool=filename, arguments=arguments):
                        child = subprocess.run([sys.executable, str(tools / filename), *arguments],
                                               capture_output=True, text=True, timeout=10)
                        self.assertEqual(child.returncode, 2)
                        self.assertEqual(child.stderr, '')
                        self.assertLess(len(child.stdout), 256)
                        self.assertEqual(json.loads(child.stdout), {'schema': schema, 'error': 'INVALID_ARGUMENTS'})
                        self.assertNotIn('PRIVATE', child.stdout)
                        self.assertEqual(list(root.iterdir()), [])
