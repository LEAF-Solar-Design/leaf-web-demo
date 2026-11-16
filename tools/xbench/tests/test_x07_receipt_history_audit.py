from contextlib import closing
from pathlib import Path
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_receipt_history_audit as audit
from x07_profile_workflow import ProfileWorkflow


class ReceiptHistoryAudit(unittest.TestCase):
    def test_settled_references_missing_tampered_and_foreign_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profiles.sqlite'; journal_path=Path(directory)/'receipts.sqlite'
            store=audit.CustomerProfileStore(path); journal=audit.ConfirmationReceiptStore(journal_path)
            pins={}
            for customer in ['a','b']:
                saved=store.save_version(customer,{'module':{'block':'PRIVATE_'+customer,'layer':'M','x':1}},expected_version=0)
                project='old-'+customer; pin=store.apply_to_project(project,customer); pins[customer]=pin
                context={'customer_key':customer,'project_key':project,'drawing_key':'drawing-'+customer}
                result=ProfileWorkflow._result(context,pin,saved['roles'],{'count':1})
                journal.run(context,'0'*64,lambda:result,lambda value:None)
            read=audit.CustomerProfileStore(path,read_only=True); receipts=audit.ConfirmationReceiptStore(journal_path,read_only=True)
            before=(path.read_bytes(),journal_path.read_bytes())
            clean=audit.audit_receipts(read,receipts,customer_key='a')
            self.assertEqual(clean,{'schema':'leaf.x07.receipt-history-audit.v1','customer_key':'a','receipt_count':1,'healthy':True,'issues':{'invalid_receipts':0,'missing_references':0,'inconsistent_references':0}})
            self.assertEqual((path.read_bytes(),journal_path.read_bytes()),before)
            with closing(sqlite3.connect(journal_path)) as db:
                original_encoded=db.execute('SELECT result FROM confirmations WHERE project_key=?',('old-a',)).fetchone()[0]
            for kind in ['boolean','overflow']:
                with self.subTest(kind=kind):
                    changed=json.loads(original_encoded)
                    if kind=='boolean': changed['resolved_roles']['module']['x']=True
                    else: changed['applied_profile']['overflow']=1.0
                    encoded=json.dumps(changed)
                    if kind=='overflow': encoded=encoded.replace('"overflow": 1.0','"overflow": 1e999')
                    with closing(sqlite3.connect(journal_path)) as db,db:
                        db.execute('UPDATE confirmations SET result=? WHERE project_key=?',(encoded,'old-a'))
                    snapshot=(path.read_bytes(),journal_path.read_bytes())
                    try:
                        result=audit.audit_receipts(read,receipts,customer_key='a')
                        self.assertFalse(result['healthy'])
                        self.assertEqual(result['issues'],{'invalid_receipts':int(kind=='overflow'),'missing_references':0,'inconsistent_references':int(kind=='boolean')})
                        child=subprocess.run([sys.executable,audit.__file__,'--database',str(path),'--journal',str(journal_path),'--customer','a'],capture_output=True,text=True,timeout=10)
                        self.assertEqual(child.returncode,1,child.stderr); self.assertEqual(child.stderr,'')
                        self.assertEqual(json.loads(child.stdout),result)
                        self.assertNotIn('PRIVATE',child.stdout)
                        self.assertEqual((path.read_bytes(),journal_path.read_bytes()),snapshot)
                        self.assertEqual(read.application_record('old-a','a'),pins['a'])
                    finally:
                        with closing(sqlite3.connect(journal_path)) as db,db:
                            db.execute('UPDATE confirmations SET result=? WHERE project_key=?',(original_encoded,'old-a'))
            with closing(sqlite3.connect(journal_path)) as db,db:
                original=json.loads(db.execute('SELECT result FROM confirmations WHERE project_key=?',('old-a',)).fetchone()[0])
                tampered=copy.deepcopy(original); tampered['resolved_roles']['module']['block']='PRIVATE_CHANGED'
                db.execute('UPDATE confirmations SET result=? WHERE project_key=?',(json.dumps(tampered),'old-a'))
                missing=copy.deepcopy(original); missing['project_key']='missing'
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)',('missing','a','0'*64,json.dumps(missing)))
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)',('invalid','a','0'*64,'PRIVATE_MALFORMED'))
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)',('foreign-corrupt','b','0'*64,'PRIVATE_FOREIGN_UNREAD'))
                missing_version=copy.deepcopy(original)
                missing_version['project_key']='missing-version'
                missing_version['applied_profile']['project_key']='missing-version'
                missing_version['applied_profile']['version']=9
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)',('missing-version','a','0'*64,json.dumps(missing_version)))
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('INSERT INTO project_applications VALUES (?,?,?,?)',('missing-version','a',pins['a']['profile_id'],9))
            before=(path.read_bytes(),journal_path.read_bytes())
            result=audit.audit_receipts(read,receipts,customer_key='a')
            self.assertEqual(result['receipt_count'],4); self.assertFalse(result['healthy'])
            self.assertEqual(result['issues'],{'invalid_receipts':1,'missing_references':1,'inconsistent_references':2})
            self.assertNotIn('PRIVATE',json.dumps(result)); self.assertNotIn('old-b',json.dumps(result))
            for customer,pin in pins.items(): self.assertEqual(read.application_record('old-'+customer,customer),pin)
            self.assertEqual((path.read_bytes(),journal_path.read_bytes()),before)
            with self.assertRaises(ValueError): audit.audit_receipts(store,receipts,customer_key='a')
