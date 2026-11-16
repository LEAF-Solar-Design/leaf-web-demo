# X04 five-tool offline replay

This Studio X-bench X04 recipe exports synthetic entity evidence, validates it, compares two selections, batches artifacts, then checks the saved reports after deleting the artifacts. It does not open a customer drawing or use a network service. Run from the repository root on the assigned off-host verification rail. Choose an empty scratch directory for the relative files below.

Create two inputs with Python:

```python
import json
from pathlib import Path
from copy import deepcopy
import sys
sys.path.insert(0, str(Path("server").resolve()))
from drawing_dependency_report import from_pairs
payload = {
    'schema': 'leaf.x04.offline-evidence.v1',
    'synthetic': True,
    'drawing': {'id': 'synthetic-five-tool', 'intake': {
        'inserts': [{'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}],
        'polylines': [{'layer': 'Outline', 'handle': 'B', 'closed': True,
                      'pts': [[0, 0], [2, 0], [2, 1], [0, 1]]}]},
        'dependency_pairs': []},
    'observed_dependencies': from_pairs([]),
    'request': {'drawing_id': 'synthetic-five-tool',
                'set_ids': ['["INSERT","Modules","Panel"]', '["LWPOLYLINE","Outline",""]']},
}
Path('before-input.json').write_text(json.dumps(payload), encoding='utf-8')
after = deepcopy(payload)
after['request']['set_ids'] = ['["INSERT","Modules","Panel"]']
Path('after-input.json').write_text(json.dumps(after), encoding='utf-8')
```

```text
python -P scripts/export_x04_offline_evidence.py before-input.json --out before-artifact.json
python -P scripts/export_x04_offline_evidence.py after-input.json --out after-artifact.json
python -P scripts/validate_x04_offline_evidence.py before-artifact.json --out artifact-check.json
python -P scripts/compare_x04_offline_evidence.py before-artifact.json after-artifact.json --out comparison.json
python -P scripts/batch_validate_x04_evidence.py before-artifact.json after-artifact.json before-artifact.json --out batch.json
```

Each command exits 0. The artifact check recomputes count 2 and highlights A/B. Comparison has before 2, after 1, delta -1 and removes highlight B. Batch reports total 3, passed 3, failed 0 with indices 0/1/2 and repeated before-artifact path labels. Original-byte integrity remains unverified.

Delete only the synthetic files made by this recipe, then check the reports:

```python
from pathlib import Path
for name in ('before-input.json', 'after-input.json', 'before-artifact.json', 'after-artifact.json'):
    Path(name).unlink()
```

```text
python -P scripts/validate_x04_review_report.py comparison.json --out comparison-check.json
python -P scripts/validate_x04_review_report.py batch.json --out batch-check.json
```

Both exit 0 although the underlying artifacts no longer exist. Their summaries declare internal consistency, `underlying_artifacts_revalidated: false`, `embedded_paths_opened: false` and integrity `unverified`. Embedded path labels are inert. The checker does not carry opaque provenance extensions into its success summary.

Forge a contradictory report and retain a valid retry source:

```python
import json
from pathlib import Path
valid = Path('comparison.json').read_bytes()
Path('comparison-valid.json').write_bytes(valid)
forged = json.loads(valid)
forged['count']['delta'] = 999
Path('comparison-forged.json').write_text(json.dumps(forged), encoding='utf-8')
```

```text
python -P scripts/validate_x04_review_report.py comparison-forged.json --out comparison-check.json
python -P scripts/validate_x04_review_report.py comparison-valid.json --out comparison-check.json
python -P scripts/bench_x04_review_report_payload_width.py
```

The forgery exits 2, emits a refusal on stderr and preserves the existing check file. The valid retry exits 0. The benchmark fixes 20 rows while increasing literal widths, records encoded bytes/time/peak memory and checks immutable inputs without timing thresholds.

Internal consistency cannot prove artifact validity, a simultaneous filesystem snapshot or authenticated source bytes. A self-consistent forged report can pass. Fresh artifact validation requires the entity-bearing artifact and its trusted recomputation. Real intake, rendered selection and the full-ten staging grader remain separate X04 acceptance receipts.
