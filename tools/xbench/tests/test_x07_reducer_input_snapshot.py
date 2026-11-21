from contextlib import redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('snapshot_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class ReducerInputSnapshot(unittest.TestCase):
    def test_unchanged_descriptor_is_one_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            history = [{'op': 'noise', 'padding': 'a' * 100000}, {'op': 'synthetic_failure'}]
            payload = json.dumps(history).encode()
            path.write_bytes(payload)
            self.assertEqual(reducer.read_history(path), history)
            self.assertEqual(path.read_bytes(), payload)

    def test_synchronized_changes_yield_verified_snapshot_or_refuse_before_oracle(self):
        real_read = os.read
        for mode in ['truncate', 'grow', 'replace']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, foreign = root / 'history.json', root / 'foreign.bin'
                original = json.dumps([{'op': 'noise', 'padding': 'a' * 100000}, {'op': 'synthetic_failure'}]).encode()
                path.write_bytes(original); foreign.write_bytes(b'foreign owner')
                started, finished = threading.Event(), threading.Event()
                errors, expected, blocked_replace = [], [], []
                def writer():
                    try:
                        if not started.wait(5): raise AssertionError('reader barrier missing')
                        payload = b'[]' if mode == 'truncate' else original + b' ' if mode == 'grow' else original.replace(b'synthetic_failure', b'synthetic_changed')
                        if mode == 'replace':
                            replacement = root / 'replacement.json'
                            replacement.write_bytes(payload)
                            os.replace(replacement, path)
                        else:
                            path.write_bytes(payload)
                        expected.append(payload)
                    except BaseException as error:
                        if mode == 'replace' and os.name == 'nt' and isinstance(error, PermissionError):
                            # Windows can deny replacement while the reader holds
                            # its descriptor. That leaves a verified old snapshot.
                            blocked_replace.append(True)
                            expected.append(original)
                            (root / 'replacement.json').unlink(missing_ok=True)
                        else:
                            errors.append(error)
                    finally:
                        finished.set()
                intercepted = []
                def synchronized_read(descriptor, amount):
                    chunk = real_read(descriptor, amount)
                    if not intercepted:
                        intercepted.append(True); started.set()
                        if not finished.wait(5): raise AssertionError('writer barrier missing')
                    return chunk
                thread = threading.Thread(target=writer)
                thread.start()
                output = io.StringIO()
                try:
                    with patch.object(reducer.os, 'read', side_effect=synchronized_read), patch.object(reducer, 'reduce_history', wraps=reducer.reduce_history) as oracle, patch.object(sys, 'argv', ['reducer', '--history', str(path)]), redirect_stdout(output):
                        self.assertEqual(reducer.main(), 0 if blocked_replace else 2)
                    if blocked_replace:
                        oracle.assert_called_once()
                        self.assertEqual(oracle.call_args.args[0], json.loads(original))
                    else:
                        oracle.assert_not_called()
                finally:
                    started.set(); finished.set(); thread.join(6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                if blocked_replace:
                    self.assertTrue(json.loads(output.getvalue())['complete'])
                else:
                    self.assertEqual(json.loads(output.getvalue())['error'], 'INVALID_HISTORY_INPUT')
                    self.assertLess(len(output.getvalue()), 200)
                self.assertEqual(path.read_bytes(), expected[0])
                self.assertEqual(foreign.read_bytes(), b'foreign owner')
                self.assertEqual(sorted(p.name for p in root.iterdir()), ['foreign.bin', 'history.json'])
