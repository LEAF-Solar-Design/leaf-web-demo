from pathlib import Path
import importlib.util
import itertools
import tempfile
import unicodedata
import unittest

spec = importlib.util.spec_from_file_location('unicode_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffUnicodeLabels(unittest.TestCase):
    def test_exact_unicode_case_and_nul_labels_against_independent_map(self):
        composed, decomposed = 'Café', 'Cafe\u0301'
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(unicodedata.normalize('NFC', composed), unicodedata.normalize('NFC', decomposed))
        maps = [{'module': {'block': composed, 'layer': 'Module'}},
                {'module': {'block': decomposed, 'layer': 'module'}, 'roof': {'layer': 'roof\0one'}, 'obstruction': {'layer': 'É'}},
                {'module': {'block': composed, 'layer': 'MODULE'}, 'roof': {'layer': 'roof\0two'}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            for version, roles in enumerate(maps):
                saved = store.save_version('a', roles, expected_version=version)
                if not version: pin = store.apply_to_project('old', 'a')
            before = path.read_bytes()
            for a, b in itertools.product(range(3), repeat=2):
                expected = {'added': {}, 'removed': {}, 'changed': {}}
                for role in sorted(set(maps[a]) | set(maps[b])):
                    if role not in maps[a]: expected['added'][role] = maps[b][role]
                    elif role not in maps[b]: expected['removed'][role] = maps[a][role]
                    elif maps[a][role] != maps[b][role]: expected['changed'][role] = {'before': maps[a][role], 'after': maps[b][role]}
                with self.subTest(a=a, b=b):
                    result = diff.compare_versions(store, customer_key='a', profile_id=saved['profile_id'], before_version=a+1, after_version=b+1)
                    self.assertEqual({key: result[key] for key in expected}, expected)
            self.assertEqual(store.application_record('old', 'a'), pin)
            self.assertEqual(path.read_bytes(), before)
