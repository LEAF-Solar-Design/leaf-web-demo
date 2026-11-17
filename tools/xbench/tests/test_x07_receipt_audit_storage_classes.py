from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptStorageClasses(unittest.TestCase):
    def test_actual_storage_classes_under_supported_schema_never_false_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, original, read, _, pins = seed(directory)
            with closing(sqlite3.connect(original)) as connection:
                valid = connection.execute("SELECT result FROM confirmations WHERE customer_key='a'").fetchone()[0]
            journal = Path(directory) / 'classes.sqlite'
            canonical = audit.ConfirmationReceiptStore._DDL
            with closing(sqlite3.connect(journal)) as connection, connection:
                # Deliberate physical-row corruption fixture: preserve native
                # storage classes, then restore only the declared canonical DDL.
                connection.execute(canonical.replace('result TEXT NOT NULL', 'result BLOB'))
                values = [None, 7, 1.25, '{', sqlite3.Binary(valid.encode()), valid]
                for index, value in enumerate(values):
                    connection.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('old-a' if index == 5 else 'bad-' + str(index), 'a', 'x', value))
                connection.execute('PRAGMA writable_schema=ON')
                connection.execute("UPDATE sqlite_master SET sql=? WHERE name='confirmations'", (canonical,))
                connection.execute('PRAGMA user_version=1')
                connection.execute('PRAGMA schema_version=2')
            with closing(sqlite3.connect(journal)) as connection:
                kinds = [row[0] for row in connection.execute('SELECT typeof(result) FROM confirmations ORDER BY project_key')]
            self.assertEqual(kinds, ['null', 'integer', 'real', 'text', 'blob', 'text'])
            receipts = audit.ConfirmationReceiptStore(journal, read_only=True)
            before = profile.read_bytes(), journal.read_bytes()
            report = audit.audit_receipts(read, receipts, customer_key='a')
            self.assertEqual(report['receipt_count'], 6)
            self.assertEqual(report['issues'], {'invalid_receipts': 5, 'missing_references': 0, 'inconsistent_references': 0})
            self.assertFalse(report['healthy'])
            self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
            for customer, pin in pins.items():
                self.assertEqual(read.application_record('old-' + customer, customer), pin)
