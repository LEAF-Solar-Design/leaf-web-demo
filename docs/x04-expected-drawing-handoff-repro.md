# Expected drawing labels in X04's offline artifact review

Studio X-bench X04's artifact validator optionally accepts `--expected-drawing-id`. It compares the decoded `drawing.id` exactly before recomputing entity-derived claims. A blank expectation refuses during argument parsing before I/O. Nonblank expectations retain case, Unicode normalization form, leading/trailing spaces and path-like text. Omitting the option retains prior behavior. The pure `validate_evidence` API remains unchanged.

Start from the repository root in PowerShell. Derive absolute tool paths from this checkout and create two fresh synthetic source payloads. Expected labels are explicit caller choices, not a value extracted from a validation summary:

```powershell
$exporter = (Resolve-Path -LiteralPath './scripts/export_x04_offline_evidence.py').Path
$validator = (Resolve-Path -LiteralPath './scripts/validate_x04_offline_evidence.py').Path
$demo = 'D:/agent-data/leaf-research/x04-synthetic-expected-drawing'
$expectedA = 'synthetic-A'
$expectedB = 'synthetic-B'
@'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[4]).resolve().parents[1] / 'server'))
from drawing_dependency_report import from_pairs
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
for name, identity, count in [('a', sys.argv[2], 1), ('b', sys.argv[3], 2)]:
    value = {'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
        'drawing': {'id': identity, 'intake': {'inserts': [
            {'layer': 'Modules', 'name': 'Panel', 'handle': 'H' + str(i)} for i in range(count)]},
            'dependency_pairs': []},
        'request': {'drawing_id': identity, 'set_ids': ['["INSERT","Modules","Panel"]']},
        'observed_dependencies': from_pairs([])}
    (root / (name + '-source.json')).write_bytes(json.dumps(value).encode('utf-8'))
'@ | python -P - $demo $expectedA $expectedB $exporter
if ($LASTEXITCODE -ne 0) { throw 'Expected fresh synthetic source creation' }
python $exporter "$demo/a-source.json" --out "$demo/a.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected first synthetic artifact export' }
python $exporter "$demo/b-source.json" --out "$demo/b.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected second synthetic artifact export' }
```

Run the first artifact through its file and the second through raw binary stdin from an unrelated directory. The block first succeeds with the intended label, then refuses the other valid drawing's label without replacing the prior summary, and finally recovers with the corrected expectation. Python passes binary stdin directly to avoid PowerShell text re-encoding.

```powershell
@'
import json, subprocess, sys
from pathlib import Path
root, validator = Path(sys.argv[1]), sys.argv[2]
for name, expected, wrong, stdin, count in [('a', sys.argv[3], sys.argv[4], False, 1),
                                         ('b', sys.argv[4], sys.argv[3], True, 2)]:
    source = root / (name + '.json'); output = root / (name + '-checked.json'); raw = source.read_bytes()
    def run(identity):
        return subprocess.run([sys.executable, '-P', validator, '-' if stdin else str(source),
            '--expected-drawing-id', identity, '--out', str(output)],
            input=raw if stdin else None, cwd=root, capture_output=True, timeout=30)
    result = run(expected)
    if result.returncode != 0: raise RuntimeError(result.stderr.decode())
    prior = output.read_bytes()
    result = run(wrong)
    if result.returncode != 2 or result.stdout or output.read_bytes() != prior:
        raise RuntimeError('Wrong drawing did not preserve prior output')
    result = run(expected)
    if result.returncode != 0 or output.read_bytes() != prior or source.read_bytes() != raw:
        raise RuntimeError('Corrected drawing expectation did not recover')
    checked = json.loads(output.read_bytes())
    if checked['drawing_id'] != expected: raise RuntimeError('Unexpected literal identity')
    if checked['recomputed_result'] != {'count': count, 'highlight_handles': ['H' + str(i) for i in range(count)],
                                      'original_bytes_integrity': 'unverified'}:
        raise RuntimeError('Unexpected recomputed claims')
    if list(root.glob('.x04-*')): raise RuntimeError('Unexpected temporary file')
    print(name, 'matching label, wrong-label refusal and corrected retry passed')
'@ | python -P - $demo $validator $expectedA $expectedB
if ($LASTEXITCODE -ne 0) { throw 'Expected both synthetic drawing controls to pass' }
```

The label check follows bounded byte reading and JSON decoding. A wrong label refuses before recomputation. A matching label still faces the original request/drawing consistency, dependency, selection, count, highlight and integrity checks. The expectation is a label rather than a path to open, a normalization instruction, a byte digest or a signature.

An attacker who replaces the artifact's drawing label and the expected label together can pass identity matching. Matching identity also does not make a forged count valid. Producer authenticity and original drawing byte integrity remain unverified. This synthetic replay proves no raw dependency completeness, intended real-drawing design set, browser behavior or live ten-drawing acceptance. Existing output ceilings and atomic replacement remain, without a new fsync or hostile alias-race guarantee.
