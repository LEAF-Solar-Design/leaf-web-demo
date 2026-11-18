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

spec = importlib.util.spec_from_file_location('alias_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class ReducerOutputAliases(unittest.TestCase):
    def test_same_dot_parent_segments_and_hardlinks_refuse_before_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history, link = root / 'history.json', root / 'hardlink.json'
            payload = b'[{"op":"synthetic_failure"}]'
            history.write_bytes(payload)
            os.link(history, link)
            (root / 'sub').mkdir()
            for output in [str(history), str(root) + '/./history.json', str(root / 'sub' / '..' / 'history.json'), str(link)]:
                text = io.StringIO()
                with self.subTest(output=output), patch.object(sys, 'argv', ['reducer', '--history', str(history), '--out', output]), patch.object(reducer, 'atomic_publish') as publish, redirect_stdout(text):
                    self.assertEqual(reducer.main(), 2)
                publish.assert_not_called()
                self.assertEqual(json.loads(text.getvalue())['error'], 'OUTPUT_PUBLICATION_FAILED')
                self.assertEqual(history.read_bytes(), payload)
                self.assertEqual(link.read_bytes(), payload)
                self.assertFalse(list(root.glob('.x07-reducer-*.tmp')))
