# What comparison evidence establishes

Parent: Studio X-bench X04 intake, track 2. The offline comparison independently
validates two synthetic artifacts before reporting selected IDs, physical count,
parent highlights and literal dependency row changes. Its scope is those fields.

Start with the worked artifact in
[the portable contract](x04-offline-report-export-contract.md). This recipe makes
three valid artifacts with different embedded drawing content:

```python
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0, 'scripts')
from export_x04_offline_evidence import export_evidence

text = Path('docs/x04-offline-report-export-contract.md').read_text(encoding='utf-8')
before = export_evidence(json.loads(text.split('```json\n', 1)[1].split('```', 1)[0]))
moved = deepcopy(before)
quad = moved['drawing']['intake']['polylines'][0]
quad['pts'] = [[p[0] + 100, p[1] - 50, *p[2:]] for p in quad['pts']]
annotated = deepcopy(before)
annotated['drawing']['opaque_extension'] = {'review': 'changed synthetic annotation'}
for name, value in [('before', before), ('moved', moved), ('annotated', annotated)]:
    Path(name + '.json').write_text(json.dumps(export_evidence(value)), encoding='utf-8')
```

Run from the worktree root:

```powershell
python scripts/compare_x04_offline_evidence.py before.json moved.json
python scripts/compare_x04_offline_evidence.py before.json annotated.json
```

Both exit 0. Each reports count 7 before and after, delta 0, and empty selected-ID,
highlight and dependency membership deltas. The translated quad remains eligible.
Its parent handle stays B. The extension is retained but is outside the reported
delta fields. Inspect the drawing objects to see the changed coordinates and
annotation. Empty deltas therefore cannot establish identical embedded drawings.

Selected-ID deltas establish changes in explicit choices. Count deltas establish
changes in the recomputed physical union. Highlight deltas establish changes in
canonical parent membership, not position or shape. Literal dependency deltas
establish whole-row membership changes in embedded conversion records. They
provide no proof that an external referenced file exists or stayed unchanged.

Original-byte integrity remains unverified in every output. A matching drawing
ID is an embedded claim. Valid replay and empty deltas authenticate neither source
bytes nor their history. Compare retained original bytes and an independently
trusted SHA-256 history before making byte-identity claims. Raw dependency
completeness, browser rendering, staging delivery and the ten real drawings
remain distinct X04 acceptance receipts.

Focused tests cover argument parsing without artifact IO, cancellation before
publication, semantic scope and zero/one/all-invalid batch boundaries. The full
pipeline benchmark is `python scripts/bench_x04_comparison_pipeline.py`. It times
trusted validation plus comparison of equal, disjoint and asymmetric artifacts
at 100/1000/10000 entities. Artifact construction is outside timing. Expected
count and memberships come from independent integer sets, inputs stay immutable,
and time and peak-memory observations have no pass thresholds.
