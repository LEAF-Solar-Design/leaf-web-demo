from pathlib import Path
import importlib.util
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('seed_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffHashseed(unittest.TestCase):
    def test_cli_bytes_match_across_hash_seeds_and_role_insertion_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'base.sqlite'
            store = diff.CustomerProfileStore(base)
            saved = store.save_version('a', {'module': {'block': 'Café', 'layer': 'M'}, 'roof': {'layer': 'R'}, 'obstruction': {'layer': 'O'}}, expected_version=0)
            pin = store.apply_to_project('old', 'a')
            base_bytes = base.read_bytes()
            after = {'module': {'block': 'Cafe\u0301', 'layer': 'm'}, 'roof': {'layer': 'R\0two'}, 'obstruction': {'layer': 'o'}}
            outputs = []
            for index, keys in enumerate(itertools.permutations(after)):
                path = root / f'copy{index}.sqlite'
                shutil.copyfile(base, path)
                variant = diff.CustomerProfileStore(path)
                variant.save_version('a', {key: after[key] for key in keys}, expected_version=1)
                original = path.read_bytes()
                for seed in ['0', '1', '42']:
                    child = subprocess.run([sys.executable, diff.__file__, '--database', str(path), '--customer', 'a', '--profile-id', saved['profile_id'], '--before', '1', '--after', '2'],
                                           env={'PYTHONHASHSEED': seed, 'PYTHONUTF8': '1'}, capture_output=True, timeout=10)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(child.stderr, b'')
                    outputs.append(child.stdout)
                    self.assertEqual(path.read_bytes(), original)
                self.assertEqual(variant.application_record('old', 'a'), pin)
            self.assertTrue(all(value == outputs[0] for value in outputs))
            result = json.loads(outputs[0])
            self.assertEqual(result['changed']['module']['after'], after['module'])
            self.assertEqual(result['changed']['roof']['after'], after['roof'])
            self.assertEqual(base.read_bytes(), base_bytes)
