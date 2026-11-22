from contextlib import closing, contextmanager
import json
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptQueryOrder(unittest.TestCase):
    def test_scoped_oracle_is_stable_across_orders_and_reverse_reads(self):
        baseline = None
        for order in ((0, 1, 2), (2, 0, 1), (1, 2, 0)):
            for reverse in (0, 1):
                with self.subTest(order=order, reverse=reverse), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    with closing(sqlite3.connect(journal)) as db, db:
                        valid = json.loads(db.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0])
                        missing = dict(valid, project_key='missing')
                        foreign = dict(valid, project_key='old-b')
                        rows = [('invalid', 'a', 'x', '{'), ('missing', 'a', 'x', json.dumps(missing)),
                                ('old-b', 'a', 'x', json.dumps(foreign))]
                        db.execute("DELETE FROM confirmations WHERE project_key='old-b'")
                        for index in order:
                            db.execute('INSERT INTO confirmations VALUES (?,?,?,?)', rows[index])
                        db.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('foreign-corrupt', 'b', 'x', '{'))
                    before = profile.read_bytes(), journal.read_bytes()
                    original_profile, original_journal = read._connection, receipts._connect

                    @contextmanager
                    def reader():
                        with original_profile() as connection:
                            connection.execute('PRAGMA reverse_unordered_selects=' + str(reverse))
                            yield connection

                    def journal_reader():
                        connection = original_journal()
                        connection.execute('PRAGMA reverse_unordered_selects=' + str(reverse))
                        return connection

                    read._connection, receipts._connect = reader, journal_reader
                    try:
                        report = audit.audit_receipts(read, receipts, customer_key='a')
                    finally:
                        read._connection, receipts._connect = original_profile, original_journal
                    self.assertEqual(report['receipt_count'], 4)
                    self.assertEqual(report['issues'], {'invalid_receipts': 1, 'missing_references': 1, 'inconsistent_references': 1})
                    self.assertFalse(report['healthy'])
                    if baseline is None:
                        baseline = report
                    self.assertEqual(report, baseline)
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                    for customer, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + customer, customer), pin)
