"""Journal refusal is bounded and does not repair or disclose stored payloads."""
from pathlib import Path
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_workflow_adapter import ConfirmationReceiptStore
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES

class ReceiptJournalCorruption(unittest.TestCase):
    def test_invalid_receipts_and_foreign_scope_leave_bytes_unchanged(self):
        for fault in ('malformed','scalar','foreign_body','foreign_row','oversized'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); current=adapter(root)
                request={**COPIES[0],'expected_version':0}
                current.confirm_roles(CONTEXT,request)
                journal=root/'receipts.sqlite'; profiles=root/'profiles.sqlite'
                with closing(sqlite3.connect(journal)) as db, db:
                    body=json.loads(db.execute('SELECT result FROM confirmations').fetchone()[0])
                    if fault=='foreign_row': db.execute("UPDATE confirmations SET customer_key='PRIVATE-FOREIGN'")
                    else:
                        if fault=='foreign_body': body['customer_key']='PRIVATE-FOREIGN'
                        encoded={'malformed':'{PRIVATE-FOREIGN','scalar':'[]','oversized':'x'*262145}.get(fault,json.dumps(body))
                        db.execute('UPDATE confirmations SET result=?',(encoded,))
                before=(journal.read_bytes(),profiles.read_bytes())
                with self.assertRaises(ValueError) as caught: adapter(root).confirm_roles(CONTEXT,request)
                self.assertNotIn('PRIVATE-FOREIGN',str(caught.exception))
                self.assertLess(len(str(caught.exception)),100)
                self.assertEqual((journal.read_bytes(),profiles.read_bytes()),before)

    def test_future_schema_refuses_without_any_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); adapter(root); journal=root/'receipts.sqlite'
            with closing(sqlite3.connect(journal)) as db, db: db.execute('PRAGMA user_version=99')
            before=journal.read_bytes()
            with self.assertRaisesRegex(ValueError,'unsupported confirmation journal schema'): ConfirmationReceiptStore(journal)
            self.assertEqual(journal.read_bytes(),before)
