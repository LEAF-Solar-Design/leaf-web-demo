import tempfile
import unittest
from ._x07_receipt_audit_fixtures import seed
from .test_x07_receipt_audit_schema_failures import invoke


class ReceiptPhysicalCorruption(unittest.TestCase):
    def test_truncated_headers_and_btree_damage_refuse_without_repair(self):
        for side in ('profile', 'journal'):
            for damage in ('truncated', 'page'):
                with self.subTest(side=side, damage=damage), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    target = profile if side == 'profile' else journal
                    original = target.read_bytes()
                    damaged = bytearray(original[:12] if damage == 'truncated' else original)
                    if damage == 'page':
                        damaged[100] = 255  # invalid page-one btree kind
                    target.write_bytes(damaged)
                    before = profile.read_bytes(), journal.read_bytes()
                    self.assertEqual(invoke(profile, journal), (2, {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}))
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                    target.write_bytes(original)
                    self.assertEqual(invoke(profile, journal)[0], 0)
                    for customer, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + customer, customer), pin)
