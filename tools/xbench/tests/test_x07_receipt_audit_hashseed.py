from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditHashseed(unittest.TestCase):
    def test_row_order_and_fresh_hash_seeds_produce_identical_cli_json(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal, read, _, pins = seed(directory)
            with closing(sqlite3.connect(journal)) as connection:
                initial = connection.execute('SELECT * FROM confirmations ORDER BY project_key').fetchall()
            original = json.loads(initial[0][3])
            missing = deepcopy(original)
            missing['project_key'] = 'missing'
            missing['applied_profile']['project_key'] = 'missing'
            inconsistent = deepcopy(original)
            inconsistent['project_key'] = 'old-a'
            inconsistent['resolved_roles']['module']['layer'] = 'WRONG'
            rows = [initial[0], initial[1], ('missing', 'a', '0' * 64, json.dumps(missing)),
                    ('invalid', 'a', '0' * 64, 'PRIVATE_MALFORMED')]
            rows[0] = ('old-a', 'a', '0' * 64, json.dumps(inconsistent))
            expected = {'schema': 'leaf.x07.receipt-history-audit.v1', 'customer_key': 'a',
                        'receipt_count': 3, 'healthy': False,
                        'issues': {'invalid_receipts': 1, 'missing_references': 1, 'inconsistent_references': 1}}
            baseline = None
            for order in [rows, list(reversed(rows)), rows[1:] + rows[:1]]:
                with closing(sqlite3.connect(journal)) as connection, connection:
                    connection.execute('DELETE FROM confirmations')
                    connection.executemany('INSERT INTO confirmations VALUES (?,?,?,?)', order)
                before = profile.read_bytes(), journal.read_bytes()
                for hash_seed in ['0', '1', '4294967295']:
                    with self.subTest(order=[row[0] for row in order], seed=hash_seed):
                        child = subprocess.run([sys.executable, audit.__file__, '--database', str(profile),
                                                '--journal', str(journal), '--customer', 'a'],
                                               capture_output=True, timeout=10, env={'PYTHONHASHSEED': hash_seed})
                        self.assertEqual(child.returncode, 1, child.stderr)
                        self.assertEqual(child.stderr, b'')
                        self.assertEqual(json.loads(child.stdout), expected)
                        if baseline is None:
                            baseline = child.stdout
                        self.assertEqual(child.stdout, baseline)
                        self.assertNotIn(b'PRIVATE', child.stdout)
                        self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                for key, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + key, key), pin)
