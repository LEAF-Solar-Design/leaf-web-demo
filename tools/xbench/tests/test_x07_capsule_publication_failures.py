import errno
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsulePublicationFailures(unittest.TestCase):
    def test_short_write_enospc_and_flush_failure_remove_owned_reservation(self):
        for fault in ('short', 'enospc', 'flush'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                check = root / 'x07_publish.cjs'; check.write_text("console.log('SETTLED')\n", encoding='utf-8')
                output = root / 'failed.json'
                original = Path.open

                class Broken:
                    def __init__(self, stream):
                        self.stream = stream
                    def __getattr__(self, name):
                        return getattr(self.stream, name)
                    def write(self, value):
                        self_test.assertIn('"owned_processes_settled": true', value)
                        if fault == 'enospc':
                            self.stream.write(value[:10])
                            raise OSError(errno.ENOSPC, 'synthetic full device')
                        if fault == 'short':
                            return self.stream.write(value[:10])
                        return self.stream.write(value)
                    def flush(self):
                        if fault == 'flush':
                            raise OSError('synthetic flush refusal')
                        return self.stream.flush()

                self_test = self
                def opened(path, *args, **kwargs):
                    stream = original(path, *args, **kwargs)
                    return Broken(stream) if path == output and args and args[0] == 'x' else stream

                with patch.object(Path, 'open', opened), self.assertRaises(OSError):
                    capsule.run_check(check.name, output, checks_root=root, timeout=5)
                self.assertFalse(output.exists(), 'no partial capture may remain')
                existing = root / 'old.json'; existing.write_bytes(b'OLD_CAPTURE')
                with self.assertRaises(FileExistsError):
                    capsule.run_check(check.name, existing, checks_root=root)
                self.assertEqual(existing.read_bytes(), b'OLD_CAPTURE')
                report = capsule.run_check(check.name, root / 'retry.json', checks_root=root, timeout=5)
                self.assertEqual(capsule.exit_code(report), 0)
                self.assertTrue(report['owned_processes_settled'])
