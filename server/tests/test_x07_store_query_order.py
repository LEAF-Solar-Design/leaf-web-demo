"""X07 profile reads and receipt replay must not depend on SQLite scan order."""
from contextlib import closing, contextmanager
from pathlib import Path
import tempfile
import unittest

from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES, INTAKE


class StoreQueryOrder(unittest.TestCase):
    def test_reverse_scans_and_customer_insertion_orders_preserve_reads(self):
        for order in [('a', 'b', 'c'), ('c', 'a', 'b'), ('b', 'c', 'a')]:
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                current = adapter(Path(directory))
                store = current.workflow.store
                receipts = current._receipts
                request = {**COPIES[0], 'expected_version': 0}
                adapters, contexts, originals = {}, {}, {}
                for customer in order:
                    scope = CONTEXT if customer == 'a' else dict(customer_key=customer, project_key=customer + '-old', drawing_key='d:v1')
                    bound = type(current)(current.workflow, context=scope, intake=INTAKE, copies=COPIES,
                                          confirmation_receipts=receipts)
                    contexts[customer], adapters[customer] = scope, bound
                    originals[customer] = bound.confirm_roles(scope, request)
                pins = {customer: store.application_record('p' if customer == 'a' else customer + '-old', customer)
                        for customer in order}
                for version in (2, 3, 4):
                    for customer in reversed(order):
                        store.save_version(customer, {'module': {'block': 'Panel' if customer == 'a' else customer,
                                                               'layer': 'M'}, 'roof': {'layer': 'R' + str(version)}},
                                           expected_version=version - 1)
                expected = {customer: store.latest(customer) for customer in order}
                connection = store._connection
                connect = receipts._connect
                for reverse in (False, True, False):
                    @contextmanager
                    def reordered_profile():
                        with connection() as db:
                            db.execute('PRAGMA reverse_unordered_selects=' + str(int(reverse)))
                            self.assertEqual(db.execute('PRAGMA reverse_unordered_selects').fetchone()[0], int(reverse))
                            yield db

                    def reordered_receipt():
                        db = connect()
                        db.execute('PRAGMA reverse_unordered_selects=' + str(int(reverse)))
                        self.assertEqual(db.execute('PRAGMA reverse_unordered_selects').fetchone()[0], int(reverse))
                        return db

                    store._connection = reordered_profile
                    receipts._connect = reordered_receipt
                    for customer in order:
                        self.assertEqual(store.latest(customer), expected[customer])
                        self.assertEqual(store.latest(customer)['version'], 4)
                        self.assertEqual(store.application_record('p' if customer == 'a' else customer + '-old', customer), pins[customer])
                    for customer in order:
                        self.assertEqual(adapters[customer].confirm_roles(contexts[customer], request), originals[customer])
                    with closing(connect()) as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 3)
                store._connection = connection
                receipts._connect = connect
