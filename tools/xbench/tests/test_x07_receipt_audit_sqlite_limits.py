from contextlib import contextmanager
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit, seed
from .test_x07_receipt_audit_schema_failures import invoke


class ReceiptSqliteLimits(unittest.TestCase):
    def test_lowered_limits_on_each_reader_refuse_then_retry(self):
        for side in ('profile', 'journal'):
            for category in (sqlite3.SQLITE_LIMIT_LENGTH, sqlite3.SQLITE_LIMIT_SQL_LENGTH):
                with self.subTest(side=side, category=category), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    baseline = audit.audit_receipts(read, receipts, customer_key='a')
                    before = profile.read_bytes(), journal.read_bytes()
                    original_profile, original_journal = read._connection, receipts._connect

                    @contextmanager
                    def limited_profile():
                        with original_profile() as connection:
                            previous = connection.setlimit(category, 32)
                            try:
                                yield connection
                            finally:
                                connection.setlimit(category, previous)

                    def limited_journal():
                        connection = original_journal()
                        connection.setlimit(category, 32)
                        return connection

                    if side == 'profile':
                        read._connection = limited_profile
                    else:
                        receipts._connect = limited_journal
                    try:
                        with self.assertRaises(sqlite3.Error):
                            audit.audit_receipts(read, receipts, customer_key='a')
                        with patch.object(audit, 'CustomerProfileStore', return_value=read), patch.object(audit, 'ConfirmationReceiptStore', return_value=receipts):
                            self.assertEqual(invoke(profile, journal), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                    finally:
                        read._connection, receipts._connect = original_profile, original_journal
                    self.assertEqual(audit.audit_receipts(read, receipts, customer_key='a'), baseline)
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                    for customer, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + customer, customer), pin)
