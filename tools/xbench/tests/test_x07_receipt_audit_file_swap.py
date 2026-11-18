from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit, seed
from .test_x07_receipt_audit_schema_failures import invoke


class ReceiptFileSwap(unittest.TestCase):
    def test_replacement_after_validation_refuses_without_mixed_findings(self):
        for side in ('profile', 'journal'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.joinpath('original').mkdir(); root.joinpath('replacement').mkdir()
                profile, journal, read, receipts, pins = seed(root / 'original')
                other_profile, other_journal, _, _, _ = seed(root / 'replacement')
                target = profile if side == 'profile' else journal
                replacement = other_profile if side == 'profile' else other_journal
                if side == 'journal':
                    with closing(sqlite3.connect(replacement)) as connection, connection:
                        connection.execute("UPDATE confirmations SET result='PRIVATE_REPLACEMENT' WHERE customer_key='a'")
                saved = target.with_suffix('.original')
                original_bytes = target.read_bytes()
                replacement_bytes = replacement.read_bytes()
                validated, changed = threading.Event(), threading.Event()
                errors = []
                original = audit.audit_receipts

                def replace():
                    try:
                        if not validated.wait(10):
                            raise AssertionError('validation timeout')
                        os.replace(target, saved)
                        os.replace(replacement, target)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        changed.set()

                def after_validation(*args, **kwargs):
                    validated.set()
                    self.assertTrue(changed.wait(10))
                    self.assertEqual(errors, [])
                    return original(*args, **kwargs)

                thread = threading.Thread(target=replace)
                thread.start()
                try:
                    with patch.object(audit, 'audit_receipts', side_effect=after_validation):
                        self.assertEqual(invoke(profile, journal), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                finally:
                    validated.set(); thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(saved.read_bytes(), original_bytes)
                self.assertEqual(target.read_bytes(), replacement_bytes)
                os.replace(saved, target)  # explicit fixture restore, outside audit
                self.assertEqual(invoke(profile, journal)[0], 0)
                for customer, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + customer, customer), pin)
