from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit
from .test_x07_receipt_audit_schema_failures import invoke
from x07_profile_workflow import ProfileWorkflow


class ReceiptOutputBudget(unittest.TestCase):
    def test_maximum_findings_and_unicode_keys_fit_whole_json(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal = Path(directory) / 'profile.sqlite', Path(directory) / 'journal.sqlite'
            store, receipts = audit.CustomerProfileStore(profile), audit.ConfirmationReceiptStore(journal)
            customer, project = '\U0001f680' * 200, '\U0001f680' * 190 + '-old'
            store.save_version(customer, {'module': {'block': 'Panel', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project(project, customer)
            context = {'customer_key': customer, 'project_key': project, 'drawing_key': 'synthetic'}
            value = ProfileWorkflow._result(context, pin, pin['roles'], {'count': 1})
            receipts.run(context, '0' * 64, lambda: value, lambda _: None)
            with closing(sqlite3.connect(journal)) as connection, connection:
                connection.executemany('INSERT INTO confirmations VALUES (?,?,?,?)',
                                       ((str(index), customer, 'x', '{') for index in range(audit.MAX_AUDIT_ROWS - 1)))
            before = profile.read_bytes(), journal.read_bytes()
            code, report = invoke(profile, journal, customer)
            self.assertEqual(code, 1)
            self.assertEqual(report['receipt_count'], audit.MAX_AUDIT_ROWS)
            self.assertEqual(report['issues'], {'invalid_receipts': audit.MAX_AUDIT_ROWS - 1, 'missing_references': 0, 'inconsistent_references': 0})
            self.assertFalse(report['healthy'])
            self.assertEqual(report['customer_key'], customer)
            self.assertLessEqual(len(json.dumps(report).encode('utf-8')), 4096)
            self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
            self.assertEqual(store.application_record(project, customer), pin)
            with closing(sqlite3.connect(journal)) as connection, connection:
                connection.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('excess', customer, 'x', '{'))
            before = profile.read_bytes(), journal.read_bytes()
            self.assertEqual(invoke(profile, journal, customer), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
            self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
