from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileSchemaError


class SchemaSemanticTamperingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        store = CustomerProfileStore(self.path)
        store.save_version('a', {'module': {'block': 'Panel', 'layer': 'MODULES'}})
        store.apply_to_project('old', 'a')

    def sql(self, statement):
        db = sqlite3.connect(self.path)
        try:
            with db: db.executescript(statement)
        finally: db.close()

    def refuse_unchanged(self):
        before = self.path.read_bytes()
        messages = []
        for _ in range(2):
            with self.assertRaises(ProfileSchemaError) as error: CustomerProfileStore(self.path)
            messages.append(str(error.exception)); self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(messages[0], messages[1])

    def test_same_trigger_name_with_noop_body_refuses(self):
        self.sql('DROP TRIGGER versions_no_update; CREATE TRIGGER versions_no_update BEFORE UPDATE ON profile_versions BEGIN SELECT 1; END;')
        self.refuse_unchanged()

    def test_same_trigger_name_with_conditional_bypass_refuses(self):
        self.sql("DROP TRIGGER applications_no_delete; CREATE TRIGGER applications_no_delete BEFORE DELETE ON project_applications WHEN 0 BEGIN SELECT RAISE(ABORT, 'project applications are pinned'); END;")
        self.refuse_unchanged()

    def rebuild(self, table, transform):
        source = sqlite3.connect(self.path)
        try: ddl = source.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','trigger') ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name").fetchall()
        finally: source.close()
        target = Path(self.temp.name) / 'altered.sqlite'
        db = sqlite3.connect(target)
        try:
            with db:
                for kind, name, sql in ddl:
                    db.execute(transform(sql) if name == table and kind == 'table' else sql)
                db.execute('PRAGMA user_version=1')
        finally: db.close()
        self.path = target

    def test_same_columns_without_primary_key_refuse(self):
        self.rebuild('customers', lambda sql: sql.replace('customer_key TEXT PRIMARY KEY', 'customer_key TEXT'))
        self.refuse_unchanged()

    def test_same_columns_with_missing_foreign_key_refuse(self):
        self.rebuild('project_applications', lambda sql: sql.replace(
            'FOREIGN KEY(customer_key, version) REFERENCES profile_versions(customer_key, version)', 'CHECK(version > 0)'))
        self.refuse_unchanged()

    def test_extra_trigger_under_supported_version_refuses(self):
        self.sql('CREATE TRIGGER bypass AFTER INSERT ON profile_versions BEGIN SELECT 1; END;')
        self.refuse_unchanged()
