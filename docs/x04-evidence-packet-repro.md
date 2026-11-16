# X04 embedded evidence packet

Studio X-bench X04 can cross-check saved reports against embedded synthetic artifacts without opening paths named inside either. This is stronger than a saved report's internal arithmetic check: explicit local IDs bind comparison operands and ordered batch occurrences to the artifact data being recomputed. It does not authenticate original drawing bytes or prove live behavior.

The packet schema is `leaf.x04.evidence-packet.v1` with arrays `artifacts`, `comparisons` and `batches`:

- Artifact entries contain `id` and an embedded `artifact` accepted by the existing synthetic evidence validator. At least one artifact is required; every artifact must validate, including unused artifacts.
- Comparison entries contain `id`, `before`, `after` and `report`. Before/after reference artifact IDs. The saved comparison must pass the saved-report checker and agree with trusted recomputation in operand order. Membership order and object-key order do not matter.
- Batch entries contain `id`, nonempty ordered `artifact_ids` and `report`. Each occurrence must match its referenced validated artifact's drawing identity, count and highlight membership. Repeats are allowed and retain occurrence order. The embedded batch must be all-success because every embedded artifact is valid. Saved failure rows cannot be bound to this packet format.

IDs are globally unique ASCII labels matching `[A-Za-z][A-Za-z0-9_-]{0,63}`. References target artifacts only; missing, URL-like, path-like and report-to-report references refuse before artifact validation. Cycles cannot be represented. Batch path fields are inert display labels, not independent proof of a source file. Unknown opaque extensions do not upgrade the result's proof scope. Selected IDs, dependency memberships and counts remain claims derived from the embedded synthetic data.

The result schema is `leaf.x04.evidence-packet-validation.v1`, with scope `embedded_artifact_report_consistency`, recomputed validated artifacts and bound comparison/batch reports. Integrity stays `unverified`. Existing bounded JSON, duplicate-key refusal, stdin support, alias checks, output byte ceilings and atomic publication helpers apply. CLI exit 0 means consistent; exit 2 means refusal with a bounded stderr diagnostic and no successful file replacement. Reading the explicitly supplied packet and repository tool registry is allowed; embedded target paths are never opened.

## Synthetic construction

Run this Python from the repository root on the assigned off-host verification rail. All files below are synthetic scratch files.

```python
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path('scripts').resolve()))
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence
from validate_x04_offline_evidence import validate_evidence

raw = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
       'drawing': {'id': 'synthetic-packet', 'intake': {'inserts': [
           {'layer': 'Modules', 'name': 'Panel', 'handle': 'A', 'rows': 1, 'columns': 1}]},
           'dependency_pairs': []},
       'request': {'drawing_id': 'synthetic-packet', 'set_ids': ['["INSERT","Modules","Panel"]']},
       'observed_dependencies': from_pairs([])}
a = export_evidence(raw)
changed = deepcopy(raw)
changed['drawing']['intake']['inserts'][0]['rows'] = 2
b = export_evidence(changed)
batch = {'schema': 'leaf.x04.offline-batch-validation.v1', 'total': 3, 'passed': 3, 'failed': 0,
         'original_bytes_integrity': 'unverified', 'results': [
             {'index': i, 'path': '../../unopened.json', **validate_evidence(item)}
             for i, item in enumerate((a, b, a))]}
packet = {'schema': 'leaf.x04.evidence-packet.v1',
          'artifacts': [{'id': 'a', 'artifact': a}, {'id': 'b', 'artifact': b}],
          'comparisons': [{'id': 'c', 'before': 'a', 'after': 'b', 'report': compare_evidence(a, b)}],
          'batches': [{'id': 'batch', 'artifact_ids': ['a', 'b', 'a'], 'report': batch}]}
Path('x04-packet.json').write_text(json.dumps(packet), encoding='utf-8')
forged = deepcopy(packet)
forged['artifacts'][0]['artifact'] = deepcopy(b)
Path('x04-packet-substituted.json').write_text(json.dumps(forged), encoding='utf-8')
```

```text
python -P scripts/validate_x04_evidence_packet.py x04-packet.json --out x04-packet-check.json
python -P scripts/validate_x04_evidence_packet.py x04-packet-substituted.json --out x04-packet-check.json
python -P scripts/validate_x04_evidence_packet.py x04-packet.json --out x04-packet-check.json
python -P scripts/bench_x04_packet_validation.py
```

The valid commands exit 0, comparison counts are before 1 / after 2 / delta 1, and batch occurrence counts are 1/2/1. The substitution is individually valid evidence but contradicts the saved reports, so it exits 2 and preserves the previous check file. The last valid command recovers. A packet requires no separate artifact files. The network-denied subprocess test constructs reports with the actual export/comparison/batch producers, deletes all source files and then verifies the packet from an unrelated directory.

The benchmark varies artifact count and repeated/distributed local references using equal-byte controls. Each artifact is validated once up front; the existing comparison function also validates its two operands. Batch occurrences reuse the embedded artifact validation results and never reopen a source. It records calls, elapsed time and peak memory, checks immutable inputs, rejects a cross-bound forgery and verifies valid recovery without timing thresholds.

Cross-bound consistency proves neither raw dependency completeness, live intake/rendering, a simultaneous snapshot nor authenticated source bytes. Consistently changing both embedded artifacts and their reports can create a different valid synthetic packet. Full-ten real-drawing acceptance remains a separate X04 staging receipt.
