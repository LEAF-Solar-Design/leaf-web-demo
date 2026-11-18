# Four-tool offline replay

Parent: Studio X-bench X04 intake, track 2. This self-contained synthetic recipe
exports, validates, compares and batch-checks JSON. Source bytes stay unverified.

Save the following as make-fixtures.py in the worktree root and run it there:

```python
import json
from pathlib import Path
import sys
sys.path.insert(0, 'scripts')
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs

pairs = [[0, 'STYLE'], [3, '../synthetic-font.shx']]
payload = {'schema': SCHEMA, 'synthetic': True,
    'drawing': {'id': 'synthetic-four-tool', 'dependency_pairs': pairs,
        'intake': {'inserts': [
            {'layer': 'East', 'name': 'Module', 'handle': 'A', 'rows': 2, 'columns': 3},
            {'layer': 'West', 'name': 'Module', 'handle': 'B'}]}},
    'observed_dependencies': from_pairs(pairs),
    'request': {'drawing_id': 'synthetic-four-tool',
        'set_ids': ['["INSERT","East","Module"]', '["INSERT","West","Module"]']}}
Path('request.json').write_text(json.dumps(payload), encoding='utf-8')
payload['request']['set_ids'] = ['["INSERT","East","Module"]']
Path('after.json').write_text(json.dumps(export_evidence(payload)), encoding='utf-8')
```

Run these commands from the worktree root, or use absolute script and fixture
paths from an unrelated directory. Python safe-path mode is supported:

```powershell
python make-fixtures.py
python -P scripts/export_x04_offline_evidence.py request.json --out before.json
python -P scripts/validate_x04_offline_evidence.py before.json
python -P scripts/compare_x04_offline_evidence.py before.json after.json
python -P scripts/batch_validate_x04_evidence.py before.json after.json
```

All exit 0. Count before is independently 6 + 1 = 7. Count after is 6. The
comparison delta is -1, removes highlight B and the West selected ID, adds no
highlight and reports no dependency change. Highlight A remains. The batch
report totals are 2, 2, 0 with indices 0 and 1 in argument order.

Forge a derived count while retaining the embedded inputs:

```powershell
python -c "import json; from pathlib import Path; p=json.loads(Path('before.json').read_text()); p['result']['count']=999; Path('forged.json').write_text(json.dumps(p))"
python -P scripts/validate_x04_offline_evidence.py forged.json
python -P scripts/compare_x04_offline_evidence.py before.json forged.json
python -P scripts/batch_validate_x04_evidence.py before.json forged.json after.json
```

Validator and comparison exit 2 with diagnostics and no success JSON. Batch
exits 1 with a complete ordered report: valid, refused, valid and totals 3, 2, 1.
To recover, regenerate the forged path through trusted export, then retry:

```powershell
python -P scripts/export_x04_offline_evidence.py request.json --out forged.json
python -P scripts/batch_validate_x04_evidence.py before.json forged.json after.json
```

Recovery exits 0 with totals 3, 3, 0. These operations read embedded records and
do not follow the synthetic font target. The test subprocesses deny network
socket creation and name resolution, use unrelated working directories and
safe-path mode, and supply only synthetic hash-seed environment values.
Fixed inputs produce identical ordered JSON across tested hash seeds.

Replay proves consistency under the trusted code. Embedded drawing identities,
valid counts and empty deltas do not authenticate original bytes or prove all
drawing content unchanged. Retained original bytes and independent trusted
SHA-256 history are required for byte identity. This walkthrough establishes
no real-DWG, staging, browser or ten-drawing acceptance claim. The independent
full X04 grader remains the terminal acceptance producer.
