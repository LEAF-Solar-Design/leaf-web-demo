from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import INTAKE, COPIES
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import ConfirmationReceiptStore, WorkflowAdapter


class StoreVacuumRetention(unittest.TestCase):
    def test_vacuum_reopen_retains_versions_pins_isolation_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, journal = root / 'profiles.sqlite', root / 'receipts.sqlite'
            def bound(customer):
                context = dict(customer_key=customer, project_key='old-' + customer, drawing_key='d-' + customer)
                current = WorkflowAdapter(ProfileWorkflow(CustomerProfileStore(database)), context=context,
                    intake=INTAKE, copies=COPIES, confirmation_receipts=ConfirmationReceiptStore(journal))
                return current, context
            receipts = {}
            request = {**COPIES[0], 'expected_version': 0}
            for customer in ['a', 'b']:
                current, context = bound(customer)
                receipts[customer] = current.confirm_roles(context, request)
                current.workflow.store.save_version(customer, {'module': {'block': 'pAnEl', 'layer': 'M'}}, expected_version=1)
                current.workflow.store.apply_to_project('new-' + customer, customer, expected_version=2)
            for path in [database, journal]:
                with closing(sqlite3.connect(path)) as db: db.execute('VACUUM')
            for customer in ['a', 'b']:
                current, context = bound(customer)
                self.assertEqual(current.confirm_roles(context, request), receipts[customer])
                store = current.workflow.store
                self.assertEqual(store.latest(customer)['version'], 2)
                self.assertEqual(store.latest(customer)['roles']['module']['block'], 'pAnEl')
                self.assertEqual(store.application_record('old-' + customer, customer)['roles']['module']['block'], 'Panel')
                self.assertEqual(store.application_record('old-' + customer, customer)['version'], 1)
                self.assertEqual(store.application_record('new-' + customer, customer)['version'], 2)
                with self.assertRaises(ValueError): store.application_record('old-' + customer, 'b' if customer == 'a' else 'a')
            with CustomerProfileStore(database)._connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 4)
                with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM profile_versions')
            with closing(sqlite3.connect(journal)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 2)
