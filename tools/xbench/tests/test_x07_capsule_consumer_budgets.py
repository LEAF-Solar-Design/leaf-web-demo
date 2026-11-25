import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine, encoded, refuses


def nodes(value):
    if isinstance(value, dict):
        return 1 + sum(1 + nodes(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(map(nodes, value))
    return 1


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerBudgets(unittest.TestCase):
    def test_byte_boundaries_and_preopen_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory))
            value['padding'] = ''
            base = encoded(value)
            for delta in (-1, 0, 1):
                raw = base.replace(b'"padding": ""', b'"padding": "' + b'x' * (capsule.MAX_CAPTURE_BYTES + delta - len(base)) + b'"')
                self.assertEqual(len(raw), capsule.MAX_CAPTURE_BYTES + delta)
                path.write_bytes(raw)
                if delta <= 0:
                    self.assertEqual(capsule.read_capture(path)['stdout'], 'OK')
                else:
                    with patch.object(Path, 'open', side_effect=AssertionError('must not open')):
                        with self.assertRaises(ValueError): capsule.read_capture(path)
                self.assertEqual(path.read_bytes(), raw)

    def test_depth_boundaries_before_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory))
            for depth in (capsule.MAX_CAPTURE_DEPTH - 1, capsule.MAX_CAPTURE_DEPTH, capsule.MAX_CAPTURE_DEPTH + 1):
                sample = copy.deepcopy(value)
                padding = None
                for _ in range(depth - 1): padding = [padding]
                sample['padding'] = padding
                raw = encoded(sample)
                if depth <= capsule.MAX_CAPTURE_DEPTH:
                    path.write_bytes(raw); self.assertEqual(capsule.read_capture(path), sample)
                else:
                    with patch.object(capsule.json, 'loads', side_effect=AssertionError('decoder reached')):
                        refuses(self, path, raw)

    def test_node_boundaries_before_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory)); value['padding'] = []
            baseline = nodes(value)
            for delta in (-1, 0, 1):
                value['padding'] = [None] * (capsule.MAX_CAPTURE_NODES + delta - baseline)
                self.assertEqual(nodes(value), capsule.MAX_CAPTURE_NODES + delta)
                raw = encoded(value)
                if delta <= 0:
                    path.write_bytes(raw); self.assertEqual(capsule.read_capture(path), value)
                else:
                    with patch.object(capsule.json, 'loads', side_effect=AssertionError('decoder reached')):
                        refuses(self, path, raw)
