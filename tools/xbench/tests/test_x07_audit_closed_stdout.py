from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditClosedStdout(unittest.TestCase):
    def test_clean_findings_and_unavailable_closed_reader_preserve_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = audit.CustomerProfileStore(path)
            store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
            old = store.apply_to_project('old','a')
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT INTO customers VALUES (?,?)',('empty','synthetic-empty'))
            before = path.read_bytes()
            missing = Path(directory) / 'absent.sqlite'
            for database, scope in [(path,'a'),(path,'empty'),(missing,'a')]:
                with self.subTest(database=database.name,scope=scope):
                    reader,writer = os.pipe(); os.close(reader)
                    try: child = subprocess.Popen([sys.executable,audit.__file__,'--database',str(database),'--customer',scope],stdout=writer,stderr=subprocess.PIPE)
                    finally: os.close(writer)
                    try: _,error = child.communicate(timeout=10)
                    finally:
                        if child.poll() is None: child.kill(); child.communicate(timeout=5)
                    self.assertEqual(child.returncode,2); self.assertEqual(error,b'')
                    self.assertEqual(store.application_record('old','a'),old)
                    self.assertEqual(path.read_bytes(),before)
                    self.assertFalse(missing.exists())
