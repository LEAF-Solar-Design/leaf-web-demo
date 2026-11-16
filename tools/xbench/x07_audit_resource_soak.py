"""Bounded offline disposable audit loop with owned connection accounting."""
from contextlib import contextmanager
import ctypes
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
from x07_audit_query_plan import fixture
from x07_profile_history_audit import CustomerProfileStore, audit_history, publish_stdout
from x07_profile_version_diff import JsonArgumentParser


def handles():
    if hasattr(ctypes,'windll'):
        count=ctypes.c_ulong()
        kernel=ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype=ctypes.c_void_p
        kernel.GetProcessHandleCount.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(count)): raise OSError('handle count unavailable')
        return count.value
    return len(list(Path('/proc/self/fd').iterdir())) if Path('/proc/self/fd').is_dir() else None


def measure_resources(directory, *, iterations=100):
    if type(iterations) is not int or not 1<=iterations<=1000: raise ValueError('iteration budget')
    own_trace=not tracemalloc.is_tracing()
    if own_trace: tracemalloc.start()
    try:
        with tempfile.TemporaryDirectory(dir=directory,prefix='x07-soak-') as scratch:
            path=Path(scratch)/'store.sqlite'; old=fixture(path,0)
            store=CustomerProfileStore(path,read_only=True); original=store._connection
            before=path.read_bytes(); counts={'opened':0,'closed':0,'active':0}; samples=[]
            mode='clean'
            @contextmanager
            def tracked():
                with original() as db:
                    counts['opened']+=1; counts['active']+=1
                    try:
                        if mode=='refused': db.row_factory=None
                        yield db
                    finally: counts['active']-=1; counts['closed']+=1
            store._connection=tracked
            start_handles=handles()
            try:
                for index in range(iterations):
                    for mode in ('clean','corrupt','refused'):
                        if mode=='refused':
                            try: audit_history(store,customer_key='target')
                            except ValueError: pass
                            else: raise ValueError('refusal oracle mismatch')
                        else:
                            report=audit_history(store,customer_key='target' if mode=='clean' else 'corrupt')
                            expected=[] if mode=='clean' else [{'first':2,'last':2}]
                            if report['healthy']!=(mode=='clean') or report['issues']['version_gaps']!=expected: raise ValueError('findings oracle mismatch')
                            if report['version_count']!=(1 if mode=='clean' else 2) or report['pin_count']!=(1 if mode=='clean' else 0): raise ValueError('record oracle mismatch')
                            if any(value for key,value in report['issues'].items() if key!='version_gaps'): raise ValueError('unexpected finding')
                    if counts['active'] or counts['opened']!=counts['closed']: raise ValueError('owned connection leak')
                    if index in {0,iterations-1}:
                        current,peak=tracemalloc.get_traced_memory()
                        samples.append({'iteration':index+1,'memory_current_bytes':current,'memory_peak_bytes':peak,'handles_after_cleanup':handles(),'active_connections':counts['active']})
            finally: store._connection=original
            if store.application_record('old','target')!=old or path.read_bytes()!=before: raise ValueError('retained pin oracle mismatch')
            return {'schema':'leaf.x07.audit-resource-soak.v1','scope':'synthetic offline, audit-owned connections only','iterations':iterations,
                    'connections':counts,'handles_before':start_handles,'handles_after_cleanup':handles(),'memory_samples':samples,'pins_preserved':True}
    finally:
        if own_trace: tracemalloc.stop()


def main():
    parser=JsonArgumentParser(result_schema='leaf.x07.audit-resource-soak.v1', description=__doc__)
    parser.add_argument('--directory',type=Path,required=True); parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    try: result=measure_resources(args.directory,iterations=3 if args.smoke else 100)
    except (OSError,ValueError,sqlite3.Error):
        publish_stdout({'schema':'leaf.x07.audit-resource-soak.v1','error':'MEASUREMENT_UNAVAILABLE'}); return 2
    return publish_stdout(result)


if __name__ == '__main__': raise SystemExit(main())
