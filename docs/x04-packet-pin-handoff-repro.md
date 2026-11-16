# Replaying a separately retained packet digest

Studio X-bench X04's offline packet validator can compare exact file or stdin bytes with `--expected-packet-sha256` before trusted validation. This checks transit bytes against an expected value. It authenticates neither the sender nor original drawings. Matching pins still face embedded count/report checks, and original-byte integrity remains `unverified`.

From the repository root in PowerShell, create a fresh synthetic one-module artifact and assembly manifest. The script prints the source directory so the later deletion stays explicit and bounded.

```powershell
$validator = (Resolve-Path -LiteralPath './scripts/validate_x04_evidence_packet.py').Path
$demo = 'D:/agent-data/leaf-research/x04-synthetic-packet-handoff'
@'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
sources = root / 'sources'; sources.mkdir()
payload = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-transit', 'intake': {'inserts': [
        {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
    'request': {'drawing_id': 'synthetic-transit', 'set_ids': ['["INSERT","Modules","Panel"]']},
    'observed_dependencies': from_pairs([])}
artifact = sources / 'artifact.json'; artifact.write_bytes(json.dumps(export_evidence(payload)).encode('utf-8'))
manifest = {'schema': 'leaf.x04.evidence-packet-manifest.v1', 'artifacts': [
    {'id': 'a', 'path': artifact.name, 'expected_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}],
    'comparisons': [], 'batches': []}
(sources / 'manifest.json').write_bytes(json.dumps(manifest).encode('utf-8'))
print(sources)
'@ | python -P - $demo
python scripts/assemble_x04_evidence_packet.py "$demo/sources/manifest.json" --require-source-pins --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected synthetic assembly exit 0' }
$fixedPacketPin = (Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash
```

Keep `$fixedPacketPin` separately. Remove only the two named synthetic inputs, then validate the packet from an unrelated directory with an absolute script path:

```powershell
Remove-Item -LiteralPath "$demo/sources/artifact.json"
Remove-Item -LiteralPath "$demo/sources/manifest.json"
Push-Location -LiteralPath $demo
try {
    python $validator "$demo/packet.json" --expected-packet-sha256 $fixedPacketPin --out "$demo/summary.json"
    if ($LASTEXITCODE -ne 0) { throw 'Expected source-free pinned validation exit 0' }
} finally {
    Pop-Location
}
```

Expected summary: one artifact count 1, highlight A and integrity `unverified`. The validator does not reopen the deleted manifest or artifact. File/stdin digest matching includes BOM and whitespace. To use stdin without PowerShell text re-encoding, feed the packet's raw bytes through a Python binary pipe rather than `Get-Content`.

Append a whitespace byte. Decoded claims stay equal, but the separately retained digest must refuse and preserve the existing summary:

```powershell
$before = (Get-FileHash -LiteralPath "$demo/summary.json" -Algorithm SHA256).Hash
python -P -c "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_bytes(p.read_bytes()+b' ')" "$demo/packet.json"
python $validator "$demo/packet.json" --expected-packet-sha256 $fixedPacketPin --out "$demo/summary.json"
if ($LASTEXITCODE -ne 2) { throw 'Expected transit-byte mismatch exit 2' }
if ((Get-FileHash -LiteralPath "$demo/summary.json" -Algorithm SHA256).Hash -ne $before) { throw 'Prior summary changed' }
if (Get-ChildItem -LiteralPath $demo -Filter '.x04-*') { throw 'Unexpected temporary file' }
```

Explicitly accept this synthetic whitespace-only change and refresh the expected digest to recover:

```powershell
$refreshedPacketPin = (Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash
python $validator "$demo/packet.json" --expected-packet-sha256 $refreshedPacketPin --out "$demo/summary.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected explicit-refresh recovery exit 0' }
```

An attacker who replaces both packet and expected digest can pass this byte comparison. A matching digest for forged embedded counts or stale reports still fails semantic validation. Source-row pins are assembly-time checks and do not become provenance claims in the packet. Input ceilings apply before digest/decode; output ceilings apply after semantic validation and before publication. Atomic replacement does not promise fsync durability. No original-drawing authenticity, raw completeness, redaction, browser or live ten-drawing acceptance is established.
