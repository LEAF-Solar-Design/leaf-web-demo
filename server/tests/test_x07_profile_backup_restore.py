from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError


class BackupRestoreTests(unittest.TestCase):
    def test_sqlite_backup_fresh_restore_keeps_two_customers_versions_and_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            source, backup, restored = [Path(directory) / name for name in ['source.sqlite', 'backup.sqlite', 'restored.sqlite']]
            store = CustomerProfileStore(source); expected, pins = {}, {}
            for customer in ['a', 'b']:
                first_roles = {'module': {'block': 'Panel '+customer, 'layer': 'MODULES'}}
                first = store.save_version(customer, first_roles, expected_version=0)
                pins[customer] = store.apply_to_project('old-'+customer, customer)
                second_roles = {**first_roles, 'roof': {'layer': 'ROOF '+customer}}
                second = store.save_version(customer, second_roles, expected_version=1)
                expected[customer] = [first, second]
            for left, right in [(source, backup), (backup, restored)]:
                a, b = sqlite3.connect(left), sqlite3.connect(right)
                try: a.backup(b)
                finally: b.close(); a.close()
            recovered = CustomerProfileStore(restored)
            for customer in ['a', 'b']:
                self.assertEqual(recovered.latest(customer), expected[customer][1])
                self.assertEqual(recovered.application_record('old-'+customer, customer), pins[customer])
                self.assertEqual(pins[customer]['roles'], expected[customer][0]['roles'])
                self.assertEqual(pins[customer]['version'], 1)
                self.assertEqual(recovered.apply_to_project('new-'+customer, customer)['version'], 2)
                with self.assertRaises(ProfileScopeError): recovered.application_record('old-'+customer, 'b' if customer=='a' else 'a')
            with recovered._connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 4)
                with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM profile_versions')
            # Original source never receives restored-path applications.
            self.assertIsNone(store.application_record('new-a', 'a'))
