from pathlib import Path
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerIoFailures(unittest.TestCase):
    def test_open_read_partial_and_close_failures_settle_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory))
            original = path.read_bytes()
            with patch.object(capsule.os, 'open', side_effect=OSError('injected open')):
                with self.assertRaisesRegex(ValueError, 'capture unavailable'):
                    capsule.read_capture(path)
            fdopen = os.fdopen
            for failure in ('read', 'partial', 'close', 'fdopen'):
                handles, descriptors = [], []

                class Stream:
                    def __init__(self, inner):
                        self.inner = inner
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        self.inner.close()
                        if failure == 'close':
                            raise OSError('injected close')
                    def fileno(self):
                        return self.inner.fileno()
                    def read(self, size):
                        if failure == 'read':
                            self.inner.read(20)
                            raise OSError('injected partial-read error')
                        return self.inner.read(20 if failure == 'partial' else size)

                def wrapped(descriptor, *args, **kwargs):
                    descriptors.append(descriptor)
                    if failure == 'fdopen':
                        raise OSError('injected fdopen')
                    inner = fdopen(descriptor, *args, **kwargs)
                    handles.append(inner)
                    return Stream(inner)

                with self.subTest(failure=failure), patch.object(capsule.os, 'fdopen', side_effect=wrapped):
                    with self.assertRaises(ValueError):
                        capsule.read_capture(path)
                self.assertTrue(all(handle.closed for handle in handles))
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(capsule.read_capture(path), value)
