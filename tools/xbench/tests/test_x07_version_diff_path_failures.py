from contextlib import redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('path_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffPathFailures(unittest.TestCase):
    def assert_refusal(self, text):
        self.assertEqual(json.loads(text), {'schema': 'leaf.x07.profile-version-diff.v1', 'error': 'SCOPED_VERSIONS_UNAVAILABLE'})
        self.assertLess(len(text), 200)
        self.assertNotIn('PRIVATE_PATH', text)

    def test_absent_missing_parent_and_directory_paths_create_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'PRIVATE_PATH_directory'
            folder.mkdir()
            sentinel = folder / 'foreign.bin'; sentinel.write_bytes(b'foreign owner')
            for path in [root / 'PRIVATE_PATH_absent.sqlite', root / 'PRIVATE_PATH_missing' / 'store.sqlite', folder]:
                child = subprocess.run([sys.executable, diff.__file__, '--database', str(path), '--customer', 'a', '--profile-id', 'unknown', '--before', '1', '--after', '1'], capture_output=True, text=True, timeout=10)
                self.assertEqual(child.returncode, 2)
                self.assertEqual(child.stderr, '')
                self.assert_refusal(child.stdout)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['PRIVATE_PATH_directory'])
            self.assertEqual(sentinel.read_bytes(), b'foreign owner')

    def test_injected_open_permission_error_is_bounded_without_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'PRIVATE_PATH.sqlite'
            output = io.StringIO()
            with patch.object(sys, 'argv', ['diff', '--database', str(path), '--customer', 'a', '--profile-id', 'unknown', '--before', '1', '--after', '1']), patch.object(diff, 'CustomerProfileStore', side_effect=PermissionError('PRIVATE_PATH permission')), patch.object(diff, 'compare_versions') as compare, redirect_stdout(output):
                self.assertEqual(diff.main(), 2)
            compare.assert_not_called()
            self.assert_refusal(output.getvalue())
            self.assertFalse(path.exists())
