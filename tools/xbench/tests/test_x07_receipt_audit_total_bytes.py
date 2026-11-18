from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptTotalBudget(unittest.TestCase):
    def test_exact_utf8_total_and_excess_precede_payload_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal, read, receipts, pins = seed(directory)
            with closing(sqlite3.connect(journal)) as db, db:
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('bad', 'a', 'x', '{é'))
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('foreign', 'b', 'x', 'F' * 4096))
                texts = [row[0] for row in db.execute("SELECT result FROM confirmations WHERE customer_key='a'")]
            total = sum(len(value.encode('utf-8')) for value in texts)
            before = profile.read_bytes(), journal.read_bytes()
            for limit in (total, total + 1):
                with self.subTest(limit=limit), patch.object(audit, 'MAX_TOTAL_RECEIPT_BYTES', limit):
                    report = audit.audit_receipts(read, receipts, customer_key='a')
                    self.assertEqual(report['issues'], {'invalid_receipts': 1, 'missing_references': 0, 'inconsistent_references': 0})
                    self.assertEqual(report['receipt_count'], 2)
                    self.assertFalse(report['healthy'])
            with patch.object(audit, 'MAX_TOTAL_RECEIPT_BYTES', total - 1), patch.object(audit, 'parse_receipt', side_effect=AssertionError('payload decoded before aggregate refusal')) as decoded:
                with self.assertRaises(audit.ReceiptBudgetError):
                    audit.audit_receipts(read, receipts, customer_key='a')
                decoded.assert_not_called()
            self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
            for customer, pin in pins.items():
                self.assertEqual(read.application_record('old-' + customer, customer), pin)
