# X07 capture consumption across local trees

Parent is Studio X-bench X07 customer-profile evidence. This offline recipe creates a synthetic source tree and records its digests through the real capsule producer. The injected source observation identifies two disposable files. Node and Python are required.

Run the Python block from the checkout's `server` directory with `python -P -`. The automated portability test executes this exact block. The recipe relocates both source and capture, removes the original source tree, verifies the relocated bytes, then checks refusal after a dependency edit.

```python
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd().parent / 'tools/xbench'))
import x07_jsx_failure_capsule as capsule

with tempfile.TemporaryDirectory(prefix='x07-portable-') as directory:
    root = Path(directory)
    original = root / 'original source'
    original.mkdir()
    check = original / 'x07_portable.cjs'
    check.write_bytes(b"process.stdout.write('PORTABLE')")
    material = original / 'material.txt'
    material.write_bytes(b'SYNTHETIC MATERIAL')
    def observation(*args):
        return {'source_sha': 'a' * 40, 'dirty': False,
                'source_file_sha256': {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in (check, material)}}
    capture = original / 'capture.json'
    with patch.object(capsule, 'snapshot', side_effect=observation):
        capsule.run_check(check.name, capture, checks_root=original, timeout=5)
    retained = capture.read_bytes()
    relocated = root / 'relocated source'
    shutil.copytree(original, relocated)
    shutil.rmtree(original)
    moved = relocated / capture.name
    assert capsule.read_capture(moved)['stdout'] == 'PORTABLE'
    verified = capsule.read_capture(moved, source_root=relocated, recorded_source_root=original)
    assert verified['local_source_verification'] == {'status': 'VERIFIED', 'dependencies': 2}
    (relocated / 'material.txt').write_bytes(b'TAMPERED')
    try:
        capsule.read_capture(moved, source_root=relocated, recorded_source_root=original)
    except ValueError as error:
        assert str(error) == 'SOURCE_UNVERIFIABLE'
    else:
        raise AssertionError('changed dependency accepted')
    assert moved.read_bytes() == retained
    print(json.dumps({'strict': True, 'relocated_source': True, 'tamper_refused': True}))
```

`read_capture(path)` validates the capture alone. Supplying `source_root` opts into local digest verification. Absolute recorded dependency names also require `recorded_source_root` so relocation is explicit. Relative names resolve beneath the supplied local root. Traversal, linked dependencies, missing files, changed digests and dependencies above 8 MiB raise `ValueError('SOURCE_UNVERIFIABLE')`. Successful calls add a verification label to the returned object. Capture bytes retain their original source observation.

Regular capture reads compare path metadata with the opened descriptor before and after the bounded read. Concurrent replacement, growth, truncation or I/O failure causes refusal. These endpoint checks do not detect an edit restored to identical bytes and metadata between observations.

For a disposable smoke measurement, run `python -P ../tools/xbench/x07_capsule_consumer_perf.py --directory <owned-scratch-directory> --smoke` from `server`. Its JSON records strict parsing and stream-validation time, peak Python traced allocations and independent accepted/refused counts. Temporary captures are removed by the tool.
