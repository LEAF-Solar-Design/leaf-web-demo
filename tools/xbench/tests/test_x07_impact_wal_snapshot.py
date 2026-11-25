from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_change_impact as impact


class ImpactWalSnapshot(unittest.TestCase):
    def test_concurrent_edit_and_pin_do_not_mix_preview_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'store.sqlite';store=impact.CustomerProfileStore(path)
            first=store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
            old=store.apply_to_project('old','a')
            keeper=sqlite3.connect(path)
            try:
                self.assertEqual(keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
                read=impact.CustomerProfileStore(path,read_only=True)
                baseline=impact.preview_change(read,customer_key='a',profile_id=first['profile_id'],before_version=1,after_version=1)
                selected,committed=threading.Event(),threading.Event();errors=[]
                original=read._connection
                class Proxy:
                    def __init__(self,db):self.db=db
                    def __getattr__(self,name):return getattr(self.db,name)
                    def execute(self,sql,params=()):
                        cursor=self.db.execute(sql,params)
                        if sql.startswith('SELECT profile_id FROM main.customers'):
                            selected.set()
                            if not committed.wait(5):raise AssertionError('writer timeout')
                        return cursor
                @contextmanager
                def snapshot():
                    with original() as db:yield Proxy(db)
                def writer():
                    try:
                        if not selected.wait(5):raise AssertionError('reader timeout')
                        store.save_version('a',{'module':{'block':'B','layer':'M'}},expected_version=1)
                        store.apply_to_project('new','a')
                    except BaseException as error:errors.append(error)
                    finally:committed.set()
                thread=threading.Thread(target=writer);read._connection=snapshot;thread.start()
                try:
                    self.assertEqual(impact.preview_change(read,customer_key='a',profile_id=first['profile_id'],before_version=1,after_version=1),baseline)
                finally:
                    selected.set();committed.set();thread.join(6);read._connection=original
                self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
                after=impact.preview_change(read,customer_key='a',profile_id=first['profile_id'],before_version=1,after_version=2)
                self.assertEqual(after['retaining_different_roles'],[{'project_key':'old','version':1,'different_roles':['module']}])
                self.assertEqual(after['changed'],{'module':{'before':{'block':'A','layer':'M'},'after':{'block':'B','layer':'M'}}})
                self.assertEqual(read.application_record('old','a'),old)
                self.assertEqual(read.application_record('new','a')['version'],2)
            finally:keeper.close()
