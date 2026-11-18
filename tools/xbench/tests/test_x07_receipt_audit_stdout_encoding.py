from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit
from x07_profile_workflow import ProfileWorkflow


class ReceiptStdoutEncoding(unittest.TestCase):
    def test_ascii_and_utf8_stdout_decode_to_identical_scoped_json(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal = Path(directory) / 'profile.sqlite', Path(directory) / 'journal.sqlite'
            store, receipts = audit.CustomerProfileStore(profile), audit.ConfirmationReceiptStore(journal)
            customer, project = '雪é', 'unicode-project'
            store.save_version(customer, {'module': {'block': 'PRIVATE', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project(project, customer)
            context = {'customer_key': customer, 'project_key': project, 'drawing_key': 'synthetic'}
            value = ProfileWorkflow._result(context, pin, pin['roles'], {'count': 1})
            receipts.run(context, '0' * 64, lambda: value, lambda _: None)
            before = profile.read_bytes(), journal.read_bytes()
            baseline = None
            for encoding in ('ascii', 'utf-8'):
                script = "import sys;sys.path.insert(0,sys.argv.pop(1));sys.stdout.reconfigure(encoding=sys.argv.pop(1),errors='strict');import x07_receipt_history_audit as a;raise SystemExit(a.main())"
                child = subprocess.run([sys.executable, '-P', '-c', script, str(Path(audit.__file__).parent), encoding,
                                        '--database', str(profile), '--journal', str(journal), '--customer', customer], capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertEqual(child.stderr, b'')
                report = json.loads(child.stdout.decode(encoding))
                self.assertTrue(report['healthy'])
                self.assertEqual(report['customer_key'], customer)
                if baseline is None:
                    baseline = report
                self.assertEqual(report, baseline)
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
            self.assertEqual(store.application_record(project, customer), pin)
