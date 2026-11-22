# X04 comparison operand bindings

Studio X-bench X04 can bind named packet comparisons to ordered artifact-local references with repeated `--expected-comparison-operands ID=BEFORE,AFTER`. A partial map constrains only named comparisons. Omission retains existing packet semantic checks.

Malformed local labels, missing operands, extra delimiters and duplicate expected keys refuse before input reads. Unknown comparison keys and wrong ordered references refuse after byte ceilings, optional byte pin, strict decoding, section inventories and drawing bindings, before trusted artifact validation. Matching bindings still require valid references, globally unique packet IDs and recomputed report claims.

Run this synthetic file/stdin replay from the repository root in its Python environment. The two embedded artifacts share valid content, so either reference order is internally consistent even though fixed caller expectations distinguish the order.

```python
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path.cwd()
sys.path[:0] = [str(root / 'scripts'), str(root / 'server')]
from bench_x04_packet_validation import artifact
from compare_x04_offline_evidence import compare_evidence
from validate_x04_evidence_packet import SCHEMA

embedded = artifact()
packet = {'schema': SCHEMA, 'artifacts': [
    {'id': 'a', 'artifact': embedded}, {'id': 'b', 'artifact': deepcopy(embedded)}],
    'comparisons': [{'id': 'c', 'before': 'a', 'after': 'b',
                     'report': compare_evidence(embedded, embedded)}], 'batches': []}
validator = root / 'scripts/validate_x04_evidence_packet.py'
with tempfile.TemporaryDirectory(prefix='x04-operand-replay-') as directory:
    source = Path(directory) / 'packet.json'
    output = Path(directory) / 'summary.json'
    source.write_bytes(json.dumps(packet).encode())
    command = [sys.executable, '-P', str(validator)]
    expected = ['--expected-comparison-operands', 'c=a,b']
    subprocess.run([*command, str(source), '--out', str(output), *expected], check=True)
    previous = output.read_bytes()
    streamed = subprocess.run([*command, '-', *expected], input=source.read_bytes(), capture_output=True)
    assert streamed.returncode == 0 and streamed.stdout == previous

    packet['comparisons'][0].update(before='b', after='a')
    source.write_bytes(json.dumps(packet).encode())
    subprocess.run([*command, str(source)], check=True)  # Omitted binding permits the valid replacement.
    refused = subprocess.run([*command, str(source), '--out', str(output), *expected], capture_output=True)
    assert refused.returncode == 2 and refused.stdout == b'' and output.read_bytes() == previous
    subprocess.run([*command, str(source), '--out', str(output),
                    '--expected-comparison-operands', 'c=b,a'], check=True)
```

The bindings compare reference labels and their order. They do not establish the contents behind those labels. Artifact inventories constrain membership, drawing bindings constrain named drawing labels, byte pins constrain encoded packet bytes, and trusted validation recomputes semantic claims. Coordinated replacement of operands, their contents and caller expectations can satisfy all those consistency checks. Producer authenticity and original DWG custody need their own receipts. Original-byte integrity remains unverified.

The focused tests use unequal-count synthetic operands for reversal and substitution controls. The benchmark fixes two artifacts and grows comparisons from 2 to 10 to 100, with matching, wrong and omitted maps. It requires two trusted calls on valid inputs and zero on mismatch, recording elapsed time and peak memory without timing gates.
