from contextlib import closing, contextmanager
import json
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditNoWriteAuthorizer(unittest.TestCase):
    def test_both_connections_attempt_only_reads_for_clean_and_inconsistent_results(self):
        names = ['INSERT', 'UPDATE', 'DELETE', 'CREATE_INDEX', 'CREATE_TABLE', 'CREATE_TRIGGER', 'CREATE_VIEW',
                 'CREATE_TEMP_INDEX', 'CREATE_TEMP_TABLE', 'CREATE_TEMP_TRIGGER', 'CREATE_TEMP_VIEW',
                 'DROP_INDEX', 'DROP_TABLE', 'DROP_TRIGGER', 'DROP_VIEW', 'DROP_TEMP_INDEX', 'DROP_TEMP_TABLE',
                 'DROP_TEMP_TRIGGER', 'DROP_TEMP_VIEW', 'ALTER_TABLE', 'REINDEX', 'ANALYZE', 'ATTACH', 'DETACH']
        forbidden = {getattr(sqlite3, 'SQLITE_' + name) for name in names}
        for inconsistent in [False, True]:
            with self.subTest(inconsistent=inconsistent), tempfile.TemporaryDirectory() as directory:
                profile, journal, read, receipts, pins = seed(directory)
                if inconsistent:
                    with closing(sqlite3.connect(journal)) as connection, connection:
                        value = json.loads(connection.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0])
                        value['resolved_roles']['module']['layer'] = 'WRONG'
                        connection.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (json.dumps(value),))
                before = profile.read_bytes(), journal.read_bytes()
                original_profile, original_journal = read._connection, receipts._connect
                attempts = {'profile': [], 'journal': []}
                changes = {'profile': [], 'journal': []}

                def authorizer(side):
                    def check(action, name, argument, *rest):
                        if action in forbidden or (action == sqlite3.SQLITE_PRAGMA and argument is not None):
                            attempts[side].append((action, name, argument))
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK
                    return check

                @contextmanager
                def profile_factory():
                    with original_profile() as connection:
                        connection.set_authorizer(authorizer('profile'))
                        try:
                            yield connection
                        finally:
                            connection.set_authorizer(None)
                            changes['profile'].append(connection.total_changes)

                class JournalProxy:
                    def __init__(self, connection):
                        self.connection = connection
                    def __getattr__(self, name):
                        return getattr(self.connection, name)
                    def close(self):
                        self.connection.set_authorizer(None)
                        changes['journal'].append(self.connection.total_changes)
                        self.connection.close()

                def journal_factory():
                    connection = original_journal()
                    connection.set_authorizer(authorizer('journal'))
                    return JournalProxy(connection)

                read._connection, receipts._connect = profile_factory, journal_factory
                try:
                    result = audit.audit_receipts(read, receipts, customer_key='a')
                finally:
                    read._connection, receipts._connect = original_profile, original_journal
                self.assertEqual(result, {'schema': 'leaf.x07.receipt-history-audit.v1', 'customer_key': 'a', 'receipt_count': 1,
                                         'healthy': not inconsistent, 'issues': {'invalid_receipts': 0, 'missing_references': 0,
                                         'inconsistent_references': int(inconsistent)}})
                self.assertEqual(attempts, {'profile': [], 'journal': []})
                self.assertEqual(changes, {'profile': [0], 'journal': [0]})
                for key, pin in pins.items():
                    self.assertEqual(read.application_record('old-' + key, key), pin)
                self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
