from contextlib import closing, redirect_stdout
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import seed
import x07_profile_change_impact as impact


class ImpactSchemaDrift(unittest.TestCase):
    def test_validated_store_schema_change_refuses_whole_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal, read, receipts, pins = seed(directory)
            validated, changed = threading.Event(), threading.Event()
            errors, snapshots = [], []
            original = impact.preview_change

            def writer():
                try:
                    if not validated.wait(10):
                        raise AssertionError('validation timeout')
                    with closing(sqlite3.connect(profile)) as connection, connection:
                        connection.execute('DROP TABLE profile_versions')
                    snapshots.append(profile.read_bytes())
                except BaseException as error:
                    errors.append(error)
                finally:
                    changed.set()

            def after_validation(*args, **kwargs):
                validated.set()
                self.assertTrue(changed.wait(10))
                self.assertEqual(errors, [])
                return original(*args, **kwargs)

            thread = threading.Thread(target=writer)
            thread.start()
            output = io.StringIO()
            try:
                argv = ['impact', '--database', str(profile), '--customer', 'a', '--profile-id', pins['a']['profile_id'], '--before', '1', '--after', '1']
                with patch.object(sys, 'argv', argv), patch.object(impact, 'preview_change', side_effect=after_validation), redirect_stdout(output):
                    self.assertEqual(impact.main(), 2)
            finally:
                validated.set(); thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(output.getvalue()), {'schema': 'leaf.x07.profile-change-impact.v1', 'error': 'SCOPED_PREVIEW_UNAVAILABLE'})
            self.assertEqual(profile.read_bytes(), snapshots[0])
            with closing(sqlite3.connect(profile)) as connection:
                rows = connection.execute('SELECT project_key,customer_key,profile_id,version FROM project_applications ORDER BY project_key').fetchall()
            self.assertEqual(rows, [('old-' + key, key, pin['profile_id'], pin['version']) for key, pin in sorted(pins.items())])
