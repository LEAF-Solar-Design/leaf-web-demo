"""Separate SQLite backups retain independent profile/journal generations."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES
from x07_customer_profile_store import CustomerProfileStore


def backup(source, destination):
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(destination)) as restored:
        original.backup(restored)


class ReceiptRestoreDivergence(unittest.TestCase):
    def test_independent_generations_validate_referenced_version_and_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            current = adapter(source)
            profiles = source / 'profiles.sqlite'
            receipts = source / 'receipts.sqlite'
            backup(profiles, root / 'profile-before.sqlite')
            result = current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            backup(receipts, root / 'journal-confirmed.sqlite')
            backup(profiles, root / 'profile-confirmed.sqlite')
            store = CustomerProfileStore(profiles)
            store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M'}}, expected_version=1)
            backup(profiles, root / 'profile-newer.sqlite')
            # Latest version2 does not invalidate a project still pinned to1.
            for generation, succeeds in [('before', False), ('confirmed', True), ('newer', True)]:
                with self.subTest(generation=generation):
                    restored = root / generation
                    restored.mkdir()
                    backup(root / ('profile-' + generation + '.sqlite'), restored / 'profiles.sqlite')
                    backup(root / 'journal-confirmed.sqlite', restored / 'receipts.sqlite')
                    before = tuple((restored / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite'))
                    replay = adapter(restored)
                    if succeeds:
                        self.assertEqual(replay.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0}), result)
                        self.assertEqual(CustomerProfileStore(restored / 'profiles.sqlite').application_record('p', 'a')['version'], 1)
                    else:
                        with self.assertRaisesRegex(ValueError, 'receipt no longer matches project pin'):
                            replay.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
                    self.assertEqual(tuple((restored / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite')), before)

    def test_foreign_restore_refuses_without_disclosure_or_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            current.confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            foreign = root / 'foreign.sqlite'
            store = CustomerProfileStore(foreign)
            store.save_version('PRIVATE-FOREIGN', {'module': {'block': 'PRIVATE-LABEL', 'layer': 'M'}}, expected_version=0)
            store.apply_to_project('p', 'PRIVATE-FOREIGN', expected_version=1)
            backup(foreign, root / 'profiles.sqlite')
            before = tuple((root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite'))
            with self.assertRaises(ValueError) as caught:
                adapter(root).confirm_roles(CONTEXT, {**COPIES[0], 'expected_version': 0})
            self.assertNotIn('PRIVATE-', str(caught.exception))
            self.assertLess(len(str(caught.exception)), 100)
            self.assertEqual(tuple((root / name).read_bytes() for name in ('profiles.sqlite', 'receipts.sqlite')), before)
