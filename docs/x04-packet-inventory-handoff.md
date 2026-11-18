# Expected artifact inventory in X04's offline packet review

Studio X-bench X04's packet validator accepts repeatable `--expected-artifact-id` options. Their set must equal the decoded packet's embedded artifact-local ID set before trusted validation. Order does not matter. Invalid local IDs and duplicate expectations refuse during argument parsing before I/O. Omitting the options retains previous behavior. A matching set still faces all existing packet and artifact checks.

From the repository root in PowerShell, derive the validator from this checkout and create a fresh synthetic packet with artifact-local IDs a and b. Both deliberately contain the same synthetic drawing. Local IDs are packet labels, not distinct drawing identities:

```powershell
$validator = (Resolve-Path -LiteralPath './scripts/validate_x04_evidence_packet.py').Path
$demo = 'D:/agent-data/leaf-research/x04-synthetic-packet-inventory'
@'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]).resolve().parent))
from bench_x04_packet_validation import artifact
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
value = {'schema': 'leaf.x04.evidence-packet.v1', 'artifacts': [
    {'id': 'a', 'artifact': artifact()}, {'id': 'b', 'artifact': artifact()}], 'comparisons': [], 'batches': []}
(root / 'packet.json').write_bytes(json.dumps(value).encode('utf-8'))
'@ | python -P - $demo $validator
if ($LASTEXITCODE -ne 0) { throw 'Expected fresh synthetic packet creation' }
$fixedPacketPin = (Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash
```

Retain the packet digest and the expected labels a and b separately. This block validates by file and by raw binary stdin from an unrelated directory. It first supplies the exact set in reverse order, then refuses a missing or extra expected ID without replacing prior output, and finally recovers the identical summary with the corrected set. The packet byte pin is correct in every call, so it does not bypass the inventory guard.

```powershell
@'
import json, subprocess, sys
from pathlib import Path
root, validator, pin = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
source = root / 'packet.json'; raw = source.read_bytes()
for stdin in (False, True):
    output = root / ('stdin-summary.json' if stdin else 'file-summary.json')
    def run(ids):
        args = [sys.executable, '-P', validator, '-' if stdin else str(source), '--out', str(output),
                '--expected-packet-sha256', pin]
        for identity in ids: args += ['--expected-artifact-id', identity]
        return subprocess.run(args, input=raw if stdin else None, cwd=root, capture_output=True, timeout=30)
    result = run(['b', 'a'])
    if result.returncode != 0: raise RuntimeError(result.stderr.decode())
    previous = output.read_bytes()
    for wrong in (['a'], ['a', 'b', 'extra']):
        result = run(wrong)
        if result.returncode != 2 or result.stdout or output.read_bytes() != previous:
            raise RuntimeError('Inventory mismatch did not preserve the prior summary')
    result = run(['a', 'b'])
    if result.returncode != 0 or output.read_bytes() != previous or source.read_bytes() != raw:
        raise RuntimeError('Corrected inventory did not recover')
    summary = json.loads(previous)
    if [row['id'] for row in summary['validated_artifacts']] != ['a', 'b']: raise RuntimeError('Unexpected inventory')
    if any(row['recomputed_result'] != {'count': 1, 'highlight_handles': ['A'], 'original_bytes_integrity': 'unverified'}
           for row in summary['validated_artifacts']): raise RuntimeError('Unexpected artifact claims')
    if summary['original_bytes_integrity'] != 'unverified': raise RuntimeError('Unexpected integrity claim')
    if list(root.glob('.x04-*')): raise RuntimeError('Unexpected temporary file')
    print('stdin' if stdin else 'file', 'exact set, mismatch refusal and corrected retry passed')
'@ | python -P - $demo $validator $fixedPacketPin
if ($LASTEXITCODE -ne 0) { throw 'Expected both synthetic inventory controls to pass' }
```

Expected IDs use the existing packet-local ASCII label rule: a letter first, then letters, digits, underscore or hyphen, at most 64 characters. Comparison and batch report-local IDs are not part of the embedded artifact inventory. Actual duplicate IDs, invalid references and forged artifact counts still fail original packet checks even if a set comparison matches. The pure `validate_packet` API remains unchanged.

The order is input byte ceiling, optional packet byte digest, JSON decoding, optional inventory comparison, then trusted packet/artifact validation. An inventory mismatch reaches no trusted artifact calls. Omitted expectations perform no inventory comparison. Expected labels placed in JSON metadata do not set CLI options. A valid renamed or substituted packet can pass only when the caller changes its expected inventory accordingly; equal labels do not authenticate artifact contents or drawing identity.

An attacker who replaces the packet, expected digest and expected labels together can make them agree. Exact inventory labels, drawing-label equality, byte pins and semantic consistency are distinct checks, none a producer signature or original-DWG proof. Integrity stays `unverified`; this synthetic replay establishes no raw completeness, browser behavior or live ten-drawing acceptance. Existing atomic replacement adds no fsync or hostile alias-race guarantee. A packet must still contain at least one artifact; omitting expected-ID options is not a way to assert an empty inventory.
