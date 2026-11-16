"""Malformed synthetic receipts cannot echo payload labels into diagnostics."""
import json
from pathlib import Path
import tempfile
import unittest
import test_x07_offline_acceptance_report as fixtures
import test_x07_report_cli_integrity as cli


class ReportErrorDisclosure(unittest.TestCase):
    def test_enum_errors_hide_foreign_labels_and_raw_receipts(self):
        sentinel = 'FOREIGN_CUSTOMER_SYNTHETIC_SECRET_LABEL_711'
        faults = ['grammar', 'duplicate', 'digest', 'escape', 'assertion', 'source', 'numeric', 'step']
        expected = ['MALFORMED_RECEIPT', 'DUPLICATE_RECEIPT_KEY', 'MALFORMED_FILE_DIGEST',
                    'SOURCE_PATH_ESCAPE', 'MISSING_OR_UNKNOWN_ASSERTIONS', 'SOURCE_MISMATCH',
                    'NONFINITE_RECEIPT_VALUE', 'MALFORMED_STEPS']
        for fault, error in zip(faults, expected):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths, journey, proof = fixtures.OfflineAcceptanceReport().fixtures(root)
                journey['customer_label'] = sentinel
                raw = json.dumps(journey)
                if fault == 'grammar': raw += sentinel
                elif fault == 'duplicate': raw = raw[:-1] + ',"customer_label":"' + sentinel + '"}'
                elif fault in ['digest', 'escape']:
                    name = sentinel if fault == 'digest' else '../' + sentinel
                    journey['files'][name] = sentinel if fault == 'digest' else '0' * 64
                    raw = json.dumps(journey)
                elif fault == 'assertion':
                    journey['steps'][0]['assertions'] = {sentinel: True}; raw = json.dumps(journey)
                elif fault == 'source':
                    journey['source_sha'] = sentinel; raw = json.dumps(journey)
                elif fault == 'numeric': raw = raw[:-1] + ',"' + sentinel + '":1e9999}'
                elif fault == 'step':
                    journey['steps'][0]['n'] = sentinel; raw = json.dumps(journey)
                paths[0].write_text(raw, encoding='utf-8')
                before = [path.read_bytes() for path in paths]
                code, value = cli.ReportCliIntegrity().call(root, paths)
                self.assertEqual(code, 2)
                self.assertEqual(value['error'], error)
                encoded = json.dumps(value)
                self.assertNotIn(sentinel, encoded)
                self.assertNotIn(str(root), encoded)
                self.assertLess(len(encoded), 1024)
                self.assertRegex(value['error'], r'^[A-Z_]+$')
                self.assertEqual({step['state'] for step in value['steps']}, {'UNREADABLE'})
                self.assertEqual([path.read_bytes() for path in paths], before)
