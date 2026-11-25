from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditRowLimits(unittest.TestCase):
    def boundary(self, collection):
        for count in [audit.MAX_AUDIT_ROWS-1, audit.MAX_AUDIT_ROWS, audit.MAX_AUDIT_ROWS+1]:
            with self.subTest(collection=collection, count=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'PRIVATE_PATH.sqlite'
                store = audit.CustomerProfileStore(path)
                saved = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
                pin = store.apply_to_project('old-a', 'a')
                store.save_version('b', {'module': {'block': 'PRIVATE_B', 'layer': 'B'}}, expected_version=0)
                foreign = store.apply_to_project('old-b', 'b')
                with closing(sqlite3.connect(path)) as db, db:
                    if collection == 'versions':
                        db.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)',
                                       (('a', saved['profile_id'], version, '{"module":{"block":"A","layer":"M"}}', 'synthetic') for version in range(2,count+1)))
                    else:
                        db.executemany('INSERT INTO project_applications VALUES (?,?,?,?)',
                                       ((f'pin-{index}', 'a', saved['profile_id'], 1) for index in range(1,count)))
                read = audit.CustomerProfileStore(path, read_only=True)
                before = path.read_bytes()
                if count <= audit.MAX_AUDIT_ROWS:
                    result = audit.audit_history(read, customer_key='a')
                    self.assertTrue(result['healthy'])
                    self.assertEqual(result['version_count'], count if collection == 'versions' else 1)
                    self.assertEqual(result['pin_count'], count if collection == 'pins' else 1)
                    self.assertTrue(all(not value for value in result['issues'].values()))
                else:
                    with self.assertRaisesRegex(ValueError, '^audit row budget exceeded$'):
                        audit.audit_history(read, customer_key='a')
                    child = subprocess.run([sys.executable, audit.__file__, '--database', str(path), '--customer', 'a'], capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, 2)
                    self.assertEqual(child.stderr, '')
                    self.assertEqual(json.loads(child.stdout), {'schema': 'leaf.x07.profile-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'})
                    self.assertLess(len(child.stdout), 200); self.assertNotIn('PRIVATE', child.stdout)
                self.assertEqual(read.application_record('old-a', 'a'), pin)
                self.assertEqual(read.application_record('old-b', 'b'), foreign)
                self.assertEqual(path.read_bytes(), before)

    def test_version_counts_below_at_and_above_budget(self): self.boundary('versions')
    def test_pin_counts_below_at_and_above_budget(self): self.boundary('pins')
