# Replaying an independently fixed manifest digest

Studio X-bench X04's offline assembler can compare a manifest's bytes with a separately fixed expected digest, and can require source pins on every explicit row. The expected digest needs its own trust basis. Supplying it beside untrusted replacement bytes establishes no provenance or original-drawing authenticity.

Run these PowerShell commands from the repository root. The first script creates a fresh synthetic artifact and manifest, pins the exact artifact bytes, and prints the original manifest digest. Keep that value in `$fixedManifestPin` without recomputing it after the changes below.

```powershell
$demo = 'D:/agent-data/leaf-research/x04-synthetic-fixed-manifest'
$fixedManifestPin = @'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
payload = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-handoff', 'intake': {'inserts': [
        {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
    'request': {'drawing_id': 'synthetic-handoff', 'set_ids': ['["INSERT","Modules","Panel"]']},
    'observed_dependencies': from_pairs([])}
artifact = root / 'artifact.json'
artifact.write_bytes(json.dumps(export_evidence(payload)).encode('utf-8'))
manifest = {'schema': 'leaf.x04.evidence-packet-manifest.v1',
    'artifacts': [{'id': 'a', 'path': artifact.name, 'expected_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}],
    'comparisons': [], 'batches': []}
source = root / 'manifest.json'; source.write_bytes(json.dumps(manifest).encode('utf-8'))
print(hashlib.sha256(source.read_bytes()).hexdigest())
'@ | python -P - $demo
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --expected-manifest-sha256 $fixedManifestPin --require-source-pins --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected doubly pinned strict assembly exit 0' }
$before = (Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash
```

Expected packet: one synthetic module, count 1 and highlight A, with original-byte integrity `unverified`. Now append whitespace to the artifact and refresh its row digest together. Both files remain valid, and decoded artifact claims are equal. The old separately fixed manifest digest must refuse before source reads:

```powershell
@'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]); artifact = root / 'artifact.json'; source = root / 'manifest.json'
artifact.write_bytes(artifact.read_bytes() + b' ')
manifest = json.loads(source.read_bytes())
manifest['artifacts'][0]['expected_sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
source.write_bytes(json.dumps(manifest).encode('utf-8'))
'@ | python -P - $demo
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --expected-manifest-sha256 $fixedManifestPin --require-source-pins --out "$demo/packet.json"
if ($LASTEXITCODE -ne 2) { throw 'Expected stale fixed-manifest refusal exit 2' }
if ((Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash -ne $before) { throw 'Prior packet changed' }
if (Get-ChildItem -LiteralPath $demo -Filter '.x04-*') { throw 'Unexpected temporary file' }
```

In this synthetic example the only artifact change is whitespace. Explicitly accept that change and refresh the separately held manifest digest to recover:

```powershell
$refreshedManifestPin = python -P -c "from pathlib import Path; import hashlib,sys; print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())" "$demo/manifest.json"
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --expected-manifest-sha256 $refreshedManifestPin --require-source-pins --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected explicit-refresh recovery exit 0' }
python scripts/validate_x04_evidence_packet.py "$demo/packet.json" --out "$demo/summary.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected semantic validation exit 0' }
```

`--require-source-pins` checks every explicit row during preflight, including all artifact, comparison and batch rows. Missing pins refuse before payload reads; malformed pins also refuse. Without that option, unpinned rows remain compatible. Strict coverage does not require a manifest pin automatically, make digests trustworthy, or prove semantic correctness. Matching byte pins still face entity/report validation. Reads remain successive, integrity stays `unverified`, atomic replacement has no fsync guarantee, and none of this establishes raw dependency completeness, redaction, browser behavior or live ten-drawing acceptance.
