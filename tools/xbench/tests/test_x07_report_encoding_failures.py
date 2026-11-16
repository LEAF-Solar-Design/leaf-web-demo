from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('encoding_fixtures', root / 'server/tests/test_x07_offline_acceptance_report.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ReportEncodingFailures(unittest.TestCase):
    def test_invalid_utf8_bom_and_unpaired_surrogates_refuse(self):
        for fault in ['invalid', 'truncated', 'bom', 'high', 'low']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(source)
                raw = json.dumps(journey).encode('utf-8')
                if fault == 'invalid': raw += b'\x80'
                elif fault == 'truncated': raw += b'\xe9\x9b'
                elif fault == 'bom': raw = b'\xef\xbb\xbf' + raw
                else: raw = raw[:-1] + (b',"extra":"\\ud800"}' if fault == 'high' else b',"extra":"\\udfff"}')
                paths[0].write_bytes(raw)
                output = StringIO()
                args = ['report', '--source-sha', fixtures.SOURCE, '--source-root', str(source),
                        '--journey', str(paths[0]), '--proof', str(paths[1])]
                with patch.object(sys, 'argv', args), redirect_stdout(output): self.assertEqual(fixtures.report.main(), 2)
                value = json.loads(output.getvalue())
                self.assertEqual({step['state'] for step in value['steps']}, {'UNREADABLE'})
                self.assertLess(len(value['error']), 80)
                self.assertEqual(paths[0].read_bytes(), raw)

    def test_valid_utf8_and_paired_surrogate_escape_remain_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(source)
            journey['extra'] = 'Panel \u96ea\U0001f600'
            for escaped in [True, False]:
                paths[0].write_text(json.dumps(journey, ensure_ascii=escaped), encoding='utf-8')
                result = fixtures.report.aggregate(paths[0], [paths[1]], fixtures.SOURCE, source)
                self.assertTrue(all(step['state'] == 'SYNTHETIC_PASS' for step in result['steps']))
