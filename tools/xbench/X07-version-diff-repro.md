# X07 disposable version-diff replay

This recipe checks Studio X-bench X07's offline immutable profile comparison.
Run from the repository root. Every store is disposable and both customer keys
are explicit synthetic inputs. It makes no LIVE acceptance claim.

```powershell
@'
from pathlib import Path
import json
import subprocess
import sys
import tempfile
sys.path.insert(0, 'tools/xbench')
from x07_profile_version_diff import CustomerProfileStore, compare_versions

script = Path('tools/xbench/x07_profile_version_diff.py').resolve()
with tempfile.TemporaryDirectory() as temporary:
    path = Path(temporary) / 'profiles.sqlite'
    store = CustomerProfileStore(path)
    first = store.save_version('synthetic-a', {'module': {'block': 'A', 'layer': 'M'}, 'roof': {'layer': 'R'}}, expected_version=0)
    old = store.apply_to_project('old-a', 'synthetic-a')
    store.save_version('synthetic-a', {'module': {'block': 'A2', 'layer': 'M'}, 'obstruction': {'layer': 'O'}}, expected_version=1)
    foreign = store.save_version('synthetic-b', {'module': {'block': 'PRIVATE_B', 'layer': 'B'}}, expected_version=0)
    old_b = store.apply_to_project('old-b', 'synthetic-b')
    before = path.read_bytes()
    def cli(customer, profile, a, b, exit_code):
        child = subprocess.run([sys.executable, str(script), '--database', str(path), '--customer', customer,
                                '--profile-id', profile, '--before', str(a), '--after', str(b)],
                               capture_output=True, text=True, timeout=10)
        assert child.returncode == exit_code and child.stderr == ''
        assert child.stdout.endswith('\n')
        return json.loads(child.stdout)
    identity = cli('synthetic-a', first['profile_id'], 1, 1, 0)
    forward = cli('synthetic-a', first['profile_id'], 1, 2, 0)
    reverse = cli('synthetic-a', first['profile_id'], 2, 1, 0)
    assert {key: identity[key] for key in ['added', 'removed', 'changed']} == {'added': {}, 'removed': {}, 'changed': {}}
    expected = {'added': {'obstruction': {'layer': 'O'}}, 'removed': {'roof': {'layer': 'R'}},
                'changed': {'module': {'before': {'block': 'A', 'layer': 'M'}, 'after': {'block': 'A2', 'layer': 'M'}}}}
    assert {key: forward[key] for key in expected} == expected
    assert reverse['added'] == forward['removed'] and reverse['removed'] == forward['added']
    assert reverse['changed'] == {'module': {'before': {'block': 'A2', 'layer': 'M'}, 'after': {'block': 'A', 'layer': 'M'}}}
    assert forward == compare_versions(store, customer_key='synthetic-a', profile_id=first['profile_id'], before_version=1, after_version=2)
    refused = cli('synthetic-a', foreign['profile_id'], 1, 1, 2)
    assert refused == {'schema': 'leaf.x07.profile-version-diff.v1', 'error': 'SCOPED_VERSIONS_UNAVAILABLE'}
    assert 'PRIVATE_B' not in json.dumps(refused)
    assert store.application_record('old-a', 'synthetic-a') == old
    assert store.application_record('old-b', 'synthetic-b') == old_b
    assert path.read_bytes() == before
    print(json.dumps({'forward': expected, 'identity_empty': True, 'reverse_matches': True,
                      'foreign_exit': 2, 'old_pins_and_bytes_unchanged': True}, sort_keys=True))
'@ | python -
```

Successful CLI calls exit 0 and emit one JSON object with schema
`leaf.x07.profile-version-diff.v1`, explicit customer_key, generated profile_id,
before_version, after_version, added, removed and changed. The generated profile
ID is read from this run. The assertions above check exact role values and the
complete CLI object against the API result.

Foreign or absent versions exit 2 and emit exactly the two-field refusal object
asserted above, without role labels. Invalid arguments exit 2 with the same
schema and error `INVALID_ARGUMENTS`. Output ends with a real newline. Neither
identity nor reverse comparison saves a version or changes an old project pin.
