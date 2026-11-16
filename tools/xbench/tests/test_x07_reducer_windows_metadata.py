from contextlib import redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('metadata_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)
FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')


class ReducerWindowsMetadata(unittest.TestCase):
    def test_injected_stable_and_divergent_metadata_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            payload = b'[{"op":"synthetic_failure"}]'
            path.write_bytes(payload)
            baseline = dict(st_mode=stat.S_IFREG, st_dev=11, st_ino=22, st_size=len(payload), st_mtime_ns=100, st_ctime_ns=200)
            stable = SimpleNamespace(**baseline)
            with patch.object(Path, 'lstat', side_effect=[stable, stable]), patch.object(reducer.os, 'fstat', side_effect=[stable, stable]):
                self.assertEqual(reducer.read_history(path), json.loads(payload))
            for source in ('descriptor', 'path'):
                for field in FIELDS:
                    changed = SimpleNamespace(**{**baseline, field: baseline[field] + 1})
                    output = io.StringIO()
                    with self.subTest(source=source, field=field), patch.object(Path, 'lstat', side_effect=[stable, changed if source == 'path' else stable]), patch.object(reducer.os, 'fstat', side_effect=[stable, changed if source == 'descriptor' else stable]), patch.object(reducer, 'reduce_history') as oracle, patch.object(sys, 'argv', ['reducer', '--history', str(path)]), redirect_stdout(output):
                        self.assertEqual(reducer.main(), 2)
                    oracle.assert_not_called()
                    self.assertEqual(json.loads(output.getvalue())['error'], 'INVALID_HISTORY_INPUT')
                    self.assertEqual(path.read_bytes(), payload)

    def test_native_unchanged_reads_record_compared_fields(self):
        real_fstat, real_lstat = os.fstat, Path.lstat
        with tempfile.TemporaryDirectory() as directory:
            for index in range(5):
                path = Path(directory) / f'history{index}.json'
                payload = json.dumps([{'op': 'noise', 'padding': 'a'*100000}]).encode()
                path.write_bytes(payload)
                events = []
                def observed_fstat(descriptor):
                    value = real_fstat(descriptor)
                    events.append(('descriptor', tuple(getattr(value, key) for key in FIELDS)))
                    return value
                def observed_lstat(current, *args, **kwargs):
                    value = real_lstat(current, *args, **kwargs)
                    if current == path: events.append(('path', tuple(getattr(value, key) for key in FIELDS)))
                    return value
                with self.subTest(index=index), patch.object(reducer.os, 'fstat', side_effect=observed_fstat), patch.object(Path, 'lstat', observed_lstat):
                    try:
                        result = reducer.read_history(path)
                    except ValueError as error:
                        # A native metadata shift is a conservative refusal,
                        # not evidence of byte mutation or the old root cause.
                        self.assertEqual(str(error), 'history changed during read')
                        self.assertTrue(any(value != events[1][1] for _, value in events[2:]), events)
                    else:
                        self.assertEqual(result, json.loads(payload))
                        self.assertEqual(len(events), 4)
                        self.assertTrue(all(value == events[0][1] for _, value in events))
                self.assertEqual(path.read_bytes(), payload)
