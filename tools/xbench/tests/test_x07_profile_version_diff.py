from pathlib import Path
import importlib.util
import json
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('version_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class ProfileVersionDiff(unittest.TestCase):
    def test_deterministic_role_changes_preserve_bytes_and_old_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'
            store = diff.CustomerProfileStore(path)
            first = store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M'}, 'roof': {'layer': 'R'}}, expected_version=0)
            pin = store.apply_to_project('old', 'a')
            store.save_version('a', {'module': {'block': 'Panel 2', 'layer': 'M'}, 'obstruction': {'layer': 'O'}}, expected_version=1)
            before = path.read_bytes()
            read = diff.CustomerProfileStore(path, read_only=True)
            options = dict(customer_key='a', profile_id=first['profile_id'], before_version=1, after_version=2)
            result = diff.compare_versions(read, **options)
            self.assertEqual(result['added'], {'obstruction': {'layer': 'O'}})
            self.assertEqual(result['removed'], {'roof': {'layer': 'R'}})
            self.assertEqual(result['changed'], {'module': {'before': {'block': 'Panel', 'layer': 'M'}, 'after': {'block': 'Panel 2', 'layer': 'M'}}})
            self.assertEqual(json.dumps(result), json.dumps(diff.compare_versions(read, **options)))
            self.assertEqual(read.application_record('old', 'a'), pin)
            self.assertEqual(path.read_bytes(), before)

    def test_foreign_profile_or_unavailable_version_refuses_without_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.sqlite'
            store = diff.CustomerProfileStore(path)
            a = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
            b = store.save_version('b', {'module': {'block': 'PRIVATE_B', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project('foreign-old', 'b')
            before = path.read_bytes()
            for customer, profile, version in [('a', b['profile_id'], 1), ('b', a['profile_id'], 1), ('a', a['profile_id'], 2)]:
                with self.subTest(customer=customer, version=version), self.assertRaisesRegex(ValueError, '^scoped versions unavailable$'):
                    diff.compare_versions(store, customer_key=customer, profile_id=profile, before_version=1, after_version=version)
            self.assertEqual(store.application_record('foreign-old', 'b'), pin)
            self.assertEqual(path.read_bytes(), before)
