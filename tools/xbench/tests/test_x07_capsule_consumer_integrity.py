import copy
from pathlib import Path
import shutil
import tempfile
import unittest
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine, encoded, refuses


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerIntegrity(unittest.TestCase):
    def test_independent_evidence_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory))
            changes = [('stdout_bytes', 1), ('stderr_received_bytes', 99), ('stdout_sha256', '0'*64),
                       ('stdout', 'XX'), ('stdout_encoding', 'binary-base64'), ('stdout_base64', 'T0s='),
                       ('diagnostics_complete', False), ('stdout_complete', False), ('owned_processes_settled', False)]
            for key, replacement in changes:
                sample = copy.deepcopy(value); sample[key] = replacement
                with self.subTest(key=key): refuses(self, path, encoded(sample))
            for key, replacement in [('readers_alive', 1), ('readers_started', True), ('parent_reaped', False), ('job_closed', False)]:
                sample = copy.deepcopy(value); sample['resources'][key] = replacement
                with self.subTest(resource=key): refuses(self, path, encoded(sample))

    def test_genuine_overflow_and_binary_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value, path = genuine(root, "process.stdout.write('x'.repeat(65))")
            self.assertFalse(value['diagnostics_complete'])
            self.assertEqual(capsule.read_capture(path), value)
            sample = copy.deepcopy(value); sample['diagnostics_complete'] = True
            refuses(self, path, encoded(sample)); path.unlink()
            value, path = genuine(root, 'process.stdout.write(Buffer.from([255,0,254]))')
            self.assertEqual(capsule.read_capture(path), value)
            for key, replacement in [('stdout_base64', '%%%'), ('stdout_base64', '/wD/'), ('stdout', 'wrong')]:
                sample = copy.deepcopy(value); sample[key] = replacement
                with self.subTest(key=key, replacement=replacement): refuses(self, path, encoded(sample))
