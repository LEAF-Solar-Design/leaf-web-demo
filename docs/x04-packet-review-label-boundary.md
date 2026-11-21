# X04 packet batch labels and bound claims

Parent: Studio X-bench X04 offline packet review. This guide comes from static source inspection. The example is synthetic and was not executed for this document. A batch path is a display label; an artifact ID binds an embedded artifact.

The smallest example needs one valid artifact and one batch occurrence. It needs no comparison report or separate artifact file:

```python
from copy import deepcopy
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence

a = export_evidence({'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-label', 'intake': {'inserts': [
        {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
    'request': {'drawing_id': 'synthetic-label', 'set_ids': ['["INSERT","Modules","Panel"]']},
    'observed_dependencies': from_pairs([])})
packet = {'schema': 'leaf.x04.evidence-packet.v1', 'artifacts': [{'id': 'a', 'artifact': a}],
    'comparisons': [], 'batches': [{'id': 'batch', 'artifact_ids': ['a'], 'report': {
        'schema': 'leaf.x04.offline-batch-validation.v1', 'total': 1, 'passed': 1, 'failed': 0,
        'original_bytes_integrity': 'unverified', 'results': [
            {'index': 0, 'path': 'original-label.json', **validate_evidence(a)}]}}]}
relabeled = deepcopy(packet)
relabeled['batches'][0]['report']['results'][0]['path'] = '../../unopened/review-label.json'
```

Manual expectation: both packets are consistent. Their only difference is a path label. `artifact_ids` stays `['a']`; each row still claims drawing `synthetic-label`, count 1 and highlight `A`. Neither label is opened or checked against a source file. This is a permitted relabel, not an artifact substitution or source-identity receipt.

`scripts/validate_x04_evidence_packet.py:62-74` resolves IDs only through its embedded artifact map and validates all artifacts. The saved-report checker requires `path` to be a string (`scripts/validate_x04_review_report.py:100`). The packet copies that same literal label into its expected row at `scripts/validate_x04_evidence_packet.py:95`. It compares the row's drawing ID, count and highlight membership at lines 96-100, then copies the reconstructed row at line 101. There is no independent path-to-artifact map.

Bound claims are the occurrence's position and packet-local ID, successful validation status, matching drawing identity, exact recomputed count and unique highlight membership. The saved-report checker also enforces row schema, indices, totals and unverified integrity (`scripts/validate_x04_review_report.py:89-118`). Batch summaries do not carry a separate selected-set or dependency claim. Those remain part of embedded artifact validation; comparison reports bind their deltas when present.

Not bound by a label: filesystem existence, current contents at that path, original upload bytes, a raw-dependency inventory, a user-visible listing or pick, and rendered/live behavior. Changing embedded content while keeping old reports can refuse; changing the label alone is not that check. The packet's result remains `embedded_artifact_report_consistency` with original-byte integrity `unverified` (`scripts/validate_x04_evidence_packet.py:105-109`).
