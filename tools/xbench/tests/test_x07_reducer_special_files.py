from contextlib import redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('special_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class ReducerSpecialFiles(unittest.TestCase):
    def test_metadata_refuses_before_open_or_read_and_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            path.write_bytes(b'[]')
            for mode in [stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFDIR, stat.S_IFLNK]:
                with self.subTest(mode=mode), patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=mode)), patch.object(reducer.os, 'open') as opened:
                    with self.assertRaisesRegex(ValueError, 'regular file'):
                        reducer.read_history(path)
                    opened.assert_not_called()
            with patch.object(reducer.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0)), patch.object(reducer.os, 'read') as read:
                output = io.StringIO()
                with patch.object(sys, 'argv', ['reducer', '--history', str(path)]), redirect_stdout(output), patch.object(reducer, 'reduce_history') as oracle:
                    self.assertEqual(reducer.main(), 2)
                oracle.assert_not_called(); read.assert_not_called()
                self.assertEqual(json.loads(output.getvalue())['error'], 'INVALID_HISTORY_INPUT')
            self.assertEqual(path.read_bytes(), b'[]')

    def test_disposable_directory_and_fifo_cli_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root]
            if hasattr(os, 'mkfifo'):
                pipe = root / 'history.pipe'
                os.mkfifo(pipe)
                paths.append(pipe)
            for path in paths:
                with self.subTest(path=path.name):
                    process = subprocess.run([sys.executable, reducer.__file__, '--history', str(path)], capture_output=True, text=True, timeout=5)
                    self.assertEqual(process.returncode, 2)
                    self.assertEqual(process.stderr, '')
                    self.assertEqual(json.loads(process.stdout)['error'], 'INVALID_HISTORY_INPUT')
                    self.assertLess(len(process.stdout), 200)
