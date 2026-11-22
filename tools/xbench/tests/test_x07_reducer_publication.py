from contextlib import redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publication_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class ReducerPublication(unittest.TestCase):
    def invoke(self, args):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['reducer', *map(str, args)]), redirect_stdout(output):
            try:
                code = reducer.main()
            except SystemExit as error:
                code = error.code
        return code, output.getvalue()

    def test_atomic_write_and_replace_failures_preserve_old_bytes(self):
        real_fdopen = os.fdopen
        class PartialWrite:
            def __init__(self, *args, **kwargs): self.stream = real_fdopen(*args, **kwargs)
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def write(self, value):
                self.stream.write(value[:4])
                raise OSError('PRIVATE_WRITE_FAILURE')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history, output = root / 'history.json', root / 'output.json'
            history.write_bytes(b'[{"op":"synthetic_failure"}]')
            output.write_bytes(b'prior output')
            for seam, failure in [('fdopen', PartialWrite), ('replace', None), ('fsync', None)]:
                with self.subTest(seam=seam):
                    mock = patch.object(reducer.os, seam, side_effect=failure if failure else OSError('PRIVATE_FAILURE'))
                    with mock:
                        code, text = self.invoke(['--history', history, '--out', output])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(text)['error'], 'OUTPUT_PUBLICATION_FAILED')
                    self.assertNotIn('PRIVATE', text)
                    self.assertEqual(history.read_bytes(), b'[{"op":"synthetic_failure"}]')
                    self.assertEqual(output.read_bytes(), b'prior output')
                    self.assertEqual(sorted(path.name for path in root.iterdir()), ['history.json', 'output.json'])

    def test_atomic_success_and_input_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history, output = root / 'history.json', root / 'output.json'
            history.write_bytes(b'[]')
            output.write_bytes(b'prior output')
            code, text = self.invoke(['--history', history, '--out', output])
            self.assertEqual(code, 0)
            self.assertEqual(output.read_bytes(), text.encode())
            self.assertTrue(text.endswith('\n'))
            alias = root / 'alias.json'
            os.link(history, alias)
            for target in [root / '.' / 'history.json', alias]:
                code, text = self.invoke(['--history', history, '--out', target])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(text)['error'], 'OUTPUT_PUBLICATION_FAILED')
                self.assertEqual(history.read_bytes(), b'[]')
            self.assertFalse(list(root.glob('.x07-reducer-*.tmp')))

    def test_duplicate_and_escaped_duplicate_keys_refuse_before_oracle(self):
        payloads = [b'[{"op":1,"op":2}]', b'[{"op":1,"\\u006fp":2}]',
                    b'[{"outer":{"a":1,"a":2}}]', b'[{"outer":[{"a":1,"\\u0061":2}]}]']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            for payload in payloads:
                with self.subTest(payload=payload), patch.object(reducer, 'reduce_history') as evaluate:
                    path.write_bytes(payload)
                    code, text = self.invoke(['--history', path])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(text)['error'], 'INVALID_HISTORY_INPUT')
                    self.assertLess(len(text), 200)
                    self.assertEqual(path.read_bytes(), payload)
                    evaluate.assert_not_called()

    def test_depth_node_boundaries_and_cycles(self):
        for depth in [31, 32, 33]:
            history = 0
            for _ in range(depth): history = [history]
            before = json.dumps(history)
            calls = []
            with self.subTest(depth=depth):
                if depth <= reducer.MAX_JSON_DEPTH:
                    self.assertTrue(reducer.reduce_history(history, lambda rows: calls.append(rows))['complete'])
                else:
                    with self.assertRaisesRegex(ValueError, 'budget'):
                        reducer.reduce_history(history, lambda rows: calls.append(rows))
                    self.assertEqual(calls, [])
                self.assertEqual(json.dumps(history), before)
        for nodes in [9999, 10000, 10001]:
            history = [[0] * (nodes - 2)]
            before = json.dumps(history)
            calls = []
            with self.subTest(nodes=nodes):
                if nodes <= reducer.MAX_JSON_NODES:
                    self.assertTrue(reducer.reduce_history(history, lambda rows: calls.append(True))['complete'])
                else:
                    with self.assertRaisesRegex(ValueError, 'budget'):
                        reducer.reduce_history(history, lambda rows: calls.append(True))
                    self.assertEqual(calls, [])
                self.assertEqual(json.dumps(history), before)
        history = []
        history.append(history)
        with patch.object(reducer.copy, 'deepcopy') as clone, self.assertRaisesRegex(ValueError, 'cycle'):
            reducer.reduce_history(history, lambda rows: self.fail('oracle called'))
        clone.assert_not_called()
        self.assertIs(history[0], history)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deep.json'
            payload = b'[' * 2000 + b'0' + b']' * 2000
            path.write_bytes(payload)
            code, text = self.invoke(['--history', path])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(text)['error'], 'INVALID_HISTORY_INPUT')
            self.assertEqual(path.read_bytes(), payload)

    def test_arguments_refuse_without_disclosure_or_fixture_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            history, output = Path(directory) / 'input.json', Path(directory) / 'output.json'
            history.write_bytes(b'[]'); output.write_bytes(b'prior output')
            for args in [['--history'], ['--out'], ['--max-calls'], ['--PRIVATE_ARGUMENT'],
                         *(['--max-calls', value] for value in ['true', '1.0', '0', '-1', '4097', '9'*5000])]:
                with self.subTest(args=str(args)[:40]), patch.object(reducer, 'reduce_history') as evaluate:
                    code, text = self.invoke(['--history', history, '--out', output, *args])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(text)['error'], 'INVALID_ARGUMENTS')
                    self.assertLess(len(text), 200)
                    self.assertNotIn('PRIVATE_ARGUMENT', text)
                    self.assertEqual(history.read_bytes(), b'[]')
                    self.assertEqual(output.read_bytes(), b'prior output')
                    evaluate.assert_not_called()
            for budget in [1, 4096]:
                code, text = self.invoke(['--history', history, '--max-calls', budget])
                self.assertEqual(code, 0)
                self.assertLessEqual(json.loads(text)['oracle_calls'], budget)
