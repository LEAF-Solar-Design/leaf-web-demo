from pathlib import Path
import os
import shutil
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine, encoded


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerFileSwap(unittest.TestCase):
    def test_replacement_after_metadata_refuses_and_preserves_foreign_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value, path = genuine(root)
            original = path.read_bytes()
            value['exit_code'] = 9
            replacement = root / 'foreign.json'
            foreign = encoded(value)
            replacement.write_bytes(foreign)
            ready, settled = Event(), Event()
            errors = []

            def replace():
                try:
                    if not ready.wait(5):
                        raise TimeoutError('metadata barrier')
                    os.replace(replacement, path)
                except BaseException as error:
                    errors.append(error)
                finally:
                    settled.set()

            opened = os.open

            def synchronized_open(name, flags, *args, **kwargs):
                ready.set()
                if not settled.wait(5):
                    raise TimeoutError('replacement barrier')
                return opened(name, flags, *args, **kwargs)

            worker = Thread(target=replace)
            worker.start()
            try:
                with patch.object(capsule.os, 'open', side_effect=synchronized_open), patch.object(capsule, '_bounded_json', side_effect=AssertionError('mixed file parsed')):
                    with self.assertRaisesRegex(ValueError, 'changed before read'):
                        capsule.read_capture(path)
            finally:
                ready.set()
                worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(path.read_bytes(), foreign)
            self.assertNotEqual(original, foreign)
            self.assertEqual(capsule.read_capture(path), value)
