from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit,seed


class ReceiptAuditDuplicateKeys(unittest.TestCase):
    def test_duplicate_decoded_members_never_audit_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            profile,journal,read,receipts,pins=seed(directory)
            with closing(sqlite3.connect(journal)) as db:
                original=db.execute('SELECT result FROM confirmations WHERE project_key=?',('old-a',)).fetchone()[0]
            variants=[original.replace('"state": "applied"','"state": "ignored", "state": "applied"',1),
                      original.replace('"state": "applied"','"st\\u0061te": "ignored", "state": "applied"',1),
                      original.replace('"layer": "M"','"layer": "M", "layer": "M"'),
                      original.replace('"layer": "M"','"la\\u0079er": "M", "layer": "M"')]
            for index,encoded in enumerate(variants):
                with self.subTest(variant=index):
                    self.assertNotEqual(encoded,original)
                    with closing(sqlite3.connect(journal)) as db,db:
                        db.execute('UPDATE confirmations SET result=? WHERE project_key=?',(encoded,'old-a'))
                    before=(profile.read_bytes(),journal.read_bytes())
                    result=audit.audit_receipts(read,receipts,customer_key='a')
                    self.assertEqual(result['issues'],{'invalid_receipts':1,'missing_references':0,'inconsistent_references':0})
                    self.assertFalse(result['healthy'])
                    child=subprocess.run([sys.executable,audit.__file__,'--database',str(profile),'--journal',str(journal),'--customer','a'],capture_output=True,text=True,timeout=10)
                    self.assertEqual(child.returncode,1,child.stderr);self.assertEqual(child.stderr,'')
                    self.assertEqual(json.loads(child.stdout),result);self.assertNotIn('PRIVATE',child.stdout)
                    for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
                    self.assertEqual((profile.read_bytes(),journal.read_bytes()),before)
            array='['+','.join(['null']*audit.MAX_RECEIPT_NODES)+']'
            combined=[variants[0][:-1]+',"extra":'+array+'}',
                      '{"extra":'+array+','+variants[1][1:],
                      variants[2][:-1]+',"extra":'+array+'}',
                      '{"extra":'+array+','+variants[3][1:]]
            for index,encoded in enumerate(combined):
                with self.subTest(overbudget_duplicate=index):
                    self.assertLess(len(encoded.encode('utf-8')),audit.MAX_RECEIPT_BYTES)
                    with closing(sqlite3.connect(journal)) as db,db:
                        db.execute('UPDATE confirmations SET result=? WHERE project_key=?',(encoded,'old-a'))
                    before=(profile.read_bytes(),journal.read_bytes())
                    with self.assertRaises(audit.ReceiptBudgetError):audit.audit_receipts(read,receipts,customer_key='a')
                    child=subprocess.run([sys.executable,audit.__file__,'--database',str(profile),'--journal',str(journal),'--customer','a'],capture_output=True,text=True,timeout=10)
                    self.assertEqual(child.returncode,2,child.stderr);self.assertEqual(child.stderr,'')
                    self.assertEqual(json.loads(child.stdout),{'schema':'leaf.x07.receipt-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                    self.assertNotIn('healthy',child.stdout)
                    for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
                    self.assertEqual((profile.read_bytes(),journal.read_bytes()),before)
