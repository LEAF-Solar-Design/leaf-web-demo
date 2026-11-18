from pathlib import Path
import os
import shutil
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerGrowth(unittest.TestCase):
    def test_mutation_after_open_refuses_and_stable_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory))
            original = path.read_bytes()
            for mutation in ('grow', 'truncate'):
                with self.subTest(mutation=mutation):
                    path.write_bytes(original)
                    ready, settled = Event(), Event()
                    errors, requests = [], []

                    def mutate():
                        try:
                            if not ready.wait(5):
                                raise TimeoutError('read barrier')
                            path.write_bytes(original + b' ' * 1024 if mutation == 'grow' else original[:20])
                        except BaseException as error:
                            errors.append(error)
                        finally:
                            settled.set()

                    fdopen = os.fdopen

                    class Stream:
                        def __init__(self, inner):
                            self.inner = inner
                        def __enter__(self):
                            return self
                        def __exit__(self, *args):
                            return self.inner.__exit__(*args)
                        def fileno(self):
                            return self.inner.fileno()
                        def read(self, size):
                            requests.append(size)
                            ready.set()
                            if not settled.wait(5):
                                raise TimeoutError('mutation barrier')
                            return self.inner.read(size)

                    worker = Thread(target=mutate)
                    worker.start()
                    try:
                        with patch.object(capsule.os, 'fdopen', side_effect=lambda *args, **kwargs: Stream(fdopen(*args, **kwargs))):
                            with self.assertRaisesRegex(ValueError, 'changed during read'):
                                capsule.read_capture(path)
                    finally:
                        ready.set()
                        worker.join(6)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(requests, [capsule.MAX_CAPTURE_BYTES + 1])
                    changed = original + b' ' * 1024 if mutation == 'grow' else original[:20]
                    self.assertEqual(path.read_bytes(), changed)
                    path.write_bytes(original)
                    self.assertEqual(capsule.read_capture(path), value)
