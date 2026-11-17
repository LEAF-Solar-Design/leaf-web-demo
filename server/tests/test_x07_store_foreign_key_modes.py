"""X07 injected connections cannot bypass foreign-key enforcement."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES
from x07_customer_profile_store import ProfileSchemaError


class StoreForeignKeyModes(unittest.TestCase):
    def test_disabled_foreign_keys_refuse_profile_and_journal_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            current = adapter(Path(directory))
            store, receipts = current.workflow.store, current._receipts
            original = current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            connect_profile = store._connection
            @contextmanager
            def disabled_profile():
                with connect_profile() as db:
                    db.execute('PRAGMA foreign_keys=OFF')
                    self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 0)
                    yield db
            with patch.object(store, '_connection', disabled_profile):
                for operation in [lambda: store.save_version('a', original['resolved_roles'], expected_version=1),
                                  lambda: store.apply_to_project('new', 'a'), lambda: store.latest('a')]:
                    with self.assertRaisesRegex(ProfileSchemaError, 'unsafe SQLite connection settings'):
                        operation()
            connect = receipts._connect
            def disabled_journal():
                db = connect(); db.execute('PRAGMA foreign_keys=OFF'); return db
            produced = []
            with patch.object(receipts, '_connect', disabled_journal):
                with self.assertRaisesRegex(ProfileSchemaError, 'unsafe SQLite connection settings'):
                    receipts.run({**CONTEXT, 'project_key': 'new'}, 'digest', lambda: produced.append(True), lambda value: None)
            self.assertEqual(produced, [])
            self.assertEqual(store.application_record('p', 'a'), original['applied_profile'])
            self.assertIsNone(store.application_record('new', 'a'))
            self.assertEqual(store.latest('a')['version'], 1)
            saved = store.save_version('a', original['resolved_roles'], expected_version=1)
            self.assertEqual(saved['version'], 2)
            self.assertEqual(store.apply_to_project('new', 'a')['version'], 2)
            self.assertEqual(current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0}), original)
