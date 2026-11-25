from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed
import x07_profile_change_impact as impact


class ImpactRowBoundaries(unittest.TestCase):
    def check_boundary(self, collection):
        for count in [audit.MAX_AUDIT_ROWS - 1, audit.MAX_AUDIT_ROWS, audit.MAX_AUDIT_ROWS + 1]:
            with self.subTest(collection=collection, count=count), tempfile.TemporaryDirectory() as directory:
                profile, _, read, _, pins = seed(directory)
                profile_id = pins['a']['profile_id']
                original_roles = pins['a']['roles']
                edited_roles = {'module': {'block': 'EDIT', 'layer': 'M'}}
                if collection == 'pins':
                    audit.CustomerProfileStore(profile).save_version('a', edited_roles, expected_version=1)
                with closing(sqlite3.connect(profile)) as connection, connection:
                    if collection == 'versions':
                        rows = [('a', profile_id, version,
                                 json.dumps(edited_roles if version == count else original_roles), 'synthetic')
                                for version in range(2, count + 1)]
                        connection.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)', rows)
                        after_version = count
                        expected_pins = [{'project_key': 'old-a', 'version': 1, 'different_roles': ['module']}]
                    else:
                        rows = [(f'p{index}', 'a', profile_id, 2 if index == 1 else 1) for index in range(1, count)]
                        connection.executemany('INSERT INTO project_applications VALUES (?,?,?,?)', rows)
                        after_version = 2
                        expected_pins = [{'project_key': project, 'version': 1, 'different_roles': ['module']}
                                         for project in sorted(['old-a'] + [f'p{i}' for i in range(2, count)])]
                arguments = dict(customer_key='a', profile_id=profile_id, before_version=1, after_version=after_version)
                before = profile.read_bytes()
                if count <= audit.MAX_AUDIT_ROWS:
                    result = impact.preview_change(read, **arguments)
                    self.assertEqual(result['changed'], {'module': {'before': original_roles['module'], 'after': edited_roles['module']}})
                    self.assertEqual(result['added'], {})
                    self.assertEqual(result['removed'], {})
                    self.assertEqual(result['retaining_different_roles'], expected_pins)
                else:
                    with self.assertRaisesRegex(ValueError, 'row budget'):
                        impact.preview_change(read, **arguments)
                    child = subprocess.run([sys.executable, impact.__file__, '--database', str(profile), '--customer', 'a',
                                            '--profile-id', profile_id, '--before', '1', '--after', str(after_version)],
                                           capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, 2, child.stderr)
                    self.assertEqual(child.stderr, '')
                    self.assertEqual(json.loads(child.stdout), {'schema': 'leaf.x07.profile-change-impact.v1', 'error': 'SCOPED_PREVIEW_UNAVAILABLE'})
                    self.assertNotIn('retaining', child.stdout)
                for key, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + key, key), pin)
                self.assertEqual(profile.read_bytes(), before)

    def test_version_rows_below_at_and_above_budget(self):
        self.check_boundary('versions')

    def test_project_pin_rows_below_at_and_above_budget(self):
        self.check_boundary('pins')
