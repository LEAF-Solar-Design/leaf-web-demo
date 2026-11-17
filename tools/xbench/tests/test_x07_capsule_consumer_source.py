import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine, encoded


def source_capture(root):
    source = root / 'original'
    source.mkdir()
    dependency = source / 'material.txt'
    dependency.write_bytes(b'SYNTHETIC MATERIAL')

    def measured(check, *args):
        return {'source_sha': 'a' * 40, 'dirty': False,
                'source_file_sha256': {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in (check, dependency)}}

    # Inject synthetic observations into the real producer before publication.
    with patch.object(capsule, 'snapshot', side_effect=measured):
        value, path = genuine(source)
    return value, path, source


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerSource(unittest.TestCase):
    def test_explicit_relocation_missing_and_changed_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value, path, source = source_capture(root)
            relocated = root / 'relocated'
            shutil.copytree(source, relocated)
            moved = relocated / path.name
            before = moved.read_bytes()
            self.assertEqual(capsule.read_capture(moved), value)
            verified = capsule.read_capture(moved, source_root=relocated, recorded_source_root=source)
            self.assertEqual(verified['local_source_verification'], {'status': 'VERIFIED', 'dependencies': 2})
            dependency = relocated / 'material.txt'
            for change in ('changed', 'missing'):
                if change == 'changed':
                    dependency.write_bytes(b'CHANGED')
                else:
                    dependency.unlink()
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'SOURCE_UNVERIFIABLE'):
                    capsule.read_capture(moved, source_root=relocated, recorded_source_root=source)
                self.assertEqual(moved.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'SOURCE_UNVERIFIABLE'):
                capsule.read_capture(moved, source_root=relocated)
            with self.assertRaisesRegex(ValueError, 'SOURCE_UNVERIFIABLE'):
                capsule.read_capture(moved, recorded_source_root=source)

    def test_relative_and_escaping_source_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value, path, source = source_capture(root)
            value['source_file_sha256'] = {'material.txt': hashlib.sha256(b'SYNTHETIC MATERIAL').hexdigest()}
            path.write_bytes(encoded(value))
            self.assertEqual(capsule.read_capture(path, source_root=source)['local_source_verification']['dependencies'], 1)
            outside = root / 'sentinel'
            outside.write_bytes(b'SENTINEL')
            for name in ('../sentinel', '..\\sentinel', str(outside.resolve()), 'C:\\foreign\\sentinel', '/foreign/sentinel'):
                value['source_file_sha256'] = {name: hashlib.sha256(b'SENTINEL').hexdigest()}
                path.write_bytes(encoded(value))
                before = path.read_bytes()
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'SOURCE_UNVERIFIABLE'):
                    capsule.read_capture(path, source_root=source, recorded_source_root=source)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(outside.read_bytes(), b'SENTINEL')
