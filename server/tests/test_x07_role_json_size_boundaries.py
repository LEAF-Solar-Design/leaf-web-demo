import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, MAX_PROFILE_VERSION

LIMIT = 128 * 1024


class RoleJSONSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = CustomerProfileStore(Path(self.temp.name) / 'profiles.sqlite')
        self.base = self.store.save_version('a', {'module': {'block': 'Panel', 'layer': 'MODULES'}}, expected_version=0)
        self.pin = self.store.apply_to_project('old', 'a')

    def boundary(self, prefix):
        current = 1
        for target in [LIMIT-1, LIMIT, LIMIT+1]:
            roles = {'module': {'block': prefix, 'layer': 'MODULES'}}
            # Persisted format uses escaped JSON by default. Count serialized
            # UTF-8 bytes, not Python characters or unescaped label bytes.
            overhead = len(json.dumps(roles, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8'))
            roles['module']['block'] += 'x' * (target-overhead)
            encoded = json.dumps(roles, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
            self.assertEqual(len(encoded), target)
            before = self.store.latest('a')
            if target <= LIMIT:
                result = self.store.save_version('a', roles, expected_version=current); current += 1
                self.assertEqual(result['roles'], roles)
                self.assertEqual(self.store.latest('a'), result)
            else:
                with self.assertRaisesRegex(ValueError, '128 KiB'): self.store.save_version('a', roles, expected_version=current)
                self.assertEqual(self.store.latest('a'), before)
            self.assertEqual(self.store.application_record('old', 'a'), self.pin)
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 3)

    def test_ascii_immediately_below_at_above_limit(self): self.boundary('Panel')
    def test_escaped_label_immediately_below_at_above_limit(self): self.boundary('Panel "\\\n\t')
    def test_multibyte_label_immediately_below_at_above_limit(self): self.boundary('Pánel 雪😀')

    def seed(self, version):
        with self.store._connection() as db:
            db.execute('INSERT INTO profile_versions SELECT customer_key,profile_id,?,roles_json,created_at FROM profile_versions WHERE version=1', (version,))

    def test_safe_integer_ceiling_refuses_next_save_before_writing(self):
        self.seed(MAX_PROFILE_VERSION)
        before = self.store.latest('a')
        with self.assertRaisesRegex(ValueError, 'version limit'): self.store.save_version('a', before['roles'], expected_version=MAX_PROFILE_VERSION)
        self.assertEqual(self.store.latest('a'), before)
        self.assertEqual(self.store.application_record('old', 'a'), self.pin)

    def test_unsafe_expected_versions_refuse_without_changes(self):
        for version in [MAX_PROFILE_VERSION+1, True, 1.5, float(MAX_PROFILE_VERSION)]:
            with self.subTest(version=version), self.assertRaises(ValueError): self.store.save_version('a', self.base['roles'], expected_version=version)
        self.assertEqual(self.store.latest('a'), self.base)
        self.assertEqual(self.store.application_record('old', 'a'), self.pin)

    def test_unsafe_persisted_version_read_and_pin_refuse_without_new_records(self):
        self.seed(MAX_PROFILE_VERSION+1)
        for operation in [lambda: self.store.latest('a'), lambda: self.store.apply_to_project('new', 'a')]:
            with self.assertRaisesRegex(ValueError, 'safe integer'): operation()
        self.assertIsNone(self.store.application_record('new', 'a'))
        self.assertEqual(self.store.application_record('old', 'a'), self.pin)
