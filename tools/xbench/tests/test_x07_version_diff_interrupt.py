from contextlib import contextmanager, redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('interrupt_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffInterrupt(unittest.TestCase):
    def test_real_progress_interrupt_refuses_then_cleared_query_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            saved = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project('old', 'a')
            store.save_version('a', {'module': {'block': 'B', 'layer': 'M'}}, expected_version=1)
            read = diff.CustomerProfileStore(path, read_only=True)
            original = read._connection
            callbacks, changes = [], []
            @contextmanager
            def interrupted():
                with original() as db:
                    def progress(): callbacks.append(True); return 1
                    db.set_progress_handler(progress, 1)
                    try:
                        yield db
                    finally:
                        db.set_progress_handler(None, 0)
                        changes.append(db.total_changes)
            before = path.read_bytes()
            read._connection = interrupted
            options = dict(customer_key='a', profile_id=saved['profile_id'], before_version=1, after_version=2)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                    diff.compare_versions(read, **options)
                text = io.StringIO()
                with patch.object(sys, 'argv', ['diff', '--database', str(path), '--customer', 'a', '--profile-id', saved['profile_id'], '--before', '1', '--after', '2']), patch.object(diff, 'CustomerProfileStore', return_value=read), redirect_stdout(text):
                    self.assertEqual(diff.main(), 2)
                self.assertEqual(json.loads(text.getvalue())['error'], 'SCOPED_VERSIONS_UNAVAILABLE')
                self.assertNotIn('changed', text.getvalue())
            finally:
                read._connection = original
            self.assertGreater(len(callbacks), 0)
            self.assertEqual(changes, [0, 0])
            self.assertEqual(read.application_record('old', 'a'), pin)
            recovered = diff.compare_versions(read, **options)
            self.assertEqual(recovered['changed']['module']['after']['block'], 'B')
            self.assertEqual(path.read_bytes(), before)
