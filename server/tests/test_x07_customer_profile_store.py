from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        self.store = CustomerProfileStore(self.path)
        self.a = {'module': {'block': 'Panel A', 'layer': 'MODULES A'}}

    def test_same_customer_reuses_profile_across_projects_and_reopen(self):
        saved = self.store.save_version('customer-a', self.a)
        first = self.store.apply_to_project('project-one', 'customer-a')
        second = CustomerProfileStore(self.path).apply_to_project('project-two', 'customer-a')
        self.assertEqual(first['profile_id'], saved['profile_id'])
        self.assertEqual(second['roles'], self.a)
        self.assertEqual(second['version'], 1)

    def test_customer_isolation_and_foreign_project_refusal(self):
        a = self.store.save_version('customer-a', self.a)
        b = self.store.save_version('customer-b', {'module': {'layer': 'MODULES B'}})
        self.store.apply_to_project('project-a', 'customer-a')
        applied = self.store.apply_to_project('project-b', 'customer-b')
        self.assertEqual(applied['profile_id'], b['profile_id'])
        self.assertNotEqual(applied['profile_id'], a['profile_id'])
        for operation in (self.store.apply_to_project, self.store.application_record):
            with self.assertRaises(ProfileScopeError):
                operation('project-a', 'customer-b')
        self.assertIsNone(self.store.apply_to_project('fresh-project', 'customer-c'))

    def test_new_version_does_not_change_old_project_pin(self):
        old = self.store.save_version('customer-a', self.a)
        self.store.apply_to_project('old-project', 'customer-a')
        new = self.store.save_version('customer-a', {'module': {'layer': 'REVISED'}}, expected_version=1)
        self.assertEqual((old['profile_id'], 2), (new['profile_id'], new['version']))
        self.assertEqual(self.store.apply_to_project('old-project', 'customer-a')['roles'], self.a)
        self.assertEqual(self.store.apply_to_project('new-project', 'customer-a')['version'], 2)

    def test_stale_edit_refuses_without_creating_version(self):
        self.store.save_version('customer-a', self.a)
        with self.assertRaises(ValueError):
            self.store.save_version('customer-a', {}, expected_version=0)
        self.assertEqual(self.store.latest('customer-a')['version'], 1)

    def test_versions_and_applications_are_immutable_at_sqlite_boundary(self):
        self.store.save_version('customer-a', self.a)
        self.store.apply_to_project('project-one', 'customer-a')
        with closing(sqlite3.connect(self.path)) as db:
            for sql in ('UPDATE profile_versions SET roles_json=\'{}\'', 'DELETE FROM profile_versions',
                        'UPDATE project_applications SET version=2', 'DELETE FROM project_applications'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_input_and_return_values_do_not_alias_storage(self):
        saved = self.store.save_version('customer-a', self.a)
        saved['roles']['module']['layer'] = 'changed'
        self.assertEqual(self.store.latest('customer-a')['roles'], self.a)
        with self.assertRaises(ValueError):
            self.store.save_version('', self.a)
        with self.assertRaises(ValueError):
            self.store.save_version('customer-a', {'bad': float('inf')})
