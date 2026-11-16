from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'x07_history_reducer.py'


class ReducerClosedStdout(unittest.TestCase):
    def invoke(self, args):
        read, write = os.pipe()
        os.close(read)
        try:
            child = subprocess.Popen([sys.executable, str(SCRIPT), *map(str, args)], stdout=write, stderr=subprocess.PIPE)
        finally:
            os.close(write)
        try:
            _, error = child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill(); child.communicate(timeout=5)
        self.assertEqual(child.returncode, 2)
        self.assertEqual(error, b'')

    def test_success_has_complete_or_absent_output_with_closed_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / 'history.json'
            original = b'[{"op":"noise"},{"op":"synthetic_failure"}]'
            history.write_bytes(original)
            for publish in [False, True]:
                with self.subTest(publish=publish):
                    output = root / 'output.json'
                    self.invoke(['--history', history, *(['--out', output] if publish else [])])
                    if publish:
                        replay = json.loads(output.read_bytes())
                        self.assertTrue(replay['complete'])
                        self.assertEqual(replay['commands'], [{'op': 'synthetic_failure'}])
                        self.assertEqual(replay['signature'], 'SYNTHETIC_FAILURE')
                    else:
                        self.assertFalse(output.exists())
                    self.assertEqual(history.read_bytes(), original)
                    self.assertFalse(list(root.glob('.x07-reducer-*.tmp')))

    def test_refusals_leave_atomic_output_absent_and_history_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history, output = root / 'history.json', root / 'output.json'
            for payload, extra in [(b'{PRIVATE_INVALID', []), (b'[]', ['--max-calls', '0'])]:
                with self.subTest(payload=payload):
                    history.write_bytes(payload)
                    self.invoke(['--history', history, '--out', output, *extra])
                    self.assertEqual(history.read_bytes(), payload)
                    self.assertFalse(output.exists())
                    self.assertFalse(list(root.glob('.x07-reducer-*.tmp')))
