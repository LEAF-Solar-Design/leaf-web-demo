"""Bounded offline constructor/callback refusal resource measurement."""
import argparse
from contextlib import closing
import ctypes
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import tracemalloc
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter, ConfirmationReceiptStore


def handles():
    if os.name == 'nt':
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
            raise OSError('own process handle measurement unavailable')
        return count.value
    return len(os.listdir('/proc/self/fd'))


def run(iterations):
    if type(iterations) is not int or not 1 <= iterations <= 1024:
        raise ValueError('iterations must be1..1024')
    active = set()
    measured = {'maximum_connections': 0, 'opened_connections': 0}
    connect = sqlite3.connect
    class Tracked(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            active.add(id(self)); measured['opened_connections'] += 1
            measured['maximum_connections'] = max(measured['maximum_connections'], len(active))
        def close(self):
            try: super().close()
            finally: active.discard(id(self))
    def tracked(*args, **kwargs): return connect(*args, factory=Tracked, **kwargs)
    with tempfile.TemporaryDirectory(prefix='x07-refusal-soak-') as directory:
        root = Path(directory)
        profile, journal = root / 'profiles.sqlite', root / 'receipts.sqlite'
        store = CustomerProfileStore(profile)
        roles = {'module': {'block': 'Panel', 'layer': 'M'}}
        store.save_version('a', roles, expected_version=0)
        pin = store.apply_to_project('old', 'a', expected_version=1)
        ConfirmationReceiptStore(journal)
        bad = root / 'unsupported.sqlite'
        with closing(connect(bad)) as db, db: db.execute('PRAGMA user_version=77')
        before = [path.read_bytes() for path in [profile, journal]]
        context = dict(customer_key='a', project_key='old', drawing_key='d:v1')
        intake = {'inserts': [dict(name='Panel', layer='M', x=0, y=0)]}
        copies = [dict(block='Panel', layer='M', design_window=[0, 0, 0, 0], oracle_count=1)]
        gc.collect(); start_handles = handles()
        tracemalloc.start(); baseline = tracemalloc.get_traced_memory()[0]
        refusals = 0
        try:
            with patch.object(sqlite3, 'connect', tracked):
                for _ in range(iterations):
                    current = WorkflowAdapter.from_paths(profile_path=profile, receipt_path=journal,
                        context=context, intake=intake, copies=copies)
                    readonly = ConfirmationReceiptStore(journal, read_only=True)
                    actions = [lambda: CustomerProfileStore(bad), lambda: ConfirmationReceiptStore(bad),
                        lambda: WorkflowAdapter(current.workflow, context=context, intake=intake,
                            copies=[{**copies[0], 'oracle_count': 99}]),
                        lambda: current.load_workflow({**context, 'customer_key': 'foreign'}),
                        lambda: current.load_profile('foreign'),
                        lambda: current.save_profile('a', roles, expected_version=0),
                        lambda: current.confirm_roles(context, {**copies[0], 'expected_version': 0}),
                        lambda: readonly.run(context, '0'*64, lambda: (_ for _ in ()).throw(AssertionError('producer must not run')),
                                             lambda value: None)]
                    for action in actions:
                        try: action()
                        except ValueError: refusals += 1
                        else: raise AssertionError('expected refusal')
                    assert not active, 'connection survived a refusal cycle'
                del current, readonly, actions, action
                gc.collect()
                retained, peak = tracemalloc.get_traced_memory()
                end_handles = handles()
            assert not active
            assert end_handles <= start_handles + 4
            assert retained - baseline < 8 * 1024 * 1024
            assert measured['maximum_connections'] <= 2
            assert refusals == iterations * 8
            assert store.application_record('old', 'a') == pin
            assert store.latest('a')['version'] == 1
            assert [path.read_bytes() for path in [profile, journal]] == before
            return {'schema': 'leaf.x07.failure-resource-soak.v1', 'proof_mode': 'synthetic_offline',
                'state': 'PASS', 'iterations': iterations, 'refusals': refusals,
                **measured, 'open_connections_after_cleanup': len(active),
                'handles_before': start_handles, 'handles_after': end_handles,
                'retained_bytes': max(0, retained-baseline), 'peak_traced_bytes': peak,
                'old_pin_version': 1, 'unchanged_store_bytes': True,
                'source_file_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        finally: tracemalloc.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=256)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    result = run(min(args.iterations, 8) if args.smoke else args.iterations)
    encoded = json.dumps(result, sort_keys=True) + '\n'
    if args.out: args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return 0


if __name__ == '__main__': raise SystemExit(main())
