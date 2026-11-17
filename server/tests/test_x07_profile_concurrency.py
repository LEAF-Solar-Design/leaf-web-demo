from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError


class ProfileConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        self.store = CustomerProfileStore(self.path)
        self.store.save_version('a', {'module': {'block': 'Panel A', 'layer': 'MODULES A'}})

    def test_simultaneous_expected_version_edits_have_one_winner(self):
        barrier = Barrier(3)
        def edit(label):
            store = CustomerProfileStore(self.path)
            barrier.wait(timeout=10)
            try:
                return store.save_version('a', {'module': {'block': label, 'layer': 'MODULES A'}}, expected_version=1)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(edit, label) for label in ('Choice one', 'Choice two')]
            barrier.wait(timeout=10)
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.store.latest('a')['version'], 2)
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 2)

    def test_racing_applications_and_edit_keep_one_project_pin(self):
        barrier = Barrier(4)
        def operation(edit=False):
            store = CustomerProfileStore(self.path)
            barrier.wait(timeout=10)
            if edit:
                return store.save_version('a', {'module': {'block': 'Revised', 'layer': 'MODULES A'}}, expected_version=1)
            return store.apply_to_project('one-project', 'a')
        with ThreadPoolExecutor(max_workers=3) as pool:
            applications = [pool.submit(operation) for _ in range(2)]
            editor = pool.submit(operation, True)
            barrier.wait(timeout=10)
            applied = [future.result(timeout=10) for future in applications]
            editor.result(timeout=10)
        self.assertEqual(applied[0], applied[1])
        self.assertIn(applied[0]['version'], (1, 2))
        self.assertEqual(self.store.application_record('one-project', 'a'), applied[0])
        self.assertEqual(self.store.latest('a')['version'], 2)
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0], 1)

    def test_customers_racing_for_same_project_cannot_cross_scope(self):
        self.store.save_version('b', {'module': {'block': 'Panel B', 'layer': 'MODULES B'}})
        barrier = Barrier(3)
        def apply(customer):
            store = CustomerProfileStore(self.path)
            barrier.wait(timeout=10)
            try:
                return customer, store.apply_to_project('shared-key', customer)
            except ProfileScopeError:
                return customer, None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(apply, customer) for customer in ('a', 'b')]
            barrier.wait(timeout=10)
            results = [future.result(timeout=10) for future in futures]
        winners = [(customer, record) for customer, record in results if record is not None]
        self.assertEqual(len(winners), 1)
        customer, record = winners[0]
        self.assertEqual(record['customer_key'], customer)
        with self.assertRaises(ProfileScopeError):
            self.store.application_record('shared-key', 'b' if customer == 'a' else 'a')
