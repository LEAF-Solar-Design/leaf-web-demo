from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditConnectionPolicy(unittest.TestCase):
    def refuse_cli(self, path, read):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['audit', '--database', str(path), '--customer', 'a']), patch.object(audit, 'CustomerProfileStore', return_value=read), redirect_stdout(output):
            self.assertEqual(audit.main(), 2)
        self.assertEqual(json.loads(output.getvalue()), {'schema':'leaf.x07.profile-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
        self.assertLess(len(output.getvalue()), 200)

    def seed(self, path):
        store = audit.CustomerProfileStore(path)
        saved = store.save_version('a', {'module': {'block':'PRIVATE','layer':'M'}}, expected_version=0)
        old = store.apply_to_project('old', 'a')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)', ('a',saved['profile_id'],3,'{}','synthetic'))
        return saved, old

    def test_row_and_text_factories_refuse_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'store.sqlite'
            _, old = self.seed(path)
            read = audit.CustomerProfileStore(path, read_only=True)
            baseline = audit.audit_history(read, customer_key='a')
            self.assertFalse(baseline['healthy']); self.assertEqual(baseline['issues']['version_gaps'], [{'first':2,'last':2}])
            original = read._connection
            before = path.read_bytes()
            cases = [(None,str), (lambda cursor,row: dict(zip([c[0] for c in cursor.description],row)),str), (sqlite3.Row,bytes), (sqlite3.Row,lambda b:b.decode())]
            for row_factory, text_factory in cases:
                with self.subTest(row=row_factory, text=text_factory):
                    changes = []
                    @contextmanager
                    def altered():
                        with original() as db:
                            db.row_factory, db.text_factory = row_factory, text_factory
                            try: yield db
                            finally: changes.append(db.total_changes)
                    read._connection = altered
                    try:
                        with self.assertRaises(ValueError): audit.audit_history(read, customer_key='a')
                        self.refuse_cli(path, read)
                    finally: read._connection = original
                    self.assertEqual(changes, [0,0])
                    self.assertEqual(audit.audit_history(read, customer_key='a'), baseline)
            self.assertEqual(read.application_record('old','a'), old)
            self.assertEqual(path.read_bytes(), before)

    def test_shared_cache_dirty_gap_fill_never_reports_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'store.sqlite'
            saved, old = self.seed(path)
            read = audit.CustomerProfileStore(path, read_only=True)
            baseline = audit.audit_history(read, customer_key='a')
            before = path.read_bytes()
            writer = sqlite3.connect(path.resolve().as_uri()+'?mode=rw&cache=shared', uri=True)
            reader = sqlite3.connect(path.resolve().as_uri()+'?mode=ro&cache=shared', uri=True)
            original = read._connection
            @contextmanager
            def dirty(): yield reader
            try:
                reader.row_factory = sqlite3.Row
                reader.execute('PRAGMA foreign_keys=ON'); reader.execute('PRAGMA read_uncommitted=ON')
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)', ('a',saved['profile_id'],2,'PRIVATE_PENDING','synthetic'))
                self.assertEqual(reader.execute('SELECT version FROM profile_versions WHERE version=2').fetchone()[0], 2)
                read._connection = dirty
                with self.assertRaises(ValueError): audit.audit_history(read, customer_key='a')
                self.refuse_cli(path, read)
                self.assertEqual(reader.total_changes, 0)
            finally:
                read._connection = original
                writer.rollback(); reader.close(); writer.close()
            self.assertEqual(audit.audit_history(read, customer_key='a'), baseline)
            self.assertFalse(baseline['healthy'])
            self.assertEqual(read.application_record('old','a'), old)
            self.assertEqual(path.read_bytes(), before)
