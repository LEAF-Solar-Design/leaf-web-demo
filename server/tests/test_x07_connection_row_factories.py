from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES


def dict_rows(cursor, row):
    return dict(zip((field[0] for field in cursor.description), row))


class ConnectionRowFactories(unittest.TestCase):
    def test_supported_rows_or_explicit_factory_refusal_and_normal_retry(self):
        for factory, text in [(sqlite3.Row, str), (dict_rows, str), (sqlite3.Row, bytes)]:
            with self.subTest(factory=factory.__name__, text=text.__name__), tempfile.TemporaryDirectory() as directory:
                current = adapter(Path(directory)); store, receipts = current.workflow.store, current._receipts
                request = {**COPIES[0], 'expected_version': 0}
                old = current.confirm_roles(CONTEXT, request)
                connection = store._connection
                @contextmanager
                def injected():
                    with connection() as db:
                        db.row_factory, db.text_factory = factory, text
                        yield db
                before = store.path.read_bytes()
                with patch.object(store, '_connection', injected):
                    if factory is sqlite3.Row and text is str:
                        self.assertEqual(store.application_record('p', 'a'), old['applied_profile'])
                        self.assertEqual(store.latest('a')['version'], 1)
                    else:
                        for operation in [lambda: store.latest('a'), lambda: store.save_version('a', old['resolved_roles'], expected_version=1)]:
                            with self.assertRaisesRegex(ValueError, 'unsupported SQLite row or text factory'):
                                operation()
                self.assertEqual(store.path.read_bytes(), before)
                connect = receipts._connect
                def journal():
                    db = connect(); db.row_factory, db.text_factory = factory, text; return db
                with patch.object(receipts, '_connect', journal):
                    with self.assertRaises(ValueError):
                        current.confirm_roles(CONTEXT, request)
                self.assertEqual(current.confirm_roles(CONTEXT, request), old)
                self.assertEqual(store.save_version('a', old['resolved_roles'], expected_version=1)['version'], 2)
                self.assertEqual(store.application_record('p', 'a'), old['applied_profile'])
