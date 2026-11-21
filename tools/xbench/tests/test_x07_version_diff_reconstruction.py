from pathlib import Path
import copy
import importlib.util
import random
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('reconstruct_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffReconstruction(unittest.TestCase):
    def test_seeded_three_version_histories_reconstruct_exact_after_maps(self):
        rng = random.Random(19)
        for seed in range(12):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'store.sqlite'
                store = diff.CustomerProfileStore(path)
                maps = []
                for version in range(3):
                    roles = {'module': {'block': rng.choice(['A', 'é', 'e\u0301', 'X\0Y']), 'layer': rng.choice(['M', 'm'])}}
                    for role in ['roof', 'obstruction']:
                        if rng.randrange(2): roles[role] = {'layer': rng.choice(['R', 'r', 'O\0one'])}
                    maps.append(roles)
                    saved = store.save_version('a', roles, expected_version=version)
                    if not version: pin = store.apply_to_project('old', 'a')
                read = diff.CustomerProfileStore(path, read_only=True)
                original = path.read_bytes()
                for a in range(3):
                    for b in range(3):
                        result = diff.compare_versions(read, customer_key='a', profile_id=saved['profile_id'], before_version=a+1, after_version=b+1)
                        rebuilt = copy.deepcopy(maps[a])
                        groups = [set(result[key]) for key in ['added', 'removed', 'changed']]
                        self.assertTrue(all(not groups[x] & groups[y] for x,y in [(0,1),(0,2),(1,2)]))
                        for role, old in result['removed'].items(): self.assertEqual(rebuilt.pop(role), old)
                        for role, change in result['changed'].items():
                            self.assertEqual(rebuilt[role], change['before'])
                            rebuilt[role] = copy.deepcopy(change['after'])
                        for role, added in result['added'].items():
                            self.assertNotIn(role, rebuilt)
                            rebuilt[role] = copy.deepcopy(added)
                        self.assertEqual(rebuilt, maps[b])
                self.assertEqual(read.application_record('old', 'a'), pin)
                self.assertEqual(path.read_bytes(), original)
