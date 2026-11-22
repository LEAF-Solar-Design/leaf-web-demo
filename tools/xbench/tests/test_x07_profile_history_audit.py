from contextlib import closing
from pathlib import Path
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('history_audit', Path(__file__).resolve().parents[1] / 'x07_profile_history_audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def independent_record_oracle(profile_id, versions, pins):
    present = {version for version, _ in versions}
    missing = [version for version in range(1, max(present, default=0)+1) if version not in present]
    intervals = []
    for version in missing:
        if intervals and intervals[-1]['last'] == version-1:
            intervals[-1]['last'] = version
        else:
            intervals.append({'first': version, 'last': version})
    return {'missing_profile': int(profile_id is None), 'empty_chain': int(profile_id is not None and not versions), 'version_gaps': intervals, 'invalid_versions': 0,
            'foreign_version_profiles': len([1 for _, profile in versions if profile != profile_id]),
            'dangling_pin_versions': len([1 for version, _ in pins if version not in present]),
            'foreign_pin_profiles': len([1 for _, profile in pins if profile != profile_id])}


class ProfileHistoryAudit(unittest.TestCase):
    def fixture(self, path):
        store = audit.CustomerProfileStore(path)
        a = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
        pin = store.apply_to_project('old-a', 'a')
        b = store.save_version('b', {'module': {'block': 'PRIVATE_FOREIGN_LABEL', 'layer': 'PRIVATE_B_LAYER'}}, expected_version=0)
        foreign_pin = store.apply_to_project('old-b', 'b')
        return store, a, b, pin, foreign_pin

    def test_healthy_scoped_history_matches_records_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store, a, b, pin, foreign_pin = self.fixture(path)
            store.save_version('a', {'module': {'block': 'A2', 'layer': 'M'}}, expected_version=1)
            read = audit.CustomerProfileStore(path, read_only=True)
            before = path.read_bytes()
            result = audit.audit_history(read, customer_key='a')
            self.assertTrue(result['healthy'])
            self.assertEqual(result['issues'], independent_record_oracle(a['profile_id'], [(1,a['profile_id']),(2,a['profile_id'])], [(1,a['profile_id'])]))
            self.assertEqual((result['version_count'], result['pin_count']), (2,1))
            self.assertNotIn('PRIVATE', json.dumps(result))
            child = subprocess.run([sys.executable, audit.__file__, '--database', str(path), '--customer', 'a'], capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), result)
            self.assertEqual(read.application_record('old-a', 'a'), pin)
            self.assertEqual(read.application_record('old-b', 'b'), foreign_pin)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'read-only'):
                audit.audit_history(store, customer_key='a')
            missing = audit.audit_history(read, customer_key='unknown')
            self.assertFalse(missing['profile_found'])
            self.assertEqual(missing['version_count'], 0)

    def test_corrupt_gap_dangling_and_foreign_records_are_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store, a, b, pin, foreign_pin = self.fixture(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)', ('a', b['profile_id'], 3, '{"PRIVATE_FOREIGN_LABEL":"PRIVATE_B_LAYER"}', 'synthetic'))
                db.execute('INSERT INTO project_applications VALUES (?,?,?,?)', ('bad-missing', 'a', a['profile_id'], 9))
                db.execute('INSERT INTO project_applications VALUES (?,?,?,?)', ('bad-foreign', 'a', b['profile_id'], 1))
                db.execute('INSERT INTO customers VALUES (?,?)', ('empty', 'synthetic-empty-profile'))
            read = audit.CustomerProfileStore(path, read_only=True)
            before = path.read_bytes()
            result = audit.audit_history(read, customer_key='a')
            self.assertFalse(result['healthy'])
            self.assertEqual(result['issues'], independent_record_oracle(a['profile_id'], [(1,a['profile_id']),(3,b['profile_id'])], [(1,a['profile_id']),(9,a['profile_id']),(1,b['profile_id'])]))
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn(b['profile_id'], json.dumps(result))
            child = subprocess.run([sys.executable, audit.__file__, '--database', str(path), '--customer', 'a'], capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 1, child.stderr)
            self.assertEqual(json.loads(child.stdout), result)
            self.assertTrue(audit.audit_history(read, customer_key='b')['healthy'])
            empty = audit.audit_history(read, customer_key='empty')
            self.assertFalse(empty['healthy'])
            self.assertEqual(empty['issues'], independent_record_oracle('synthetic-empty-profile', [], []))
            self.assertEqual(read.application_record('old-a', 'a'), pin)
            self.assertEqual(read.application_record('old-b', 'b'), foreign_pin)
            self.assertEqual(path.read_bytes(), before)
