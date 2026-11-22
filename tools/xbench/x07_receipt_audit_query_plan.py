"""Measure SELECT statements executed by the offline receipt audit."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
from x07_impact_receipt_audit_perf import prepare_fixture
from x07_receipt_history_audit import CustomerProfileStore, ConfirmationReceiptStore, audit_receipts, publish_stdout
from x07_profile_version_diff import JsonArgumentParser


def measure_plans(directory, *, smoke=False):
    cases = []
    for foreign_count in ([0, 16] if smoke else [0, 100, 1000]):
        with tempfile.TemporaryDirectory(dir=directory, prefix='x07-receipt-plans-') as scratch:
            profile, journal = Path(scratch) / 'profile.sqlite', Path(scratch) / 'journal.sqlite'
            pins, _, _ = prepare_fixture(profile, journal, 1, foreign_count)
            with closing(sqlite3.connect(journal)) as connection, connection:
                connection.execute('INSERT INTO confirmations VALUES (?,?,?,?)', ('invalid', 'a', '0' * 64, '{'))
            store, receipts = CustomerProfileStore(profile, read_only=True), ConfirmationReceiptStore(journal, read_only=True)
            original_profile, original_journal = store._connection, receipts._connect
            before = profile.read_bytes(), journal.read_bytes()
            statements = {}

            class Observed:
                def __init__(self, connection, side):
                    self.connection, self.side = connection, side

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def execute(self, sql, parameters=()):
                    if sql.startswith('SELECT ') and 'sqlite_temp_master' not in sql:
                        statements[(self.side, sql)] = parameters
                    return self.connection.execute(sql, parameters)

            @contextmanager
            def profile_reader():
                with original_profile() as connection:
                    yield Observed(connection, 'profile')

            store._connection = profile_reader
            receipts._connect = lambda: Observed(original_journal(), 'journal')
            try:
                report = audit_receipts(store, receipts, customer_key='a')
            finally:
                store._connection, receipts._connect = original_profile, original_journal
            expected = {'invalid_receipts': 1, 'missing_references': 0, 'inconsistent_references': 0}
            if report['receipt_count'] != 2 or report['healthy'] or report['issues'] != expected:
                raise ValueError('independent receipt oracle mismatch')
            plans = []
            for (side, sql), parameters in sorted(statements.items()):
                with closing(sqlite3.connect((profile if side == 'profile' else journal).as_uri() + '?mode=ro', uri=True)) as connection:
                    details = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql, parameters)]
                plans.append({'store': side, 'statement': sql, 'details': details,
                              'foreign_table_scan': any(detail.upper().startswith('SCAN ') for detail in details)})
            if len(plans) != 5:
                raise ValueError('unexpected actual SELECT inventory')
            for customer, pin in pins.items():
                if store.application_record('old-' + customer, customer) != pin:
                    raise ValueError('old pin changed')
            if (profile.read_bytes(), journal.read_bytes()) != before:
                raise ValueError('store bytes changed')
            cases.append({'additional_foreign_history': foreign_count, 'audit': report, 'plans': plans,
                          'old_pins_preserved': True, 'store_bytes_preserved': True})
    return {'schema': 'leaf.x07.receipt-audit-query-plan.v1', 'scope': 'synthetic settled offline stores',
            'full_foreign_table_scan': any(plan['foreign_table_scan'] for case in cases for plan in case['plans']), 'cases': cases}


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.receipt-audit-query-plan.v1', description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    arguments = parser.parse_args()
    try:
        report = measure_plans(arguments.directory, smoke=arguments.smoke)
    except (OSError, ValueError, sqlite3.Error):
        publish_stdout({'schema': 'leaf.x07.receipt-audit-query-plan.v1', 'error': 'MEASUREMENT_UNAVAILABLE'})
        return 2
    return publish_stdout(report)


if __name__ == '__main__':
    raise SystemExit(main())
