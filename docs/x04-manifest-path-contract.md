# X04 explicit manifest path contract

Studio X-bench X04's offline assembler opens only the manifest and its explicit artifact/report files. Embedded dependency names and batch path labels remain inert. Successful validation does not redact those names or authenticate original drawing bytes.

First run the complete synthetic fixture creation and producer commands in [the assembly replay](x04-manifest-assembly-repro.md). This gives a count-1 artifact, a self-comparison with delta 0 and a repeated batch with counts 1/1. These examples reuse that fresh synthetic directory; they do not use customer data.

Relative `path` values resolve from the manifest's directory, regardless of the process cwd. Absolute paths are accepted as explicit local sources. There is no repository-root restriction. The strings `-`, `--artifact.json`, `with spaces.json` and `módulo-模块.json` are literal source filenames when they occur in the manifest. They do not select stdin or flags. A manifest positional operand is itself a local file; use an absolute path or `./--manifest.json` for a leading-hyphen operand, or use `--` before positional operands after all options.

Create portable filename variants and an absolute-source variant:

```powershell
$demo = 'D:/agent-data/leaf-research/x04-synthetic-assembly-repro'
@'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
original = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
data = (root / 'artifact.json').read_bytes()
for label, filename in [('dash', '-'), ('hyphen', '--artifact.json'),
                        ('space', 'with spaces.json'), ('unicode', 'módulo-模块.json')]:
    (root / filename).write_bytes(data)
    manifest = json.loads(json.dumps(original))
    manifest['artifacts'][0]['path'] = filename
    (root / (label + '-manifest.json')).write_text(json.dumps(manifest), encoding='utf-8')
absolute = json.loads(json.dumps(original))
absolute['artifacts'][0]['path'] = str(root / 'artifact.json')
(root / 'absolute-manifest.json').write_text(json.dumps(absolute), encoding='utf-8')
'@ | python -P - $demo
```

From any unrelated directory, use the repository script's absolute path. Replace the repository path below if using a different checkout. Each command exits 0 and writes a complete valid packet. The original source bytes survive.

```powershell
$assembler = 'D:/agent-wt/xbench-x04-evidence-cli/scripts/assemble_x04_evidence_packet.py'
foreach ($label in 'dash','hyphen','space','unicode','absolute') {
    python $assembler "$demo/$label-manifest.json" --out "$demo/$label-packet.json"
    if ($LASTEXITCODE -ne 0) { throw 'Expected successful literal path replay' }
}
```

Output must differ from the manifest and every explicit source. Normalized paths, symlinks and hardlinks to a source also refuse before replacement. This direct alias check leaves the literal dash source unchanged, then repairs the output path:

```powershell
$before = (Get-FileHash -LiteralPath "$demo/-" -Algorithm SHA256).Hash
python $assembler "$demo/dash-manifest.json" --out "$demo/-"
if ($LASTEXITCODE -ne 2) { throw 'Expected alias refusal exit 2' }
if ((Get-FileHash -LiteralPath "$demo/-" -Algorithm SHA256).Hash -ne $before) { throw 'Source changed' }
python $assembler "$demo/dash-manifest.json" --out "$demo/dash-recovered.json"
if ($LASTEXITCODE -ne 0) { throw 'Expected repaired output exit 0' }
```

`--max-input-bytes` limits each file independently. Optional `--max-total-input-bytes` counts actual manifest and explicit-source bytes across the whole call, including repeated reads. Exact totals succeed; excess bytes refuse before publication and later sources are not read. One excess byte may be consumed to distinguish EOF from growth. `--max-output-bytes` bounds encoded output after validation. Source reads remain successive, without a simultaneous snapshot or a defense against hostile concurrent alias replacement. Original-byte integrity remains `unverified`.
