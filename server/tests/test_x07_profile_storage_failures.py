from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class ProfileStorageFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        self.store = CustomerProfileStore(self.path)
        self.roles = {'module': {'block': 'Panel', 'layer': 'MODULES'}}
        self.store.save_version('a', self.roles)
        self.store.apply_to_project('old-project', 'a')

    @contextmanager
    def failure_after(self, statement):
        original = self.store._connection
        class Proxy:
            def __init__(self, db): self.db = db
            def execute(self, sql, parameters=()):
                result = self.db.execute(sql, parameters)
                if sql.startswith(statement):
                    raise sqlite3.OperationalError('injected failure after write')
                return result
        @contextmanager
        def connection():
            with original() as db:
                yield Proxy(db)
        self.store._connection = connection
        try:
            yield
        finally:
            self.store._connection = original

    def test_version_write_failure_rolls_back_and_retry_keeps_sequence(self):
        with self.failure_after('INSERT INTO profile_versions'):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.save_version('a', {'module': {'block': 'Changed', 'layer': 'MODULES'}}, expected_version=1)
        self.assertEqual(self.store.latest('a')['version'], 1)
        self.assertEqual(self.store.application_record('old-project', 'a')['roles'], self.roles)
        self.assertEqual(self.store.save_version('a', self.roles, expected_version=1)['version'], 2)

    def test_failed_first_version_does_not_leave_a_customer_root(self):
        with self.failure_after('INSERT INTO profile_versions'):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.save_version('new', self.roles)
        self.assertIsNone(self.store.latest('new'))
        with self.store._connection() as db:
            self.assertIsNone(db.execute('SELECT * FROM customers WHERE customer_key=?', ('new',)).fetchone())
        self.assertEqual(self.store.save_version('new', self.roles)['version'], 1)

    def test_project_pin_failure_preserves_previous_pin_and_can_retry(self):
        with self.failure_after('INSERT INTO project_applications'):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.apply_to_project('new-project', 'a')
        self.assertIsNone(self.store.application_record('new-project', 'a'))
        self.assertEqual(self.store.application_record('old-project', 'a')['version'], 1)
        self.assertEqual(self.store.apply_to_project('new-project', 'a')['version'], 1)

    def test_lock_contention_has_bounded_failure_and_recovers(self):
        original_connect = sqlite3.connect
        with closing(original_connect(self.path)) as lock:
            lock.execute('BEGIN IMMEDIATE')
            def connect(*args, **kwargs):
                return original_connect(*args, **{**kwargs, 'timeout': 0.02})
            started = time.monotonic()
            try:
                with patch('x07_customer_profile_store.sqlite3.connect', side_effect=connect):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.store.save_version('a', self.roles, expected_version=1)
            finally:
                lock.rollback()
            self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.store.latest('a')['version'], 1)
        self.assertEqual(self.store.save_version('a', self.roles, expected_version=1)['version'], 2)

    def test_corrupt_database_refuses_without_overwrite_and_recovers(self):
        backup = self.path.read_bytes()
        corrupt = b'not a SQLite database'
        self.path.write_bytes(corrupt)
        with self.assertRaises(sqlite3.DatabaseError):
            CustomerProfileStore(self.path)
        self.assertEqual(self.path.read_bytes(), corrupt)
        self.path.write_bytes(backup)
        recovered = CustomerProfileStore(self.path)
        self.assertEqual(recovered.latest('a')['version'], 1)
        self.assertEqual(recovered.application_record('old-project', 'a')['roles'], self.roles)
        self.assertEqual(recovered.save_version('a', self.roles, expected_version=1)['version'], 2)
