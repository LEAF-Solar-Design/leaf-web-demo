import copy
from pathlib import Path
import shutil
import tempfile
import unittest
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine, encoded, refuses


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerSchema(unittest.TestCase):
    def test_missing_future_wrong_types_and_boolean_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory))
            for key in value:
                sample = copy.deepcopy(value); del sample[key]
                with self.subTest(missing=key): refuses(self, path, encoded(sample))
            for key, replacement in [('schema', 'leaf.x07.jsx-failure-capsule.v2'), ('schema', {}),
                                     ('source_sha', 1), ('source_file_sha256', ['fake']), ('command', [True]),
                                     ('exit_code', True), ('worker_pid', True), ('stream_limit', True),
                                     ('stdout_bytes', True), ('stderr_received_bytes', False), ('dirty', 1)]:
                sample = copy.deepcopy(value); sample[key] = replacement
                with self.subTest(key=key): refuses(self, path, encoded(sample))
            path.write_bytes(encoded(value)); self.assertEqual(capsule.read_capture(path), value)
