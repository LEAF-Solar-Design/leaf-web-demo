from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerPaths(unittest.TestCase):
    def test_nonregular_metadata_refuses_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value, path = genuine(root); raw = path.read_bytes()
            with self.assertRaises(ValueError): capsule.read_capture(root)
            for mode in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFLNK):
                with self.subTest(mode=mode), patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=mode)), patch.object(Path, 'open', side_effect=AssertionError('blocking read')):
                    with self.assertRaises(ValueError): capsule.read_capture(path)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(capsule.read_capture(path), value)
