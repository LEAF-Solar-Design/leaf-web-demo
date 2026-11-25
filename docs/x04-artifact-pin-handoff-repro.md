# Artifact byte pins in X04's offline evidence review

Studio X-bench X04's artifact validator optionally accepts `--expected-artifact-sha256` to compare exact file or stdin bytes before decoding, drawing-ID checks and entity recomputation. The expected value has 64 hexadecimal characters and includes BOM and whitespace. Omitting it retains the previous reader behavior. A matching digest does not waive drawing-ID or semantic checks, and original-byte integrity remains `unverified`.

From the repository root in PowerShell, derive absolute tool paths and create one fresh synthetic artifact. Its one module has handle A and drawing label `synthetic-transit`:

```powershell
$exporter = (Resolve-Path -LiteralPath './scripts/export_x04_offline_evidence.py').Path
$validator = (Resolve-Path -LiteralPath './scripts/validate_x04_offline_evidence.py').Path
$demo = 'D:/agent-data/leaf-research/x04-synthetic-artifact-pin'
@'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]).resolve().parents[1] / 'server'))
from drawing_dependency_report import from_pairs
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
value = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
    'drawing': {'id': 'synthetic-transit', 'intake': {'inserts': [
        {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
    'request': {'drawing_id': 'synthetic-transit', 'set_ids': ['["INSERT","Modules","Panel"]']},
    'observed_dependencies': from_pairs([])}
(root / 'source.json').write_bytes(json.dumps(value).encode('utf-8'))
'@ | python -P - $demo $exporter
if ($LASTEXITCODE -ne 0) { throw 'Expected fresh synthetic source creation' }
python $exporter "$demo/source.json" --out "$demo/artifact.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected synthetic artifact export' }
$fixedArtifactPin = (Get-FileHash -LiteralPath "$demo/artifact.json" -Algorithm SHA256).Hash
```

Retain `$fixedArtifactPin` separately from the artifact. This block runs both file and raw binary stdin validation from an unrelated directory. Each first passes, appends one whitespace byte, refuses the stale expected digest without replacing its summary, then explicitly refreshes the expected digest and recovers identical claims. Python sends the raw stdin buffer without PowerShell text re-encoding.

```powershell
@'
import hashlib, json, subprocess, sys
from pathlib import Path
root, validator, fixed = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
source = root / 'artifact.json'; original = source.read_bytes()
for stdin in (False, True):
    source.write_bytes(original)
    output = root / ('stdin-checked.json' if stdin else 'file-checked.json')
    def run(pin):
        return subprocess.run([sys.executable, '-P', validator, '-' if stdin else str(source),
            '--expected-artifact-sha256', pin, '--expected-drawing-id', 'synthetic-transit', '--out', str(output)],
            input=source.read_bytes() if stdin else None, cwd=root, capture_output=True, timeout=30)
    result = run(fixed)
    if result.returncode != 0: raise RuntimeError(result.stderr.decode())
    prior = output.read_bytes(); source.write_bytes(original + b' ')
    result = run(fixed)
    if result.returncode != 2 or result.stdout or output.read_bytes() != prior:
        raise RuntimeError('Stale artifact digest did not preserve prior output')
    refreshed = hashlib.sha256(source.read_bytes()).hexdigest()
    result = run(refreshed)
    if result.returncode != 0 or output.read_bytes() != prior:
        raise RuntimeError('Explicit digest refresh did not recover')
    checked = json.loads(output.read_bytes())
    if checked['recomputed_result'] != {'count': 1, 'highlight_handles': ['A'], 'original_bytes_integrity': 'unverified'}:
        raise RuntimeError('Unexpected recomputed claims')
    if checked['drawing_id'] != 'synthetic-transit': raise RuntimeError('Unexpected identity')
    if list(root.glob('.x04-*')): raise RuntimeError('Unexpected temporary file')
    print('stdin' if stdin else 'file', 'matched bytes, stale refusal and explicit refresh passed')
'@ | python -P - $demo $validator $fixedArtifactPin
if ($LASTEXITCODE -ne 0) { throw 'Expected both synthetic artifact-pin controls to pass' }
```

The input ceiling precedes digest comparison; digest comparison precedes strict JSON decoding; optional exact drawing-ID comparison precedes recomputation and original semantic checks. The hash uses the one buffer that is decoded rather than a reopened file. Malformed pins refuse during parsing before I/O. Existing output ceilings and atomic replacement preserve prior output on handled pre-publication refusal, without fsync or hostile alias-race guarantees.

A changed artifact and changed expected digest can agree. A correctly pinned artifact can still have the wrong drawing label or forged count and must refuse those checks. Byte matching authenticates neither producer nor expected value and proves no original DWG bytes, raw dependency completeness, browser behavior or live ten-drawing acceptance. Separate expected-value custody remains necessary.
