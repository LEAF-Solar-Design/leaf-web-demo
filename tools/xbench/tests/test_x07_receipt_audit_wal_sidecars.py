from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import seed
from .test_x07_receipt_audit_schema_failures import invoke


class ReceiptWalSidecars(unittest.TestCase):
    def test_owned_wal_copies_refuse_missing_or_inaccessible_shm(self):
        for side in ('profile', 'journal'):
            for fault in ('none', 'missing', 'permission'):
                with self.subTest(side=side, fault=fault), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    source = profile if side == 'profile' else journal
                    with closing(sqlite3.connect(source)) as writer:
                        writer.execute('PRAGMA journal_mode=WAL')
                        writer.execute('PRAGMA wal_autocheckpoint=0')
                        if side == 'profile':
                            writer.execute('INSERT INTO profile_versions SELECT customer_key,profile_id,2,roles_json,created_at FROM profile_versions WHERE customer_key=?', ('a',))
                        else:
                            writer.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('bad', 'a', 'x', '{'))
                        writer.commit()
                        copies = {}
                        for key, original in [('profile', profile), ('journal', journal)]:
                            copies[key] = Path(directory) / ('copy-' + key + '.sqlite')
                            copies[key].write_bytes(original.read_bytes())
                        target = copies[side]
                        Path(str(target) + '-wal').write_bytes(Path(str(source) + '-wal').read_bytes())
                        shared = Path(str(target) + '-shm')
                        if fault != 'missing':
                            shared.write_bytes(Path(str(source) + '-shm').read_bytes())
                        files = set(Path(directory).iterdir())
                        before = copies['profile'].read_bytes(), copies['journal'].read_bytes()
                        original_open = Path.open

                        def readable(path, *args, **kwargs):
                            if fault == 'permission' and path == shared:
                                raise PermissionError('PRIVATE_SHM')
                            return original_open(path, *args, **kwargs)

                        with patch.object(Path, 'open', readable):
                            code, report = invoke(copies['profile'], copies['journal'])
                        if fault == 'none':
                            self.assertEqual(code, 0 if side == 'profile' else 1)
                            self.assertEqual(report['receipt_count'], 1 if side == 'profile' else 2)
                            self.assertEqual(report['issues'], {'invalid_receipts': int(side == 'journal'), 'missing_references': 0, 'inconsistent_references': 0})
                        else:
                            self.assertEqual((code, report), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                        self.assertEqual(set(Path(directory).iterdir()), files)
                        self.assertEqual((copies['profile'].read_bytes(), copies['journal'].read_bytes()), before)
                        for customer, pin in pins.items():
                            self.assertEqual(read.application_record('old-' + customer, customer), pin)
