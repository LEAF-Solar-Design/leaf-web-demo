# X07 Windows snapshot replay

This recipe belongs to Studio X-bench X07's offline history-reader checks.
The historical unchanged-file refusal is unresolved. Injected metadata
divergence reproduces the refusal rule, but does not establish the cause of
that earlier native observation. No timestamp or identity check was relaxed.

From the repository root, run this bounded recipe. It uses disposable D: scratch
and captures the exact compared fields before cleanup.

```powershell
@'
from pathlib import Path
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, 'tools/xbench')
from x07_history_reducer import read_history
fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
scratch = Path('D:/tmp')
scratch.mkdir(exist_ok=True)
tempfile.tempdir = str(scratch)
real_fstat, real_lstat = os.fstat, Path.lstat
with tempfile.TemporaryDirectory(prefix='x07-snapshot-', dir=scratch) as directory:
    path = Path(directory) / 'history.json'
    payload = b'[{"op":"noise"}]'
    path.write_bytes(payload)
    observations = []
    def observed_fstat(descriptor):
        value = real_fstat(descriptor)
        observations.append({'source': 'descriptor', 'fields': {key: getattr(value, key) for key in fields}})
        return value
    def observed_lstat(current, *args, **kwargs):
        value = real_lstat(current, *args, **kwargs)
        if current == path:
            observations.append({'source': 'path', 'fields': {key: getattr(value, key) for key in fields}})
        return value
    with patch.object(os, 'fstat', side_effect=observed_fstat), patch.object(Path, 'lstat', observed_lstat):
        try:
            assert read_history(path) == json.loads(payload)
            outcome = 'VERIFIED_SNAPSHOT'
        except ValueError as error:
            assert str(error) == 'history changed during read'
            outcome = 'METADATA_REFUSAL'
    print(json.dumps({'unchanged_outcome': outcome, 'observations': observations}, sort_keys=True))
    assert path.read_bytes() == payload
    if outcome == 'METADATA_REFUSAL':
        assert any(row['fields'] != observations[1]['fields'] for row in observations[2:])

spec = importlib.util.spec_from_file_location('snapshot_controls', Path('tools/xbench/tests/test_x07_reducer_input_snapshot.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
case = module.ReducerInputSnapshot('test_synchronized_changes_yield_verified_snapshot_or_refuse_before_oracle')
result = unittest.TextTestRunner().run(unittest.TestSuite([case]))
assert result.wasSuccessful()
print(json.dumps({'mutation_controls': 'PASS', 'historical_cause': 'UNRESOLVED'}, sort_keys=True))
'@ | python -
```

With stable descriptor and path metadata, the first object reports
`VERIFIED_SNAPSHOT`. A compared-field mismatch reports `METADATA_REFUSAL` and
the reader raises `ValueError('history changed during read')`. The reducer CLI
translates that into schema `leaf.x07.reducer-diagnostic.v1`, error
`INVALID_HISTORY_INPUT`, exit 2, before oracle evaluation or output replacement.

The mutation control synchronizes a writer between descriptor chunks. Actual
truncation, growth or successful replacement must refuse before the oracle.
Windows can instead deny replacement while the descriptor is open. That
specific `PermissionError` leaves the original complete snapshot, which may
proceed. The check distinguishes this OS outcome from a successful replacement.

The injected metadata matrix is executable with
`test_x07_reducer_windows_metadata.py`. It varies descriptor and final-path
device, inode, size, modification time and creation/change time independently.
Such synthetic refusals are not native reproductions of the historical event.

All children and writer threads are joined by their fixtures. Only disposable
owned directories and known replacement files are cleaned. Preserve printed
observations when diagnosing a refusal. Do not delete foreign scratch, weaken
the metadata predicate, or infer a LIVE result from a passing offline recipe.
