from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('closed_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffClosedStdout(unittest.TestCase):
    def test_closed_reader_on_success_and_refusals_preserves_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            first = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project('old', 'a')
            store.save_version('a', {'module': {'block': 'B', 'layer': 'M'}}, expected_version=1)
            original = path.read_bytes()
            base = ['--database', str(path), '--customer', 'a', '--profile-id', first['profile_id'], '--before', '1']
            for tail in [['--after', '2'], ['--after', '3'], ['--after', 'PRIVATE_INVALID']]:
                with self.subTest(tail=tail):
                    read, write = os.pipe()
                    os.close(read)
                    try:
                        process = subprocess.Popen([sys.executable, diff.__file__, *base, *tail], stdout=write, stderr=subprocess.PIPE)
                    finally:
                        os.close(write)
                    try:
                        _, error = process.communicate(timeout=15)
                    finally:
                        if process.poll() is None:
                            process.kill(); process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 2)
                    self.assertEqual(error, b'')
                    self.assertEqual(store.application_record('old', 'a'), pin)
                    self.assertEqual(path.read_bytes(), original)
