from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES


class ReceiptTriggerTampering(unittest.TestCase):
    def test_added_altering_and_suppressing_triggers_refuse_before_produce_or_replay(self):
        for cached in [False, True]:
            for action in ['suppress', 'alter']:
                with self.subTest(cached=cached, action=action), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    current = adapter(root)
                    request = {**COPIES[0], 'expected_version': 0}
                    original = current.confirm_roles(CONTEXT, request) if cached else None
                    with closing(sqlite3.connect(root / 'receipts.sqlite')) as db, db:
                        sql = ('CREATE TRIGGER injected BEFORE INSERT ON confirmations BEGIN SELECT RAISE(IGNORE); END;'
                               if action == 'suppress' else
                               "CREATE TRIGGER injected AFTER INSERT ON confirmations BEGIN UPDATE confirmations SET result='SYNTHETIC-FOREIGN-PAYLOAD'; END;")
                        db.execute(sql)
                    before = [(root / p).read_bytes() for p in ['profiles.sqlite', 'receipts.sqlite']]
                    with self.assertRaisesRegex(ValueError, '^unsupported confirmation journal schema$') as caught:
                        current.confirm_roles(CONTEXT, request)
                    self.assertNotIn('SYNTHETIC-', str(caught.exception))
                    self.assertEqual(current.workflow.store.latest('a')['version'] if cached else current.workflow.store.latest('a'),
                                     1 if cached else None)
                    self.assertEqual(current.workflow.store.application_record('p', 'a'), original['applied_profile'] if cached else None)
                    self.assertEqual([(root / p).read_bytes() for p in ['profiles.sqlite', 'receipts.sqlite']], before)
                    with self.assertRaisesRegex(ValueError, '^unsupported confirmation journal schema$'): adapter(root)
                    self.assertEqual([(root / p).read_bytes() for p in ['profiles.sqlite', 'receipts.sqlite']], before)
