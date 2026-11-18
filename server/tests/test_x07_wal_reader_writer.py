from pathlib import Path
from contextlib import ExitStack
import sqlite3
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore


class WALReaderWriterTests(unittest.TestCase):
    def test_held_reader_snapshot_old_then_restart_new_with_old_pin_retained(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            path=Path(directory)/'store.sqlite';store=CustomerProfileStore(path)
            roles={'module':{'block':'Panel','layer':'M'}};store.save_version('a',roles);old=store.apply_to_project('old','a')
            reader=sqlite3.connect(path);resources.callback(reader.close)
            self.assertEqual(reader.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            reader.execute('BEGIN');self.assertEqual(reader.execute('SELECT MAX(version) FROM profile_versions').fetchone()[0],1)
            start=threading.Barrier(2);done=threading.Event();errors=[]
            def write():
                try:
                    start.wait(timeout=5);other=CustomerProfileStore(path)
                    other.save_version('a',{**roles,'roof':{'layer':'ROOF'}},expected_version=1);other.apply_to_project('new','a')
                except Exception as error:errors.append(error)
                finally:done.set()
            worker=threading.Thread(target=write);worker.start();start.wait(timeout=5)
            self.assertTrue(done.wait(timeout=10));worker.join(timeout=5);self.assertEqual(errors,[])
            self.assertEqual(reader.execute('SELECT MAX(version) FROM profile_versions').fetchone()[0],1)
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0],1)
            reader.rollback();reader.execute('BEGIN')
            self.assertEqual(reader.execute('SELECT MAX(version) FROM profile_versions').fetchone()[0],2)
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0],2);reader.rollback()
            fresh=CustomerProfileStore(path);self.assertEqual(fresh.application_record('old','a'),old)
            self.assertEqual(fresh.application_record('new','a')['version'],2)
