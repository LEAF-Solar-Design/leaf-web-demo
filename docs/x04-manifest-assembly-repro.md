# Replaying X04 manifest assembly

This is synthetic offline evidence for Studio X-bench X04. Run these commands from the repository root in PowerShell with Python available. No network or customer drawing is needed. The examples write only under the named synthetic research directory. Use a fresh directory for this replay.

Create one saved intake and an explicit manifest. The source contains one module INSERT, selected by its listing ID. The dependency report is generated from the embedded empty record list rather than guessed.

```powershell
$demo = 'D:/agent-data/leaf-research/x04-synthetic-assembly-repro'
@'
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd() / 'server'))
from drawing_dependency_report import from_pairs
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
payload = {
    'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-assembly', 'intake': {'inserts': [
        {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
    'request': {'drawing_id': 'synthetic-assembly', 'set_ids': ['["INSERT","Modules","Panel"]']},
    'observed_dependencies': from_pairs([])}
manifest = {'schema': 'leaf.x04.evidence-packet-manifest.v1',
    'artifacts': [{'id': 'a', 'path': 'artifact.json'}],
    'comparisons': [{'id': 'same', 'before': 'a', 'after': 'a', 'path': 'comparison.json'}],
    'batches': [{'id': 'twice', 'artifact_ids': ['a', 'a'], 'path': 'batch.json'}]}
for name, value in [('source.json', payload), ('manifest.json', manifest)]:
    (root / name).write_text(json.dumps(value), encoding='utf-8')
'@ | python -P - $demo
```

Produce the valid artifact and saved reports, then assemble and validate. Each Python command below exits 0 and emits no stdout because it writes to `--out`.

```powershell
python scripts/export_x04_offline_evidence.py "$demo/source.json" --out "$demo/artifact.json"
python scripts/compare_x04_offline_evidence.py "$demo/artifact.json" "$demo/artifact.json" --out "$demo/comparison.json"
python scripts/batch_validate_x04_evidence.py "$demo/artifact.json" "$demo/artifact.json" --out "$demo/batch.json"
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --out "$demo/packet.json"
python scripts/validate_x04_evidence_packet.py "$demo/packet.json" --out "$demo/summary.json"
```

The summary has artifact `a` with count 1 and highlight `A`. Comparison `same` has before 1, after 1, delta 0 and empty membership differences. Batch `twice` has two successful rows with counts 1/1 and highlights A/A. Every integrity field remains `unverified`.

Prove missing-source refusal preserves an existing output, then repair only the missing source:

```powershell
$before = (Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash
Move-Item -LiteralPath "$demo/artifact.json" -Destination "$demo/artifact.saved.json"
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --out "$demo/packet.json"
if ($LASTEXITCODE -ne 2) { throw 'Expected refusal exit 2' }
if ((Get-FileHash -LiteralPath "$demo/packet.json" -Algorithm SHA256).Hash -ne $before) { throw 'Prior output changed' }
if (Get-ChildItem -LiteralPath $demo -Filter '.x04-*') { throw 'Unexpected temporary file' }
Move-Item -LiteralPath "$demo/artifact.saved.json" -Destination "$demo/artifact.json"
python scripts/assemble_x04_evidence_packet.py "$demo/manifest.json" --out "$demo/packet.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected repaired assembly exit 0' }
python scripts/validate_x04_evidence_packet.py "$demo/packet.json" --out "$demo/summary.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected repaired validation exit 0' }
```

Refusal writes a bounded diagnostic to stderr and no success JSON. Manifest and explicit source/output aliases also refuse. Relative inputs resolve from the manifest directory, so moving the manifest and all relative files together preserves these bindings. `--max-input-bytes` is a per-file ceiling, not an aggregate packet limit; exact encoded byte counts include whitespace. `--max-output-bytes` limits encoded publication after binding validation. Writes use a temporary file and atomic replacement, without an fsync durability promise.

Assembly reads each occurrence in order. Repeated paths are read again under each distinct local ID. There is no simultaneous snapshot guarantee. An assembled packet retains embedded entity data, literal dependency names, report labels and extensions; validation does not redact them. The result proves only embedded synthetic consistency. It does not authenticate original bytes, prove complete raw dependency coverage, or satisfy live ten-drawing acceptance.
