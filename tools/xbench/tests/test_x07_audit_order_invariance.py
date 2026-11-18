from contextlib import closing, contextmanager
from pathlib import Path
import itertools
import json
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, audit_history


class AuditOrderInvariance(unittest.TestCase):
    def test_corrupt_orders_and_reverse_pragma_match_independent_intervals(self):
        expected = {'schema': 'leaf.x07.profile-history-audit.v1', 'customer_key': 'a', 'profile_found': True,
                    'version_count': 4, 'pin_count': 4, 'healthy': False,
                    'issues': {'missing_profile': 0, 'empty_chain': 0, 'version_gaps': [{'first':2,'last':2},{'first':5,'last':6}],
                               'invalid_versions': 0, 'foreign_version_profiles': 1, 'dangling_pin_versions': 1, 'foreign_pin_profiles': 1}}
        outputs = []
        for order in itertools.permutations([3,4,7]):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'store.sqlite'
                store = CustomerProfileStore(path)
                a = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
                old = store.apply_to_project('old-a', 'a')
                b = store.save_version('b', {'module': {'block': 'PRIVATE_B', 'layer': 'B'}}, expected_version=0)
                foreign = store.apply_to_project('old-b', 'b')
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('PRAGMA foreign_keys=OFF')
                    for version in order:
                        db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)', ('a', b['profile_id'] if version==4 else a['profile_id'], version, 'PRIVATE_B', 'synthetic'))
                    rows = [('new-a','a',a['profile_id'],7), ('bad-missing','a',a['profile_id'],9), ('bad-foreign','a',b['profile_id'],3)]
                    for index in order:
                        db.execute('INSERT INTO project_applications VALUES (?,?,?,?)', rows[[3,4,7].index(index)])
                read = CustomerProfileStore(path, read_only=True)
                original = read._connection
                before = path.read_bytes()
                for reverse in [0,1]:
                    @contextmanager
                    def reordered():
                        with original() as db:
                            db.execute('PRAGMA reverse_unordered_selects='+str(reverse))
                            yield db
                    read._connection = reordered
                    result = audit_history(read, customer_key='a')
                    self.assertEqual(result, expected)
                    outputs.append(json.dumps(result, sort_keys=True))
                    self.assertNotIn('PRIVATE', outputs[-1]); self.assertNotIn(b['profile_id'], outputs[-1])
                read._connection = original
                self.assertEqual(read.application_record('old-a', 'a'), old)
                self.assertEqual(read.application_record('old-b', 'b'), foreign)
                self.assertEqual(path.read_bytes(), before)
        self.assertTrue(all(output == outputs[0] for output in outputs))
