# X07 disposable history audit replay

This recipe belongs to Studio X-bench X07's offline customer-profile recovery.
It uses two synthetic customers and an explicit empty scope. The audit reads
metadata only and performs no repair. It establishes no LIVE result.

Run from the repository root:

```powershell
@'
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
sys.path.insert(0, 'tools/xbench')
from x07_profile_history_audit import CustomerProfileStore
script = Path('tools/xbench/x07_profile_history_audit.py').resolve()
scratch = Path('D:/tmp'); scratch.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='x07-audit-', dir=scratch) as directory:
    path = Path(directory) / 'profiles.sqlite'
    store = CustomerProfileStore(path)
    a = store.save_version('synthetic-a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
    old_a = store.apply_to_project('old-a', 'synthetic-a')
    b = store.save_version('synthetic-b', {'module': {'block': 'PRIVATE_B', 'layer': 'B'}}, expected_version=0)
    old_b = store.apply_to_project('old-b', 'synthetic-b')
    def cli(database, customer, expected_exit):
        child = subprocess.run([sys.executable, str(script), '--database', str(database), '--customer', customer],
                               capture_output=True, text=True, timeout=10)
        assert child.returncode == expected_exit and child.stderr == ''
        assert child.stdout.endswith('\n') and 'PRIVATE_B' not in child.stdout
        return json.loads(child.stdout)
    clean_bytes = path.read_bytes()
    clean = cli(path, 'synthetic-a', 0)
    zeros = {'missing_profile': 0, 'empty_chain': 0, 'version_gaps': [], 'invalid_versions': 0,
             'foreign_version_profiles': 0, 'dangling_pin_versions': 0, 'foreign_pin_profiles': 0}
    assert clean == {'schema': 'leaf.x07.profile-history-audit.v1', 'customer_key': 'synthetic-a',
                     'profile_found': True, 'version_count': 1, 'pin_count': 1, 'healthy': True, 'issues': zeros}
    assert path.read_bytes() == clean_bytes
    # Intentional disposable corruption, not an audit operation.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('INSERT INTO customers VALUES (?,?)', ('empty', 'synthetic-empty-profile'))
        db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)', ('synthetic-a', b['profile_id'], 3, 'PRIVATE_B', 'synthetic'))
        db.execute('INSERT INTO project_applications VALUES (?,?,?,?)', ('bad-missing', 'synthetic-a', a['profile_id'], 9))
        db.execute('INSERT INTO project_applications VALUES (?,?,?,?)', ('bad-foreign', 'synthetic-a', b['profile_id'], 1))
    before = path.read_bytes()
    finding = cli(path, 'synthetic-a', 1)
    expected = {**zeros, 'version_gaps': [{'first': 2, 'last': 2}], 'foreign_version_profiles': 1,
                'dangling_pin_versions': 1, 'foreign_pin_profiles': 1}
    assert finding == {'schema': 'leaf.x07.profile-history-audit.v1', 'customer_key': 'synthetic-a',
                       'profile_found': True, 'version_count': 2, 'pin_count': 3, 'healthy': False, 'issues': expected}
    empty = cli(path, 'empty', 1)
    assert empty == {'schema': 'leaf.x07.profile-history-audit.v1', 'customer_key': 'empty',
                     'profile_found': True, 'version_count': 0, 'pin_count': 0, 'healthy': False,
                     'issues': {**zeros, 'empty_chain': 1}}
    missing = Path(directory) / 'missing' / 'absent.sqlite'
    assert cli(missing, 'synthetic-a', 2) == {'schema': 'leaf.x07.profile-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'}
    assert not missing.exists() and not missing.parent.exists()
    assert cli(path, 'synthetic-a', 1) == finding
    read = CustomerProfileStore(path, read_only=True)
    assert read.application_record('old-a', 'synthetic-a') == old_a
    assert read.application_record('old-b', 'synthetic-b') == old_b
    assert cli(path, 'synthetic-b', 0)['healthy']
    assert path.read_bytes() == before
    print(json.dumps({'clean_exit': 0, 'finding_exit': 1, 'unavailable_exit': 2,
                      'issues': expected, 'old_pins_and_bytes_unchanged': True}, sort_keys=True))
'@ | python -
```

The assertions specify exact report JSON. Exit 0 means a clean scoped audit,
1 means complete findings, and 2 means unavailable input or execution.
Unknown customer scopes report missing_profile with exit 1. Invalid arguments
return the audit schema with error `INVALID_ARGUMENTS` and exit 2.

`MAX_AUDIT_ROWS` is 10000 per version or pin collection, not a combined limit.
Counts above it refuse with `AUDIT_UNAVAILABLE`. Reopen the known disposable
store read-only to recover after unavailable input or removed SQLite handlers.
Findings persist until a separate authorized operation changes the data; the
audit never repairs records or changes old pins.

Connections and child processes are closed before disposable directory cleanup.
Delete only the owned fixture directory. Do not use this corruption recipe on
customer data or infer identity from a drawing or role label.
