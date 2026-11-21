"""X07 shared-cache dirty-read connections refuse before profile or replay reads."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES
from x07_customer_profile_store import ProfileSchemaError


class SQLiteReadUncommitted(unittest.TestCase):
    def test_shared_cache_dirty_rows_never_become_profile_or_replay_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            store, receipts = current.workflow.store, current._receipts
            request = {**COPIES[0], 'expected_version': 0}
            original = current.confirm_roles(CONTEXT, request)
            profile_uri = store.path.resolve().as_uri() + '?cache=shared'
            journal_uri = receipts.path.resolve().as_uri() + '?cache=shared'
            with closing(sqlite3.connect(profile_uri, uri=True)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO profile_versions SELECT customer_key,profile_id,2,roles_json,created_at FROM profile_versions WHERE version=1')
                @contextmanager
                def dirty_profile():
                    with closing(sqlite3.connect(profile_uri, uri=True)) as reader:
                        reader.row_factory = sqlite3.Row
                        reader.execute('PRAGMA foreign_keys=ON'); reader.execute('PRAGMA read_uncommitted=ON')
                        # Prove this connection really can observe the pending row.
                        self.assertEqual(reader.execute('SELECT MAX(version) FROM profile_versions').fetchone()[0], 2)
                        yield reader
                with patch.object(store, '_connection', dirty_profile):
                    for operation in [lambda: store.latest('a'), lambda: store.application_record('p', 'a'),
                                      lambda: store.apply_to_project('dirty', 'a')]:
                        with self.assertRaisesRegex(ProfileSchemaError, 'unsafe SQLite connection settings'):
                            operation()
                writer.rollback()
            with closing(sqlite3.connect(journal_uri, uri=True)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('UPDATE confirmations SET result=? WHERE project_key=?', ('{"private":"dirty-receipt"}', 'p'))
                def dirty_journal():
                    db = sqlite3.connect(journal_uri, uri=True)
                    db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA read_uncommitted=ON')
                    self.assertIn('dirty-receipt', db.execute('SELECT result FROM confirmations').fetchone()[0])
                    return db
                with patch.object(receipts, '_connect', dirty_journal):
                    with self.assertRaisesRegex(ProfileSchemaError, 'unsafe SQLite connection settings') as caught:
                        current.confirm_roles(CONTEXT, request)
                self.assertNotIn('dirty-receipt', str(caught.exception))
                writer.rollback()
            self.assertEqual(store.latest('a')['version'], 1)
            self.assertEqual(store.application_record('p', 'a'), original['applied_profile'])
            self.assertIsNone(store.application_record('dirty', 'a'))
            self.assertEqual(current.confirm_roles(CONTEXT, request), original)
