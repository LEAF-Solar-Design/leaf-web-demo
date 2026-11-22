"""Bounded disposable benchmark for X07 scoped impact and receipt audits."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import tracemalloc
from x07_profile_change_impact import CustomerProfileStore, preview_change
from x07_receipt_history_audit import ConfirmationReceiptStore, audit_receipts, publish_stdout
from x07_profile_workflow import ProfileWorkflow
from x07_profile_version_diff import JsonArgumentParser


def receipt_result(pin):
    context = {'customer_key': pin['customer_key'], 'project_key': pin['project_key'],
               'drawing_key': 'synthetic-' + pin['project_key']}
    return ProfileWorkflow._result(context, pin, pin['roles'], {'count': 1})


def prepare_fixture(profile, journal, scoped_count, foreign_count):
    store = CustomerProfileStore(profile)
    receipts = ConfirmationReceiptStore(journal)
    pins = {}
    for customer, block in [('a', 'A'), ('b', 'FOREIGN_B')]:
        store.save_version(customer, {'module': {'block': block, 'layer': 'M'}}, expected_version=0)
        pin = store.apply_to_project('old-' + customer, customer)
        pins[customer] = pin
        context = {'customer_key': customer, 'project_key': pin['project_key'], 'drawing_key': 'synthetic-' + pin['project_key']}
        receipts.run(context, '0' * 64, lambda: receipt_result(pin), lambda value: None)
    saved = store.save_version('a', {'module': {'block': 'A2', 'layer': 'M'}}, expected_version=1)
    journal_rows = []
    profile_rows = []
    scoped_projects = {'old-a'}
    # Bulk rows retain the schema and immutable snapshots produced above.
    for customer, count in [('a', scoped_count - 1), ('b', foreign_count)]:
        for index in range(count):
            project = f'extra-{customer}-{index}'
            pin = deepcopy(pins[customer])
            pin['project_key'] = project
            profile_rows.append((project, customer, pin['profile_id'], 1))
            journal_rows.append((project, customer, '0' * 64, json.dumps(receipt_result(pin))))
            if customer == 'a':
                scoped_projects.add(project)
    with closing(sqlite3.connect(profile)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executemany('INSERT INTO project_applications VALUES (?,?,?,?)', profile_rows)
        connection.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)',
                               (('b', pins['b']['profile_id'], version, json.dumps(pins['b']['roles']), 'synthetic')
                                for version in range(2, foreign_count + 2)))
    with closing(sqlite3.connect(journal)) as connection, connection:
        connection.executemany('INSERT INTO confirmations VALUES (?,?,?,?)', journal_rows)
    return pins, saved['profile_id'], scoped_projects


def benchmark(directory, *, smoke=False):
    scoped_counts = [1, 8] if smoke else [1, 100, 1000]
    foreign_counts = [0, 16] if smoke else [0, 100, 1000]
    owns_tracing = not tracemalloc.is_tracing()
    if owns_tracing:
        tracemalloc.start()
    cases = []
    try:
        for scoped_count in scoped_counts:
            for foreign_count in foreign_counts:
                with tempfile.TemporaryDirectory(dir=directory, prefix='x07-impact-receipt-perf-') as temporary:
                    profile = Path(temporary) / 'profile.sqlite'
                    journal = Path(temporary) / 'journal.sqlite'
                    pins, profile_id, projects = prepare_fixture(profile, journal, scoped_count, foreign_count)
                    read = CustomerProfileStore(profile, read_only=True)
                    receipts = ConfirmationReceiptStore(journal, read_only=True)
                    before = profile.read_bytes(), journal.read_bytes()
                    started = time.perf_counter()
                    impact = preview_change(read, customer_key='a', profile_id=profile_id, before_version=1, after_version=2)
                    impact_seconds = time.perf_counter() - started
                    started = time.perf_counter()
                    receipt_audit = audit_receipts(read, receipts, customer_key='a')
                    receipt_seconds = time.perf_counter() - started
                    current_memory, peak_memory = tracemalloc.get_traced_memory()
                    expected_impact = [{'project_key': project, 'version': 1, 'different_roles': ['module']}
                                       for project in sorted(projects)]
                    expected_change = {'module': {'before': {'block': 'A', 'layer': 'M'},
                                                  'after': {'block': 'A2', 'layer': 'M'}}}
                    if (impact['retaining_different_roles'] != expected_impact or impact['changed'] != expected_change
                            or impact['added'] or impact['removed']):
                        raise ValueError('impact oracle mismatch')
                    expected_issues = {'invalid_receipts': 0, 'missing_references': 0, 'inconsistent_references': 0}
                    if receipt_audit['receipt_count'] != scoped_count or not receipt_audit['healthy'] or receipt_audit['issues'] != expected_issues:
                        raise ValueError('receipt oracle mismatch')
                    for customer, pin in pins.items():
                        if read.application_record('old-' + customer, customer) != pin:
                            raise ValueError('old pin changed')
                    if (profile.read_bytes(), journal.read_bytes()) != before:
                        raise ValueError('audit changed store bytes')
                    cases.append({'scoped_pins': scoped_count, 'scoped_receipts': scoped_count,
                                  'additional_foreign_history': foreign_count,
                                  'impact_seconds': round(impact_seconds, 6), 'receipt_audit_seconds': round(receipt_seconds, 6),
                                  'traced_current_bytes': current_memory, 'traced_peak_bytes': peak_memory,
                                  'retaining_projects': len(impact['retaining_different_roles']),
                                  'receipt_issues': expected_issues, 'old_pins_preserved': True, 'store_bytes_preserved': True})
        return {'schema': 'leaf.x07.impact-receipt-audit-perf.v1', 'scope': 'disposable synthetic offline stores',
                'memory_scope': 'process traced allocations during benchmark', 'cases': cases}
    finally:
        if owns_tracing:
            tracemalloc.stop()


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.impact-receipt-audit-perf.v1', description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    arguments = parser.parse_args()
    try:
        result = benchmark(arguments.directory, smoke=arguments.smoke)
    except (OSError, ValueError, sqlite3.Error):
        publish_stdout({'schema': 'leaf.x07.impact-receipt-audit-perf.v1', 'error': 'MEASUREMENT_UNAVAILABLE'})
        return 2
    return publish_stdout(result)


if __name__ == '__main__':
    raise SystemExit(main())
