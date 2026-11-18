from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit,seed


def nodes(value):
    return 1+sum(nodes(v) for v in (value.values() if isinstance(value,dict) else value if isinstance(value,list) else []))


class ReceiptAuditJsonBudgets(unittest.TestCase):
    def case(self,transform,code):
        with tempfile.TemporaryDirectory() as directory:
            profile,journal,read,receipts,pins=seed(directory)
            with closing(sqlite3.connect(journal)) as db,db:
                original=db.execute('SELECT result FROM confirmations WHERE project_key=?',('old-a',)).fetchone()[0]
                db.execute('UPDATE confirmations SET result=? WHERE project_key=?',(transform(original),'old-a'))
            before=(profile.read_bytes(),journal.read_bytes())
            if code==2:
                with self.assertRaises(audit.ReceiptBudgetError):audit.audit_receipts(read,receipts,customer_key='a')
            else:self.assertTrue(audit.audit_receipts(read,receipts,customer_key='a')['healthy'])
            child=subprocess.run([sys.executable,audit.__file__,'--database',str(profile),'--journal',str(journal),'--customer','a'],capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,code,child.stderr);self.assertEqual(child.stderr,'')
            value=json.loads(child.stdout);self.assertLess(len(child.stdout),500)
            if code==2:
                self.assertEqual(value,{'schema':'leaf.x07.receipt-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                self.assertNotIn('healthy',child.stdout)
            else:self.assertTrue(value['healthy'])
            for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
            self.assertEqual((profile.read_bytes(),journal.read_bytes()),before)

    def test_depth_below_at_above_and_very_deep_is_bounded(self):
        for depth in [audit.MAX_RECEIPT_DEPTH-2,audit.MAX_RECEIPT_DEPTH-1,audit.MAX_RECEIPT_DEPTH,5000]:
            with self.subTest(depth=depth):
                self.case(lambda raw:raw[:-1]+',"extra":'+'['*depth+'0'+']'*depth+'}',0 if depth+1<=audit.MAX_RECEIPT_DEPTH else 2)
        def quoted(raw):
            value=json.loads(raw);value['extra']='[{'*1000+'\\"'+']}'*1000
            return json.dumps(value)
        self.case(quoted,0)

    def test_exact_node_and_utf8_byte_limits(self):
        for count in [audit.MAX_RECEIPT_NODES-1,audit.MAX_RECEIPT_NODES,audit.MAX_RECEIPT_NODES+1]:
            def payload(raw):
                value=json.loads(raw);value['extra']=[]
                value['extra']=[None]*(count-nodes(value))
                self.assertEqual(nodes(value),count)
                return json.dumps(value)
            with self.subTest(nodes=count):self.case(payload,0 if count<=audit.MAX_RECEIPT_NODES else 2)
        for count in [audit.MAX_RECEIPT_BYTES-1,audit.MAX_RECEIPT_BYTES,audit.MAX_RECEIPT_BYTES+1]:
            def payload(raw):
                value=json.loads(raw);value['extra']=''
                baseline=json.dumps(value,separators=(',',':'))
                value['extra']='x'*(count-len(baseline.encode('utf-8')))
                result=json.dumps(value,separators=(',',':'))
                self.assertEqual(len(result.encode('utf-8')),count)
                return result
            with self.subTest(bytes=count):self.case(payload,0 if count<=audit.MAX_RECEIPT_BYTES else 2)
