# Review synthetic evidence changes

Parent: Studio X-bench X04 raw drawing intake, track 2. These commands compare
offline consistency of synthetic claims. They never authenticate source bytes.

From the repository root, create two independently valid artifacts:

```python
import json
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs

text = Path('docs/x04-offline-report-export-contract.md').read_text(encoding='utf-8')
before = json.loads(text.split('```json\n', 1)[1].split('```', 1)[0])
Path('before.json').write_text(json.dumps(export_evidence(before)), encoding='utf-8')
before['request']['set_ids'] = ['["INSERT","East","Panel"]']
before['drawing']['dependency_pairs'] = [[0, 'STYLE'], [3, '../literal-font.shx']]
before['observed_dependencies'] = from_pairs(before['drawing']['dependency_pairs'])
Path('after.json').write_text(json.dumps(export_evidence(before)), encoding='utf-8')
```

Run each validator, then compare and batch-check the explicit paths:

```powershell
python scripts/validate_x04_offline_evidence.py before.json
python scripts/validate_x04_offline_evidence.py after.json
python scripts/compare_x04_offline_evidence.py before.json after.json --out delta.json
python scripts/batch_validate_x04_evidence.py before.json after.json
```

The count changes from 7 to 6, a delta of -1. Highlight B is removed and A stays.
The selected quad ID `["LWPOLYLINE","East",""]` is removed. The dependency row
for notes.shx is removed and the font row for ../literal-font.shx is added. Check
the literal strings and dispositions in both reports. Dependency paths are not
resolved, normalized or opened. Reversing the arguments reverses the deltas.
The comparison reports set membership, so list order and repeated selected IDs
do not create membership changes. The trusted count remains a physical union.

Both artifacts must validate before any comparison output is emitted. Differing
drawing IDs are refused even when both validate. A matching ID is a claim and
does not prove unchanged drawing content. The comparison covers selected IDs,
counts, highlights and literal dependency rows. It does not compare every opaque
extension or detect all edits to unselected geometry. Changes to disposition
details appear as a removed row and an added row.

Batch validation preserves argument order and repeated paths. Each failure gets
its own row while later paths still run. Totals count paths, not unique files.
An all-valid batch exits 0, a batch with refused paths exits 1, and a command or
output failure exits 2. A complete batch report can therefore contain failures.
No directory scanning or dependency traversal occurs. Batch input is explicit
file paths. Comparison permits one stdin stream through the existing `-` input
convention, but two stdin arguments are refused. Both tools share input byte
limits, optional output byte limits, input/output alias refusal and atomic file
publication with the existing exporter. In a batch, an alias to any input
refuses the command before output replacement.

Valid replay shows that embedded entities and claims agree under the current
trusted code. An author can change entities, selections and dependencies together
and produce another valid artifact. Original-byte integrity stays unverified.
Establish retained original bytes and SHA-256 through an independent trusted
source history before asserting byte identity or authenticity. These examples
provide no raw DWG, browser, staging or real ten-drawing acceptance proof.

Focused checks: test_x04_evidence_compare.py, test_x04_evidence_batch.py and
test_x04_evidence_equivalent_json_spellings.py. Run the spelling benchmark with
`python scripts/bench_x04_evidence_spellings.py`. It observes 1k, 10k and 100k
synthetic INSERTs with equivalent compact, escaped and indented JSON, checks
exact counts, highlights and literal dependencies, and has no timing gates.
