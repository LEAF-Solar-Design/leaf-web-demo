# Replaying the X04 aggregate byte ceiling

Studio X-bench X04's offline assembler counts actual manifest and explicit-source bytes. Repeated paths count once per occurrence. This synthetic replay includes multibyte Unicode and derives the exact total with standard byte reads, independently of the assembler. It proves embedded consistency only; original-byte integrity stays `unverified`.

Run from the repository root in PowerShell, using a fresh synthetic output directory. The Python source uses Unicode escapes so the PowerShell pipeline encoding does not change the intended text. This creates a one-module saved artifact and a manifest that reads it twice under distinct IDs. It prints one integer: manifest UTF-8 bytes plus twice the artifact UTF-8 bytes.

```powershell
$demo = 'D:/agent-data/leaf-research/x04-synthetic-aggregate-repro'
$total = @'
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
payload = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-aggregate', 'note': 'm\u00f3dulo \u6a21\u5757',
        'intake': {'inserts': [{'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]},
        'dependency_pairs': []},
    'request': {'drawing_id': 'synthetic-aggregate', 'set_ids': ['["INSERT","Modules","Panel"]']},
    'observed_dependencies': from_pairs([])}
artifact = export_evidence(payload)
manifest = {'schema': 'leaf.x04.evidence-packet-manifest.v1',
    'artifacts': [{'id': 'a', 'path': 'artifact.json'}, {'id': 'b', 'path': 'artifact.json'}],
    'comparisons': [], 'batches': []}
for name, value in [('artifact.json', artifact), ('manifest.json', manifest)]:
    (root / name).write_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))
manifest_bytes = (root / 'manifest.json').read_bytes()
artifact_bytes = (root / 'artifact.json').read_bytes()
assert len(artifact_bytes) > len(artifact_bytes.decode('utf-8'))
print(len(manifest_bytes) + 2 * len(artifact_bytes))
'@ | python -P - $demo
```

Assemble at the exact aggregate ceiling. Both commands exit 0. The packet has IDs a/b with identical count 1, highlight A and the exact Unicode note. The normalized summary has two successful artifact validations and integrity `unverified`.

```powershell
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --max-total-input-bytes $total --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected exact-limit exit 0' }
python scripts/validate_x04_evidence_packet.py "$demo/packet.json" --out "$demo/summary.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected validation exit 0' }
```

Reduce the ceiling by one byte. Refusal exits 2 and emits a bounded stderr diagnostic, with no success JSON. The existing packet and source files survive. Then restore the adequate ceiling:

```powershell
$packetHash = (Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash
$sourceHash = (Get-FileHash -LiteralPath "$demo/artifact.json" -Algorithm SHA256).Hash
$manifestHash = (Get-FileHash -LiteralPath "$demo/manifest.json" -Algorithm SHA256).Hash
$under = [long]$total - 1
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --max-total-input-bytes $under --out "$demo/packet.json"
if ($LASTEXITCODE -ne 2) { throw 'Expected one-byte-under exit 2' }
if ((Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash -ne $packetHash) { throw 'Prior output changed' }
if ((Get-FileHash -LiteralPath "$demo/artifact.json" -Algorithm SHA256).Hash -ne $sourceHash) { throw 'Source changed' }
if ((Get-FileHash -LiteralPath "$demo/manifest.json" -Algorithm SHA256).Hash -ne $manifestHash) { throw 'Manifest changed' }
if (Get-ChildItem -LiteralPath $demo -Filter '.x04-*') { throw 'Unexpected temporary file' }
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --max-total-input-bytes $total --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected adequate-budget recovery exit 0' }
```

`--max-input-bytes` remains an independent per-file ceiling; set it high enough for every individual file. Optional `--max-output-bytes` limits encoded publication after validation. Aggregate reads may consume one excess byte to detect growth, then stop later inputs. Reads are successive and do not establish a simultaneous snapshot. The assembler rejects manifest containers at depth 256, including ignored fields; embedding artifact/report objects adds three container levels before their own data. Atomic replacement does not imply fsync durability, redaction, raw dependency completeness, authenticated bytes or live acceptance.
