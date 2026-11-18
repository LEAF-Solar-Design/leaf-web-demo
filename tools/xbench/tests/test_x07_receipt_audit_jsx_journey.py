from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ReceiptActualJsx(unittest.TestCase):
    def test_two_customer_actual_jsx_journey(self):
        root = Path(__file__).resolve().parents[3]
        candidates = [root / 'web/node_modules', Path('C:/tmp/x07-web-check-20261115/node_modules')]
        dependencies = next((path for path in candidates if (path / 'jsdom').is_dir()), None)
        node = shutil.which('node')
        if dependencies is None or node is None:
            self.skipTest('actual JSX dependency set unavailable on this test host; native replay required')
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([node, str(Path(__file__).with_name('x07_receipt_audit_jsx_journey.cjs')), '--deps', str(dependencies)],
                                    cwd=root, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('PASS: actual JSX two-customer', result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])
