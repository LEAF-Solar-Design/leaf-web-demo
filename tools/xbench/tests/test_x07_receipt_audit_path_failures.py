from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .test_x07_receipt_audit_schema_failures import invoke
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptPathFailures(unittest.TestCase):
    def test_absence_directories_and_permission_errors_refuse_then_retry(self):
        for side in ('profile', 'journal'):
            for fault in ('absent', 'directory', 'permission'):
                with self.subTest(side=side, fault=fault), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    before = profile.read_bytes(), journal.read_bytes()
                    paths = {'profile': profile, 'journal': journal}
                    failed = Path(directory) / 'PRIVATE_PATH'
                    if fault == 'directory':
                        failed.mkdir()
                    if fault != 'permission':
                        paths[side] = failed
                    real_check = audit.require_database_file

                    def check(path):
                        if fault == 'permission' and path == paths[side]:
                            raise PermissionError('PRIVATE_PATH')
                        return real_check(path)

                    with patch.object(audit, 'require_database_file', side_effect=check):
                        self.assertEqual(invoke(paths['profile'], paths['journal']), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                    self.assertEqual(failed.exists(), fault == 'directory')
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                    self.assertEqual(invoke(profile, journal)[0], 0)
                    for key, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + key, key), pin)
