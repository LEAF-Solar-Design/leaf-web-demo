from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter
from x07_customer_profile_store import CustomerProfileStore


class BackupActiveTransaction(unittest.TestCase):
    def test_online_backup_excludes_pending_rows_then_includes_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            store = current.workflow.store
            store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M'}}, expected_version=0)
            old = store.apply_to_project('old', 'a', expected_version=1)
            database = root / 'profiles.sqlite'
            with closing(sqlite3.connect(database)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO profile_versions SELECT customer_key,profile_id,2,roles_json,created_at FROM profile_versions WHERE version=1')
                for name, expected in [('pending.sqlite', 1), ('committed.sqlite', 2)]:
                    if expected == 2: writer.commit()
                    target = root / name
                    with closing(sqlite3.connect(database)) as reader, closing(sqlite3.connect(target)) as destination:
                        reader.backup(destination)
                    restored = CustomerProfileStore(target)
                    self.assertEqual(restored.latest('a')['version'], expected)
                    self.assertEqual(restored.application_record('old', 'a'), old)
                    with restored._connection() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], expected)
