from contextlib import contextmanager, redirect_stdout
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import seed
import x07_profile_change_impact as audit


class ImpactShadowTables(unittest.TestCase):
    def test_decoys_cannot_replace_intended_main_records(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal, read, receipts, pins = seed(directory)
            baseline = audit.preview_change(read, customer_key='a', profile_id=pins['a']['profile_id'], before_version=1, after_version=1)
            before = profile.read_bytes(), journal.read_bytes()
            original_profile, original_journal = read._connection, receipts._connect
            for side in ['profile']:
                for mode in ['temp', 'attached']:
                    with self.subTest(store=side, mode=mode):
                        connection = sqlite3.connect((profile if side == 'profile' else journal).resolve().as_uri() + '?mode=ro', uri=True)
                        connection.row_factory = sqlite3.Row if side == 'profile' else None
                        connection.execute('PRAGMA foreign_keys=ON')
                        namespace = 'temp'
                        if mode == 'attached':
                            connection.execute("ATTACH DATABASE ':memory:' AS decoy")
                            namespace = 'decoy'
                        if side == 'profile':
                            connection.execute(f'CREATE TABLE {namespace}.customers (customer_key, profile_id)')
                            connection.execute(f'INSERT INTO {namespace}.customers VALUES (?,?)', ('a', 'PRIVATE_DECOY'))
                        else:
                            connection.execute(f'CREATE TABLE {namespace}.confirmations (project_key, customer_key, digest, result)')
                            connection.execute(f'INSERT INTO {namespace}.confirmations VALUES (?,?,?,?)', ('PRIVATE_DECOY', 'a', 'x', 'PRIVATE_INVALID'))
                        connection.commit()

                        @contextmanager
                        def borrowed():
                            yield connection

                        if side == 'profile':
                            read._connection = borrowed
                        else:
                            receipts._connect = lambda: connection
                        output = io.StringIO()
                        try:
                            argv = ['audit', '--database', str(profile), '--customer', 'a', '--profile-id', pins['a']['profile_id'], '--before', '1', '--after', '1']
                            with patch.object(sys, 'argv', argv), patch.object(audit, 'CustomerProfileStore', return_value=read), redirect_stdout(output):
                                code = audit.main()
                            if side == 'profile':
                                self.assertEqual(code, 2)
                                self.assertEqual(json.loads(output.getvalue()), {'schema': 'leaf.x07.profile-change-impact.v1', 'error': 'SCOPED_PREVIEW_UNAVAILABLE'})
                            else:
                                self.assertEqual(code, 0)
                                self.assertEqual(json.loads(output.getvalue()), baseline)
                            self.assertNotIn('PRIVATE', output.getvalue())
                        finally:
                            read._connection, receipts._connect = original_profile, original_journal
                            connection.close()
            for key, pin in pins.items():
                self.assertEqual(read.application_record('old-' + key, key), pin)
            self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
