from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptNumericIdentity(unittest.TestCase):
    def test_numeric_tokens_never_hide_invalid_identity_or_count(self):
        for field in ('version', 'count'):
            for token in ('true', '1.0', '1.5', '1e999', '-0', '-0.0', '-1', '9007199254740992'):
                with self.subTest(field=field, token=token), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    with closing(sqlite3.connect(journal)) as db, db:
                        value = json.loads(db.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0])
                        target = value['applied_profile'] if field == 'version' else value['selection']
                        target[field] = 'NUMERIC_TOKEN'
                        encoded = json.dumps(value).replace('"NUMERIC_TOKEN"', token)
                        db.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (encoded,))
                    before = profile.read_bytes(), journal.read_bytes()
                    report = audit.audit_receipts(read, receipts, customer_key='a')
                    expected = {'invalid_receipts': int(token == '1e999' or field == 'count'),
                                'missing_references': 0,
                                'inconsistent_references': int(field == 'version' and token != '1e999')}
                    self.assertEqual(report['issues'], expected)
                    self.assertFalse(report['healthy'])
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                    for customer, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + customer, customer), pin)

    def test_supported_integer_count_boundaries(self):
        for count in (0, 1, 9007199254740991):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                profile, journal, read, receipts, pins = seed(directory)
                with closing(sqlite3.connect(journal)) as db, db:
                    value = json.loads(db.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0])
                    value['selection']['count'] = count
                    db.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (json.dumps(value),))
                before = profile.read_bytes(), journal.read_bytes()
                self.assertTrue(audit.audit_receipts(read, receipts, customer_key='a')['healthy'])
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
