# X04 packet consistency boundary

Studio X-bench X04 binds saved reports to embedded synthetic artifacts. An embedded drawing edit can remain individually valid while making those saved reports stale. This recipe shows that refusal, then regenerates matching reports. It proves embedded consistency only, with original-byte integrity unverified.

Run from the repository root on the assigned off-host verification rail. The recipe writes only the named synthetic scratch files. Repository tools and the registry are local; no drawing or dependency path is opened.

```python
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path('scripts').resolve()))
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence
from compare_x04_offline_evidence import compare_evidence

raw = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
       'drawing': {'id': 'synthetic-edit', 'intake': {'inserts': [
           {'layer': 'Modules', 'name': 'Panel', 'handle': 'A', 'rows': 1, 'columns': 1}]},
           'dependency_pairs': []},
       'observed_dependencies': from_pairs([]),
       'request': {'drawing_id': 'synthetic-edit', 'set_ids': ['["INSERT","Modules","Panel"]']}}
a = export_evidence(raw)
b = deepcopy(a)

def reports(a, b):
    return {
        'comparisons': [{'id': 'comparison', 'before': 'a', 'after': 'b',
                         'report': compare_evidence(a, b)}],
        'batches': [{'id': 'batch', 'artifact_ids': ['a', 'b', 'a'], 'report': {
            'schema': 'leaf.x04.offline-batch-validation.v1', 'total': 3, 'passed': 3, 'failed': 0,
            'original_bytes_integrity': 'unverified', 'results': [
                {'index': i, 'path': '../../unopened.json', **validate_evidence(item)}
                for i, item in enumerate((a, b, a))]}}]}

packet = {'schema': 'leaf.x04.evidence-packet.v1',
          'artifacts': [{'id': 'a', 'artifact': a}, {'id': 'b', 'artifact': b}], **reports(a, b)}
source, output = Path('x04-edit-packet.json'), Path('x04-edit-checked.json')
script = Path('scripts/validate_x04_evidence_packet.py').resolve()
def check(value):
    source.write_text(json.dumps(value), encoding='utf-8')
    return subprocess.run([sys.executable, '-P', str(script), str(source), '--out', str(output)],
                          capture_output=True)
assert check(packet).returncode == 0
previous = output.read_bytes()

# Recompute the edited artifact so its own evidence is valid; keep saved reports stale.
edited = deepcopy(raw)
edited['drawing']['intake']['inserts'][0]['rows'] = 2
changed = export_evidence(edited)
assert validate_evidence(changed)['recomputed_result']['count'] == 2
packet['artifacts'][0]['artifact'] = changed
refused = check(packet)
assert refused.returncode == 2 and refused.stdout == b'' and refused.stderr
assert output.read_bytes() == previous

# Regenerate both reports against the new embedded operands and occurrences.
packet.update(reports(changed, b))
assert check(packet).returncode == 0
result = json.loads(output.read_bytes())
assert result['comparisons'][0]['report']['count'] == {'before': 2, 'after': 1, 'delta': -1}
assert [row['recomputed_result']['count'] for row in result['batches'][0]['report']['results']] == [2, 1, 2]
assert result['original_bytes_integrity'] == 'unverified'
```

The initial valid packet has comparison counts 1/1/delta 0 and batch counts 1/1/1. The stale-report packet exits 2 and preserves the previous output. Regenerated reports exit 0 with comparison 2/1/delta -1 and batch 2/1/2. A consistent regeneration can pass even though the drawing changed. The checker therefore cannot establish that drawing content or original bytes stayed unchanged.

Packet IDs are local labels, not paths or URLs. Identical local IDs in concurrent calls do not identify the same drawing or share a validation cache. Type/schema errors, malformed options and input/output aliases refuse without replacing a previous output. Stream failures report exit 2; a stdout sink may already have received bytes before its flush fails. File replacement and stream delivery have different limits.

```text
python -P scripts/bench_x04_packet_reference_preflight.py
```

The benchmark places missing references first, middle and last in growing packets. It asserts zero trusted artifact validations before refusal, immutable inputs and valid-control recovery, while recording elapsed time and peak memory without thresholds. Neither matching reports nor these checks prove raw dependency completeness, simultaneous filesystem snapshots, authenticated source bytes or real drawing/browser/staging acceptance.
