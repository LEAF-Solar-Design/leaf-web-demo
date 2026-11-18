from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditOuterTransaction(unittest.TestCase):
    def test_caller_transactions_are_refused_without_commit_or_rollback(self):
        for statement in ['BEGIN','BEGIN IMMEDIATE']:
            with self.subTest(statement=statement),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'store.sqlite'; store=audit.CustomerProfileStore(path)
                saved=store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
                old=store.apply_to_project('old','a'); read=audit.CustomerProfileStore(path,read_only=True)
                before=path.read_bytes(); db=sqlite3.connect(path)
                original=read._connection
                @contextmanager
                def caller_owned(): yield db
                try:
                    db.row_factory=sqlite3.Row; db.execute('PRAGMA foreign_keys=ON'); db.execute(statement)
                    db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',('a',saved['profile_id'],2,'{}','pending'))
                    traces=[]; db.set_trace_callback(traces.append)
                    read._connection=caller_owned
                    with self.assertRaisesRegex(ValueError,'caller transaction'): audit.audit_history(read,customer_key='a')
                    self.assertTrue(db.in_transaction)
                    self.assertEqual(db.execute('SELECT count(*) FROM profile_versions WHERE customer_key=?',('a',)).fetchone()[0],2)
                    self.assertFalse(any(sql.upper().startswith(('COMMIT','ROLLBACK')) for sql in traces))
                finally:
                    read._connection=original; db.rollback(); db.close()
                self.assertEqual(read.application_record('old','a'),old)
                self.assertEqual(path.read_bytes(),before)
                self.assertEqual(audit.audit_history(read,customer_key='a')['version_count'],1)
