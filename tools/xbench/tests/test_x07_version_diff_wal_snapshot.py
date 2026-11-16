from contextlib import contextmanager
from pathlib import Path
import importlib.util
import sqlite3
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location('wal_diff', Path(__file__).resolve().parents[1] / 'x07_profile_version_diff.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


class VersionDiffWalSnapshot(unittest.TestCase):
    def overlap(self, after):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = diff.CustomerProfileStore(path)
            first = store.save_version('a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
            pin = store.apply_to_project('old', 'a')
            store.save_version('a', {'module': {'block': 'B', 'layer': 'M'}}, expected_version=1)
            keeper = sqlite3.connect(path)
            try:
                self.assertEqual(keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                read = diff.CustomerProfileStore(path, read_only=True)
                selected, committed = threading.Event(), threading.Event()
                errors, changes, writes = [], [], []
                original = read._connection
                class Proxy:
                    def __init__(self, db): self.db = db
                    def __getattr__(self, name): return getattr(self.db, name)
                    def execute(self, sql, params=()):
                        cursor = self.db.execute(sql, params)
                        if sql.startswith('SELECT * FROM profile_versions'):
                            selected.set()
                            if not committed.wait(5): raise AssertionError('writer did not commit')
                        return cursor
                @contextmanager
                def snapshot():
                    with original() as db:
                        def authorize(action, *args):
                            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                                writes.append(action)
                                return sqlite3.SQLITE_DENY
                            return sqlite3.SQLITE_OK
                        db.set_authorizer(authorize)
                        yield Proxy(db)
                        changes.append(db.total_changes)
                def writer():
                    try:
                        if not selected.wait(5): raise AssertionError('reader did not establish snapshot')
                        independent = diff.CustomerProfileStore(path)
                        independent.save_version('a', {'module': {'block': 'C', 'layer': 'M'}}, expected_version=2)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        committed.set()
                thread = threading.Thread(target=writer)
                read._connection = snapshot
                thread.start()
                try:
                    if after == 2:
                        result = diff.compare_versions(read, customer_key='a', profile_id=first['profile_id'], before_version=1, after_version=2)
                        self.assertEqual(result['changed'], {'module': {'before': {'block': 'A', 'layer': 'M'}, 'after': {'block': 'B', 'layer': 'M'}}})
                        self.assertEqual(result['added'], {}); self.assertEqual(result['removed'], {})
                    else:
                        with self.assertRaisesRegex(ValueError, '^scoped versions unavailable$'):
                            diff.compare_versions(read, customer_key='a', profile_id=first['profile_id'], before_version=1, after_version=3)
                finally:
                    selected.set(); committed.set()
                    thread.join(6)
                    read._connection = original
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(changes, [0])
                self.assertEqual(writes, [])
                self.assertEqual(read.latest('a')['version'], 3)
                self.assertEqual(read.application_record('old', 'a'), pin)
                fresh = diff.compare_versions(read, customer_key='a', profile_id=first['profile_id'], before_version=1, after_version=3)
                self.assertEqual(fresh['changed']['module']['after']['block'], 'C')
            finally:
                keeper.close()

    def test_immutable_pair_stays_coherent_during_new_version_commit(self):
        self.overlap(2)

    def test_new_version_is_absent_until_fresh_snapshot(self):
        self.overlap(3)
