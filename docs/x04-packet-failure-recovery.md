# X04 packet failure and recovery

Studio X-bench X04 checks every embedded synthetic artifact, even one that no report references. The recipe below builds a packet, proves exact byte-limit refusal and rejects a tampered unused artifact while preserving a previous output. It requires no customer files and opens no embedded path labels. Run from the repository root on the assigned off-host verification rail.

```python
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path('scripts').resolve()))
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence

artifact = export_evidence({'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-recovery', 'intake': {'inserts': [
        {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
    'observed_dependencies': from_pairs([]),
    'request': {'drawing_id': 'synthetic-recovery', 'set_ids': ['["INSERT","Modules","Panel"]']}})
packet = {'schema': 'leaf.x04.evidence-packet.v1',
          'artifacts': [{'id': 'a', 'artifact': artifact},
                        {'id': 'unused', 'artifact': deepcopy(artifact)}],
          'comparisons': [{'id': 'c', 'before': 'a', 'after': 'a',
                           'report': compare_evidence(artifact, artifact)}], 'batches': []}
source, output = Path('x04-recovery-packet.json'), Path('x04-recovery-check.json')
raw = json.dumps(packet).encode('utf-8')
source.write_bytes(raw)
script = Path('scripts/validate_x04_evidence_packet.py').resolve()
def run(*args):
    return subprocess.run([sys.executable, '-P', str(script), str(source), *map(str, args)],
                          capture_output=True)
valid = run()
assert valid.returncode == 0
output.write_bytes(b'prior output')
for option, limit in [('--max-input-bytes', len(raw) - 1),
                      ('--max-output-bytes', len(valid.stdout) - 1)]:
    refused = run('--out', output, option, limit)
    assert refused.returncode == 2 and refused.stdout == b'' and refused.stderr
    assert output.read_bytes() == b'prior output' and source.read_bytes() == raw
assert run('--out', output, '--max-input-bytes', len(raw),
           '--max-output-bytes', len(valid.stdout)).returncode == 0
previous = output.read_bytes()
forged = deepcopy(packet)
forged['artifacts'][1]['artifact']['result']['count'] = 999
source.write_text(json.dumps(forged), encoding='utf-8')
refused = run('--out', output)
assert refused.returncode == 2 and refused.stdout == b'' and refused.stderr
assert output.read_bytes() == previous
source.write_bytes(raw)
assert run('--out', output).returncode == 0 and output.read_bytes() == previous
assert json.loads(previous)['original_bytes_integrity'] == 'unverified'
```

The first and last valid commands exit 0. Each one-byte-under limit and unused-artifact forgery exits 2, reports only a bounded stderr diagnostic, and preserves the existing output. Input limits include all bytes and output limits include the trailing newline. Duplicate keys, trailing JSON and excessive nesting refuse too. Exact `-` reads stdin; `./-` is a literal filename.

Publication faults during temporary write, flush or replacement preserve prior destination bytes and remove only the current writer's temporary file. Cancellation propagates its original exception after cleanup. This is atomic replacement, not proof of durable storage or a simultaneous source snapshot. Consistent packet-local ID renaming and entry reordering preserve cross-bound counts and memberships; an incomplete rename refuses. Optimized and hash-seeded safe-path subprocesses retain the same complete summary or refusal.

```text
python -P scripts/bench_x04_packet_unused_artifacts.py
```

The benchmark holds the three report references fixed while growing unused artifacts, counts every trusted validation, rejects tampering of the last unused artifact and checks immutable inputs and valid recovery. It records time and peak memory without timing thresholds. Packet consistency does not establish raw dependency completeness, a browser/live receipt, original-byte authentication or trustworthy provenance. The real-drawing full-ten staging grader remains separate X04 acceptance.
