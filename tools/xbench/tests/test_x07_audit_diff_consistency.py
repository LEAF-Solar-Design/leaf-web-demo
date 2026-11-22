from contextlib import closing
from copy import deepcopy
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, audit_history
from x07_profile_version_diff import compare_versions


class AuditDiffConsistency(unittest.TestCase):
    def rebuild(self,before,delta):
        result = deepcopy(before)
        for role,value in delta['removed'].items(): self.assertEqual(result.pop(role),value)
        for role,change in delta['changed'].items():
            self.assertEqual(result[role],change['before'])
            result[role] = deepcopy(change['after'])
        for role,value in delta['added'].items():
            self.assertNotIn(role,result)
            result[role] = deepcopy(value)
        return result

    def test_seeded_histories_reconstruct_then_removed_version_is_detected_without_repair(self):
        rng = random.Random(29)
        for seed in range(6):
            with self.subTest(seed=seed),tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'store.sqlite'
                store = CustomerProfileStore(path)
                maps = []
                for version in range(4):
                    roles = {'module':{'block':rng.choice(['A','B','é','e\u0301']),'layer':rng.choice(['M','m'])}}
                    for role in ['roof','obstruction']:
                        if rng.randrange(2): roles[role] = {'layer':rng.choice(['R','r','O\0one'])}
                    maps.append(deepcopy(roles))
                    saved = store.save_version('a',roles,expected_version=version)
                    if version==0: old = store.apply_to_project('old','a')
                latest_pin = store.apply_to_project('new','a')
                read = CustomerProfileStore(path,read_only=True)
                before = path.read_bytes()
                healthy = audit_history(read,customer_key='a')
                self.assertTrue(healthy['healthy'])
                self.assertEqual((healthy['version_count'],healthy['pin_count']),(4,2))
                reconstructed = maps[0]
                for version in range(2,5):
                    delta = compare_versions(read,customer_key='a',profile_id=saved['profile_id'],before_version=version-1,after_version=version)
                    reconstructed = self.rebuild(reconstructed,delta)
                    self.assertEqual(reconstructed,maps[version-1])
                self.assertEqual(read.application_record('old','a'),old)
                self.assertEqual(path.read_bytes(),before)
                # Disposable corruption only: restore the exact immutability
                # guard in the same transaction after deleting unpinned v2.
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    schema_before = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
                    trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='versions_no_delete'").fetchone()[0]
                    db.execute('DROP TRIGGER versions_no_delete')
                    db.execute('DELETE FROM profile_versions WHERE customer_key=? AND version=?',('a',2))
                    db.execute(trigger)
                    self.assertEqual(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall(),schema_before)
                read = CustomerProfileStore(path,read_only=True)
                changed = path.read_bytes()
                damaged = audit_history(read,customer_key='a')
                self.assertFalse(damaged['healthy'])
                self.assertEqual(damaged['version_count'],3)
                self.assertEqual(damaged['issues']['version_gaps'],[{'first':2,'last':2}])
                self.assertEqual(damaged['issues']['dangling_pin_versions'],0)
                with self.assertRaisesRegex(ValueError,'^scoped versions unavailable$'):
                    compare_versions(read,customer_key='a',profile_id=saved['profile_id'],before_version=1,after_version=2)
                delta = compare_versions(read,customer_key='a',profile_id=saved['profile_id'],before_version=1,after_version=4)
                self.assertEqual(self.rebuild(maps[0],delta),maps[3])
                self.assertEqual(read.application_record('old','a'),old)
                self.assertEqual(read.application_record('new','a'),latest_pin)
                self.assertEqual(path.read_bytes(),changed)
