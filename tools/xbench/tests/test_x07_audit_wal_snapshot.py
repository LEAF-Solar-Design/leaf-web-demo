from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, audit_history


class AuditWalSnapshot(unittest.TestCase):
    def test_roots_versions_and_pins_share_one_committed_snapshot(self):
        for existing in [False, True]:
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'store.sqlite'
                store = CustomerProfileStore(path)
                if existing:
                    profile = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
                    old_pin = store.apply_to_project('old-a', 'a')
                else:
                    profile = {'profile_id': 'synthetic-new-profile'}
                store.save_version('b', {'module': {'block': 'PRIVATE_B', 'layer': 'B'}}, expected_version=0)
                foreign = store.apply_to_project('old-b', 'b')
                keeper = sqlite3.connect(path)
                try:
                    self.assertEqual(keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                    read = CustomerProfileStore(path, read_only=True)
                    before = audit_history(read, customer_key='a')
                    selected, committed = threading.Event(), threading.Event()
                    errors, changes, writes = [], [], []
                    original = read._connection
                    class Proxy:
                        def __init__(self, db): self.db = db
                        def __getattr__(self, name): return getattr(self.db, name)
                        def execute(self, sql, params=()):
                            cursor = self.db.execute(sql, params)
                            if sql.startswith('SELECT profile_id FROM customers'):
                                selected.set()
                                if not committed.wait(5): raise AssertionError('writer barrier timed out')
                            return cursor
                    @contextmanager
                    def snapshot():
                        with original() as db:
                            def authorize(action, *args):
                                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                                    writes.append(action); return sqlite3.SQLITE_DENY
                                return sqlite3.SQLITE_OK
                            db.set_authorizer(authorize)
                            yield Proxy(db)
                            changes.append(db.total_changes)
                    def writer():
                        try:
                            if not selected.wait(5): raise AssertionError('reader barrier timed out')
                            with store._connection() as db:
                                db.execute('BEGIN IMMEDIATE')
                                if not existing:
                                    db.execute('INSERT INTO customers VALUES (?,?)', ('a', profile['profile_id']))
                                version = 2 if existing else 1
                                db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)', ('a', profile['profile_id'], version, '{"module":{"block":"NEW","layer":"M"}}', 'synthetic'))
                                db.execute('INSERT INTO project_applications VALUES (?,?,?,?)', ('new-a', 'a', profile['profile_id'], version))
                        except BaseException as error:
                            errors.append(error)
                        finally:
                            committed.set()
                    thread = threading.Thread(target=writer)
                    read._connection = snapshot
                    thread.start()
                    try:
                        self.assertEqual(audit_history(read, customer_key='a'), before)
                    finally:
                        selected.set(); committed.set(); thread.join(6)
                        read._connection = original
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(changes, [0]); self.assertEqual(writes, [])
                    after = audit_history(read, customer_key='a')
                    self.assertTrue(after['profile_found'] and after['healthy'])
                    self.assertEqual(after['version_count'], 2 if existing else 1)
                    self.assertEqual(after['pin_count'], 2 if existing else 1)
                    self.assertEqual(read.application_record('old-b', 'b'), foreign)
                    if existing: self.assertEqual(read.application_record('old-a', 'a'), old_pin)
                finally:
                    keeper.close()
