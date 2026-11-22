from contextlib import contextmanager
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditOuterTransactions(unittest.TestCase):
    def test_each_caller_transaction_remains_open_and_pending(self):
        for side in ['profile', 'journal']:
            for statement in ['BEGIN', 'BEGIN IMMEDIATE']:
                with self.subTest(store=side, statement=statement), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    before = profile.read_bytes(), journal.read_bytes()
                    connection = sqlite3.connect(profile if side == 'profile' else journal)
                    original_profile, original_journal = read._connection, receipts._connect

                    @contextmanager
                    def borrowed_profile():
                        yield connection

                    try:
                        connection.row_factory = sqlite3.Row
                        connection.execute('PRAGMA foreign_keys=ON')
                        connection.execute(statement)
                        if side == 'profile':
                            connection.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',
                                               ('a', pins['a']['profile_id'], 2, '{}', 'pending'))
                            read._connection = borrowed_profile
                            query = 'SELECT count(*) FROM profile_versions WHERE version=2'
                        else:
                            connection.execute('INSERT INTO confirmations VALUES (?,?,?,?)',
                                               ('pending', 'a', '0' * 64, 'PRIVATE_PENDING'))
                            receipts._connect = lambda: connection
                            query = "SELECT count(*) FROM confirmations WHERE project_key='pending'"
                        trace = []
                        connection.set_trace_callback(trace.append)
                        with self.assertRaises(ValueError):
                            audit.audit_receipts(read, receipts, customer_key='a')
                        self.assertTrue(connection.in_transaction)
                        self.assertEqual(connection.execute(query).fetchone()[0], 1)
                        self.assertFalse(any(sql.upper().startswith(('COMMIT', 'ROLLBACK')) for sql in trace))
                    finally:
                        read._connection, receipts._connect = original_profile, original_journal
                        try:
                            connection.rollback()
                        except sqlite3.Error:
                            pass
                        connection.close()
                    self.assertTrue(audit.audit_receipts(read, receipts, customer_key='a')['healthy'])
                    for key, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + key, key), pin)
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
