from contextlib import closing, contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditConnectionPolicy(unittest.TestCase):
    def cli(self, profile, journal, read, receipts):
        output = io.StringIO()
        arguments = ['audit', '--database', str(profile), '--journal', str(journal), '--customer', 'a']
        with patch.object(sys, 'argv', arguments), patch.object(audit, 'CustomerProfileStore', return_value=read), patch.object(audit, 'ConfirmationReceiptStore', return_value=receipts), redirect_stdout(output):
            code = audit.main()
        return code, json.loads(output.getvalue())

    def test_row_and_text_factory_policy_closes_owned_read_connections(self):
        for side in ['profile', 'journal']:
            cases = [(sqlite3.Row, str, True), (None, str, side == 'journal'),
                     (lambda cursor, row: dict(zip([item[0] for item in cursor.description], row)), str, False),
                     (sqlite3.Row, bytes, False), (sqlite3.Row, lambda value: value.decode(), False)]
            for row_factory, text_factory, accepted in cases:
                with self.subTest(store=side, row=row_factory, text=text_factory), tempfile.TemporaryDirectory() as directory:
                    profile, journal, read, receipts, pins = seed(directory)
                    baseline = audit.audit_receipts(read, receipts, customer_key='a')
                    before = profile.read_bytes(), journal.read_bytes()
                    original_profile, original_journal = read._connection, receipts._connect
                    connections, changes = [], []

                    @contextmanager
                    def profile_factory():
                        with original_profile() as connection:
                            connections.append(connection)
                            connection.row_factory, connection.text_factory = row_factory, text_factory
                            try:
                                yield connection
                            finally:
                                changes.append(connection.total_changes)

                    class OwnedJournal:
                        def __init__(self, connection):
                            self.connection = connection
                        def __getattr__(self, name):
                            return getattr(self.connection, name)
                        def close(self):
                            changes.append(self.connection.total_changes)
                            self.connection.close()

                    def journal_factory():
                        connection = original_journal()
                        connections.append(connection)
                        connection.row_factory, connection.text_factory = row_factory, text_factory
                        return OwnedJournal(connection)

                    if side == 'profile':
                        read._connection = profile_factory
                    else:
                        receipts._connect = journal_factory
                    try:
                        if accepted:
                            self.assertEqual(audit.audit_receipts(read, receipts, customer_key='a'), baseline)
                        else:
                            with self.assertRaises(ValueError):
                                audit.audit_receipts(read, receipts, customer_key='a')
                        code, value = self.cli(profile, journal, read, receipts)
                        self.assertEqual(code, 0 if accepted else 2)
                        self.assertEqual(value, baseline if accepted else {'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'})
                    finally:
                        read._connection, receipts._connect = original_profile, original_journal
                    self.assertEqual(changes, [0, 0])
                    for connection in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            connection.execute('SELECT 1')
                    for key, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + key, key), pin)
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)

    def test_dirty_readers_refuse_pending_repairs_on_either_store(self):
        for side in ['profile', 'journal']:
            with self.subTest(store=side), tempfile.TemporaryDirectory() as directory:
                profile, journal, read, receipts, pins = seed(directory)
                with closing(sqlite3.connect(journal)) as connection:
                    original_result = connection.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0]
                with closing(sqlite3.connect(journal)) as connection, connection:
                    if side == 'profile':
                        extra = json.loads(original_result)
                        extra['project_key'] = 'extra-a'
                        extra['applied_profile']['project_key'] = 'extra-a'
                        connection.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('extra-a', 'a', '0' * 64, json.dumps(extra)))
                    else:
                        changed = deepcopy(json.loads(original_result))
                        changed['resolved_roles']['module']['layer'] = 'WRONG'
                        connection.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (json.dumps(changed),))
                baseline = audit.audit_receipts(read, receipts, customer_key='a')
                self.assertFalse(baseline['healthy'])
                self.assertEqual(baseline['issues'], {'invalid_receipts': 0, 'missing_references': int(side == 'profile'), 'inconsistent_references': int(side == 'journal')})
                before = profile.read_bytes(), journal.read_bytes()
                database = profile if side == 'profile' else journal
                writer = sqlite3.connect(database.resolve().as_uri() + '?mode=rw&cache=shared', uri=True)
                original_profile, original_journal = read._connection, receipts._connect
                connections, observed = [], []

                def dirty_reader():
                    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro&cache=shared', uri=True)
                    connections.append(connection)
                    connection.row_factory = sqlite3.Row
                    connection.execute('PRAGMA foreign_keys=ON')
                    connection.execute('PRAGMA read_uncommitted=ON')
                    if side == 'profile':
                        observed.append(connection.execute("SELECT project_key FROM project_applications WHERE project_key='extra-a'").fetchone()[0])
                    else:
                        observed.append(connection.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0])
                    return connection

                @contextmanager
                def profile_factory():
                    connection = dirty_reader()
                    try:
                        yield connection
                    finally:
                        connection.close()

                try:
                    writer.execute('BEGIN IMMEDIATE')
                    if side == 'profile':
                        writer.execute('INSERT INTO project_applications VALUES (?,?,?,?)', ('extra-a', 'a', pins['a']['profile_id'], 1))
                        read._connection = profile_factory
                    else:
                        writer.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (original_result,))
                        receipts._connect = dirty_reader
                    # Independently prove the unsafe reader can hide the
                    # committed finding, before the settled-file preflight
                    # refuses without acquiring any injected reader.
                    probe = dirty_reader()
                    probe.close()
                    with self.assertRaises(ValueError):
                        audit.audit_receipts(read, receipts, customer_key='a')
                    code, value = self.cli(profile, journal, read, receipts)
                    self.assertEqual(code, 2)
                    self.assertEqual(value['error'], 'AUDIT_UNAVAILABLE')
                    self.assertEqual(observed, ['extra-a' if side == 'profile' else original_result])
                    self.assertTrue(writer.in_transaction)
                finally:
                    read._connection, receipts._connect = original_profile, original_journal
                    writer.rollback()
                    writer.close()
                    for connection in connections:
                        connection.close()
                self.assertEqual(audit.audit_receipts(read, receipts, customer_key='a'), baseline)
                for key, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + key, key), pin)
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
