"""X07 journal initialization rolls back denied schema operations."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_x07_receipt_retry_races import ConfirmationReceiptStore


class ReceiptSchemaAuthorizer(unittest.TestCase):
    def test_denied_schema_initialization_is_atomic_and_reopens_explicitly(self):
        for denied in (sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_PRAGMA):
            for legacy in (False, True):
                if legacy and denied != sqlite3.SQLITE_PRAGMA:
                    continue  # Existing schema does not execute CREATE on reopen.
                with self.subTest(denied=denied, legacy=legacy), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'receipts.sqlite'
                    with closing(sqlite3.connect(path)) as db:
                        if legacy:
                            db.execute(ConfirmationReceiptStore._DDL)
                            db.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('old', 'customer', 'digest', '{}'))
                        db.commit()
                    before = path.read_bytes()
                    seen = []
                    connect = ConfirmationReceiptStore._connect

                    def refused(instance):
                        db = connect(instance)
                        def authorize(action, first, second, *rest):
                            writes_version = action == sqlite3.SQLITE_PRAGMA and first == 'user_version' and second is not None
                            if action == denied and (action != sqlite3.SQLITE_PRAGMA or writes_version):
                                seen.append((action, first))
                                return sqlite3.SQLITE_DENY
                            return sqlite3.SQLITE_OK
                        db.set_authorizer(authorize)
                        return db

                    with patch.object(ConfirmationReceiptStore, '_connect', refused):
                        with self.assertRaises(sqlite3.DatabaseError) as caught:
                            ConfirmationReceiptStore(path)
                    self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_AUTH)
                    self.assertTrue(seen)
                    self.assertEqual(path.read_bytes(), before)
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
                        if legacy:
                            self.assertEqual(db.execute('SELECT * FROM confirmations').fetchall(), [('old', 'customer', 'digest', '{}')])
                        else:
                            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
                    ConfirmationReceiptStore(path)
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], int(legacy))
                        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
