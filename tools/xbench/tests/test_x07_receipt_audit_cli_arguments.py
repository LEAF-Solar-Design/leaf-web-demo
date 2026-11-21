from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditCliArguments(unittest.TestCase):
    def call(self, arguments):
        child = subprocess.run([sys.executable, audit.__file__, *arguments], capture_output=True, text=True, timeout=10)
        self.assertEqual(child.stderr, '')
        self.assertLess(len(child.stdout), 512)
        return child.returncode, json.loads(child.stdout)

    def test_invalid_arguments_emit_generic_json_without_creating_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / 'PRIVATE_PROFILE.sqlite'
            journal = root / 'PRIVATE_JOURNAL.sqlite'
            valid = ['--database', str(profile), '--journal', str(journal), '--customer', 'a']
            cases = [[], ['--database'], ['--journal'], ['--customer'],
                     valid + ['--PRIVATE_UNKNOWN', 'PRIVATE_VALUE'],
                     ['--database', str(profile), '--journal', str(journal), '--customer', ''],
                     ['--database', str(root / 'missing' / 'profile.sqlite'), '--journal', str(journal), '--customer', 'a']]
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    code, value = self.call(arguments)
                    self.assertEqual(code, 2)
                    self.assertEqual(value['schema'], 'leaf.x07.receipt-history-audit.v1')
                    self.assertIn(value['error'], ['INVALID_ARGUMENTS', 'AUDIT_UNAVAILABLE'])
                    self.assertNotIn('PRIVATE', json.dumps(value))
                    self.assertEqual(list(root.iterdir()), [])
            _, _, read, _, pins = seed(root)
            code, value = self.call(['--database', str(root / 'profile.sqlite'), '--journal', str(root / 'journal.sqlite'), '--customer', 'a'])
            self.assertEqual(code, 0)
            self.assertTrue(value['healthy'])
            self.assertEqual(read.application_record('old-a', 'a'), pins['a'])

    def test_literal_metacharacter_paths_for_either_store_and_retry(self):
        for side in ['profile', 'journal', 'both']:
            with self.subTest(store=side), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                profile, journal, _, _, pins = seed(root)
                if side in ['profile', 'both']:
                    profile = profile.rename(root / 'profile ; $() & [literal].sqlite')
                if side in ['journal', 'both']:
                    journal = journal.rename(root / 'journal ; $() & [literal].sqlite')
                read = audit.CustomerProfileStore(profile, read_only=True)
                before = profile.read_bytes(), journal.read_bytes()
                wrong, value = self.call(['--database', str(journal), '--journal', str(profile), '--customer', 'a'])
                self.assertEqual(wrong, 2)
                self.assertEqual(value['error'], 'AUDIT_UNAVAILABLE')
                code, value = self.call(['--database', str(profile), '--journal', str(journal), '--customer', 'a'])
                self.assertEqual(code, 0)
                self.assertTrue(value['healthy'])
                self.assertEqual(value['receipt_count'], 1)
                for key, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + key, key), pin)
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
                self.assertEqual(len(list(root.iterdir())), 2)
