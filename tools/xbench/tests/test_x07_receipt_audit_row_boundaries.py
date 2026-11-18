from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditRowBoundaries(unittest.TestCase):
    def test_same_customer_rows_below_at_and_above_budget(self):
        for count in [audit.MAX_AUDIT_ROWS - 1, audit.MAX_AUDIT_ROWS, audit.MAX_AUDIT_ROWS + 1]:
            with self.subTest(receipts=count), tempfile.TemporaryDirectory() as directory:
                profile, journal, read, receipts, pins = seed(directory)
                with closing(sqlite3.connect(journal)) as connection:
                    original = json.loads(connection.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0])
                profile_rows, journal_rows = [], []
                for index in range(1, count):
                    project = f'extra-{index}'
                    value = deepcopy(original)
                    value['project_key'] = project
                    value['applied_profile']['project_key'] = project
                    if index % 3 == 0:
                        encoded = 'PRIVATE_MALFORMED'
                    else:
                        if index % 3 == 2:
                            profile_rows.append((project, 'a', pins['a']['profile_id'], 1))
                            value['resolved_roles']['module']['layer'] = 'WRONG'
                        encoded = json.dumps(value)
                    journal_rows.append((project, 'a', '0' * 64, encoded))
                with closing(sqlite3.connect(profile)) as connection, connection:
                    connection.execute('PRAGMA foreign_keys=ON')
                    connection.executemany('INSERT INTO project_applications VALUES (?,?,?,?)', profile_rows)
                with closing(sqlite3.connect(journal)) as connection, connection:
                    connection.executemany('INSERT INTO confirmations VALUES (?,?,?,?)', journal_rows)
                before = profile.read_bytes(), journal.read_bytes()
                if count <= audit.MAX_AUDIT_ROWS:
                    extra = count - 1
                    expected = {'schema': 'leaf.x07.receipt-history-audit.v1', 'customer_key': 'a', 'receipt_count': count,
                                'healthy': False, 'issues': {'invalid_receipts': extra // 3,
                                'missing_references': (extra + 2) // 3, 'inconsistent_references': (extra + 1) // 3}}
                    self.assertEqual(audit.audit_receipts(read, receipts, customer_key='a'), expected)
                else:
                    with self.assertRaisesRegex(ValueError, 'row budget'):
                        audit.audit_receipts(read, receipts, customer_key='a')
                    expected = {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}
                child = subprocess.run([sys.executable, audit.__file__, '--database', str(profile), '--journal', str(journal), '--customer', 'a'],
                                       capture_output=True, text=True, timeout=15)
                self.assertEqual(child.returncode, 1 if count <= audit.MAX_AUDIT_ROWS else 2, child.stderr)
                self.assertEqual(child.stderr, '')
                self.assertEqual(json.loads(child.stdout), expected)
                self.assertNotIn('PRIVATE', child.stdout)
                for key, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + key, key), pin)
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
