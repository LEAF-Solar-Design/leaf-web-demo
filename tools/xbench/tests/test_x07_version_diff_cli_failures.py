from pathlib import Path
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py'
spec = importlib.util.spec_from_file_location('cli_diff', SCRIPT)
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffCliFailures(unittest.TestCase):
    def refusal(self, path, args):
        before = path.read_bytes() if path.exists() else None
        process = subprocess.run([sys.executable, str(SCRIPT), '--database', str(path), *args], capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stderr, '')
        self.assertLess(len(process.stdout), 256)
        self.assertTrue(process.stdout.endswith('\n'))
        result = json.loads(process.stdout)
        self.assertIn(result['error'], ('INVALID_ARGUMENTS', 'SCOPED_VERSIONS_UNAVAILABLE'))
        self.assertNotIn('PRIVATE_FOREIGN_SENTINEL', process.stdout)
        self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_missing_boolean_fractional_and_unsafe_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            first = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project('old', 'a')
            scope = ['--customer', 'a', '--profile-id', first['profile_id']]
            for args in (['--before', '1'], ['--after', '1'], *([flag, value, other, '1'] for flag, other in [('--before', '--after'), ('--after', '--before')] for value in ['true', 'false', '1.0', '1/2', '9007199254740992'])):
                with self.subTest(args=args):
                    self.refusal(path, scope + args)
            self.assertEqual(store.application_record('old', 'a'), pin)

    def test_corrupt_missing_and_foreign_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = root / 'corrupt.sqlite'
            corrupt.write_bytes(b'PRIVATE_FOREIGN_SENTINEL corrupt database')
            args = ['--customer', 'a', '--profile-id', 'unknown', '--before', '1', '--after', '1']
            self.refusal(corrupt, args)
            self.refusal(root / 'missing.sqlite', args)
            path = root / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            saved = store.save_version('b', {'module': {'block': 'PRIVATE_FOREIGN_SENTINEL', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project('foreign-old', 'b')
            args[3] = saved['profile_id']
            self.refusal(path, args)
            self.assertEqual(store.application_record('foreign-old', 'b'), pin)
