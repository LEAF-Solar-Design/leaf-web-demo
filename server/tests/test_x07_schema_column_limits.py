"""X07 SQLite column limits refuse initialization without partial schemas."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import CustomerProfileStore, ConfirmationReceiptStore


class SchemaColumnLimits(unittest.TestCase):
    def test_profile_and_journal_limits_rollback_and_reopen(self):
        for kind in ('profile', 'journal'):
            for existing in (False, True):
                with self.subTest(kind=kind, existing=existing), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'store.sqlite'
                    cls = CustomerProfileStore if kind == 'profile' else ConfirmationReceiptStore
                    if existing:
                        current = cls(path)
                        if kind == 'profile':
                            current.save_version('a', {'module': {'block': 'Panel', 'layer': 'M'}}, expected_version=0)
                            current.apply_to_project('old', 'a')
                        else:
                            with closing(sqlite3.connect(path)) as db, db:
                                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('old', 'a', 'digest', '{}'))
                    else:
                        sqlite3.connect(path).close()
                    before = path.read_bytes()
                    if kind == 'profile':
                        connect = cls._connection
                        @contextmanager
                        def limited(instance):
                            with connect(instance) as db:
                                db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 3)
                                yield db
                        seam = '_connection'
                    else:
                        connect = cls._connect
                        def limited(instance):
                            db = connect(instance); db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 2); return db
                        seam = '_connect'
                    with patch.object(cls, seam, limited):
                        with self.assertRaises(sqlite3.DatabaseError):
                            cls(path)
                    self.assertEqual(path.read_bytes(), before)
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], int(existing))
                        if not existing:
                            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
                    reopened = cls(path)
                    if existing and kind == 'profile':
                        self.assertEqual(reopened.application_record('old', 'a')['version'], 1)
                    if existing and kind == 'journal':
                        with closing(sqlite3.connect(path)) as db:
                            self.assertEqual(db.execute('SELECT * FROM confirmations').fetchall(), [('old', 'a', 'digest', '{}')])
