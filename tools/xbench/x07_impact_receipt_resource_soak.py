"""Offline disposable impact and receipt loops with owned resource accounting."""
from contextlib import contextmanager
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
from x07_audit_resource_soak import handles
from x07_impact_receipt_audit_perf import prepare_fixture
from x07_profile_change_impact import CustomerProfileStore, preview_change
from x07_receipt_history_audit import ConfirmationReceiptStore, audit_receipts, publish_stdout
from x07_profile_version_diff import JsonArgumentParser


def measure_resources(directory, *, iterations=100):
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ValueError('iteration budget')
    own_trace = not tracemalloc.is_tracing()
    if own_trace:
        tracemalloc.start()
    counts = {'opened': 0, 'closed': 0, 'active': 0}
    samples = []
    try:
        with tempfile.TemporaryDirectory(dir=directory, prefix='x07-impact-receipt-soak-') as scratch:
            root = Path(scratch)
            paths = []
            for mode in ('clean', 'inconsistent'):
                profile, journal = root / (mode + '.sqlite'), root / (mode + '-journal.sqlite')
                pins, identity, _ = prepare_fixture(profile, journal, 1, 0)
                if mode == 'inconsistent':
                    with sqlite3.connect(journal) as connection:
                        connection.execute("UPDATE confirmations SET result=replace(result, 'A2', 'TAMPERED')")
                        value = json.loads(connection.execute("SELECT result FROM confirmations WHERE project_key='old-a'").fetchone()[0])
                        value['resolved_roles']['module']['block'] = 'TAMPERED'
                        connection.execute("UPDATE confirmations SET result=? WHERE project_key='old-a'", (json.dumps(value),))
                    connection.close()
                paths.append((mode, profile, journal, pins, identity))
            start_handles = handles()
            originals = {}
            readers = {}
            for mode, profile, journal, pins, identity in paths:
                store = CustomerProfileStore(profile, read_only=True)
                receipts = ConfirmationReceiptStore(journal, read_only=True)
                originals[mode] = (profile.read_bytes(), journal.read_bytes())
                profile_connect, journal_connect = store._connection, receipts._connect

                @contextmanager
                def tracked_profile(connect=profile_connect):
                    with connect() as connection:
                        counts['opened'] += 1
                        counts['active'] += 1
                        try:
                            yield connection
                        finally:
                            counts['active'] -= 1
                            counts['closed'] += 1

                class TrackedJournal:
                    def __init__(self, connection):
                        self.connection = connection
                        self.closed = False
                        counts['opened'] += 1
                        counts['active'] += 1

                    def __getattr__(self, name):
                        return getattr(self.connection, name)

                    def close(self):
                        if not self.closed:
                            self.connection.close()
                            self.closed = True
                            counts['active'] -= 1
                            counts['closed'] += 1

                store._connection = tracked_profile
                receipts._connect = lambda connect=journal_connect: TrackedJournal(connect())
                readers[mode] = store, receipts
            for index in range(iterations):
                for mode, profile, journal, pins, identity in paths:
                    store, receipts = readers[mode]
                    impact = preview_change(store, customer_key='a', profile_id=identity, before_version=1, after_version=2)
                    expected = [{'project_key': 'old-a', 'version': 1, 'different_roles': ['module']}]
                    if impact['retaining_different_roles'] != expected:
                        raise ValueError('impact oracle mismatch')
                    report = audit_receipts(store, receipts, customer_key='a')
                    issues = {'invalid_receipts': 0, 'missing_references': 0, 'inconsistent_references': int(mode == 'inconsistent')}
                    if report['issues'] != issues or report['healthy'] != (mode == 'clean'):
                        raise ValueError('receipt oracle mismatch')
                    for operation in (
                        lambda: preview_change(store, customer_key='b', profile_id=identity, before_version=1, after_version=2),
                        lambda: audit_receipts(store, receipts, customer_key=''),
                    ):
                        try:
                            operation()
                        except ValueError:
                            pass
                        else:
                            raise ValueError('refusal oracle mismatch')
                if counts['active'] or counts['opened'] != counts['closed']:
                    raise ValueError('owned connection leak')
                if index in (0, iterations - 1):
                    gc.collect()
                    current, peak = tracemalloc.get_traced_memory()
                    samples.append({'iteration': index + 1, 'memory_current_bytes': current, 'memory_peak_bytes': peak,
                                    'handles_after_cleanup': handles(), 'active_connections': counts['active']})
            for mode, profile, journal, pins, identity in paths:
                store, _ = readers[mode]
                for customer, pin in pins.items():
                    if store.application_record('old-' + customer, customer) != pin:
                        raise ValueError('pin changed')
                if (profile.read_bytes(), journal.read_bytes()) != originals[mode]:
                    raise ValueError('store bytes changed')
            return {'schema': 'leaf.x07.impact-receipt-resource-soak.v1', 'iterations': iterations,
                    'scope': 'synthetic offline; owned connection accounting, process handle and traced allocation samples',
                    'connections': counts, 'handles_before': start_handles, 'handles_after_cleanup': handles(),
                    'memory_samples': samples, 'pins_preserved': True, 'store_bytes_preserved': True}
    finally:
        if own_trace:
            tracemalloc.stop()


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.impact-receipt-resource-soak.v1', description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    arguments = parser.parse_args()
    try:
        report = measure_resources(arguments.directory, iterations=3 if arguments.smoke else 100)
    except (OSError, ValueError, sqlite3.Error):
        publish_stdout({'schema': 'leaf.x07.impact-receipt-resource-soak.v1', 'error': 'MEASUREMENT_UNAVAILABLE'})
        return 2
    return publish_stdout(report)


if __name__ == '__main__':
    raise SystemExit(main())
