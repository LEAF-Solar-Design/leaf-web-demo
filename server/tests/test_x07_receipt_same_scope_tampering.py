"""Same-scope IDs do not validate a corrupted cached confirmation payload."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES


class ReceiptSameScopeTampering(unittest.TestCase):
    def test_cached_roles_counts_and_snapshot_metadata_refuse_without_writes(self):
        for fault in ['role', 'count', 'bool_count', 'window', 'indices', 'snapshot', 'nested_snapshot']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                current = adapter(root)
                payload = {**COPIES[0], 'expected_version': 0,
                           'plan_token': current.load_workflow(CONTEXT)['plan_token']}
                original = current.confirm_roles(CONTEXT, payload)
                altered = deepcopy(original)
                if fault == 'role': altered['resolved_roles']['module']['layer'] = 'SYNTHETIC-FOREIGN-ROLE'
                elif fault == 'count': altered['selection']['count'] = 99
                elif fault == 'bool_count': altered['selection']['count'] = True
                elif fault == 'window': altered['selection']['design_window'] = COPIES[1]['design_window']
                elif fault == 'indices': altered['selection']['insert_indices'] = [1]
                elif fault == 'snapshot': altered['plan_token'] = 'SYNTHETIC-FOREIGN-TOKEN'
                else: altered['selection']['plan_token'] = 'SYNTHETIC-FOREIGN-TOKEN'
                self.assertEqual(altered['applied_profile'], original['applied_profile'])
                with closing(sqlite3.connect(root / 'receipts.sqlite')) as db, db:
                    db.execute('UPDATE confirmations SET result=? WHERE project_key=?',
                               (json.dumps(altered), CONTEXT['project_key']))
                before = [(root / name).read_bytes() for name in ['profiles.sqlite', 'receipts.sqlite']]
                replay = adapter(root)
                with patch.object(replay.workflow, 'confirm', side_effect=AssertionError('must not reconfirm')):
                    with self.assertRaisesRegex(ValueError, '^confirmation receipt payload is inconsistent$') as caught:
                        replay.confirm_roles(CONTEXT, payload)
                self.assertNotIn('SYNTHETIC-', str(caught.exception))
                self.assertEqual([(root / name).read_bytes() for name in ['profiles.sqlite', 'receipts.sqlite']], before)
                self.assertEqual(replay.workflow.store.application_record('p', 'a'), original['applied_profile'])

    def test_unchanged_receipt_replays_after_newer_version_without_reconfirming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            payload = {**COPIES[0], 'expected_version': 0}
            result = current.confirm_roles(CONTEXT, payload)
            current.workflow.store.save_version('a', {'module': {'block': 'Panel', 'layer': 'M'}}, expected_version=1)
            replay = adapter(root)
            with patch.object(replay.workflow, 'confirm', side_effect=AssertionError('must not reconfirm')):
                self.assertEqual(replay.confirm_roles(CONTEXT, payload), result)
            self.assertEqual(replay.workflow.store.latest('a')['version'], 2)
            self.assertEqual(replay.workflow.store.application_record('p', 'a')['version'], 1)
