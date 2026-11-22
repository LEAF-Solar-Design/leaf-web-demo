from contextlib import contextmanager,redirect_stdout
from pathlib import Path
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditShadowTables(unittest.TestCase):
    def test_temp_and_attached_decoys_refuse_without_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'store.sqlite'; store=audit.CustomerProfileStore(path)
            store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
            old=store.apply_to_project('old','a'); read=audit.CustomerProfileStore(path,read_only=True)
            before=path.read_bytes(); original=read._connection
            for mode in ['temp','attached']:
                with self.subTest(mode=mode):
                    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
                    db.row_factory=sqlite3.Row; db.execute('PRAGMA foreign_keys=ON')
                    if mode=='temp':
                        db.execute('CREATE TEMP TABLE customers (customer_key,profile_id)')
                        db.execute('INSERT INTO temp.customers VALUES (?,?)',('a','PRIVATE_DECOY'))
                    else:
                        db.execute("ATTACH DATABASE ':memory:' AS decoy")
                        db.execute('CREATE TABLE decoy.customers (customer_key,profile_id)')
                        db.execute('INSERT INTO decoy.customers VALUES (?,?)',('a','PRIVATE_DECOY'))
                    db.commit()
                    @contextmanager
                    def injected(): yield db
                    read._connection=injected
                    try:
                        with self.assertRaises(ValueError): audit.audit_history(read,customer_key='a')
                        output=io.StringIO()
                        with patch.object(sys,'argv',['audit','--database',str(path),'--customer','a']),patch.object(audit,'CustomerProfileStore',return_value=read),redirect_stdout(output):
                            self.assertEqual(audit.main(),2)
                        self.assertEqual(json.loads(output.getvalue()),{'schema':'leaf.x07.profile-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                        self.assertNotIn('PRIVATE',output.getvalue()); self.assertFalse(db.in_transaction)
                    finally: read._connection=original; db.close()
            self.assertTrue(audit.audit_history(read,customer_key='a')['healthy'])
            self.assertEqual(read.application_record('old','a'),old); self.assertEqual(path.read_bytes(),before)
