"""Offline numeric lexeme bounds, independent of historical evidence."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from ._x07_report_fixtures import fixtures, cli


class ReportNumericTokens(unittest.TestCase):
    def call(self, raw, expected_code, expected_error=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            paths[0].write_text(raw(json.dumps(journey)), encoding='utf-8')
            before = [path.read_bytes() for path in paths]
            code, value = cli.ReportCliIntegrity().call(root, paths)
            self.assertEqual(code, expected_code)
            self.assertLess(len(json.dumps(value)), 4096)
            if expected_error:
                self.assertEqual(value['error'], expected_error)
                self.assertEqual({step['state'] for step in value['steps']}, {'UNREADABLE'})
            self.assertEqual([path.read_bytes() for path in paths], before)

    def test_huge_integer_tokens_refuse_at_explicit_budget(self):
        for token in ['9' * 4097, '-' + '9' * 4097, '9' * 100000]:
            with self.subTest(length=len(token)):
                self.call(lambda raw: raw[:-1] + ',"extra":' + token + '}', 2,
                          'RECEIPT_INTEGER_BUDGET_EXCEEDED')

    def test_overflowing_exponents_refuse_even_in_unused_fields(self):
        for token in ['1e9999', '-1e9999', '1.7976931348623159e308']:
            with self.subTest(token=token):
                self.call(lambda raw: raw[:-1] + ',"extra":' + token + '}', 2,
                          'NONFINITE_RECEIPT_VALUE')

    def test_supported_numeric_boundaries_and_signed_zero_are_exact(self):
        for token in ['9' * 4096, '-' + '9' * 4096, '1.7976931348623157e308',
                      '5e-324', '-0', '-0.0', '0', '0.0']:
            with self.subTest(token=token[:40]):
                self.call(lambda raw: raw[:-1] + ',"extra":' + token + '}', 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'numeric.json'
            path.write_text('{"integer":-0,"negative":-0.0,"positive":0.0,"tiny":5e-324}')
            value = fixtures.report.read_receipt(path)
            self.assertIs(type(value['integer']), int)
            self.assertEqual(value['integer'], 0)
            self.assertEqual(math.copysign(1, value['negative']), -1)
            self.assertEqual(math.copysign(1, value['positive']), 1)
            self.assertEqual(value['tiny'].hex(), '0x0.0000000000001p-1022')

    def test_numeric_values_cannot_replace_typed_step_fields(self):
        for token in ['-0', '-0.0', '1.0', '9' * 4096]:
            with self.subTest(token=token[:40]):
                self.call(lambda raw: raw.replace('"n": 1', '"n": ' + token, 1), 2,
                          'MALFORMED_STEPS')
        for token in ['-0', '-0.0', '1']:
            with self.subTest(assertion=token):
                self.call(lambda raw: raw.replace('"fresh_proposal": true', '"fresh_proposal": ' + token, 1),
                          2, 'MALFORMED_ASSERTIONS')
