from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit,seed,ProfileWorkflow


def backup(source,target):
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as read,closing(sqlite3.connect(target)) as write:
        read.backup(write)


class ReceiptAuditRestoreGenerations(unittest.TestCase):
    def test_independent_restore_generations_follow_reference_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);profile,journal,_,_,pins=seed(root)
            p1,j1,p2,j2=[root/name for name in ['p1.sqlite','j1.sqlite','p2.sqlite','j2.sqlite']]
            backup(profile,p1);backup(journal,j1)
            store=audit.CustomerProfileStore(profile);receipts=audit.ConfirmationReceiptStore(journal)
            context={'customer_key':'a','project_key':'new-a','drawing_key':'new-drawing'}
            receipts.run(context,'1'*64,lambda:ProfileWorkflow(store).confirm({'inserts':[{'name':'A2','layer':'M','x':0,'y':0}]},**context,roles={'module':{'block':'A2','layer':'M'}},window=[0,0,0,0],expected_version=1),lambda value:None)
            backup(profile,p2);backup(journal,j2)
            originals={path:path.read_bytes() for path in [profile,journal,p1,j1,p2,j2]}
            for pg,jg in [(1,1),(2,2),(1,2),(2,1)]:
                with self.subTest(profile=pg,journal=jg):
                    rp,rj=root/f'restore-p-{pg}-{jg}.sqlite',root/f'restore-j-{pg}-{jg}.sqlite'
                    backup(p1 if pg==1 else p2,rp);backup(j1 if jg==1 else j2,rj)
                    read=audit.CustomerProfileStore(rp,read_only=True);journal_read=audit.ConfirmationReceiptStore(rj,read_only=True)
                    before=(rp.read_bytes(),rj.read_bytes())
                    for customer in ['a','b']:
                        missing=int(customer=='a' and pg==1 and jg==2)
                        expected={'schema':'leaf.x07.receipt-history-audit.v1','customer_key':customer,'receipt_count':1+int(customer=='a' and jg==2),'healthy':not missing,'issues':{'invalid_receipts':0,'missing_references':missing,'inconsistent_references':0}}
                        self.assertEqual(audit.audit_receipts(read,journal_read,customer_key=customer),expected)
                        self.assertEqual(read.application_record('old-'+customer,customer),pins[customer])
                    self.assertEqual((rp.read_bytes(),rj.read_bytes()),before)
            self.assertEqual({path:path.read_bytes() for path in originals},originals)
