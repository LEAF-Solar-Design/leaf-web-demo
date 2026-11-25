from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from .test_x07_receipt_audit_schema_failures import invoke
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptSpecialFiles(unittest.TestCase):
    def test_nonregular_metadata_refuses_before_database_open(self):
        for side in ('profile', 'journal'):
            for mode in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFLNK):
                with self.subTest(side=side, mode=mode), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    before = profile.read_bytes(), journal.read_bytes()
                    target = profile if side == 'profile' else journal
                    original = Path.lstat

                    def metadata(path, *args, **kwargs):
                        return SimpleNamespace(st_mode=mode) if path == target else original(path, *args, **kwargs)

                    with patch.object(Path, 'lstat', metadata), patch.object(audit.sqlite3, 'connect', side_effect=AssertionError('blocking open attempted')) as opened:
                        self.assertEqual(invoke(profile, journal), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                        opened.assert_not_called()
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                    for key, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + key, key), pin)
