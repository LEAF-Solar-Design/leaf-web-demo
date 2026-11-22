from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileSchemaError


class SchemaEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'

    def sql(self, text):
        db = sqlite3.connect(self.path)
        try:
            with db: return db.execute(text).fetchall()
        finally: db.close()

    def test_missing_database_initializes_version_one_then_supported_reopen(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(CustomerProfileStore(self.path).schema_status['decision'], 'initialize')
        self.assertEqual(self.sql('PRAGMA user_version'), [(1,)])
        self.assertEqual(CustomerProfileStore(self.path).schema_status['decision'], 'supported')

    def test_unversioned_supported_legacy_is_adopted_without_record_changes(self):
        store = CustomerProfileStore(self.path)
        record = store.save_version('a', {'module': {'block': 'Panel', 'layer': 'MODULES'}})
        self.sql('PRAGMA user_version=0')
        adopted = CustomerProfileStore(self.path)
        self.assertEqual(adopted.schema_status['decision'], 'adopt_legacy')
        self.assertEqual(adopted.latest('a'), record)

    def test_future_version_refuses_without_modifying_database_bytes(self):
        CustomerProfileStore(self.path); self.sql('PRAGMA user_version=99')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ProfileSchemaError, 'unsupported profile schema version: 99'):
            CustomerProfileStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_unversioned_shape_refuses_without_clobbering(self):
        self.sql('CREATE TABLE unrelated (id INTEGER)')
        before = self.path.read_bytes()
        with self.assertRaises(ProfileSchemaError): CustomerProfileStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_initialization_rolls_back_partial_ddl_and_version(self):
        def fail(db):
            db.execute('CREATE TABLE partial (id INTEGER)')
            db.execute('PRAGMA user_version=1')
            raise sqlite3.OperationalError('injected init failure')
        with patch.object(CustomerProfileStore, '_create_schema', side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError): CustomerProfileStore(self.path)
        self.assertEqual(self.sql("SELECT name FROM sqlite_master WHERE type='table'"), [])
        self.assertEqual(self.sql('PRAGMA user_version'), [(0,)])
        self.assertEqual(CustomerProfileStore(self.path).schema_status['decision'], 'initialize')

    def test_supported_version_missing_immutability_trigger_refuses_without_repair(self):
        CustomerProfileStore(self.path); self.sql('DROP TRIGGER versions_no_update')
        before = self.path.read_bytes()
        with self.assertRaises(ProfileSchemaError): CustomerProfileStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)
