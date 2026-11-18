from contextlib import redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('encoding_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class ReducerEncodingFailures(unittest.TestCase):
    def test_invalid_encodings_boms_and_lone_surrogates_refuse_before_oracle(self):
        payloads = [b'\xff', b'["\xe2\x82"]', b'\xef\xbb\xbf[]', b'\xff\xfe[\x00]\x00',
                    b'["\\ud800"]', b'[{"nested":["\\udfff"]}]', b'[{"\\ud800":"value"}]']
        with tempfile.TemporaryDirectory() as directory:
            history, output = Path(directory) / 'history.json', Path(directory) / 'output.json'
            output.write_bytes(b'prior output')
            for payload in payloads:
                text = io.StringIO()
                history.write_bytes(payload)
                with self.subTest(payload=payload), patch.object(sys, 'argv', ['reducer', '--history', str(history), '--out', str(output)]), patch.object(reducer, 'reduce_history') as oracle, patch.object(reducer, 'atomic_publish') as publish, redirect_stdout(text):
                    self.assertEqual(reducer.main(), 2)
                oracle.assert_not_called(); publish.assert_not_called()
                self.assertEqual(json.loads(text.getvalue())['error'], 'INVALID_HISTORY_INPUT')
                self.assertLess(len(text.getvalue()), 200)
                self.assertEqual(history.read_bytes(), payload)
                self.assertEqual(output.read_bytes(), b'prior output')
                self.assertFalse(list(Path(directory).glob('.x07-reducer-*.tmp')))

    def test_valid_surrogate_pair_and_api_unicode_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            path.write_bytes(b'["\\ud83d\\ude00"]')
            self.assertEqual(reducer.read_history(path), ['\U0001f600'])
        for history in [['\ud800'], [{'\udfff': 'value'}]]:
            with self.subTest(history=repr(history)), patch.object(reducer.copy, 'deepcopy') as clone:
                with self.assertRaises(ValueError):
                    reducer.reduce_history(history, lambda rows: self.fail('oracle called'))
                clone.assert_not_called()
        self.assertTrue(reducer.reduce_history(['é', '\U0001f600'], lambda rows: None)['complete'])
