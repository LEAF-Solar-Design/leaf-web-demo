from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit,seed


class ReceiptAuditForeignBudget(unittest.TestCase):
    def test_foreign_payloads_and_row_budget_do_not_change_target(self):
        with tempfile.TemporaryDirectory() as directory:
            profile,journal,read,receipts,pins=seed(directory)
            baseline=json.dumps(audit.audit_receipts(read,receipts,customer_key='a'),sort_keys=True)
            with closing(sqlite3.connect(journal)) as db,db:
                db.execute('UPDATE confirmations SET result=? WHERE customer_key=?',('PRIVATE_FOREIGN_'+('x'*300000),'b'))
                db.executemany('INSERT INTO confirmations VALUES (?,?,?,?)',((f'foreign-{i}','b','0'*64,'PRIVATE_MALFORMED') for i in range(audit.MAX_AUDIT_ROWS+1)))
            before=(profile.read_bytes(),journal.read_bytes())
            actual=json.dumps(audit.audit_receipts(read,receipts,customer_key='a'),sort_keys=True)
            self.assertEqual(actual,baseline);self.assertNotIn('PRIVATE',actual)
            with self.assertRaises(ValueError):audit.audit_receipts(read,receipts,customer_key='b')
            for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
            self.assertEqual((profile.read_bytes(),journal.read_bytes()),before)
