from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit, seed
from .test_x07_receipt_audit_schema_failures import invoke


class ReceiptSchemaDrift(unittest.TestCase):
    def test_barrier_schema_change_after_validation_refuses_whole_audit(self):
        for side in ('profile', 'journal'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                profile, journal, read, receipts, pins = seed(directory)
                validated, changed = threading.Event(), threading.Event()
                errors, snapshots = [], []
                target = profile if side == 'profile' else journal
                original = audit.audit_receipts

                def writer():
                    try:
                        if not validated.wait(10):
                            raise AssertionError('validation barrier timeout')
                        with closing(sqlite3.connect(target)) as connection, connection:
                            connection.execute('DROP TABLE ' + ('customers' if side == 'profile' else 'confirmations'))
                        snapshots.append((profile.read_bytes(), journal.read_bytes()))
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
                try:
                    with patch.object(audit, 'audit_receipts', side_effect=after_validation):
                        self.assertEqual(invoke(profile, journal), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                finally:
                    validated.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), snapshots[0])
                with closing(sqlite3.connect(profile)) as connection:
                    rows = connection.execute('SELECT project_key,customer_key,profile_id,version FROM project_applications ORDER BY project_key').fetchall()
                self.assertEqual(rows, [('old-' + key, key, pin['profile_id'], pin['version']) for key, pin in sorted(pins.items())])
