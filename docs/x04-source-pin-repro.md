# Replaying X04 explicit-source byte pins

Studio X-bench X04's offline assembler can check optional per-occurrence `expected_sha256` values. This replay creates only synthetic data: one module artifact, a self-comparison and a repeated batch. Matching hashes authenticate neither the manifest nor original drawings. Trusted entity/report checks still run, and packet integrity remains `unverified`.

From the repository root in PowerShell, create a fresh synthetic directory and every required source file. The script pins the exact bytes it writes, not a reserialized interpretation.

```powershell
$demo = 'D:/agent-data/leaf-research/x04-synthetic-source-pins'
@'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from export_x04_offline_evidence import export_evidence
from compare_x04_offline_evidence import compare_evidence
from validate_x04_offline_evidence import validate_evidence
from drawing_dependency_report import from_pairs
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
payload = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-pinned', 'intake': {'inserts': [
        {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
    'request': {'drawing_id': 'synthetic-pinned', 'set_ids': ['["INSERT","Modules","Panel"]']},
    'observed_dependencies': from_pairs([])}
artifact = export_evidence(payload)
checked = validate_evidence(artifact)
batch = {'schema': 'leaf.x04.offline-batch-validation.v1', 'total': 2, 'passed': 2, 'failed': 0,
    'original_bytes_integrity': 'unverified', 'results': [
        {'index': i, 'path': '../../unopened.json', **checked} for i in range(2)]}
for name, value in [('artifact.json', artifact), ('comparison.json', compare_evidence(artifact, artifact)), ('batch.json', batch)]:
    (root / name).write_bytes(json.dumps(value).encode('utf-8'))
manifest = {'schema': 'leaf.x04.evidence-packet-manifest.v1',
    'artifacts': [{'id': 'a', 'path': 'artifact.json'}],
    'comparisons': [{'id': 'same', 'before': 'a', 'after': 'a', 'path': 'comparison.json'}],
    'batches': [{'id': 'twice', 'artifact_ids': ['a', 'a'], 'path': 'batch.json'}]}
for key in ('artifacts', 'comparisons', 'batches'):
    for row in manifest[key]:
        row['expected_sha256'] = hashlib.sha256((root / row['path']).read_bytes()).hexdigest()
(root / 'manifest.json').write_bytes(json.dumps(manifest).encode('utf-8'))
'@ | python -P - $demo
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected pinned assembly exit 0' }
python scripts/validate_x04_evidence_packet.py "$demo/packet.json" --out "$demo/summary.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected packet validation exit 0' }
```

Expected summary: artifact count 1, highlight A, comparison before/after 1/1 and delta 0, batch counts 1/1. All byte-integrity fields remain `unverified`. Pin fields are consumed and do not appear as packet-entry trust claims.

Append one whitespace byte to the artifact. It stays valid JSON with identical decoded claims, but its stale byte pin must refuse. The packet keeps its prior SHA-256:

```powershell
$before = (Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash
python -P -c "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_bytes(p.read_bytes()+b' ')" "$demo/artifact.json"
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --out "$demo/packet.json"
if ($LASTEXITCODE -ne 2) { throw 'Expected stale-pin exit 2' }
if ((Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash -ne $before) { throw 'Prior packet changed' }
if (Get-ChildItem -LiteralPath $demo -Filter '.x04-*') { throw 'Unexpected temporary file' }
```

Refresh the expected digest only after independently deciding that the changed bytes are acceptable. This synthetic whitespace edit is acceptable by construction. Refreshing a digest is not a way to legitimize forged counts or stale reports: semantic validation still refuses those.

```powershell
@'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]); path = root / 'manifest.json'
manifest = json.loads(path.read_bytes())
manifest['artifacts'][0]['expected_sha256'] = hashlib.sha256((root / 'artifact.json').read_bytes()).hexdigest()
path.write_bytes(json.dumps(manifest).encode('utf-8'))
'@ | python -P - $demo
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected refreshed-pin exit 0' }
python scripts/validate_x04_evidence_packet.py "$demo/packet.json" --out "$demo/summary.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected recovered validation exit 0' }
```

Every repeated path occurrence is read and checked against its own optional pin. Pins accept uppercase/lowercase hexadecimal and include BOM/whitespace bytes. Per-file and aggregate ceilings apply before digest comparison, which applies before UTF-8/JSON decoding. Reads remain successive, without a simultaneous snapshot or protection against a maliciously replaced manifest. An untrusted producer can change both bytes and pins; no original drawing authentication, raw coverage, redaction or live acceptance follows.
