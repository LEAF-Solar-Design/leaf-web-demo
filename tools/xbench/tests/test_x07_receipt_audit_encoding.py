from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditEncoding(unittest.TestCase):
    def test_blob_bom_and_lone_surrogate_payloads_refuse_then_unicode_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal, read, receipts, pins = seed(directory)
            with closing(sqlite3.connect(journal)) as connection:
                original = connection.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0]
            cases = [(b'\xff\xfePRIVATE', 1), (b'\xe2\x82', 1), (b'\xef\xbb\xbf' + original.encode(), 1),
                     ('\ufeff' + original, 1),
                     (original[:-1] + ',"extra":"\\ud800"}', 1),
                     (original[:-1] + ',"\\udfff":"extra"}', 1)]
            for index, (payload, expected_code) in enumerate(cases):
                with self.subTest(encoding=index):
                    with closing(sqlite3.connect(journal)) as connection, connection:
                        connection.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (payload,))
                    before = profile.read_bytes(), journal.read_bytes()
                    result = audit.audit_receipts(read, receipts, customer_key='a')
                    self.assertFalse(result['healthy'])
                    self.assertEqual(result['issues'], {'invalid_receipts': 1, 'missing_references': 0, 'inconsistent_references': 0})
                    child = subprocess.run([sys.executable, audit.__file__, '--database', str(profile), '--journal', str(journal), '--customer', 'a'],
                                           capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, expected_code, child.stderr)
                    self.assertEqual(child.stderr, '')
                    self.assertEqual(json.loads(child.stdout), result)
                    self.assertNotIn('PRIVATE', child.stdout)
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                    for key, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + key, key), pin)
            valid = original[:-1] + ',"extra":"\\ud83d\\ude00"}'
            with closing(sqlite3.connect(journal)) as connection, connection:
                connection.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (valid,))
            before = profile.read_bytes(), journal.read_bytes()
            self.assertTrue(audit.audit_receipts(read, receipts, customer_key='a')['healthy'])
            self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
