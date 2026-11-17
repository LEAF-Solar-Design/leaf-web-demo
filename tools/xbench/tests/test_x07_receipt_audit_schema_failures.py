from contextlib import closing, redirect_stdout
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit, seed


def invoke(profile, journal, customer='a'):
    output = io.StringIO()
    with patch.object(sys, 'argv', ['audit', '--database', str(profile), '--journal', str(journal), '--customer', customer]), redirect_stdout(output):
        code = audit.main()
    raw = output.getvalue()
    if len(raw) > 4096:
        raise AssertionError('unbounded diagnostic')
    return code, json.loads(raw)


class ReceiptSchemaFailures(unittest.TestCase):
    def test_either_store_schema_refuses_without_repair_then_restores(self):
        for side in ('profile', 'journal'):
            for fault in ('missing', 'future', 'altered'):
                with self.subTest(side=side, fault=fault), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    baseline = invoke(profile, journal)
                    original = profile.read_bytes(), journal.read_bytes()
                    target = profile if side == 'profile' else journal
                    table = 'customers' if side == 'profile' else 'confirmations'
                    with closing(sqlite3.connect(target)) as connection, connection:
                        if fault == 'missing':
                            connection.execute('DROP TABLE ' + table)
                        elif fault == 'future':
                            connection.execute('PRAGMA user_version=999')
                        else:
                            connection.execute('ALTER TABLE ' + table + ' ADD COLUMN private_decoy TEXT')
                    broken = profile.read_bytes(), journal.read_bytes()
                    self.assertEqual(invoke(profile, journal), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), broken)
                    with closing(sqlite3.connect(profile)) as connection:
                        actual = connection.execute('SELECT project_key,customer_key,profile_id,version FROM project_applications ORDER BY project_key').fetchall()
                    self.assertEqual(actual, [('old-' + key, key, pin['profile_id'], pin['version']) for key, pin in sorted(pins.items())])
                    profile.write_bytes(original[0]); journal.write_bytes(original[1])
                    self.assertEqual(invoke(profile, journal), baseline)
                    for key, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + key, key), pin)
