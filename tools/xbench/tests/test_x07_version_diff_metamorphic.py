from pathlib import Path
import importlib.util
import itertools
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('metamorphic_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


def oracle(before, after):
    result = {'added': {}, 'removed': {}, 'changed': {}}
    for role in sorted(set(before) | set(after)):
        if role not in before:
            result['added'][role] = after[role]
        elif role not in after:
            result['removed'][role] = before[role]
        elif before[role] != after[role]:
            result['changed'][role] = {'before': before[role], 'after': after[role]}
    return result


class VersionDiffMetamorphic(unittest.TestCase):
    def test_identity_and_reverse_against_map_oracle(self):
        maps = [dict(module={'block': 'A', 'layer': 'M'}, roof={'layer': 'R'}),
                dict(module={'block': 'B', 'layer': 'M'}, obstruction={'layer': 'O'}),
                dict(module={'block': 'B', 'layer': 'M'}, roof={'layer': 'R'}, obstruction={'layer': 'O2'})]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            first = store.save_version('a', maps[0], expected_version=0)
            pin = store.apply_to_project('old', 'a')
            for version, roles in enumerate(maps[1:], 1):
                store.save_version('a', roles, expected_version=version)
            store.save_version('b', {'module': {'block': 'FOREIGN', 'layer': 'M'}}, expected_version=0)
            foreign = store.apply_to_project('old-b', 'b')
            original = path.read_bytes()
            for a, b in itertools.product(range(3), repeat=2):
                with self.subTest(a=a, b=b):
                    result = diff.compare_versions(store, customer_key='a', profile_id=first['profile_id'], before_version=a+1, after_version=b+1)
                    self.assertEqual({key: result[key] for key in ('added', 'removed', 'changed')}, oracle(maps[a], maps[b]))
                    reverse = diff.compare_versions(store, customer_key='a', profile_id=first['profile_id'], before_version=b+1, after_version=a+1)
                    self.assertEqual(result['added'], reverse['removed'])
                    self.assertEqual(result['removed'], reverse['added'])
                    self.assertEqual(result['changed'], {key: {'before': value['after'], 'after': value['before']} for key, value in reverse['changed'].items()})
            self.assertEqual(store.application_record('old', 'a'), pin)
            self.assertEqual(store.application_record('old-b', 'b'), foreign)
            self.assertEqual(path.read_bytes(), original)

    def test_role_key_permutations_are_identity(self):
        roles = dict(module={'block': 'A', 'layer': 'M'}, roof={'layer': 'R'}, obstruction={'layer': 'O'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            for version, keys in enumerate(itertools.permutations(roles)):
                saved = store.save_version('a', {key: roles[key] for key in keys}, expected_version=version)
                if not version:
                    pin = store.apply_to_project('old', 'a')
            original = path.read_bytes()
            for version in range(1, 7):
                result = diff.compare_versions(store, customer_key='a', profile_id=saved['profile_id'], before_version=1, after_version=version)
                self.assertEqual({key: result[key] for key in ('added', 'removed', 'changed')}, oracle(roles, roles))
            self.assertEqual(store.application_record('old', 'a'), pin)
            self.assertEqual(path.read_bytes(), original)
