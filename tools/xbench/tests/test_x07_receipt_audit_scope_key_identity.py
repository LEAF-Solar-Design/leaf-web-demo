from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit
from x07_profile_workflow import ProfileWorkflow


class ReceiptScopeIdentity(unittest.TestCase):
    def test_delimiters_unicode_and_nul_remain_exact_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal = Path(directory) / 'profiles.sqlite', Path(directory) / 'receipts.sqlite'
            store = audit.CustomerProfileStore(profile)
            receipts = audit.ConfirmationReceiptStore(journal)
            keys = [('a:b', 'c'), ('a', 'b:c'), ('é', 'unicode-1'), ('e\u0301', 'unicode-2'), ('nul\0tail', 'nul-1'), ('nul', 'nul-2')]
            pins = {}
            for index, (customer, project) in enumerate(keys):
                store.save_version(customer, {'module': {'block': 'PRIVATE_' + str(index), 'layer': 'M'}}, expected_version=0)
                pin = store.apply_to_project(project, customer)
                pins[customer] = pin
                context = {'customer_key': customer, 'project_key': project, 'drawing_key': 'synthetic'}
                value = ProfileWorkflow._result(context, pin, pin['roles'], {'count': 1})
                receipts.run(context, '0' * 64, lambda: value, lambda _: None)
                if index % 2:
                    with closing(sqlite3.connect(journal)) as connection, connection:
                        connection.execute('UPDATE confirmations SET result=? WHERE customer_key=?', ('{PRIVATE_FOREIGN', customer))
            read = audit.CustomerProfileStore(profile, read_only=True)
            receipts = audit.ConfirmationReceiptStore(journal, read_only=True)
            before = profile.read_bytes(), journal.read_bytes()
            for index, (customer, project) in enumerate(keys):
                with self.subTest(customer=customer):
                    report = audit.audit_receipts(read, receipts, customer_key=customer)
                    self.assertEqual(report['receipt_count'], 1)
                    self.assertEqual(report['issues'], {'invalid_receipts': index % 2, 'missing_references': 0, 'inconsistent_references': 0})
                    self.assertEqual(report['healthy'], index % 2 == 0)
                    self.assertNotIn('PRIVATE', json.dumps(report))
                    self.assertEqual(read.application_record(project, customer), pins[customer])
            self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
